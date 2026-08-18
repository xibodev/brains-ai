"""Semantic (embedding-based) repo retrieval.

Builds on the existing file-level indexer: ``embed_repo`` chunks each indexed
text artifact, embeds the chunks via :mod:`brains.context.embeddings`, and
stores the vectors on ``chunks`` (with a content-hash guard so re-embedding an
unchanged file is a no-op). ``search_repo_semantic`` embeds the query and ranks
chunks by cosine similarity. The query is always embedded with the SAME model
recorded in ``chunks_meta`` so vectors are comparable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from brains.config import settings
from brains.context.embeddings import (
    embed_text,
    embed_texts,
    pack_vector,
    score_blobs,
)
from brains.context.repo_indexer import REPO_SOURCE_TYPE, index_repo_persisted
from brains.control.common import normalize_path
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Artifact, Chunk, ChunksMeta, Source, Workspace


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 100) -> list[str]:
    """Split text into overlapping character windows. Whole-file if it fits."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        pieces.append(text[start:end])
        if end == len(text):
            break
        start = max(end - overlap, start + 1)
    return pieces


def _repo_source(session, workspace) -> Source | None:
    return (
        session.query(Source)
        .filter(
            Source.workspace_id == workspace.id,
            Source.source_type == REPO_SOURCE_TYPE,
            Source.uri == workspace.path,
        )
        .one_or_none()
    )


def _is_relative(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _source_has_embeddings(session, source: Source) -> bool:
    art_ids = [
        r[0] for r in session.query(Artifact.id).filter(Artifact.source_id == source.id).all()
    ]
    if not art_ids:
        return False
    return (
        session.query(Chunk.id)
        .filter(Chunk.artifact_id.in_(art_ids), Chunk.embedding.isnot(None))
        .first()
        is not None
    )


def _indexed_workspace_slugs(session) -> list[str]:
    """Slugs of every workspace that actually has embedded chunks (so a miss can
    point the caller at where embeddings DO live instead of returning a bare [])."""
    slugs: list[str] = []
    for src in session.query(Source).filter(Source.source_type == REPO_SOURCE_TYPE).all():
        if _source_has_embeddings(session, src):
            ws = session.get(Workspace, src.workspace_id)
            if ws is not None:
                slugs.append(ws.slug)
    return sorted(set(slugs))


def _resolve_repo_source(session, path) -> tuple[Source | None, Path, str]:
    """Find the best EMBEDDED repo source for ``path``.

    Prefers an exact workspace match; otherwise the closest path-related
    workspace that actually has embeddings — so searching a repo root finds an
    index registered under a subdir (e.g. ``src/``) and vice versa, instead of
    silently returning nothing. Returns ``(source_or_none, root, status)`` where
    status is ``ok`` | ``resolved_related`` | ``not_embedded`` | ``workspace_not_indexed``.
    """
    workspace = register_workspace(path)
    requested_root = Path(normalize_path(path)).expanduser().resolve()
    exact = _repo_source(session, workspace)
    if exact is not None and _source_has_embeddings(session, exact):
        return exact, requested_root, "ok"

    best: Source | None = None
    best_root: Path | None = None
    best_dist: int | None = None
    for src in session.query(Source).filter(Source.source_type == REPO_SOURCE_TYPE).all():
        if src is exact or not _source_has_embeddings(session, src):
            continue
        try:
            src_root = Path(normalize_path(src.uri)).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if not (
            src_root == requested_root
            or _is_relative(src_root, requested_root)
            or _is_relative(requested_root, src_root)
        ):
            continue
        dist = abs(len(src_root.parts) - len(requested_root.parts))
        if best_dist is None or dist < best_dist:
            best, best_root, best_dist = src, src_root, dist
    if best is not None and best_root is not None:
        return best, best_root, "resolved_related"
    if exact is not None:
        return None, requested_root, "not_embedded"
    return None, requested_root, "workspace_not_indexed"


def _path_allowed(
    rel_path: str | None, include: list[str] | None, exclude: list[str] | None
) -> bool:
    """Filter a chunk's file by simple path substrings/globs.

    ``include`` (if given) keeps only paths matching at least one pattern;
    ``exclude`` drops any path matching a pattern. Patterns with ``*`` or ``?``
    are treated as globs (fnmatch), otherwise as case-insensitive substrings.
    Lets callers ask for implementation only (e.g. exclude docs + tests) so prose
    docs and test files don't crowd real source out of the top results.

    A leading ``/`` is prepended to the path before matching so segment patterns
    like ``/docs/`` match a repo-relative path such as ``docs/ref/x.txt`` (which
    has no leading slash) — without this the whole filter silently no-ops on real
    repos.
    """
    if not rel_path:
        return not include
    low = "/" + rel_path.replace("\\", "/").lstrip("/").lower()

    def _match(pattern: str) -> bool:
        p = pattern.lower()
        if "*" in p or "?" in p:
            from fnmatch import fnmatch

            return fnmatch(low, p) or fnmatch(low, f"*{p}*")
        return p in low

    if include and not any(_match(p) for p in include):
        return False
    return not (exclude and any(_match(p) for p in exclude))


def _rank_chunks(
    session,
    source: Source,
    root: Path,
    query: str,
    limit: int,
    use_model: str,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[dict]:
    artifacts = {
        a.id: a
        for a in session.query(Artifact).filter(Artifact.source_id == source.id).all()
        if _path_allowed(a.path, include, exclude)
    }
    if not artifacts:
        return []
    # Load only the columns needed to RANK (id, artifact_id, ordinal, embedding) —
    # not the chunk ``content`` text — so a big repo doesn't hydrate tens of
    # thousands of snippet strings just to score them. Content for the winning
    # rows is fetched after ranking.
    rows = (
        session.query(Chunk.id, Chunk.artifact_id, Chunk.ordinal, Chunk.embedding)
        .filter(Chunk.artifact_id.in_(list(artifacts)))
        .all()
    )
    embedded = [r for r in rows if r[3]]
    if not embedded:
        return []
    qvec = embed_text(query, model=use_model)
    # Batched cosine over all chunk vectors at once (numpy fast-path when present,
    # pure-Python fallback otherwise) — avoids a per-chunk unpack+loop that is the
    # dominant cost on a large repo.
    scores = score_blobs(qvec, [r[3] for r in embedded])
    scored: list[dict] = []
    for (chunk_id, artifact_id, ordinal, _emb), score in zip(embedded, scores, strict=False):
        art = artifacts.get(artifact_id)
        scored.append(
            {
                "chunk_id": chunk_id,
                "rel_path": art.path if art else None,
                "path": str(root / art.path) if art else None,
                "ordinal": ordinal,
                "score": round(score, 4),
            }
        )
    scored.sort(key=lambda row: (-row["score"], row["rel_path"] or "", row["ordinal"]))
    # Collapse to the best-scoring chunk per file so the limited result set covers
    # more distinct files instead of repeating one file's adjacent chunks.
    deduped: list[dict] = []
    seen_files: set[str] = set()
    for row in scored:
        key = row["rel_path"] or row["path"] or str(row["ordinal"])
        if key in seen_files:
            continue
        seen_files.add(key)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    # Fetch snippets only for the winning rows.
    if deduped:
        contents = dict(
            session.query(Chunk.id, Chunk.content)
            .filter(Chunk.id.in_([row["chunk_id"] for row in deduped]))
            .all()
        )
        for row in deduped:
            row["snippet"] = (contents.get(row["chunk_id"]) or "")[:240]
            row.pop("chunk_id", None)
    return deduped


def embed_repo(path: str, model: str | None = None, max_chars: int = 1200) -> dict:
    """Chunk + embed every indexed text artifact in a workspace's repo.

    Re-indexes first (cheap, content-hash deduped). Files whose chunks already
    carry the file hash and an embedding are skipped. Records the embed model +
    dim in ``chunks_meta`` (one row per DB — mixing models corrupts search).
    """
    model = model or settings.embed_model
    if not model:
        raise ValueError("no embedding model configured (set embed_model)")
    index_repo_persisted(path)
    workspace = register_workspace(path)
    root = Path(normalize_path(path)).expanduser().resolve()
    init_db()

    embedded_files = 0
    embedded_chunks = 0
    skipped = 0
    dim = 0
    with SessionLocal() as session:
        source = _repo_source(session, workspace)
        if source is None:
            return {"workspace": workspace.slug, "embedded_files": 0, "embedded_chunks": 0}
        artifacts = session.query(Artifact).filter(Artifact.source_id == source.id).all()
        for art in artifacts:
            if art.size and art.size > settings.repo_index_max_file_size:
                continue
            abs_path = root / art.path
            try:
                payload = abs_path.read_bytes()
            except OSError:
                continue
            if b"\x00" in payload[:2048]:
                continue
            pieces = chunk_text(payload.decode("utf-8", errors="ignore"), max_chars=max_chars)
            if not pieces:
                continue
            file_hash = art.hash or hashlib.sha256(payload).hexdigest()
            existing = session.query(Chunk).filter(Chunk.artifact_id == art.id).all()
            if existing and existing[0].hash == file_hash and all(c.embedding for c in existing):
                skipped += 1
                continue
            for stale in existing:
                session.delete(stale)
            vectors = embed_texts(pieces, model=model)
            for ordinal, (piece, vec) in enumerate(zip(pieces, vectors, strict=False)):
                dim = len(vec)
                session.add(
                    Chunk(
                        artifact_id=art.id,
                        ordinal=ordinal,
                        content=piece,
                        token_estimate=max(len(piece) // 4, 1),
                        hash=file_hash,
                        embedding=pack_vector(vec),
                    )
                )
                embedded_chunks += 1
            embedded_files += 1

        meta = session.get(ChunksMeta, 1)
        if meta is None:
            session.add(ChunksMeta(id=1, embed_model=model, embed_dim=dim or 0))
        else:
            meta.embed_model = model
            if dim:
                meta.embed_dim = dim
        session.commit()

    return {
        "workspace": workspace.slug,
        "embed_model": model,
        "embedded_files": embedded_files,
        "embedded_chunks": embedded_chunks,
        "skipped_unchanged": skipped,
        "embed_dim": dim,
    }


def _count_candidate_files(path: str, cap: int) -> int:
    """Fast, bounded count of indexable files under ``path`` (stops at ``cap``+1).

    Used to decide whether an inline auto-embed is cheap enough; mirrors the
    indexer's ignore set so the estimate matches what would actually be embedded.
    """
    import os

    from brains.context.code_graph import IGNORE_DIRS

    root = Path(normalize_path(path)).expanduser()
    if not root.is_dir():
        return 0
    count = 0
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        count += len(filenames)
        if count > cap:
            return count
    return count


def _auto_embed_if_enabled(path: str, use_model: str) -> str | None:
    """Best-effort inline auto-embed on a retrieval miss. Returns a status tag
    (``auto_embedded``) on success, or ``None`` if it was skipped/failed (the
    caller then falls back to the explicit-embed hint). Bounded by
    ``auto_index_max_files`` and never raises."""
    if not settings.semantic_auto_embed:
        return None
    cap = settings.auto_index_max_files
    if cap and _count_candidate_files(path, cap) > cap:
        return None
    try:
        result = embed_repo(path, model=use_model)
    except Exception:
        return None
    return (
        "auto_embedded"
        if result.get("embedded_chunks") or result.get("skipped_unchanged")
        else None
    )


def semantic_search_with_status(
    path: str,
    query: str,
    limit: int = 10,
    model: str | None = None,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict:
    """Semantic search that NEVER fails silently and self-bootstraps.

    Returns ``{results, status, active_workspace, indexed_workspaces, hint?}``.
    On a miss it first tries to auto-embed the repo (local Ollama, idempotent,
    bounded by ``auto_index_max_files``); only if that is disabled/too-big/down
    does it return an actionable ``hint`` so the caller does not burn a turn on a
    bare ``[]`` and then grep. ``status`` is one of ``ok`` | ``auto_embedded`` |
    ``resolved_related`` | ``no_embed_model`` | ``not_embedded`` |
    ``workspace_not_indexed``.

    ``include`` / ``exclude`` filter results by path substrings/globs — e.g.
    ``exclude=["/docs/", "/tests/", "test_"]`` surfaces IMPLEMENTATION instead of
    letting prose docs and test files crowd out real source.
    """
    workspace = register_workspace(path)
    init_db()

    def _lookup() -> dict | None:
        with SessionLocal() as session:
            meta = session.get(ChunksMeta, 1)
            use_model = model or (meta.embed_model if meta else None) or settings.embed_model
            indexed = _indexed_workspace_slugs(session)
            if not use_model:
                return {
                    "results": [],
                    "status": "no_embed_model",
                    "active_workspace": workspace.slug,
                    "indexed_workspaces": indexed,
                    "hint": "no embedding model configured (set embed_model) — semantic "
                    "search is disabled; use search_repo (substring) or grep.",
                    "_use_model": use_model,
                }
            source, root, status = _resolve_repo_source(session, path)
            if source is None:
                return {
                    "results": [],
                    "status": status,
                    "active_workspace": workspace.slug,
                    "indexed_workspaces": indexed,
                    "_use_model": use_model,
                }
            out: dict = {
                "results": _rank_chunks(
                    session,
                    source,
                    root,
                    query,
                    limit,
                    use_model,
                    include=include,
                    exclude=exclude,
                ),
                "status": status,
                "active_workspace": workspace.slug,
                "indexed_workspaces": indexed,
                "_use_model": use_model,
            }
            if status == "resolved_related":
                out["hint"] = (
                    "no embeddings under the active workspace path; searched the nearest "
                    f"indexed workspace instead (source: {source.uri})."
                )
            return out

    found = _lookup()
    if found and found["status"] in {"workspace_not_indexed", "not_embedded"}:
        use_model = found.get("_use_model")
        if use_model and _auto_embed_if_enabled(path, use_model):
            found = _lookup()
            if found and found.get("results"):
                found["status"] = "auto_embedded"
        if found and not found.get("results") and "hint" not in found:
            indexed = found.get("indexed_workspaces") or []
            where = (
                f"workspaces WITH embeddings: {indexed}. "
                if indexed
                else "no workspace has embeddings yet. "
            )
            found["hint"] = (
                "this workspace path has no embeddings — run "
                "`brains-ai embed-repo --path <repo>` first. "
                + where
                + "until then, use search_repo (substring) or grep."
            )
    if found is not None:
        found.pop("_use_model", None)
    return found or {
        "results": [],
        "status": "workspace_not_indexed",
        "active_workspace": workspace.slug,
        "indexed_workspaces": [],
    }


def search_repo_semantic(
    path: str,
    query: str,
    limit: int = 10,
    model: str | None = None,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[dict]:
    """Rank embedded chunks by cosine similarity to ``query``.

    Backward-compatible thin wrapper over :func:`semantic_search_with_status` that
    returns just the ranked rows. Returns an empty list if nothing comparable has
    been embedded (use :func:`semantic_search_with_status` for the miss reason).
    """
    return semantic_search_with_status(
        path, query, limit=limit, model=model, include=include, exclude=exclude
    )["results"]


# Narrative-doc path fragments deprioritised by code-oriented retrieval (orient
# block, the ``orient`` MCP tool). Source + tests stay; only prose docs drop, so a
# conceptual query surfaces implementation instead of being buried under docs.
ORIENT_DOC_EXCLUDES = ["/docs/", ".txt", ".rst", ".md", "/doc/", "changelog", "license"]

# Test-file path fragments. ``orient`` exists to point at IMPLEMENTATION, and on a
# big repo test files crowd real source out of a small result set, so they are
# excluded by default too (overridable). This mirrors the config that produced the
# measured weak-model win.
ORIENT_TEST_EXCLUDES = ["/tests/", "/test/", "test_", "_test.", "conftest", "/testing/"]


def build_orientation_block(
    path: str,
    query: str,
    limit: int = 8,
    model: str | None = None,
    *,
    include_docs: bool = False,
    include_tests: bool = False,
    exclude: list[str] | None = None,
) -> str:
    """Render a ready-to-INJECT markdown orientation block for ``query``.

    This is the operational primitive behind brains' measured weak-model win: a
    capable agent won't *call* retrieval, but a cheap model handed this ranked
    code map up-front in its prompt navigates a large repo far more cheaply and
    without catastrophic grep-flailing. A launcher/wrapper prepends the returned
    string to the agent's prompt at session start.

    Implementation-focused by default (narrative docs AND test files excluded —
    the config that produced the win); pass ``include_docs`` / ``include_tests``
    to widen. Returns a short note instead of a block when nothing is indexed, so
    the caller never injects an empty/misleading section.
    """
    eff_exclude = list(exclude) if exclude else []
    if not include_docs:
        eff_exclude += ORIENT_DOC_EXCLUDES
    if not include_tests:
        eff_exclude += ORIENT_TEST_EXCLUDES
    eff_exclude = list(dict.fromkeys(eff_exclude))
    status = semantic_search_with_status(
        path, query, limit=limit, model=model, exclude=eff_exclude or None
    )
    results = status.get("results") or []
    if not results:
        hint = status.get("hint") or "no indexed embeddings for this workspace."
        return f"<!-- brains orientation unavailable: {hint} -->"
    lines = [
        "## Repo orientation (brains pre-indexed this repo — ranked implementation "
        "files for your task; you do NOT need to grep/find to locate these)",
        "",
    ]
    for row in results:
        snippet = (row.get("snippet") or "").replace("\n", " ").strip()[:120]
        lines.append(f"- `{row['rel_path']}` (relevance {row['score']}) — {snippet}")
    lines.append("")
    return "\n".join(lines)

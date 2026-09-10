"""Bounded evidence retrieval for legacy compact references.

References identify mutable stored evidence or a current-file snapshot, not an
immutable capture. A complete file's SHA-256 can match the indexed Artifact.hash;
this verifies those bytes against that recorded digest, not the index's integrity.
POSIX reads walk no-follow directory descriptors. Other platforms use component
and descriptor identity checks, with a residual concurrent path-replacement race.
This cooperative local filesystem boundary is not a security sandbox.
"""

from __future__ import annotations

import codecs
import hashlib
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath
from typing import Any

from sqlalchemy.orm import load_only

import brains.storage.db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import Artifact, Chunk, KnowledgeEntry, Source, Workspace

CONTENT_LIMIT_BYTES = 64 * 1024
_LOCAL_SOURCE_TYPES = {"repo_dir", "docs_dir"}


def _split_ref(ref: str) -> tuple[str, str]:
    kind, sep, ident = str(ref or "").partition(":")
    if not sep or not kind or not ident or kind not in {"chunk", "artifact", "knowledge"}:
        raise ValueError("ref must be one of chunk:<id>, artifact:<id>, or knowledge:<code>")
    return kind, ident


def _visibility() -> set[int] | None:
    from brains.control.memberships import visible_workspace_ids_for_current

    try:
        return visible_workspace_ids_for_current()
    except Exception:
        # A failed principal/policy lookup must never select the bootstrap path.
        return set()


def _visible_knowledge(
    session, ident: str | int, visible: set[int] | None
) -> KnowledgeEntry | None:
    from sqlalchemy import or_

    q = session.query(KnowledgeEntry).filter(
        KnowledgeEntry.id == ident if isinstance(ident, int) else KnowledgeEntry.code == ident
    )
    if visible is not None:
        q = q.filter(
            or_(
                KnowledgeEntry.scope.in_(["shared", "global"]),
                KnowledgeEntry.workspace_id.in_(visible),
            )
        )
    return q.one_or_none()


def _ancestry(session, artifact, visible, ref):
    source = (
        session.get(Source, artifact.source_id, options=[load_only(Source.id, Source.workspace_id)])
        if artifact is not None
        else None
    )
    workspace = (
        session.get(Workspace, source.workspace_id, options=[load_only(Workspace.id)])
        if source is not None and source.workspace_id is not None
        else None
    )
    if (
        source is None
        or (source.workspace_id is not None and workspace is None)
        or (visible is not None and (workspace is None or workspace.id not in visible))
    ):
        raise ValueError(f"unknown or inaccessible ref: {ref}")
    return source, workspace


def _bounded_text(value: str | None) -> tuple[str, bool]:
    # Slice characters first so a large stored body does not allocate another
    # unbounded encoded copy. UTF-8 prefix decoding never returns a split scalar.
    value = value or ""
    payload = value[:CONTENT_LIMIT_BYTES].encode("utf-8")
    truncated = len(value) > CONTENT_LIMIT_BYTES or len(payload) > CONTENT_LIMIT_BYTES
    return payload[:CONTENT_LIMIT_BYTES].decode("utf-8", errors="ignore"), truncated


def _evidence(origin: str, *, freshness: str = "unknown", **extra) -> dict[str, Any]:
    return {
        "origin": origin,
        "freshness": freshness,
        "snapshot": origin in {"stored_chunk", "current_file", "stored_knowledge"},
        "immutable_original": False,
        "original_verified": False,
        "truncated": False,
        "incomplete": False,
        "reason": None,
        "content_limit_bytes": CONTENT_LIMIT_BYTES,
        "hash_algorithm": "sha256",
        "expected_hash": None,
        "observed_hash": None,
        "hash_scope": "not_computed",
        **extra,
    }


def _with_evidence(result: dict, evidence: dict) -> dict:
    result["evidence"] = evidence
    result["metadata"].update(
        {
            key: evidence[key]
            for key in (
                "origin",
                "freshness",
                "snapshot",
                "immutable_original",
                "original_verified",
                "truncated",
                "incomplete",
            )
        }
    )
    return result


class _FileUnavailable(Exception):
    pass


def _is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _check_components(path: Path):
    """Reject symlinks/reparse points, including directory and final components."""
    current = Path(path.anchor)
    info = current.lstat()
    for component in path.parts[1:]:
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise _FileUnavailable("unsafe_path")
        current /= component
        info = current.lstat()
    if _is_link(info):
        raise _FileUnavailable("unsafe_path")
    return info


def _path(value: str) -> Path:
    # Refuse foreign absolute/drive paths on POSIX and Windows alternate streams.
    value = str(value or "")
    windows = PureWindowsPath(value)
    path = Path(value.replace("\\", "/"))
    if (
        not value
        or "\0" in value
        or ".." in path.parts
        or (windows.drive and not path.is_absolute())
        or (path.is_absolute() and any(":" in part for part in path.parts[1:]))
        or (not path.is_absolute() and ":" in value)
    ):
        raise _FileUnavailable("unsafe_path")
    return path


def _candidate(artifact: Artifact, source: Source, workspace: Workspace | None) -> Path:
    if workspace is None:
        raise _FileUnavailable("unscoped_source")
    if source.source_type not in _LOCAL_SOURCE_TYPES:
        raise _FileUnavailable("unsupported_source")
    registered = _path(workspace.path).expanduser()
    if not registered.is_absolute():
        raise _FileUnavailable("unsafe_path")
    # Check before resolve so resolution cannot erase a link/reparse component.
    if not stat.S_ISDIR(_check_components(registered).st_mode):
        raise _FileUnavailable("unsafe_path")
    root = registered.resolve(strict=True)
    source_path = _path(source.uri)
    source_root = source_path if source_path.is_absolute() else root / source_path
    if not source_root.is_relative_to(root):
        raise _FileUnavailable("unsafe_path")
    if not stat.S_ISDIR(_check_components(source_root).st_mode):
        raise _FileUnavailable("unsafe_path")
    source_root = source_root.resolve(strict=True)
    if not source_root.is_relative_to(root):
        raise _FileUnavailable("unsafe_path")
    artifact_path = _path(artifact.path)
    candidate = artifact_path if artifact_path.is_absolute() else source_root / artifact_path
    if not candidate.is_relative_to(root) or not candidate.is_relative_to(source_root):
        raise _FileUnavailable("unsafe_path")
    info = _check_components(candidate)
    if not stat.S_ISREG(info.st_mode):
        raise _FileUnavailable("not_regular_file")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_relative_to(source_root):
        raise _FileUnavailable("unsafe_path")
    # metadata_json.abs_path is deliberately never consulted: it grants no authority.
    return candidate


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_regular(path: Path) -> bytes:
    """Read at most the cap plus one byte, from a checked regular descriptor."""
    before = _check_components(path)
    if not stat.S_ISREG(before.st_mode):
        raise _FileUnavailable("not_regular_file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    with ExitStack() as stack:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(path.anchor, directory_flags)
            stack.callback(os.close, directory)
            for component in path.parts[1:-1]:
                directory = os.open(component, directory_flags, dir_fd=directory)
                stack.callback(os.close, directory)
            fd = os.open(path.name, flags, dir_fd=directory)
        else:
            fd = os.open(path, flags)
        stack.callback(os.close, fd)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
            raise _FileUnavailable("file_changed_during_read")
        if _identity(_check_components(path)) != _identity(opened):
            raise _FileUnavailable("file_changed_during_read")
        parts = []
        remaining = CONTENT_LIMIT_BYTES + 1
        while remaining:
            part = os.read(fd, remaining)
            if not part:
                break
            parts.append(part)
            remaining -= len(part)
        if _identity(os.fstat(fd)) != _identity(opened) or _identity(
            _check_components(path)
        ) != _identity(opened):
            raise _FileUnavailable("file_changed_during_read")
        return b"".join(parts)


def _sha256(value: str | None) -> str | None:
    return value.lower() if value and re.fullmatch(r"[0-9a-fA-F]{64}", value) else None


def _artifact_content(artifact: Artifact, source: Source, workspace: Workspace | None):
    expected = _sha256(artifact.hash)
    reason = None
    try:
        payload = _read_regular(_candidate(artifact, source, workspace))
        if b"\0" in payload:
            raise _FileUnavailable("binary_file")
        truncated = len(payload) > CONTENT_LIMIT_BYTES
        try:
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            text = decoder.decode(payload[:CONTENT_LIMIT_BYTES], final=not truncated)
        except UnicodeDecodeError:
            raise _FileUnavailable("binary_file") from None
        observed = None if truncated else hashlib.sha256(payload).hexdigest()
        verified = expected is not None and observed == expected
        freshness = (
            "unknown" if truncated or expected is None else "current" if verified else "stale"
        )
        return text, _evidence(
            "current_file",
            freshness=freshness,
            original_verified=verified,
            truncated=truncated,
            incomplete=truncated,
            reason="content_limit"
            if truncated
            else "hash_unavailable"
            if expected is None
            else "hash_match"
            if verified
            else "hash_mismatch",
            expected_hash=expected,
            observed_hash=observed,
            hash_scope="not_computed" if truncated else "full_file",
        )
    except _FileUnavailable as exc:
        reason = str(exc)
    except FileNotFoundError:
        reason = "file_missing"
    except (OSError, RuntimeError, ValueError):
        reason = "file_unreadable"
    summary, truncated = _bounded_text(artifact.summary)
    return summary, _evidence(
        "summary" if summary else "missing",
        reason=reason,
        expected_hash=expected,
        truncated=truncated,
        incomplete=True,
        freshness="stale" if reason == "file_missing" else "unknown",
    )


def retrieve_original(ref: str) -> dict[str, Any]:
    """Return authorized stored evidence or a bounded current-file snapshot."""
    kind, ident = _split_ref(ref)
    init_db()
    visible = _visibility()
    with _db_module.SessionLocal() as session:
        if kind == "knowledge":
            from brains.control.knowledge import knowledge_lifecycle

            row = _visible_knowledge(session, ident, visible)
            if row is None:
                raise ValueError(f"unknown or inaccessible knowledge ref: {ref}")
            lifecycle = knowledge_lifecycle(row)
            freshness = lifecycle["freshness"]
            if lifecycle["superseded"]:
                freshness = "superseded"
            elif freshness not in {"current", "stale", "expired", "unknown"}:
                freshness = "unknown"
            successor = (
                _visible_knowledge(session, row.superseded_by_id, visible)
                if row.superseded_by_id
                else None
            )
            body, truncated = _bounded_text(row.body)
            recorded_evidence, evidence_truncated = _bounded_text(row.evidence)
            evidence = _evidence(
                "stored_knowledge",
                freshness=freshness,
                truncated=truncated,
                incomplete=truncated or evidence_truncated,
                reason="content_limit" if truncated or evidence_truncated else "stored_row",
                provenance=row.provenance,
                confidence=row.confidence,
                recorded_evidence=recorded_evidence,
                recorded_evidence_truncated=evidence_truncated,
                created_at=row.created_at.isoformat() if row.created_at else None,
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
                valid_until=row.valid_until.isoformat() if row.valid_until else None,
                successor_ref=f"knowledge:{successor.code}" if successor else None,
            )
            return _with_evidence(
                {
                    "ref": ref,
                    "kind": "knowledge",
                    "id": row.code,
                    "title": row.title,
                    "content": body,
                    "body": body,
                    "metadata": {
                        "type": row.type,
                        **lifecycle,
                        "scope": row.scope,
                        "workspace_id": row.workspace_id,
                    },
                },
                evidence,
            )

        try:
            row_id = int(ident)
            if not -(2**63) <= row_id < 2**63:
                raise ValueError
        except ValueError as exc:
            raise ValueError(f"{kind} ref id must be an integer: {ref}") from exc

        # Defer content and descriptive columns until ancestry is authorized.
        chunk = (
            session.get(Chunk, row_id, options=[load_only(Chunk.id, Chunk.artifact_id)])
            if kind == "chunk"
            else None
        )
        artifact_id = chunk.artifact_id if chunk else row_id if kind == "artifact" else None
        artifact = (
            session.get(Artifact, artifact_id, options=[load_only(Artifact.id, Artifact.source_id)])
            if artifact_id is not None
            else None
        )
        if artifact is None:
            raise ValueError(f"unknown or inaccessible ref: {ref}")
        source, workspace = _ancestry(session, artifact, visible, ref)
        if chunk is not None:
            content, truncated = _bounded_text(chunk.content)
            # Chunk.hash is the source FILE hash, not the digest of chunk.content.
            # Comparing them cannot authenticate the stored chunk text or its currency.
            indexed_hash, chunk_hash = _sha256(artifact.hash), _sha256(chunk.hash)
            stale = bool(indexed_hash and chunk_hash and indexed_hash != chunk_hash)
            return _with_evidence(
                {
                    "ref": ref,
                    "kind": "chunk",
                    "id": chunk.id,
                    "content": content,
                    "metadata": {"artifact_id": chunk.artifact_id, "ordinal": chunk.ordinal},
                },
                _evidence(
                    "stored_chunk",
                    freshness="stale" if stale else "unknown",
                    truncated=truncated,
                    incomplete=truncated,
                    reason="content_limit"
                    if truncated
                    else "indexed_hash_changed"
                    if stale
                    else "stored_row",
                    expected_hash=indexed_hash,
                    captured_file_hash=chunk_hash,
                    source_id=source.id,
                    workspace_id=source.workspace_id,
                ),
            )

        content, evidence = _artifact_content(artifact, source, workspace)
        return _with_evidence(
            {
                "ref": ref,
                "kind": "artifact",
                "id": artifact.id,
                "title": artifact.title,
                "path": artifact.path,
                "content": content,
                "metadata": {
                    "source_id": artifact.source_id,
                    "language": artifact.language,
                    "size": artifact.size,
                },
            },
            evidence,
        )


__all__ = ["retrieve_original"]

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from brains.config import settings
from brains.control.common import normalize_path
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Artifact, Source

IGNORE = {".git", "node_modules", ".venv", "__pycache__", "vendor", "dist", "build"}

REPO_SOURCE_TYPE = "repo_dir"


def guess_language(path: Path) -> str:
    mapping = {".py": "python", ".ts": "typescript", ".js": "javascript", ".md": "markdown"}
    return mapping.get(path.suffix.lower(), "text")


def index_repo(path: str):
    output = []
    root = Path(path).expanduser().resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in IGNORE]
        for filename in filenames:
            file_path = Path(dirpath) / filename
            if not file_path.is_file():
                continue
            try:
                size = file_path.stat().st_size
            except OSError:
                size = 0
            content_hash = ""
            if size <= settings.repo_index_max_file_size:
                try:
                    content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
                except OSError:
                    content_hash = ""
            output.append(
                {
                    "path": str(file_path),
                    "size": size,
                    "language": guess_language(file_path),
                    "hash": content_hash,
                }
            )
    return output


def search_repo(path: str, query: str):
    matches = []
    for row in index_repo(path):
        if row["size"] > settings.repo_index_max_file_size:
            continue
        try:
            payload = Path(row["path"]).read_bytes()
            if b"\x00" in payload[:2048]:
                continue
            text = payload.decode("utf-8", errors="ignore")
        except OSError:
            continue
        if query.lower() in text.lower():
            matches.append(row)
    return matches


# ----------------------------------------------------------------------
# Persistent index — writes to Source/Artifact with content-hash dedup
# so a re-index of a 10k-file workspace is cheap when nothing changed.
# ----------------------------------------------------------------------


def _walk_repo(root: Path) -> list[dict]:
    """Walk ``root`` once and return a list of file records.

    Each record is a dict ``{rel_path, size, hash, language, mtime}``;
    ``hash`` is empty for files larger than ``settings.repo_index_max_file_size``
    so we never read multi-gigabyte binaries.
    """
    records: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in IGNORE]
        for filename in filenames:
            file_path = Path(dirpath) / filename
            if not file_path.is_file():
                continue
            try:
                stat = file_path.stat()
            except OSError:
                continue
            size = stat.st_size
            content_hash = ""
            if size <= settings.repo_index_max_file_size:
                try:
                    content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
                except OSError:
                    content_hash = ""
            try:
                rel = str(file_path.relative_to(root)).replace("\\", "/")
            except ValueError:
                rel = str(file_path)
            records.append(
                {
                    "rel_path": rel,
                    "size": size,
                    "hash": content_hash,
                    "language": guess_language(file_path),
                    "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                }
            )
    return records


def index_repo_persisted(path: str) -> dict:
    """Index a workspace's repo into the ``sources`` / ``artifacts`` tables.

    Uses content-hash dedup: a file whose hash matches the recorded
    ``Artifact.hash`` is left untouched. New files are inserted, changed
    files are updated, and vanished files are deleted. Returns counts of
    each disposition.
    """
    workspace = register_workspace(path)
    root = Path(normalize_path(path)).expanduser().resolve()
    records = _walk_repo(root)
    init_db()
    inserted = 0
    updated = 0
    unchanged = 0
    removed = 0
    with SessionLocal() as session:
        source = (
            session.query(Source)
            .filter(
                Source.workspace_id == workspace.id,
                Source.source_type == REPO_SOURCE_TYPE,
                Source.uri == workspace.path,
            )
            .one_or_none()
        )
        if source is None:
            source = Source(
                workspace_id=workspace.id,
                source_type=REPO_SOURCE_TYPE,
                uri=workspace.path,
                title=f"{workspace.slug} repo",
                status="active",
            )
            session.add(source)
            session.flush()
        else:
            source.updated_at = datetime.now(UTC)

        existing = {
            row.path: row
            for row in session.query(Artifact).filter(Artifact.source_id == source.id).all()
        }
        seen: set[str] = set()
        for record in records:
            rel = record["rel_path"]
            seen.add(rel)
            current = existing.get(rel)
            if current is None:
                session.add(
                    Artifact(
                        source_id=source.id,
                        path=rel,
                        language=record["language"],
                        size=record["size"],
                        hash=record["hash"] or None,
                        mtime=record["mtime"],
                        metadata_json=json.dumps({"abs_path": str(root / rel)}),
                    )
                )
                inserted += 1
                continue
            if current.hash and current.hash == record["hash"]:
                unchanged += 1
                continue
            current.language = record["language"]
            current.size = record["size"]
            current.hash = record["hash"] or None
            current.mtime = record["mtime"]
            current.metadata_json = json.dumps({"abs_path": str(root / rel)})
            updated += 1
        for rel, row in existing.items():
            if rel in seen:
                continue
            session.delete(row)
            removed += 1
        session.commit()
        source_id = source.id

    result = {
        "workspace": workspace.slug,
        "source_id": source_id,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "removed": removed,
        "total": len(records),
    }
    append_event(
        "repo_index",
        f"indexed {len(records)} files (+{inserted} ~{updated} ={unchanged} -{removed})",
        workspace_id=workspace.id,
        metadata=result,
    )
    return result


def search_repo_persisted(path: str, query: str, limit: int = 50) -> list[dict]:
    """Read-side companion to :func:`index_repo_persisted`.

    Iterates the persisted ``Artifact`` rows for the workspace's repo
    source, opens each file to substring-match the query, and returns
    up to ``limit`` hits. Falls back to an empty list if the workspace
    has never been indexed via the persisted writer.
    """
    workspace = register_workspace(path)
    root = Path(normalize_path(path)).expanduser().resolve()
    needle = query.lower()
    init_db()
    matches: list[dict] = []
    with SessionLocal() as session:
        source = (
            session.query(Source)
            .filter(
                Source.workspace_id == workspace.id,
                Source.source_type == REPO_SOURCE_TYPE,
                Source.uri == workspace.path,
            )
            .one_or_none()
        )
        if source is None:
            return []
        rows = session.query(Artifact).filter(Artifact.source_id == source.id).all()
    for row in rows:
        if row.size and row.size > settings.repo_index_max_file_size:
            continue
        abs_path = root / row.path
        try:
            payload = abs_path.read_bytes()
        except OSError:
            continue
        if b"\x00" in payload[:2048]:
            continue
        text = payload.decode("utf-8", errors="ignore")
        if needle not in text.lower():
            continue
        matches.append(
            {
                "path": str(abs_path),
                "rel_path": row.path,
                "size": row.size,
                "language": row.language,
                "hash": row.hash,
            }
        )
        if len(matches) >= limit:
            break
    return matches

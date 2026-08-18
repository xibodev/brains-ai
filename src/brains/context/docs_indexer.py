from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from brains.config import settings
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Artifact, Source

DOCS_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "vendor",
    "dist",
    "build",
    ".next",
    ".turbo",
    "coverage",
    ".pytest_cache",
    "playwright-report",
    "test-results",
}
DOCS_MAX_DEPTH = 3
DOCS_SUMMARY_LEN = 180


def _hash_file(path: Path, max_bytes: int) -> str | None:
    try:
        if path.stat().st_size > max_bytes:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _extract_doc_meta(path: Path) -> tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return path.stem, ""

    title = ""
    summary = ""
    for line in lines[:100]:
        text = line.strip()
        if not text:
            continue
        if not title:
            title = text.lstrip("#").strip() or text
            continue
        if text.startswith(("#", "---", "|", "`")):
            continue
        summary = text
        break
    if not title:
        title = path.stem
    if len(summary) > DOCS_SUMMARY_LEN:
        summary = summary[: DOCS_SUMMARY_LEN - 3] + "..."
    return title[:120], summary


def _is_stale(mtime: datetime) -> bool:
    return (datetime.now(UTC) - mtime).days > settings.docs_stale_days


def scan_docs(path: str, max_depth: int = DOCS_MAX_DEPTH) -> list[dict]:
    root = Path(path).expanduser().resolve()
    records: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames if name not in DOCS_SKIP_DIRS and not name.startswith(".")
        ]
        rel_dir = Path(dirpath).relative_to(root)
        if len(rel_dir.parts) >= max_depth:
            dirnames[:] = []
        for filename in filenames:
            if not filename.lower().endswith(".md"):
                continue
            full_path = Path(dirpath) / filename
            try:
                stat = full_path.stat()
            except OSError:
                continue
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            title, summary = _extract_doc_meta(full_path)
            records.append(
                {
                    "path": str(full_path),
                    "rel_path": str(full_path.relative_to(root)).replace("\\", "/"),
                    "size": stat.st_size,
                    "mtime": mtime,
                    "hash": _hash_file(full_path, settings.repo_index_max_file_size),
                    "language": "markdown",
                    "title": title,
                    "summary": summary,
                    "stale": _is_stale(mtime),
                }
            )
    records.sort(key=lambda row: (row["mtime"], row["rel_path"]), reverse=True)
    return records


def index_docs(path: str) -> dict:
    workspace = register_workspace(path)
    records = scan_docs(workspace.path)
    init_db()
    with SessionLocal() as session:
        source = (
            session.query(Source)
            .filter(Source.workspace_id == workspace.id, Source.uri == workspace.path)
            .one_or_none()
        )
        if source is None:
            source = Source(
                workspace_id=workspace.id,
                source_type="docs_dir",
                uri=workspace.path,
                title=f"{workspace.slug} docs",
                status="active",
            )
            session.add(source)
            session.flush()
        else:
            source.updated_at = datetime.now(UTC)
        session.query(Artifact).filter(Artifact.source_id == source.id).delete()
        for record in records:
            session.add(
                Artifact(
                    source_id=source.id,
                    path=record["rel_path"],
                    language=record["language"],
                    size=record["size"],
                    hash=record["hash"],
                    mtime=record["mtime"],
                    title=record["title"],
                    summary=record["summary"],
                    metadata_json=json.dumps({"stale": record["stale"]}),
                )
            )
        session.commit()
        source_id = source.id
    append_event(
        "docs_index",
        f"indexed {len(records)} docs",
        workspace_id=workspace.id,
        metadata={"source_id": source_id, "count": len(records)},
    )
    try:
        from brains.control.views import refresh_views

        refresh_views(workspace.path)
    except Exception:
        pass
    return {"workspace": workspace.slug, "count": len(records), "records": records}


def search_docs(path: str, query: str, limit: int = 20) -> list[dict]:
    workspace = register_workspace(path)
    needle = query.lower()
    init_db()
    with SessionLocal() as session:
        source = (
            session.query(Source)
            .filter(Source.workspace_id == workspace.id, Source.uri == workspace.path)
            .one_or_none()
        )
        if source is None:
            return []
        rows = session.query(Artifact).filter(Artifact.source_id == source.id).all()
        matches = []
        for row in rows:
            haystack = " ".join(value or "" for value in (row.path, row.title, row.summary)).lower()
            if needle in haystack:
                matches.append(
                    {
                        "path": row.path,
                        "title": row.title,
                        "summary": row.summary,
                        "size": row.size,
                        "mtime": row.mtime.isoformat() if row.mtime else None,
                        "language": row.language,
                    }
                )
            if len(matches) >= limit:
                break
        return matches

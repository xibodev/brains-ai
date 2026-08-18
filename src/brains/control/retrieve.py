"""Lossless retrieval for compact context references."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import brains.storage.db as _db_module
from brains.control.common import normalize_path
from brains.storage.migrations import init_db
from brains.storage.models import Artifact, Chunk, KnowledgeEntry, Source


def _split_ref(ref: str) -> tuple[str, str]:
    kind, sep, ident = str(ref or "").partition(":")
    if not sep or not kind or not ident:
        raise ValueError("ref must be one of chunk:<id>, artifact:<id>, or knowledge:<code>")
    if kind not in {"chunk", "artifact", "knowledge"}:
        raise ValueError("ref must be one of chunk:<id>, artifact:<id>, or knowledge:<code>")
    return kind, ident


def _visible_knowledge(session, code: str) -> KnowledgeEntry | None:
    from sqlalchemy import or_

    from brains.control.memberships import visible_workspace_ids_for_current

    q = session.query(KnowledgeEntry).filter(KnowledgeEntry.code == code)
    visible = visible_workspace_ids_for_current()
    if visible is not None:
        q = q.filter(
            or_(
                KnowledgeEntry.scope.in_(["shared", "global"]),
                KnowledgeEntry.workspace_id.in_(visible),
            )
        )
    return q.one_or_none()


def _artifact_content(artifact: Artifact, source: Source | None) -> str:
    candidates: list[Path] = []
    metadata: dict[str, Any] = {}
    if artifact.metadata_json:
        try:
            parsed = json.loads(artifact.metadata_json)
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            metadata = parsed
    abs_path = metadata.get("abs_path")
    if isinstance(abs_path, str) and abs_path.strip():
        candidates.append(Path(abs_path).expanduser())
    if source and source.uri:
        candidates.append(Path(normalize_path(source.uri)).expanduser() / artifact.path)

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return artifact.summary or ""


def retrieve_original(ref: str) -> dict[str, Any]:
    """Return the full content behind a compact context ``ref``."""

    kind, ident = _split_ref(ref)
    init_db()
    with _db_module.SessionLocal() as session:
        if kind == "knowledge":
            row = _visible_knowledge(session, ident)
            if row is None:
                raise ValueError(f"unknown or inaccessible knowledge ref: {ref}")
            return {
                "ref": ref,
                "kind": "knowledge",
                "id": row.code,
                "title": row.title,
                "content": row.body,
                "body": row.body,
                "metadata": {
                    "type": row.type,
                    "status": row.status,
                    "scope": row.scope,
                    "workspace_id": row.workspace_id,
                },
            }

        try:
            row_id = int(ident)
        except ValueError as exc:
            raise ValueError(f"{kind} ref id must be an integer: {ref}") from exc

        if kind == "chunk":
            chunk = session.get(Chunk, row_id)
            if chunk is None:
                raise ValueError(f"unknown chunk ref: {ref}")
            return {
                "ref": ref,
                "kind": "chunk",
                "id": chunk.id,
                "content": chunk.content,
                "metadata": {"artifact_id": chunk.artifact_id, "ordinal": chunk.ordinal},
            }

        artifact = session.get(Artifact, row_id)
        if artifact is None:
            raise ValueError(f"unknown artifact ref: {ref}")
        source = session.get(Source, artifact.source_id)
        return {
            "ref": ref,
            "kind": "artifact",
            "id": artifact.id,
            "title": artifact.title,
            "path": artifact.path,
            "content": _artifact_content(artifact, source),
            "metadata": {
                "source_id": artifact.source_id,
                "language": artifact.language,
                "size": artifact.size,
            },
        }


__all__ = ["retrieve_original"]

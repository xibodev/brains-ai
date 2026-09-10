"""Cross-operator knowledge ledger (Phase 2).

A small, visibility-scoped ledger of durable knowledge — blockers, workarounds,
resolutions, caveats, environment/dependency notes — with a lifecycle and a
supersede chain. This is the workspace-scoped, membership-filtered counterpart
to the deliberately-global ``knowledge_patterns`` library: it lets one
operator's solved blocker surface (safely, within scope) to another operator
who hits the same wall.

Scope taxonomy (neutral): ``private`` < ``workspace`` < ``shared`` < ``global``.
A non-admin operator sees an entry when it is ``shared``/``global``, or when its
workspace is one they can see (decision record 0002). ``admin`` sees everything.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime

from sqlalchemy import case, or_

import brains.storage.db as _db_module
from brains.config import settings
from brains.control.common import (
    insert_with_code_retry,
    next_sequential_code,
    utc_now,
)
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.migrations import init_db
from brains.storage.models import KnowledgeEntry, Workspace

ENTRY_TYPES = {
    "blocker",
    "workaround",
    "resolution",
    "caveat",
    "environment_note",
    "dependency_note",
}
ENTRY_SCOPES = {"private", "workspace", "shared", "global"}
ENTRY_PROVENANCE = {"extracted", "inferred", "ambiguous"}
# Statuses an entry may be transitioned to via ``resolve_knowledge_entry``.
RESOLVABLE_STATUSES = {"active", "confirmed", "resolved", "rejected", "stale"}
CONTENT_LIMIT_BYTES = 64 * 1024


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def knowledge_lifecycle(row: KnowledgeEntry, now: datetime | None = None) -> dict:
    """Describe effective lifecycle without changing the stored entry.

    SQLite's naive timestamps represent UTC. Supersession wins for effective
    status; expiry only turns stored active/confirmed entries into stale ones.
    The expiry flag also describes deadlines on historical entries.
    """
    now = _as_utc(now if now is not None else utc_now())
    expired = row.valid_until is not None and _as_utc(row.valid_until) < now
    superseded = row.superseded_by_id is not None or row.status == "superseded"
    due = expired and row.status in {"active", "confirmed"}
    status = "superseded" if superseded else "stale" if due else row.status
    if due:
        freshness = "expired"
    elif superseded:
        freshness = "superseded"
    elif row.status == "stale":
        freshness = "stale"
    elif row.status in {"active", "confirmed"}:
        freshness = "current"
    else:
        freshness = "historical"
    return {
        "status": status,
        "stored_status": row.status,
        "freshness": freshness,
        "expired": expired,
        "superseded": superseded,
    }


def _visible_entries(query, visible):
    if visible is None:
        return query
    return query.filter(
        or_(
            KnowledgeEntry.scope.in_(["shared", "global"]),
            KnowledgeEntry.workspace_id.in_(visible),
        )
    )


def _next_code(session, prefix: str = "KNOW") -> str:
    return next_sequential_code(session, KnowledgeEntry.code, prefix)


def _resolve_operator_id() -> int | None:
    try:
        from brains.control.operators import resolve_current_operator

        return resolve_current_operator().get("id")
    except Exception:
        return None


def _entry_to_dict(
    row: KnowledgeEntry,
    workspace_slug: str | None,
    now: datetime | None = None,
    *,
    visible_successor_ids: set[int] | frozenset[int] = frozenset(),
) -> dict:
    content = (row.body or "").encode("utf-8")
    evidence = (row.evidence or "").encode("utf-8")
    body_truncated = len(content) > CONTENT_LIMIT_BYTES
    evidence_truncated = len(evidence) > CONTENT_LIMIT_BYTES
    truncated = body_truncated or evidence_truncated
    result = {
        "code": row.code,
        "type": row.type,
        "title": row.title,
        "body": content[:CONTENT_LIMIT_BYTES].decode("utf-8", errors="ignore"),
        **knowledge_lifecycle(row, now),
        "truncated": truncated,
        "body_truncated": body_truncated,
        "evidence_truncated": evidence_truncated,
        "evidence_bytes": len(evidence),
        "content_bytes": len(content),
        "content_limit_bytes": CONTENT_LIMIT_BYTES,
        "scope": row.scope,
        "workspace": workspace_slug,
        "tags": row.tags,
        "confidence": row.confidence,
        "provenance": row.provenance,
        "evidence": evidence[:CONTENT_LIMIT_BYTES].decode("utf-8", errors="ignore"),
        "importance": row.importance,
        "severity": row.severity,
        "valid_until": row.valid_until.isoformat() if row.valid_until else None,
        "promoted_from": row.promoted_from,
        "superseded_by_id": row.superseded_by_id
        if row.superseded_by_id in visible_successor_ids
        else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }
    if truncated:
        result["ref"] = f"knowledge:{row.code}"
    return result


def _compact_entry(row: dict) -> dict:
    compact = dict(row)
    compact["body"] = str(row.get("body") or "")[:200]
    compact["body_truncated"] = row["content_bytes"] > len(compact["body"].encode("utf-8"))
    compact["truncated"] = compact["body_truncated"] or compact["evidence_truncated"]
    compact["ref"] = f"knowledge:{row['code']}"
    compact["compressed"] = True
    return compact


def _parse_valid_until(value: datetime | str | None) -> datetime | None:
    if value is None:
        return value
    if isinstance(value, datetime):
        return _as_utc(value)
    raw = value.strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return _as_utc(parsed)


def add_knowledge_entry(
    workspace_path: str,
    type: str,
    title: str,
    *,
    body: str = "",
    scope: str = "workspace",
    tags: str = "",
    confidence: str = "medium",
    severity: str = "info",
    evidence: str = "",
    supersedes_code: str | None = None,
    provenance: str = "inferred",
    importance: float = 0.5,
    valid_until: datetime | str | None = None,
    promoted_from: str | None = None,
    session_id: str | None = None,
    operator_id: int | None = None,
    metadata: dict | None = None,
) -> dict:
    """Record a knowledge entry against ``workspace_path``.

    When ``supersedes_code`` is given (e.g. a resolution that obsoletes an
    earlier workaround), the referenced entry is marked ``superseded`` and
    linked to the new one.
    """
    if type not in ENTRY_TYPES:
        raise ValueError(f"type must be one of {sorted(ENTRY_TYPES)}")
    if scope not in ENTRY_SCOPES:
        raise ValueError(f"scope must be one of {sorted(ENTRY_SCOPES)}")
    if provenance not in ENTRY_PROVENANCE:
        raise ValueError(f"provenance must be one of {sorted(ENTRY_PROVENANCE)}")
    parsed_valid_until = _parse_valid_until(valid_until)
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    workspace = register_workspace(workspace_path)
    resolved_operator_id = operator_id if operator_id is not None else _resolve_operator_id()
    init_db()
    holder: dict = {}

    def build(session):
        promoted_from_id = None
        if promoted_from:
            promoted = (
                _visible_entries(session.query(KnowledgeEntry), visible)
                .filter(KnowledgeEntry.code == promoted_from)
                .one_or_none()
            )
            if promoted is None:
                raise ValueError(f"unknown or inaccessible knowledge entry: {promoted_from}")
            promoted_from_id = promoted.id
        row = KnowledgeEntry(
            code=_next_code(session),
            type=type,
            title=title,
            body=body,
            status="active",
            scope=scope,
            workspace_id=workspace.id,
            tags=tags,
            confidence=confidence,
            provenance=provenance,
            importance=importance,
            severity=severity,
            valid_until=parsed_valid_until,
            promoted_from=promoted_from_id,
            evidence=evidence,
            created_by_operator_id=resolved_operator_id,
            created_by_session_id=session_id,
            metadata_json=json.dumps(metadata or {}),
        )
        session.add(row)
        session.flush()
        new_id = row.id
        holder["superseded"] = None
        if supersedes_code:
            old = (
                _visible_entries(session.query(KnowledgeEntry), visible)
                .filter(KnowledgeEntry.code == supersedes_code)
                .one_or_none()
            )
            if old is None or old.id == new_id:
                raise ValueError(f"unknown or inaccessible knowledge entry: {supersedes_code}")
            changed = (
                _visible_entries(session.query(KnowledgeEntry), visible)
                .filter(
                    KnowledgeEntry.id == old.id,
                    KnowledgeEntry.superseded_by_id.is_(None),
                    KnowledgeEntry.status != "superseded",
                )
                .update(
                    {
                        "status": "superseded",
                        "superseded_by_id": new_id,
                        "resolved_at": utc_now(),
                    },
                    synchronize_session=False,
                )
            )
            if changed != 1:
                raise ValueError(
                    f"knowledge entry changed or already superseded: {supersedes_code}"
                )
            holder["superseded"] = old.code
        return row

    def finalize(_session, row):
        result = _entry_to_dict(row, workspace.slug)
        result["supersedes"] = holder.get("superseded")
        return result

    # Retry on the unique-code race so concurrent operators logging knowledge
    # on a shared DB can't both mint KNOW-000N.
    result = insert_with_code_retry(build, finalize)
    code = result["code"]
    append_event(
        "knowledge_added",
        f"{code}: [{type}] {title}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"code": code, "type": type, "scope": scope},
    )
    return result


def resolve_knowledge_entry(code: str, status: str = "resolved") -> dict:
    """Transition an entry's lifecycle status (resolve / confirm / reject / ...)."""
    from brains.control.memberships import visible_workspace_ids_for_current

    if status not in RESOLVABLE_STATUSES:
        raise ValueError(f"status must be one of {sorted(RESOLVABLE_STATUSES)}")
    init_db()
    visible = visible_workspace_ids_for_current()
    with _db_module.SessionLocal() as session:
        row = (
            _visible_entries(session.query(KnowledgeEntry), visible)
            .filter(KnowledgeEntry.code == code)
            .one_or_none()
        )
        if row is None:
            raise ValueError(f"unknown or inaccessible knowledge entry: {code}")
        if status in {"active", "confirmed"} and knowledge_lifecycle(row)["superseded"]:
            raise ValueError(f"cannot reactivate superseded knowledge entry: {code}")
        update_query = _visible_entries(session.query(KnowledgeEntry), visible).filter(
            KnowledgeEntry.id == row.id,
            KnowledgeEntry.status == row.status,
            KnowledgeEntry.superseded_by_id == row.superseded_by_id,
        )
        if status in {"active", "confirmed"}:
            update_query = update_query.filter(
                KnowledgeEntry.status != "superseded",
                KnowledgeEntry.superseded_by_id.is_(None),
            )
        values: dict = {"status": status}
        if status in {"resolved", "rejected"}:
            values["resolved_at"] = utc_now()
        if update_query.update(values, synchronize_session=False) != 1:
            raise ValueError(f"knowledge entry changed: {code}")
        workspace_id = row.workspace_id
        session_id = row.created_by_session_id
        session.commit()
    append_event(
        "knowledge_resolved",
        f"{code} -> {status}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"code": code, "status": status},
    )
    return {"code": code, "status": status}


def search_knowledge(
    query: str | None = None,
    type: str | None = None,
    status: str | None = None,
    workspace_path: str | None = None,
    tags: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Visibility-aware search over the ledger.

    ``admin`` sees all entries; every other operator sees ``shared``/``global``
    entries plus entries whose workspace they can see (membership / shared).
    No status filter includes history, ordered by importance as before. Explicit
    status filters use effective lifecycle before the bounded result limit.
    Body and evidence are each bounded to 64 KiB of UTF-8; compression additionally
    caps only the body at 200 characters. A ref addresses stored history, not a
    promise of unbounded retrieval. Successor IDs require successor visibility.
    """
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    now = _as_utc(utc_now())
    limit = max(1, min(limit, 100))
    init_db()
    with _db_module.SessionLocal() as session:
        q = session.query(KnowledgeEntry, Workspace).outerjoin(
            Workspace, Workspace.id == KnowledgeEntry.workspace_id
        )
        if type:
            q = q.filter(KnowledgeEntry.type == type)
        if status:
            effective_status = case(
                (
                    or_(
                        KnowledgeEntry.superseded_by_id.is_not(None),
                        KnowledgeEntry.status == "superseded",
                    ),
                    "superseded",
                ),
                (
                    KnowledgeEntry.status.in_(["active", "confirmed"])
                    & (KnowledgeEntry.valid_until < now),
                    "stale",
                ),
                else_=KnowledgeEntry.status,
            )
            q = q.filter(effective_status == status)
        if query:
            like = f"%{query}%"
            q = q.filter(or_(KnowledgeEntry.title.ilike(like), KnowledgeEntry.body.ilike(like)))
        if tags:
            q = q.filter(KnowledgeEntry.tags.ilike(f"%{tags}%"))
        if workspace_path:
            workspace = register_workspace(workspace_path)
            q = q.filter(KnowledgeEntry.workspace_id == workspace.id)
        q = _visible_entries(q, visible)
        rows = (
            q.order_by(
                KnowledgeEntry.importance.desc(),
                KnowledgeEntry.created_at.desc(),
                KnowledgeEntry.id.desc(),
            )
            .limit(limit)
            .all()
        )
        successor_ids = {row.superseded_by_id for row, _ws in rows if row.superseded_by_id}
        visible_successor_ids = (
            {
                successor_id
                for (successor_id,) in _visible_entries(session.query(KnowledgeEntry.id), visible)
                .filter(KnowledgeEntry.id.in_(successor_ids))
                .all()
            }
            if successor_ids
            else set()
        )
        results = [
            _entry_to_dict(
                row, ws.slug if ws else None, now, visible_successor_ids=visible_successor_ids
            )
            for row, ws in rows
        ]
        if settings.context_compression_enabled:
            return [_compact_entry(row) for row in results]
        return results


def expire_stale_knowledge() -> int:
    """Mark active/confirmed entries stale after their ``valid_until`` time."""
    init_db()
    now = utc_now()
    expired: list[tuple[str, int | None, str | None]] = []
    with _db_module.SessionLocal() as session:
        rows = (
            session.query(KnowledgeEntry)
            .filter(
                KnowledgeEntry.status.in_(["active", "confirmed"]),
                KnowledgeEntry.valid_until.is_not(None),
                KnowledgeEntry.valid_until < now,
            )
            .all()
        )
        for row in rows:
            changed = (
                session.query(KnowledgeEntry)
                .filter(
                    KnowledgeEntry.id == row.id,
                    KnowledgeEntry.status.in_(["active", "confirmed"]),
                    KnowledgeEntry.superseded_by_id.is_(None),
                    KnowledgeEntry.valid_until < now,
                )
                .update(
                    {"status": "stale", "resolved_at": now},
                    synchronize_session=False,
                )
            )
            if changed:
                expired.append((row.code, row.workspace_id, row.created_by_session_id))
        session.commit()
    for code, workspace_id, session_id in expired:
        with contextlib.suppress(Exception):
            append_event(
                "knowledge_expired",
                f"{code} -> stale",
                workspace_id=workspace_id,
                session_id=session_id,
                metadata={"code": code, "status": "stale"},
            )
    return len(expired)


__all__ = [
    "ENTRY_PROVENANCE",
    "ENTRY_SCOPES",
    "ENTRY_TYPES",
    "RESOLVABLE_STATUSES",
    "add_knowledge_entry",
    "expire_stale_knowledge",
    "knowledge_lifecycle",
    "resolve_knowledge_entry",
    "search_knowledge",
]

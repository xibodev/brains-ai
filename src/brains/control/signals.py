"""Advisory, visibility-filtered coordination signals.

Signals are a small read projection over existing coordination data. They are
intentionally count-only so cross-workspace views can show useful presence
without leaking paths, session ids, handoff bodies, or knowledge text.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_

import brains.storage.db as _db_module
from brains.control.common import utc_now
from brains.control.sessions import register_workspace
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, Handoff, KnowledgeEntry, Workspace

KNOWLEDGE_SIGNALS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("blocked", "blocker", ("proposed", "active")),
    ("workaround_available", "workaround", ("active", "confirmed")),
    ("known_issue", "caveat", ("active", "confirmed")),
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _signal(
    type: str,
    *,
    scope: str,
    workspace: str | None,
    count: int,
    last_at: datetime | None,
) -> dict:
    return {
        "type": type,
        "scope": scope,
        "workspace": workspace,
        "count": int(count),
        "last_at": _iso(last_at),
    }


def list_signals(workspace_path: str | None = None, limit: int = 50) -> list[dict]:
    """Return advisory signals derived from visible coordination data."""
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    workspace_id: int | None = None
    workspace_slug: str | None = None
    if workspace_path:
        workspace = register_workspace(workspace_path)
        workspace_id = workspace.id
        workspace_slug = workspace.slug
    visible_workspace_slug = workspace_slug
    if visible is not None and workspace_id is not None and workspace_id not in visible:
        visible_workspace_slug = None

    signal_scope = "workspace" if workspace_id is not None else "cross_workspace"
    out: list[dict] = []
    with _db_module.SessionLocal() as session:
        for signal_type, entry_type, statuses in KNOWLEDGE_SIGNALS:
            query = session.query(
                func.count(KnowledgeEntry.id),
                func.max(func.coalesce(KnowledgeEntry.updated_at, KnowledgeEntry.created_at)),
            ).filter(
                KnowledgeEntry.type == entry_type,
                KnowledgeEntry.status.in_(statuses),
            )
            if workspace_id is not None:
                query = query.filter(KnowledgeEntry.workspace_id == workspace_id)
            if visible is not None:
                query = query.filter(
                    or_(
                        KnowledgeEntry.scope.in_(["shared", "global"]),
                        KnowledgeEntry.workspace_id.in_(visible),
                    )
                )
            count, last_at = query.one()
            if count:
                out.append(
                    _signal(
                        signal_type,
                        scope=signal_scope,
                        workspace=visible_workspace_slug,
                        count=count,
                        last_at=last_at,
                    )
                )

        handoff_query = session.query(func.count(Handoff.id), func.max(Handoff.set_at)).filter(
            Handoff.status == "active"
        )
        if workspace_id is not None:
            handoff_query = handoff_query.filter(Handoff.workspace_id == workspace_id)
        if visible is not None:
            handoff_query = handoff_query.filter(Handoff.workspace_id.in_(visible))
        handoff_count, handoff_last_at = handoff_query.one()
        if handoff_count:
            out.append(
                _signal(
                    "handoff_available",
                    scope=signal_scope,
                    workspace=visible_workspace_slug,
                    count=handoff_count,
                    last_at=handoff_last_at,
                )
            )

        cutoff = utc_now() - timedelta(minutes=30)
        duplicate_query = (
            session.query(
                AgentSession.workspace_id,
                Workspace.slug,
                func.count(AgentSession.id),
                func.max(AgentSession.last_activity_at),
            )
            .join(Workspace, Workspace.id == AgentSession.workspace_id)
            .filter(
                AgentSession.ended_at.is_(None),
                AgentSession.state != "dormant",
                AgentSession.last_activity_at >= cutoff,
            )
        )
        if workspace_id is not None:
            duplicate_query = duplicate_query.filter(AgentSession.workspace_id == workspace_id)
        if visible is not None:
            duplicate_query = duplicate_query.filter(AgentSession.workspace_id.in_(visible))
        duplicate_rows = (
            duplicate_query.group_by(AgentSession.workspace_id, Workspace.slug)
            .having(func.count(AgentSession.id) > 1)
            .all()
        )
        for _, slug, count, last_at in duplicate_rows:
            out.append(
                _signal(
                    "duplicate_work",
                    scope="workspace",
                    workspace=slug,
                    count=count,
                    last_at=last_at,
                )
            )

    out.sort(key=lambda row: (row["last_at"] or "", row["type"]), reverse=True)
    return out[: max(0, int(limit))]


__all__ = [
    "list_signals",
]

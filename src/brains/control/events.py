from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, Event


def append_event(
    kind: str,
    message: str,
    *,
    workspace_id: int | None = None,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Event:
    init_db()
    now = datetime.now(UTC)
    normalized_session_id = session_id.strip() if isinstance(session_id, str) else session_id
    if not normalized_session_id:
        normalized_session_id = None
    with SessionLocal() as session:
        row = Event(
            workspace_id=workspace_id,
            session_id=normalized_session_id,
            kind=kind,
            message=message,
            metadata_json=json.dumps(metadata or {}),
        )
        session.add(row)
        # Heartbeat: stamp last_activity_at on the originating session so
        # the reaper / resume UI can show how fresh it is, without
        # forcing every agent to call a dedicated heartbeat tool. Cheap
        # — same transaction as the event insert, single UPDATE by PK.
        if normalized_session_id:
            session.query(AgentSession).filter(AgentSession.id == normalized_session_id).update(
                {"last_activity_at": now}, synchronize_session=False
            )
        session.commit()
        session.refresh(row)
        return row


def list_events(workspace_id: int | None = None, limit: int = 100) -> list[Event]:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    # Events without a ``workspace_id`` (e.g. system-wide signals) are
    # always visible; only workspace-scoped events get filtered.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        query = session.query(Event)
        if workspace_id is not None:
            query = query.filter(Event.workspace_id == workspace_id)
        if visible is not None:
            query = query.filter((Event.workspace_id.is_(None)) | (Event.workspace_id.in_(visible)))
        return query.order_by(Event.created_at.desc(), Event.id.desc()).limit(limit).all()

from __future__ import annotations

from datetime import UTC, datetime

from brains.control.claims import list_workspace_claims
from brains.control.decisions import list_open_decisions
from brains.control.events import list_events
from brains.control.handoffs import list_handoffs
from brains.control.mailbox import read_messages
from brains.control.sessions import list_sessions, register_workspace
from brains.control.tasks import list_tasks


def get_state(
    workspace_path: str | None = None,
    session_id: str | None = None,
    limit: int = 50,
) -> dict:
    workspace = register_workspace(workspace_path) if workspace_path else None
    events = list_events(workspace_id=workspace.id if workspace else None, limit=limit)
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace": (
            {
                "id": workspace.id,
                "slug": workspace.slug,
                "path": workspace.path,
                "status": workspace.status,
                "last_touched_at": workspace.last_touched_at.isoformat()
                if workspace.last_touched_at
                else None,
                "last_summary": workspace.last_summary,
            }
            if workspace
            else None
        ),
        "active_sessions": [
            row
            for row in list_sessions(workspace_path=workspace_path, limit=limit)
            if row["ended_at"] is None
        ],
        "active_claims": list_workspace_claims(workspace_path=workspace_path),
        "open_decisions": list_open_decisions(workspace_path=workspace_path, limit=limit),
        "active_handoffs": list_handoffs(workspace_path=workspace_path, active_only=True),
        "active_tasks": list_tasks(
            workspace_path=workspace_path,
            limit=limit,
        ),
        "recent_events": [
            {
                "id": row.id,
                "kind": row.kind,
                "message": row.message,
                "session_id": row.session_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in events
        ],
    }
    if session_id:
        result["unread_messages"] = read_messages(
            session_id,
            mark_read=False,
            limit=limit,
        )
    return result

from __future__ import annotations

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, MailboxMessage, Workspace


def _message_to_dict(row: MailboxMessage, workspace_slug: str | None = None) -> dict:
    return {
        "id": row.id,
        "workspace": workspace_slug,
        "from_session_id": row.from_session_id,
        "to_session_id": row.to_session_id,
        "kind": row.kind,
        "subject": row.subject,
        "body": row.body,
        "read_at": row.read_at.isoformat() if row.read_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _workspace_id_for_session(session, session_id: str | None) -> int | None:
    if not session_id:
        return None
    row = session.query(AgentSession).filter(AgentSession.id == session_id).one_or_none()
    return row.workspace_id if row else None


def send_message(
    subject: str,
    body: str = "",
    *,
    from_session_id: str | None = None,
    to_session_id: str | None = None,
    workspace_path: str | None = None,
    kind: str = "info",
) -> dict:
    workspace_id: int | None = None
    workspace_slug: str | None = None
    if workspace_path:
        workspace = register_workspace(workspace_path)
        workspace_id = workspace.id
        workspace_slug = workspace.slug
    init_db()
    with SessionLocal() as session:
        if workspace_id is None:
            workspace_id = _workspace_id_for_session(session, from_session_id)
            if workspace_id is not None:
                workspace = session.query(Workspace).filter(Workspace.id == workspace_id).one()
                workspace_slug = workspace.slug
        row = MailboxMessage(
            workspace_id=workspace_id,
            from_session_id=from_session_id,
            to_session_id=to_session_id,
            kind=kind,
            subject=subject,
            body=body or None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        result = _message_to_dict(row, workspace_slug)
    append_event(
        "message_sent",
        subject,
        workspace_id=workspace_id,
        session_id=from_session_id,
        metadata={"to_session_id": to_session_id, "kind": kind},
    )
    return result


def read_messages(
    session_id: str,
    mark_read: bool = True,
    include_read: bool = False,
    limit: int = 50,
) -> list[dict]:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    # ``read_messages`` is session-scoped, so the workspace context is
    # the agent_session.workspace_id; the filter ensures an operator
    # can't drain another operator's private workspace mailbox by
    # passing the session id directly.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    now = utc_now()
    init_db()
    with SessionLocal() as session:
        agent_session = (
            session.query(AgentSession).filter(AgentSession.id == session_id).one_or_none()
        )
        workspace_id = agent_session.workspace_id if agent_session else None
        # If the caller can't see the session's workspace, refuse the
        # read. We return an empty list rather than raising so callers
        # that poll for mail don't crash on a workspace they used to
        # have access to (e.g. mid-session visibility change).
        if visible is not None and workspace_id is not None and workspace_id not in visible:
            return []
        query = session.query(MailboxMessage)
        if not include_read:
            query = query.filter(MailboxMessage.read_at.is_(None))
        if workspace_id is None:
            query = query.filter(MailboxMessage.to_session_id == session_id)
        else:
            query = query.filter(
                (MailboxMessage.to_session_id == session_id)
                | (
                    MailboxMessage.to_session_id.is_(None)
                    & (
                        (MailboxMessage.workspace_id == workspace_id)
                        | MailboxMessage.workspace_id.is_(None)
                    )
                )
            )
        rows = query.order_by(MailboxMessage.created_at.asc()).limit(limit).all()
        results = []
        workspace_slugs = {
            row.id: (
                session.query(Workspace).filter(Workspace.id == row.workspace_id).one().slug
                if row.workspace_id
                else None
            )
            for row in rows
        }
        for row in rows:
            results.append(_message_to_dict(row, workspace_slugs[row.id]))
            if mark_read:
                row.read_at = now
        if mark_read and rows:
            session.commit()
    # Audit / adoption signal: every read_messages call emits a
    # ``message_read`` event tied to the caller session. Always emit (not
    # just on non-empty), so the adoption query can ask "of sessions
    # where welcome offered unread mail, what fraction even checked?"
    # without the polling-vs-result-count confound.
    append_event(
        "message_read",
        f"{len(results)} message(s) read",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={
            "count": len(results),
            "marked_read": bool(mark_read),
            "include_read": bool(include_read),
        },
    )
    return results

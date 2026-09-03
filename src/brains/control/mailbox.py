from __future__ import annotations

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.sessions import register_workspace, require_live_session
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
    route_to_current: bool = False,
) -> dict:
    """Send a mailbox message.

    Attribution and recipient are validated against liveness: a dead handle
    is refused loudly (with live replacement candidates) — brains must never
    let a reaped session be impersonated or silently receive nothing (field
    report #2).

    ``route_to_current`` is the explicit sender opt-in for rerouting: when
    the addressed ``to_session_id`` is ended and its workspace has EXACTLY
    ONE live session, deliver there instead and say so in the result.
    Default off — silent rerouting changes recipient identity.
    """
    if not subject or not subject.strip():
        raise ValueError("subject is required")
    workspace_id: int | None = None
    workspace_slug: str | None = None
    if workspace_path:
        workspace = register_workspace(workspace_path)
        workspace_id = workspace.id
        workspace_slug = workspace.slug
    routed_from: str | None = None
    init_db()
    with SessionLocal() as session:
        if from_session_id:
            # Sending AS a session requires that session to be alive.
            require_live_session(session, from_session_id, action="send_message")
        if to_session_id:
            try:
                require_live_session(
                    session,
                    to_session_id,
                    action="send_message",
                    renew_lease=False,
                )
            except ValueError:
                recipient = (
                    session.query(AgentSession)
                    .filter(AgentSession.id == to_session_id)
                    .one_or_none()
                )
                candidates: list[str] = []
                if recipient is not None:
                    from brains.control.sessions import live_replacement_session_ids

                    candidates = live_replacement_session_ids(session, recipient.workspace_id)
                if route_to_current and len(candidates) == 1:
                    routed_from = to_session_id
                    to_session_id = candidates[0]
                else:
                    from brains.control.sessions import require_live_session as _rls

                    _rls(
                        session,
                        to_session_id,
                        action="send_message",
                        renew_lease=False,
                    )
        if workspace_id is None:
            workspace_id = _workspace_id_for_session(session, from_session_id)
            if workspace_id is not None:
                workspace = session.query(Workspace).filter(Workspace.id == workspace_id).one()
                workspace_slug = workspace.slug
        # A row with no workspace AND no direct recipient would be readable
        # by every session on the brain (or by no one, depending on the read
        # filter) — both wrong. Every message must be anchored somewhere.
        if workspace_id is None and not to_session_id:
            raise ValueError(
                "send_message requires to_session_id or a resolvable workspace "
                "(workspace_path or from_session_id)"
            )
        display_subject = subject.strip()
        if routed_from:
            display_subject = f"[rerouted from {routed_from}] {display_subject}"
        mail_row = MailboxMessage(
            workspace_id=workspace_id,
            from_session_id=from_session_id,
            to_session_id=to_session_id,
            kind=kind,
            subject=display_subject,
            body=body or None,
        )
        session.add(mail_row)
        session.commit()
        session.refresh(mail_row)
        result = _message_to_dict(mail_row, workspace_slug)
        if routed_from:
            result["routed_from"] = routed_from
            result["routed_to"] = to_session_id
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
    after_id: int | None = None,
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
        # Loud dead-handle contract (field report #2): the error names the
        # ended state, the recorded reason, and live replacement candidates
        # in the same workspace — "[] + exit 0" must never mean both
        # "empty inbox" and "dead handle".
        agent_session = require_live_session(session, session_id, action="read_messages")
        workspace_id = agent_session.workspace_id
        from brains.control.sessions import predecessor_session_ids

        recipient_ids = [session_id, *predecessor_session_ids(session, session_id)]
        # If the caller can't see the session's workspace, refuse the
        # read. We return an empty list rather than raising so callers
        # that poll for mail don't crash on a workspace they used to
        # have access to (e.g. mid-session visibility change).
        if visible is not None and workspace_id is not None and workspace_id not in visible:
            return []
        query = session.query(MailboxMessage)
        if after_id is not None:
            query = query.filter(MailboxMessage.id > max(0, int(after_id)))
        if not include_read:
            query = query.filter(MailboxMessage.read_at.is_(None))
        if workspace_id is None:
            query = query.filter(MailboxMessage.to_session_id.in_(recipient_ids))
        else:
            query = query.filter(
                (MailboxMessage.to_session_id.in_(recipient_ids))
                | (
                    MailboxMessage.to_session_id.is_(None)
                    & (MailboxMessage.workspace_id == workspace_id)
                )
            )
        rows = query.order_by(MailboxMessage.id.asc()).limit(limit).all()
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
        session.commit()
    # Empty polling is routine and should not dominate the operational event log.
    if results:
        append_event(
            "message_read",
            f"{len(results)} message(s) read",
            workspace_id=workspace_id,
            session_id=session_id,
            metadata={
                "count": len(results),
                "marked_read": bool(mark_read),
                "include_read": bool(include_read),
                "after_id": after_id,
                "cursor": max(row["id"] for row in results),
            },
        )
    return results


def inbox_wait(
    session_id: str,
    timeout_ms: int = 25000,
    after_message_id: int | None = None,
) -> dict:
    """Block until claimable peer help arrives, or timeout.

    ``after_message_id`` is retained only so historical callers fail safely;
    legacy running-session mail and topic rows never wake this core primitive.
    """
    import time as _time

    from brains.control.help import (
        _claimable_request_query,
        _expire_due,
    )
    from brains.control.help import (
        _poll_interval_seconds as _help_poll,
    )

    if not session_id:
        raise ValueError("inbox_wait requires session_id")
    timeout_ms = max(100, int(timeout_ms))

    init_db()
    resolved_slug: str | None = None
    my_tool: str | None = None
    from brains.control.sessions import require_live_session

    with SessionLocal() as session:
        row = require_live_session(session, session_id, action="inbox_wait")
        workspace_id = row.workspace_id
        my_tool = row.tool
        if workspace_id is not None:
            ws = session.query(Workspace).filter(Workspace.id == workspace_id).one_or_none()
            resolved_slug = ws.slug if ws else None
        session.commit()
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()

    del after_message_id

    def _claimable_request() -> dict | None:
        with SessionLocal() as session:
            _expire_due(session)
            match = _claimable_request_query(
                session,
                session_id=session_id,
                workspace_slug=resolved_slug,
                tool=my_tool,
                visible_workspace_ids=visible,
            ).first()
            session.commit()
            if match is None:
                return None
            row, _required_tool = match
            return {"code": row.code, "subject": row.subject}

    deadline = _time.monotonic() + (timeout_ms / 1000.0)
    while True:
        request = _claimable_request()
        if request is not None:
            append_event(
                "inbox_wait",
                f"wake: peer_request {request['code']}",
                session_id=session_id,
                metadata={"wakeup": "peer_request", "code": request["code"]},
            )
            return {"wakeup": "peer_request", "request": request}
        if _time.monotonic() >= deadline:
            return {"wakeup": None, "timeout": True}
        _time.sleep(_help_poll())

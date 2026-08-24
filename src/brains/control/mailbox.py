from __future__ import annotations

from sqlalchemy import or_

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
            recipient = (
                session.query(AgentSession).filter(AgentSession.id == to_session_id).one_or_none()
            )
            ended = (
                recipient is None
                or recipient.ended_at is not None
                or recipient.state
                in (
                    "completed",
                    "failed",
                )
            )
            if ended:
                candidates: list[str] = []
                if recipient is not None:
                    from brains.control.sessions import live_replacement_session_ids

                    candidates = live_replacement_session_ids(session, recipient.workspace_id)
                if route_to_current and len(candidates) == 1:
                    routed_from = to_session_id
                    to_session_id = candidates[0]
                else:
                    from brains.control.sessions import require_live_session as _rls

                    _rls(session, to_session_id, action="send_message")
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
                    & (MailboxMessage.workspace_id == workspace_id)
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


def inbox_wait(
    session_id: str,
    timeout_ms: int = 25000,
) -> dict:
    """Block until this session has something worth waking for, or timeout.

    The one poll primitive (comms slice 2): collapses the two loops an agent
    otherwise runs — periodic ``read_messages`` and ``wait_for_request`` —
    into a single long-poll that returns when EITHER arrives:

    * ``{"wakeup": "mail", ...}``          — unread inbox traffic exists;
    * ``{"wakeup": "peer_request", ...}``  — a claimable peer-help request
      matches this session/workspace/harness;
    * ``{"wakeup": None}``                 — quiet for the whole timeout.

    Cadence stays the agent's policy; this only makes each tick cheap and
    latency-bounded instead of a sleep-poll guess.
    """
    import time as _time

    from brains.control.help import _poll_interval_seconds as _help_poll
    from brains.control.help import tool_matches_requirement

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
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()

    def _unread_exists() -> bool:
        with SessionLocal() as session:
            q = session.query(MailboxMessage.id).filter(MailboxMessage.read_at.is_(None))
            if workspace_id is None:
                q = q.filter(MailboxMessage.to_session_id == session_id)
            else:
                q = q.filter(
                    (MailboxMessage.to_session_id == session_id)
                    | (
                        MailboxMessage.to_session_id.is_(None)
                        & (MailboxMessage.workspace_id == workspace_id)
                    )
                )
            return session.query(q.exists()).scalar()

    def _claimable_request() -> dict | None:
        with SessionLocal() as session:
            from brains.storage.models import HelpRequest, HelpRequestConstraint

            filters = [HelpRequest.to_session_id == session_id]
            if resolved_slug:
                filters.append(HelpRequest.to_workspace == resolved_slug)
            q = (
                session.query(HelpRequest, HelpRequestConstraint.required_tool)
                .outerjoin(
                    HelpRequestConstraint,
                    HelpRequestConstraint.request_code == HelpRequest.code,
                )
                .filter(HelpRequest.status == "open", or_(*filters))
                .order_by(HelpRequest.created_at.asc())
                .limit(10)
            )
            if visible is not None:
                q = q.filter(
                    (HelpRequest.from_workspace_id.is_(None))
                    | (HelpRequest.from_workspace_id.in_(visible))
                )
            for row, required_tool in q.all():
                if tool_matches_requirement(required_tool, my_tool):
                    return {"code": row.code, "subject": row.subject}
            return None

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
        if _unread_exists():
            append_event(
                "inbox_wait",
                "wake: mail",
                session_id=session_id,
                metadata={"wakeup": "mail"},
            )
            return {"wakeup": "mail"}
        if _time.monotonic() >= deadline:
            return {"wakeup": None, "timeout": True}
        _time.sleep(_help_poll())

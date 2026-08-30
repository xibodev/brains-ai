"""Authorized, address-based durable mailbox delivery and history."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from brains.authz import policy
from brains.authz.principal import CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.authz.resolver import resolve_local_principal
from brains.control.common import utc_now
from brains.control.durable_mailbox import (
    MailboxUnavailableError,
    MailboxValidationError,
    _authorized_workspace,
    _ensure_operator_mailbox_row,
    _operator_mailbox_visible,
    ensure_operator_mailboxes,
    require_current_agent_mailbox_in_transaction,
)
from brains.control.events import append_event
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    Mailbox,
    MailboxAttachment,
    MailDelivery,
    MailMessage,
    MailNotificationAttempt,
    MailThread,
    Operator,
    OrgMember,
    Workspace,
    WorkspaceMembership,
)

MAX_DIRECT_RECIPIENTS = 100
MAX_BROADCAST_RECIPIENTS = 500
MAX_BODY_CHARS = 65_536
MAILBOX_NUDGE = "Brains mailbox: new mail is waiting. Pull your durable inbox."
NOTIFY_MODES = frozenset({"turn_boundary", "immediate"})
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")


def _principal_or_local(principal: Principal | None) -> Principal:
    resolved = principal or resolve_local_principal()
    if not resolved.is_operator or resolved.operator_id is None:
        raise MailboxUnavailableError("mailbox unavailable")
    return resolved


def _normalize_content(
    subject: str | None,
    body: str,
    kind: str,
    operation_id: str,
) -> tuple[str | None, str | None, str, str]:
    normalized_subject = subject.strip() if subject is not None else None
    if normalized_subject is not None and not 1 <= len(normalized_subject) <= 256:
        raise MailboxValidationError("mail subject must contain 1-256 characters")
    if len(body or "") > MAX_BODY_CHARS:
        raise MailboxValidationError(f"mail body is limited to {MAX_BODY_CHARS} characters")
    normalized_kind = (kind or "").strip().lower()
    if not _KIND_RE.fullmatch(normalized_kind):
        raise MailboxValidationError("mail kind is invalid")
    normalized_operation = (operation_id or "").strip()
    if not _OPERATION_ID_RE.fullmatch(normalized_operation):
        raise MailboxValidationError(
            "operation_id must contain 1-96 letters, digits, dot, underscore, colon, or dash"
        )
    return normalized_subject, body or None, normalized_kind, normalized_operation


def _normalize_addresses(addresses: Iterable[str], *, maximum: int) -> list[str]:
    values = list(dict.fromkeys((address or "").strip() for address in addresses))
    if not values or any(not value or len(value) > 512 for value in values):
        raise MailboxValidationError("at least one valid recipient address is required")
    if len(values) > maximum:
        raise MailboxValidationError(f"mail is limited to {maximum} recipients")
    return values


def _public_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _notification_dict(row: MailNotificationAttempt) -> dict[str, Any]:
    return {
        "notification_id": row.notification_id,
        "adapter": row.adapter,
        "status": row.status,
        "attempt": row.attempt,
        "error_code": row.error_code,
        "nudge": MAILBOX_NUDGE if row.status == "claimed" else None,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at) if row.started_at else None,
        "completed_at": _iso(row.completed_at) if row.completed_at else None,
    }


def _ensure_notification_in_transaction(
    session,
    delivery: MailDelivery,
    recipient: Mailbox,
) -> MailNotificationAttempt | None:
    if recipient.kind != "agent":
        return None
    attachment = (
        session.query(MailboxAttachment)
        .filter(
            MailboxAttachment.mailbox_id == recipient.id,
            MailboxAttachment.active_slot == 1,
        )
        .one_or_none()
    )
    if attachment is None or attachment.notification_mode not in NOTIFY_MODES:
        return None
    expected_modes = {
        "claude-code": "immediate",
        "codex": "turn_boundary",
        "opencode": "immediate",
    }
    if expected_modes.get(recipient.tool or "") != attachment.notification_mode:
        return None
    key = f"mail-notify:{delivery.id}:{attachment.id}:{attachment.notification_mode}"
    existing = (
        session.query(MailNotificationAttempt)
        .filter(MailNotificationAttempt.idempotency_key == key)
        .one_or_none()
    )
    if existing is not None:
        return existing
    row = MailNotificationAttempt(
        notification_id=_public_id("note"),
        idempotency_key=key,
        delivery_id=delivery.id,
        attachment_id=attachment.id,
        adapter=recipient.tool or "unknown",
        status="queued",
        attempt=0,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        session.expire_all()
        existing = (
            session.query(MailNotificationAttempt)
            .filter(MailNotificationAttempt.idempotency_key == key)
            .one_or_none()
        )
        if existing is None:
            raise
        return existing
    return row


def _queue_unread_notifications_in_transaction(
    session,
    mailbox: Mailbox,
) -> int:
    deliveries = (
        session.query(MailDelivery)
        .filter(
            MailDelivery.recipient_mailbox_id == mailbox.id,
            MailDelivery.read_at.is_(None),
        )
        .order_by(MailDelivery.id.asc())
        .all()
    )
    return sum(
        _ensure_notification_in_transaction(session, delivery, mailbox) is not None
        for delivery in deliveries
    )


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _operation_key(sender_mailbox_id: int, action: str, operation_id: str) -> str:
    return f"mail:{sender_mailbox_id}:{action}:{operation_id}"


def _mailbox_query(session, address: str):
    query = session.query(Mailbox).filter(Mailbox.address == address)
    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return query


def _lock_mailbox_for_read(session, mailbox: Mailbox) -> None:
    """Serialize first-read attribution without locking non-mutating history reads."""
    if session.get_bind().dialect.name == "postgresql":
        session.query(Mailbox.id).filter(Mailbox.id == mailbox.id).with_for_update().one()
        return
    session.execute(update(Mailbox).where(Mailbox.id == mailbox.id).values(status=Mailbox.status))


def _workspace_readable(principal: Principal, workspace: Workspace | None) -> bool:
    return bool(
        workspace is not None
        and policy.can_see_workspace(principal, workspace.id)
        and principal.has_capability(CAP_ORG_READ, workspace.org_id)
    )


def _workspace_writable(principal: Principal, workspace: Workspace | None) -> bool:
    return bool(
        workspace is not None
        and workspace.status == "active"
        and policy.can_see_workspace(principal, workspace.id)
        and principal.has_capability(CAP_ORG_WRITE, workspace.org_id)
    )


def _operator_can_see_workspace(
    session,
    operator_id: int,
    workspace: Workspace | None,
) -> bool:
    operator = session.get(Operator, operator_id)
    if operator is None or workspace is None:
        return False
    if operator.slug == "admin":
        return True
    if workspace.org_id is None:
        return False
    if (
        session.query(OrgMember.id)
        .filter(OrgMember.operator_id == operator_id, OrgMember.org_id == workspace.org_id)
        .first()
        is None
    ):
        return False
    if workspace.visibility != "private":
        return True
    return (
        session.query(WorkspaceMembership.id)
        .filter(
            WorkspaceMembership.operator_id == operator_id,
            WorkspaceMembership.workspace_id == workspace.id,
        )
        .first()
        is not None
    )


def _require_owner_visibility(session, mailboxes: list[Mailbox], workspace_ids: set[int]) -> None:
    workspaces = {
        workspace.id: workspace
        for workspace in session.query(Workspace).filter(Workspace.id.in_(workspace_ids)).all()
    }
    if set(workspaces) != workspace_ids:
        raise MailboxUnavailableError("mailbox unavailable")
    for mailbox in mailboxes:
        if any(
            not _operator_can_see_workspace(session, mailbox.owner_operator_id, workspace)
            for workspace in workspaces.values()
        ):
            raise MailboxUnavailableError("mailbox unavailable")


def _resolve_sender(
    session,
    origin: Workspace,
    principal: Principal,
    *,
    sender_address: str | None,
    sender_session_id: str | None,
    binding_secret: str | None,
) -> tuple[Mailbox, MailboxAttachment | None, AgentSession | None]:
    if sender_session_id is not None or binding_secret is not None:
        if not sender_session_id or not binding_secret:
            raise MailboxUnavailableError("mailbox unavailable")
        mailbox, attachment, agent, workspace = require_current_agent_mailbox_in_transaction(
            session,
            sender_session_id,
            binding_secret,
            address=sender_address,
            capability=CAP_ORG_WRITE,
            principal=principal,
        )
        if workspace.id != origin.id:
            raise MailboxUnavailableError("mailbox unavailable")
        return mailbox, attachment, agent

    if not principal.is_human_channel:
        raise MailboxUnavailableError("mailbox unavailable")
    operator = session.get(Operator, principal.operator_id)
    if operator is None:
        raise MailboxUnavailableError("mailbox unavailable")
    own_mailbox, _created = _ensure_operator_mailbox_row(session, operator.id, operator.slug)
    requested = (sender_address or own_mailbox.address).strip()
    mailbox = _mailbox_query(session, requested).one_or_none()
    if (
        mailbox is None
        or mailbox.id != own_mailbox.id
        or mailbox.kind != "operator"
        or mailbox.status != "active"
    ):
        raise MailboxUnavailableError("mailbox unavailable")
    return mailbox, None, None


def _resolve_open_mailbox(
    session,
    principal: Principal,
    *,
    address: str | None,
    session_id: str | None,
    binding_secret: str | None,
    require_agent_proof: bool,
) -> tuple[Mailbox, MailboxAttachment | None, AgentSession | None]:
    if session_id is not None or binding_secret is not None or require_agent_proof:
        if not session_id or not binding_secret:
            raise MailboxUnavailableError("mailbox unavailable")
        mailbox, attachment, agent, _workspace = require_current_agent_mailbox_in_transaction(
            session,
            session_id,
            binding_secret,
            address=address,
            capability=CAP_ORG_READ,
            principal=principal,
        )
        return mailbox, attachment, agent

    if not principal.is_human_channel:
        raise MailboxUnavailableError("mailbox unavailable")
    operator = session.get(Operator, principal.operator_id)
    if operator is None:
        raise MailboxUnavailableError("mailbox unavailable")
    own_operator_mailbox, _created = _ensure_operator_mailbox_row(
        session, operator.id, operator.slug
    )
    requested = (address or own_operator_mailbox.address).strip()
    mailbox = _mailbox_query(session, requested).one_or_none()
    if mailbox is None or mailbox.status != "active":
        raise MailboxUnavailableError("mailbox unavailable")
    if mailbox.kind == "operator":
        if mailbox.owner_operator_id != principal.operator_id:
            raise MailboxUnavailableError("mailbox unavailable")
    elif mailbox.kind == "agent":
        workspace = session.get(Workspace, mailbox.workspace_id) if mailbox.workspace_id else None
        elevated = principal.is_bootstrap_admin or (
            workspace is not None and principal.role_in_org(workspace.org_id) in {"admin", "owner"}
        )
        if not _workspace_readable(principal, workspace) or (
            mailbox.owner_operator_id != principal.operator_id and not elevated
        ):
            raise MailboxUnavailableError("mailbox unavailable")
    else:
        raise MailboxUnavailableError("mailbox unavailable")
    return mailbox, None, None


def _authorized_recipient(
    session,
    address: str,
    principal: Principal,
    origin: Workspace,
) -> Mailbox:
    mailbox = _mailbox_query(session, address).one_or_none()
    if mailbox is None or mailbox.status != "active":
        raise MailboxUnavailableError("mailbox unavailable")
    if mailbox.kind == "agent":
        workspace = session.get(Workspace, mailbox.workspace_id) if mailbox.workspace_id else None
        if not _workspace_writable(principal, workspace):
            raise MailboxUnavailableError("mailbox unavailable")
    elif mailbox.kind == "operator":
        if not _operator_mailbox_visible(
            session,
            principal,
            mailbox.owner_operator_id,
            workspace=origin,
        ):
            raise MailboxUnavailableError("mailbox unavailable")
    else:
        raise MailboxUnavailableError("mailbox unavailable")
    return mailbox


def _message_rows_for_thread(session, thread_id: int) -> list[MailMessage]:
    return (
        session.query(MailMessage)
        .filter(MailMessage.thread_id == thread_id)
        .order_by(MailMessage.id.asc())
        .all()
    )


def _message_mailboxes(session, message: MailMessage) -> list[Mailbox]:
    ids = {message.sender_mailbox_id}
    ids.update(
        row[0]
        for row in session.query(MailDelivery.recipient_mailbox_id)
        .filter(MailDelivery.message_id == message.id)
        .all()
    )
    mailboxes = session.query(Mailbox).filter(Mailbox.id.in_(ids)).all()
    if {mailbox.id for mailbox in mailboxes} != ids:
        raise MailboxUnavailableError("mailbox unavailable")
    return mailboxes


def _represented_mailboxes(
    session,
    message: MailMessage,
    *,
    seen: set[int] | None = None,
) -> list[Mailbox]:
    visited = seen if seen is not None else set()
    if message.id in visited or len(visited) >= 32:
        raise MailboxUnavailableError("mailbox unavailable")
    visited.add(message.id)
    mailboxes = _message_mailboxes(session, message)
    if message.forwarded_from_id is not None:
        source = session.get(MailMessage, message.forwarded_from_id)
        if source is None:
            raise MailboxUnavailableError("mailbox unavailable")
        mailboxes.extend(_represented_mailboxes(session, source, seen=visited))
    return list({mailbox.id: mailbox for mailbox in mailboxes}.values())


def _represented_workspace_ids(
    session,
    message: MailMessage,
    *,
    seen: set[int] | None = None,
) -> set[int]:
    visited = seen if seen is not None else set()
    if message.id in visited or len(visited) >= 32:
        raise MailboxUnavailableError("mailbox unavailable")
    visited.add(message.id)
    workspace_ids = {message.origin_workspace_id}
    workspace_ids.update(
        mailbox.workspace_id
        for mailbox in _message_mailboxes(session, message)
        if mailbox.workspace_id is not None
    )
    if message.forwarded_from_id is not None:
        source = session.get(MailMessage, message.forwarded_from_id)
        if source is None:
            raise MailboxUnavailableError("mailbox unavailable")
        workspace_ids.update(_represented_workspace_ids(session, source, seen=visited))
    return workspace_ids


def _authorize_message_scope(
    session,
    message: MailMessage,
    principal: Principal,
    *,
    seen: set[int] | None = None,
) -> set[int]:
    visited = seen if seen is not None else set()
    if message.id in visited or len(visited) >= 32:
        raise MailboxUnavailableError("mailbox unavailable")
    visited.add(message.id)
    workspace_ids = {message.origin_workspace_id}
    mailboxes = _represented_mailboxes(session, message)
    workspace_ids.update(
        mailbox.workspace_id for mailbox in mailboxes if mailbox.workspace_id is not None
    )
    for workspace_id in workspace_ids:
        if not _workspace_readable(principal, session.get(Workspace, workspace_id)):
            raise MailboxUnavailableError("mailbox unavailable")
    if message.forwarded_from_id is not None:
        source = session.get(MailMessage, message.forwarded_from_id)
        if source is None:
            raise MailboxUnavailableError("mailbox unavailable")
        workspace_ids.update(_authorize_message_scope(session, source, principal, seen=visited))
    for mailbox in mailboxes:
        if mailbox.kind == "operator" and any(
            not _operator_can_see_workspace(
                session,
                mailbox.owner_operator_id,
                session.get(Workspace, workspace_id),
            )
            for workspace_id in workspace_ids
        ):
            raise MailboxUnavailableError("mailbox unavailable")
    return workspace_ids


def _mailbox_can_read_message(session, mailbox_id: int, message: MailMessage) -> bool:
    if message.sender_mailbox_id == mailbox_id:
        return True
    return (
        session.query(MailDelivery.id)
        .filter(
            MailDelivery.message_id == message.id,
            MailDelivery.recipient_mailbox_id == mailbox_id,
        )
        .first()
        is not None
    )


def _authorize_thread(
    session,
    thread: MailThread,
    mailbox: Mailbox,
    principal: Principal,
) -> list[MailMessage]:
    messages = [
        message
        for message in _message_rows_for_thread(session, thread.id)
        if _mailbox_can_read_message(session, mailbox.id, message)
    ]
    if not messages:
        raise MailboxUnavailableError("mailbox unavailable")
    for message in messages:
        _authorize_message_scope(session, message, principal)
    return messages


def _workspace_slug(session, workspace_id: int | None) -> str | None:
    workspace = session.get(Workspace, workspace_id) if workspace_id is not None else None
    return workspace.slug if workspace is not None else None


def _delivery_dict(session, delivery: MailDelivery) -> dict[str, Any]:
    recipient = session.get(Mailbox, delivery.recipient_mailbox_id)
    reader = (
        session.get(Operator, delivery.read_by_operator_id)
        if delivery.read_by_operator_id
        else None
    )
    return {
        "cursor": delivery.id,
        "delivery_id": delivery.delivery_id,
        "recipient": recipient.address if recipient is not None else None,
        "recipient_workspace": _workspace_slug(session, delivery.recipient_workspace_id),
        "state": "read" if delivery.read_at is not None else "accepted",
        "accepted_at": _iso(delivery.accepted_at),
        "read_at": _iso(delivery.read_at) if delivery.read_at else None,
        "read_by_session_id": delivery.read_by_session_id,
        "read_by_operator": reader.slug if reader is not None else None,
        "read_channel": delivery.read_channel,
    }


def _message_dict(
    session,
    message: MailMessage,
    *,
    inbox_delivery_id: int | None = None,
    viewer_mailbox_id: int | None = None,
) -> dict[str, Any]:
    thread = session.get(MailThread, message.thread_id)
    sender = session.get(Mailbox, message.sender_mailbox_id)
    in_reply_to = (
        session.get(MailMessage, message.in_reply_to_id) if message.in_reply_to_id else None
    )
    forwarded_from = (
        session.get(MailMessage, message.forwarded_from_id) if message.forwarded_from_id else None
    )
    forwarded_sender = (
        session.get(Mailbox, forwarded_from.sender_mailbox_id)
        if forwarded_from is not None
        else None
    )
    deliveries = (
        session.query(MailDelivery)
        .filter(MailDelivery.message_id == message.id)
        .order_by(MailDelivery.id.asc())
        .all()
    )
    if viewer_mailbox_id is not None and message.sender_mailbox_id != viewer_mailbox_id:
        deliveries = [
            delivery
            for delivery in deliveries
            if delivery.recipient_mailbox_id == viewer_mailbox_id
        ]
    result = {
        "cursor": message.id,
        "message_id": message.message_id,
        "thread_id": thread.thread_id if thread is not None else None,
        "sender": sender.address if sender is not None else None,
        "sender_session_id": message.sender_session_id,
        "origin_workspace": _workspace_slug(session, message.origin_workspace_id),
        "audience": message.audience,
        "in_reply_to": in_reply_to.message_id if in_reply_to is not None else None,
        "forwarded_from": forwarded_from.message_id if forwarded_from is not None else None,
        "forwarded_message": (
            {
                "message_id": forwarded_from.message_id,
                "forwarded_from": (
                    session.get(MailMessage, forwarded_from.forwarded_from_id).message_id
                    if forwarded_from.forwarded_from_id is not None
                    and session.get(MailMessage, forwarded_from.forwarded_from_id) is not None
                    else None
                ),
                "sender": forwarded_sender.address if forwarded_sender is not None else None,
                "origin_workspace": _workspace_slug(session, forwarded_from.origin_workspace_id),
                "kind": forwarded_from.kind,
                "subject": forwarded_from.subject,
                "body": forwarded_from.body or "",
                "created_at": _iso(forwarded_from.created_at),
            }
            if forwarded_from is not None
            else None
        ),
        "kind": message.kind,
        "subject": message.subject,
        "body": message.body or "",
        "created_at": _iso(message.created_at),
        "deliveries": [_delivery_dict(session, delivery) for delivery in deliveries],
    }
    if inbox_delivery_id is not None:
        selected = next(
            (delivery for delivery in deliveries if delivery.id == inbox_delivery_id),
            None,
        )
        result["inbox_delivery"] = (
            _delivery_dict(session, selected) if selected is not None else None
        )
    return result


def _thread_dict(
    session,
    thread: MailThread,
    messages: list[MailMessage],
    *,
    viewer_mailbox_id: int,
) -> dict[str, Any]:
    starter = session.get(Mailbox, thread.started_by_mailbox_id)
    return {
        "thread_id": thread.thread_id,
        "origin_workspace": _workspace_slug(session, thread.origin_workspace_id),
        "started_by": starter.address if starter is not None else None,
        "subject": thread.subject,
        "created_at": _iso(thread.created_at),
        "updated_at": _iso(messages[-1].created_at),
        "messages": [
            _message_dict(session, message, viewer_mailbox_id=viewer_mailbox_id)
            for message in messages
        ],
    }


def _verify_replay(
    session,
    message: MailMessage,
    *,
    sender: Mailbox,
    origin: Workspace,
    audience: str,
    subject: str,
    body: str | None,
    kind: str,
    in_reply_to_id: int | None,
    forwarded_from_id: int | None,
    recipient_addresses: set[str] | None,
) -> None:
    actual_recipient_addresses = {
        row[0]
        for row in session.query(Mailbox.address)
        .join(MailDelivery, MailDelivery.recipient_mailbox_id == Mailbox.id)
        .filter(MailDelivery.message_id == message.id)
        .all()
    }
    if (
        message.sender_mailbox_id != sender.id
        or message.origin_workspace_id != origin.id
        or message.audience != audience
        or message.subject != subject
        or message.body != body
        or message.kind != kind
        or message.in_reply_to_id != in_reply_to_id
        or message.forwarded_from_id != forwarded_from_id
        or (recipient_addresses is not None and actual_recipient_addresses != recipient_addresses)
    ):
        raise MailboxValidationError("operation_id already identifies a different mailbox message")


def _send(
    workspace_path: str,
    *,
    action: str,
    audience: str,
    recipients: list[str] | None,
    subject: str | None,
    body: str,
    kind: str,
    operation_id: str,
    sender_address: str | None,
    sender_session_id: str | None,
    binding_secret: str | None,
    source_message_id: str | None,
    principal: Principal | None,
) -> dict[str, Any]:
    normalized_subject, normalized_body, normalized_kind, normalized_operation = _normalize_content(
        subject, body, kind, operation_id
    )
    normalized_recipients = (
        _normalize_addresses(recipients, maximum=MAX_DIRECT_RECIPIENTS)
        if recipients is not None
        else None
    )
    resolved = _principal_or_local(principal)
    ensure_operator_mailboxes()
    init_db()
    result: dict[str, Any] | None = None
    created = False
    event_workspace_id: int | None = None
    event_session_id: str | None = None

    for _attempt in range(8):
        with _db_module.SessionLocal() as session:
            try:
                origin = _authorized_workspace(
                    session,
                    resolved,
                    workspace_path=workspace_path,
                    capability=CAP_ORG_WRITE,
                )
                sender, _attachment, sender_agent = _resolve_sender(
                    session,
                    origin,
                    resolved,
                    sender_address=sender_address,
                    sender_session_id=sender_session_id,
                    binding_secret=binding_secret,
                )
                source = None
                source_thread = None
                if source_message_id is not None:
                    source = (
                        session.query(MailMessage)
                        .filter(MailMessage.message_id == source_message_id.strip())
                        .one_or_none()
                    )
                    if source is None:
                        raise MailboxUnavailableError("mailbox unavailable")
                    source_thread = session.get(MailThread, source.thread_id)
                    if source_thread is None:
                        raise MailboxUnavailableError("mailbox unavailable")
                    if not _mailbox_can_read_message(session, sender.id, source):
                        raise MailboxUnavailableError("mailbox unavailable")
                    _authorize_message_scope(session, source, resolved)

                if action == "reply":
                    assert source is not None
                    if normalized_subject is None:
                        normalized_subject = (
                            source.subject
                            if source.subject.lower().startswith("re:")
                            else f"Re: {source.subject}"
                        )[:256]
                elif action == "forward" and normalized_subject is None:
                    assert source is not None
                    normalized_subject = (
                        source.subject
                        if source.subject.lower().startswith("fwd:")
                        else f"Fwd: {source.subject}"
                    )[:256]
                assert normalized_subject is not None

                key = _operation_key(sender.id, action, normalized_operation)
                existing = (
                    session.query(MailMessage)
                    .filter(MailMessage.operation_key == key)
                    .one_or_none()
                )
                in_reply_to_id = source.id if action == "reply" and source is not None else None
                forwarded_from_id = (
                    source.id if action == "forward" and source is not None else None
                )
                if existing is not None:
                    _authorize_message_scope(session, existing, resolved)
                    _verify_replay(
                        session,
                        existing,
                        sender=sender,
                        origin=origin,
                        audience=audience,
                        subject=normalized_subject,
                        body=normalized_body,
                        kind=normalized_kind,
                        in_reply_to_id=in_reply_to_id,
                        forwarded_from_id=forwarded_from_id,
                        recipient_addresses=(
                            set(normalized_recipients)
                            if action in {"send", "forward"} and normalized_recipients is not None
                            else None
                        ),
                    )
                    result = _message_dict(session, existing)
                    result["created"] = False
                    return result

                if action == "reply":
                    assert source is not None
                    reply_ids = [source.sender_mailbox_id]
                    if source.sender_mailbox_id == sender.id:
                        reply_ids = [
                            row[0]
                            for row in session.query(MailDelivery.recipient_mailbox_id)
                            .filter(
                                MailDelivery.message_id == source.id,
                                MailDelivery.recipient_mailbox_id != sender.id,
                            )
                            .all()
                        ]
                    possible_recipients = [
                        session.get(Mailbox, mailbox_id) for mailbox_id in reply_ids
                    ]
                    if any(mailbox is None for mailbox in possible_recipients):
                        raise MailboxUnavailableError("mailbox unavailable")
                    recipient_mailboxes: list[Mailbox] = [
                        _authorized_recipient(session, mailbox.address, resolved, origin)
                        for mailbox in possible_recipients
                        if mailbox is not None
                    ]
                elif action == "broadcast":
                    recipient_mailboxes = (
                        session.query(Mailbox)
                        .filter(
                            Mailbox.kind == "agent",
                            Mailbox.workspace_id == origin.id,
                            Mailbox.status == "active",
                            Mailbox.id != sender.id,
                        )
                        .order_by(Mailbox.id.asc())
                        .all()
                    )
                    if (
                        not recipient_mailboxes
                        or len(recipient_mailboxes) > MAX_BROADCAST_RECIPIENTS
                    ):
                        raise MailboxUnavailableError("mailbox unavailable")
                else:
                    assert normalized_recipients is not None
                    recipient_mailboxes = [
                        _authorized_recipient(session, address, resolved, origin)
                        for address in normalized_recipients
                    ]

                recipient_mailboxes = list(
                    {mailbox.id: mailbox for mailbox in recipient_mailboxes}.values()
                )
                if not recipient_mailboxes:
                    raise MailboxUnavailableError("mailbox unavailable")
                assert normalized_subject is not None

                represented_workspace_ids = {origin.id}
                represented_workspace_ids.update(
                    mailbox.workspace_id
                    for mailbox in recipient_mailboxes
                    if mailbox.workspace_id is not None
                )
                if source is not None:
                    represented_workspace_ids.update(_represented_workspace_ids(session, source))
                _require_owner_visibility(
                    session,
                    [
                        sender,
                        *recipient_mailboxes,
                        *(_represented_mailboxes(session, source) if source is not None else []),
                    ],
                    represented_workspace_ids,
                )

                now = utc_now()
                if action == "reply":
                    assert source_thread is not None
                    thread = source_thread
                    thread.updated_at = now
                else:
                    thread = MailThread(
                        thread_id=_public_id("thr"),
                        origin_workspace_id=origin.id,
                        started_by_mailbox_id=sender.id,
                        subject=normalized_subject,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(thread)
                    session.flush()
                message = MailMessage(
                    message_id=_public_id("msg"),
                    operation_key=key,
                    thread_id=thread.id,
                    sender_mailbox_id=sender.id,
                    sender_session_id=sender_agent.id if sender_agent is not None else None,
                    origin_workspace_id=origin.id,
                    audience=audience,
                    in_reply_to_id=in_reply_to_id,
                    forwarded_from_id=forwarded_from_id,
                    kind=normalized_kind,
                    subject=normalized_subject,
                    body=normalized_body,
                    created_at=now,
                )
                session.add(message)
                session.flush()
                for recipient in recipient_mailboxes:
                    delivery = MailDelivery(
                        delivery_id=_public_id("del"),
                        message_id=message.id,
                        recipient_mailbox_id=recipient.id,
                        recipient_workspace_id=recipient.workspace_id,
                        accepted_at=now,
                    )
                    session.add(delivery)
                    session.flush()
                    _ensure_notification_in_transaction(session, delivery, recipient)
                session.flush()
                session.commit()
                result = _message_dict(session, message)
                result["created"] = True
                created = True
                event_workspace_id = origin.id
                event_session_id = sender_agent.id if sender_agent is not None else None
                break
            except IntegrityError:
                session.rollback()
                continue
    if result is None:
        raise MailboxUnavailableError("mailbox unavailable")
    if created:
        append_event(
            "mailbox_message_sent",
            "durable mailbox message accepted",
            workspace_id=event_workspace_id,
            session_id=event_session_id,
            metadata={
                "message_id": result["message_id"],
                "audience": audience,
                "recipient_count": len(result["deliveries"]),
            },
        )
    return result


def send_mailbox_message(
    workspace_path: str,
    recipients: list[str],
    subject: str,
    operation_id: str,
    *,
    body: str = "",
    kind: str = "info",
    sender_address: str | None = None,
    sender_session_id: str | None = None,
    binding_secret: str | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Commit one idempotent direct message to explicit durable addresses."""
    return _send(
        workspace_path,
        action="send",
        audience="direct",
        recipients=recipients,
        subject=subject,
        body=body,
        kind=kind,
        operation_id=operation_id,
        sender_address=sender_address,
        sender_session_id=sender_session_id,
        binding_secret=binding_secret,
        source_message_id=None,
        principal=principal,
    )


def broadcast_mailbox_message(
    workspace_path: str,
    subject: str,
    operation_id: str,
    *,
    body: str = "",
    kind: str = "info",
    sender_address: str | None = None,
    sender_session_id: str | None = None,
    binding_secret: str | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Explicitly deliver to every other active agent address in one Workspace."""
    return _send(
        workspace_path,
        action="broadcast",
        audience="broadcast",
        recipients=None,
        subject=subject,
        body=body,
        kind=kind,
        operation_id=operation_id,
        sender_address=sender_address,
        sender_session_id=sender_session_id,
        binding_secret=binding_secret,
        source_message_id=None,
        principal=principal,
    )


def reply_mailbox_message(
    workspace_path: str,
    in_reply_to: str,
    operation_id: str,
    *,
    subject: str | None = None,
    body: str = "",
    kind: str = "info",
    sender_address: str | None = None,
    sender_session_id: str | None = None,
    binding_secret: str | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Reply in the source thread to the referenced message's sender."""
    return _send(
        workspace_path,
        action="reply",
        audience="direct",
        recipients=None,
        subject=subject,
        body=body,
        kind=kind,
        operation_id=operation_id,
        sender_address=sender_address,
        sender_session_id=sender_session_id,
        binding_secret=binding_secret,
        source_message_id=in_reply_to,
        principal=principal,
    )


def forward_mailbox_message(
    workspace_path: str,
    forwarded_from: str,
    recipients: list[str],
    operation_id: str,
    *,
    subject: str | None = None,
    body: str = "",
    kind: str = "info",
    sender_address: str | None = None,
    sender_session_id: str | None = None,
    binding_secret: str | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Start a new thread retaining authorized forward provenance."""
    return _send(
        workspace_path,
        action="forward",
        audience="direct",
        recipients=recipients,
        subject=subject,
        body=body,
        kind=kind,
        operation_id=operation_id,
        sender_address=sender_address,
        sender_session_id=sender_session_id,
        binding_secret=binding_secret,
        source_message_id=forwarded_from,
        principal=principal,
    )


def _read_channel(agent: AgentSession | None, principal: Principal) -> str:
    if agent is not None:
        return "agent"
    return "browser" if principal.channel == "browser" else "local"


def _mark_deliveries_read(
    deliveries: list[MailDelivery],
    *,
    agent: AgentSession | None,
    principal: Principal,
) -> int:
    now = utc_now()
    marked = 0
    for delivery in deliveries:
        if delivery.read_at is not None:
            continue
        delivery.read_at = now
        delivery.read_channel = _read_channel(agent, principal)
        if agent is not None:
            delivery.read_by_session_id = agent.id
        else:
            delivery.read_by_operator_id = principal.operator_id
        marked += 1
    return marked


def _settle_read_notifications(
    session,
    deliveries: list[MailDelivery],
) -> None:
    """Stop stale nudges after the authoritative inbox state was consumed."""
    if not deliveries:
        return
    delivery_ids = [delivery.id for delivery in deliveries]
    now = utc_now()
    rows = (
        session.query(MailNotificationAttempt)
        .filter(
            MailNotificationAttempt.delivery_id.in_(delivery_ids),
            MailNotificationAttempt.status == "queued",
        )
        .all()
    )
    for row in rows:
        row.attempt = 1
        row.started_at = now
        row.status = "failed"
        row.error_code = "mail_already_read"
        row.completed_at = now


def _unread_count(session, mailbox_id: int, principal: Principal) -> int:
    rows = (
        session.query(MailDelivery)
        .filter(
            MailDelivery.recipient_mailbox_id == mailbox_id,
            MailDelivery.read_at.is_(None),
        )
        .all()
    )
    count = 0
    for delivery in rows:
        message = session.get(MailMessage, delivery.message_id)
        if message is None:
            continue
        try:
            _authorize_message_scope(session, message, principal)
        except MailboxUnavailableError:
            continue
        count += 1
    return count


def unread_mailbox_count_in_transaction(
    session,
    mailbox_id: int,
    *,
    principal: Principal | None = None,
) -> int:
    """Count only unread deliveries whose full represented scope remains visible."""
    return _unread_count(session, mailbox_id, _principal_or_local(principal))


def list_browser_mailboxes(
    *,
    principal: Principal | None = None,
) -> list[dict[str, Any]]:
    """List mailboxes this human-bound principal may open in Coordination."""
    resolved = _principal_or_local(principal)
    if not resolved.is_human_channel:
        raise MailboxUnavailableError("mailbox unavailable")
    operator_id = resolved.operator_id
    assert operator_id is not None
    init_db()
    with _db_module.SessionLocal() as session:
        operator = session.get(Operator, operator_id)
        if operator is None:
            raise MailboxUnavailableError("mailbox unavailable")
        own_operator_mailbox, _created = _ensure_operator_mailbox_row(
            session,
            operator.id,
            operator.slug,
        )
        rows = (
            session.query(Mailbox, Workspace, Operator)
            .outerjoin(Workspace, Workspace.id == Mailbox.workspace_id)
            .outerjoin(Operator, Operator.id == Mailbox.owner_operator_id)
            .filter(Mailbox.status == "active")
            .order_by(Mailbox.kind.desc(), Mailbox.address.asc())
            .yield_per(200)
        )
        result: list[dict[str, Any]] = []
        for mailbox, workspace, owner in rows:
            if mailbox.kind == "operator":
                if mailbox.id != own_operator_mailbox.id:
                    continue
                can_send = True
            elif mailbox.kind == "agent":
                elevated = resolved.is_bootstrap_admin or (
                    workspace is not None
                    and resolved.role_in_org(workspace.org_id) in {"admin", "owner"}
                )
                if (
                    workspace is None
                    or workspace.status != "active"
                    or not _workspace_readable(resolved, workspace)
                    or (mailbox.owner_operator_id != operator_id and not elevated)
                ):
                    continue
                can_send = False
            else:
                continue
            result.append(
                {
                    "address": mailbox.address,
                    "kind": mailbox.kind,
                    "workspace": workspace.slug if workspace is not None else None,
                    "tool": mailbox.tool,
                    "owner_operator": owner.slug if owner is not None else None,
                    "unread_count": _unread_count(session, mailbox.id, resolved),
                    "can_open": True,
                    "can_send": can_send,
                    "deep_link": f"/app/coordination?mailbox={quote(mailbox.address, safe='')}",
                }
            )
            if len(result) >= 500:
                break
        session.commit()
        return result


def take_mailbox_notification(
    session_id: str,
    binding_secret: str,
    *,
    notification_id: str | None = None,
    wait_ms: int = 0,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Claim one body-free adapter nudge; durable mail remains pull-authoritative."""
    resolved = _principal_or_local(principal)
    deadline = time.monotonic() + max(0, min(int(wait_ms), 30_000)) / 1000
    init_db()
    while True:
        with _db_module.SessionLocal() as session:
            mailbox, attachment, _agent = _resolve_open_mailbox(
                session,
                resolved,
                address=None,
                session_id=session_id,
                binding_secret=binding_secret,
                require_agent_proof=True,
            )
            assert attachment is not None
            if attachment.notification_mode not in NOTIFY_MODES:
                session.commit()
                return {"notification": None, "fallback": "pull", "timeout": False}
            _queue_unread_notifications_in_transaction(session, mailbox)
            query = session.query(MailNotificationAttempt).filter(
                MailNotificationAttempt.attachment_id == attachment.id,
                MailNotificationAttempt.status == "queued",
            )
            if notification_id:
                query = query.filter(
                    MailNotificationAttempt.notification_id == notification_id.strip()
                )
            row = query.order_by(MailNotificationAttempt.id.asc()).first()
            if row is not None:
                updated = (
                    session.query(MailNotificationAttempt)
                    .filter(
                        MailNotificationAttempt.id == row.id,
                        MailNotificationAttempt.status == "queued",
                    )
                    .update(
                        {
                            "status": "claimed",
                            "attempt": MailNotificationAttempt.attempt + 1,
                            "started_at": utc_now(),
                        },
                        synchronize_session=False,
                    )
                )
                if not updated:
                    session.rollback()
                    continue
                session.commit()
                row = session.get(MailNotificationAttempt, row.id)
                assert row is not None
                session.refresh(row)
                return {
                    "notification": _notification_dict(row),
                    "fallback": "pull",
                }
            session.commit()
        if notification_id:
            raise MailboxUnavailableError("mailbox unavailable")
        if time.monotonic() >= deadline:
            return {"notification": None, "fallback": "pull", "timeout": wait_ms > 0}
        time.sleep(0.05)


def settle_mailbox_notification(
    session_id: str,
    binding_secret: str,
    notification_id: str,
    *,
    status: str,
    error_code: str | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Record the adapter-observed nudge outcome without changing local delivery."""
    normalized = (status or "").strip().lower()
    if normalized not in {"delivered", "failed"}:
        raise MailboxValidationError("notification status must be delivered or failed")
    normalized_error = (error_code or "").strip() or None
    if normalized == "failed" and (
        normalized_error is None
        or len(normalized_error) > 64
        or not _KIND_RE.fullmatch(normalized_error)
    ):
        raise MailboxValidationError("failed notification requires a bounded error_code")
    if normalized == "delivered" and normalized_error is not None:
        raise MailboxValidationError("delivered notification cannot carry error_code")
    resolved = _principal_or_local(principal)
    init_db()
    with _db_module.SessionLocal() as session:
        _mailbox, attachment, _agent = _resolve_open_mailbox(
            session,
            resolved,
            address=None,
            session_id=session_id,
            binding_secret=binding_secret,
            require_agent_proof=True,
        )
        assert attachment is not None
        row = (
            session.query(MailNotificationAttempt)
            .filter(
                MailNotificationAttempt.notification_id == (notification_id or "").strip(),
                MailNotificationAttempt.attachment_id == attachment.id,
            )
            .one_or_none()
        )
        if row is None:
            raise MailboxUnavailableError("mailbox unavailable")
        if row.status in {"delivered", "failed"}:
            if row.status != normalized or row.error_code != normalized_error:
                raise MailboxUnavailableError("mailbox unavailable")
            return {"notification": _notification_dict(row), "fallback": "pull"}
        if row.status != "claimed":
            raise MailboxUnavailableError("mailbox unavailable")
        now = utc_now()
        stmt = (
            update(MailNotificationAttempt)
            .where(
                MailNotificationAttempt.id == row.id,
                MailNotificationAttempt.status == "claimed",
            )
            .values(
                status=normalized,
                error_code=normalized_error,
                completed_at=now,
            )
        )
        if session.get_bind().dialect.name == "postgresql":
            stmt = stmt.returning(MailNotificationAttempt.id)
            updated = session.execute(stmt).scalar_one_or_none() is not None
        else:
            updated = bool(getattr(session.execute(stmt), "rowcount", 0))
        if not updated:
            session.rollback()
            replay = (
                session.query(MailNotificationAttempt)
                .filter(
                    MailNotificationAttempt.notification_id == (notification_id or "").strip(),
                    MailNotificationAttempt.attachment_id == attachment.id,
                )
                .one_or_none()
            )
            if (
                replay is not None
                and replay.status == normalized
                and replay.error_code == normalized_error
            ):
                return {"notification": _notification_dict(replay), "fallback": "pull"}
            raise MailboxUnavailableError("mailbox unavailable")
        session.commit()
        row = session.get(MailNotificationAttempt, row.id)
        assert row is not None
        session.refresh(row)
        result = {"notification": _notification_dict(row), "fallback": "pull"}
    notification = result.get("notification")
    assert isinstance(notification, dict)
    append_event(
        "mailbox_notification_delivered"
        if normalized == "delivered"
        else "mailbox_notification_failed",
        f"durable mailbox notification {normalized}",
        session_id=session_id,
        metadata={
            "notification_id": notification_id,
            "adapter": notification["adapter"],
            "error_code": normalized_error,
        },
    )
    return result


def read_mailbox_inbox(
    *,
    address: str | None = None,
    session_id: str | None = None,
    binding_secret: str | None = None,
    mark_read: bool = True,
    include_read: bool = False,
    after_delivery_id: int | None = None,
    limit: int = 50,
    require_agent_proof: bool = False,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Read one authorized mailbox sequentially and advance its agent cursor."""
    resolved = _principal_or_local(principal)
    cap = max(1, min(int(limit), 200))
    init_db()
    marked = 0
    event_workspace_id = None
    event_session_id = None
    with _db_module.SessionLocal() as session:
        mailbox, attachment, agent = _resolve_open_mailbox(
            session,
            resolved,
            address=address,
            session_id=session_id,
            binding_secret=binding_secret,
            require_agent_proof=require_agent_proof,
        )
        if mark_read:
            _lock_mailbox_for_read(session, mailbox)
        floor = max(0, int(after_delivery_id or 0))
        if after_delivery_id is None and attachment is not None:
            floor = attachment.last_seen_delivery_id
        query = session.query(MailDelivery).filter(MailDelivery.recipient_mailbox_id == mailbox.id)
        if after_delivery_id is not None or include_read:
            query = query.filter(MailDelivery.id > floor)
        if not include_read:
            query = query.filter(MailDelivery.read_at.is_(None))
        deliveries = query.order_by(MailDelivery.id.asc()).all()
        selected: list[tuple[MailDelivery, MailMessage]] = []
        scanned_cursor = floor
        for delivery in deliveries:
            message = session.get(MailMessage, delivery.message_id)
            if message is None:
                continue
            try:
                _authorize_message_scope(session, message, resolved)
            except MailboxUnavailableError:
                continue
            scanned_cursor = max(scanned_cursor, delivery.id)
            selected.append((delivery, message))
            if len(selected) >= cap:
                break
        selected_deliveries = [delivery for delivery, _message in selected]
        if mark_read:
            marked = _mark_deliveries_read(
                selected_deliveries,
                agent=agent,
                principal=resolved,
            )
            _settle_read_notifications(
                session,
                selected_deliveries,
            )
        if attachment is not None:
            if mark_read:
                attachment.last_seen_delivery_id = max(
                    attachment.last_seen_delivery_id,
                    scanned_cursor,
                )
            cursor = attachment.last_seen_delivery_id
        else:
            cursor = scanned_cursor
        session.flush()
        messages = [
            _message_dict(
                session,
                message,
                inbox_delivery_id=delivery.id,
                viewer_mailbox_id=mailbox.id,
            )
            for delivery, message in selected
        ]
        unread = _unread_count(session, mailbox.id, resolved)
        event_workspace_id = mailbox.workspace_id
        event_session_id = agent.id if agent is not None else None
        result = {
            "mailbox": mailbox.address,
            "cursor": cursor,
            "unread_count": unread,
            "messages": messages,
        }
        session.commit()
    if marked:
        append_event(
            "mailbox_message_read",
            f"{len(messages)} durable mailbox message(s) returned",
            workspace_id=event_workspace_id,
            session_id=event_session_id,
            metadata={
                "count": len(messages),
                "marked_read": marked,
                "cursor": result["cursor"],
            },
        )
    return result


def read_mailbox_sent(
    *,
    address: str | None = None,
    session_id: str | None = None,
    binding_secret: str | None = None,
    after_message_id: int | None = None,
    limit: int = 50,
    require_agent_proof: bool = False,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """List authorized messages sent from one durable mailbox."""
    resolved = _principal_or_local(principal)
    cap = max(1, min(int(limit), 200))
    init_db()
    with _db_module.SessionLocal() as session:
        mailbox, _attachment, _agent = _resolve_open_mailbox(
            session,
            resolved,
            address=address,
            session_id=session_id,
            binding_secret=binding_secret,
            require_agent_proof=require_agent_proof,
        )
        floor = max(0, int(after_message_id or 0))
        rows = (
            session.query(MailMessage)
            .filter(
                MailMessage.sender_mailbox_id == mailbox.id,
                MailMessage.id > floor,
            )
            .order_by(MailMessage.id.asc())
            .all()
        )
        messages = []
        scanned_cursor = floor
        for message in rows:
            try:
                _authorize_message_scope(session, message, resolved)
            except MailboxUnavailableError:
                continue
            scanned_cursor = max(scanned_cursor, message.id)
            messages.append(_message_dict(session, message))
            if len(messages) >= cap:
                break
        cursor = scanned_cursor
        session.commit()
        return {"mailbox": mailbox.address, "cursor": cursor, "messages": messages}


def read_mailbox_thread(
    thread_id: str,
    *,
    address: str | None = None,
    session_id: str | None = None,
    binding_secret: str | None = None,
    mark_read: bool = True,
    require_agent_proof: bool = False,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Return an authorized thread timeline and optionally mark its deliveries read."""
    resolved = _principal_or_local(principal)
    init_db()
    marked = 0
    event_workspace_id = None
    event_session_id = None
    with _db_module.SessionLocal() as session:
        mailbox, attachment, agent = _resolve_open_mailbox(
            session,
            resolved,
            address=address,
            session_id=session_id,
            binding_secret=binding_secret,
            require_agent_proof=require_agent_proof,
        )
        if mark_read:
            _lock_mailbox_for_read(session, mailbox)
        thread = (
            session.query(MailThread)
            .filter(MailThread.thread_id == (thread_id or "").strip())
            .one_or_none()
        )
        if thread is None:
            raise MailboxUnavailableError("mailbox unavailable")
        messages = _authorize_thread(session, thread, mailbox, resolved)
        mailbox_deliveries = (
            session.query(MailDelivery)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(
                MailMessage.thread_id == thread.id,
                MailDelivery.recipient_mailbox_id == mailbox.id,
            )
            .all()
        )
        if mark_read:
            marked = _mark_deliveries_read(
                mailbox_deliveries,
                agent=agent,
                principal=resolved,
            )
            _settle_read_notifications(
                session,
                mailbox_deliveries,
            )
            if attachment is not None and mailbox_deliveries:
                attachment.last_seen_delivery_id = max(
                    attachment.last_seen_delivery_id,
                    *(delivery.id for delivery in mailbox_deliveries),
                )
        session.flush()
        result = _thread_dict(
            session,
            thread,
            messages,
            viewer_mailbox_id=mailbox.id,
        )
        result["mailbox"] = mailbox.address
        result["unread_count"] = _unread_count(session, mailbox.id, resolved)
        result["cursor"] = (
            attachment.last_seen_delivery_id
            if attachment is not None
            else max([0, *(delivery.id for delivery in mailbox_deliveries)])
        )
        event_workspace_id = mailbox.workspace_id
        event_session_id = agent.id if agent is not None else None
        session.commit()
    if marked:
        append_event(
            "mailbox_thread_read",
            "durable mailbox thread read",
            workspace_id=event_workspace_id,
            session_id=event_session_id,
            metadata={"thread_id": thread_id, "marked_read": marked},
        )
    return result


__all__ = [
    "broadcast_mailbox_message",
    "forward_mailbox_message",
    "list_browser_mailboxes",
    "read_mailbox_inbox",
    "read_mailbox_sent",
    "read_mailbox_thread",
    "reply_mailbox_message",
    "send_mailbox_message",
    "settle_mailbox_notification",
    "take_mailbox_notification",
    "unread_mailbox_count_in_transaction",
]

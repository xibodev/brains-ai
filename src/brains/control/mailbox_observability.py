"""Count-only operational health for durable mailbox state."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_

from brains.control.common import utc_now
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    Mailbox,
    MailboxAttachment,
    MailDelivery,
    MailLegacyRecord,
    MailNotificationAttempt,
    MailSmtpOutbox,
    Operator,
    SessionLease,
    Workspace,
)

AGED_UNREAD_HOURS = 24
STALLED_NOTIFICATION_MINUTES = 5
AGED_SMTP_OPEN_MINUTES = 5
_TERMINAL_SESSION_STATES = ("dormant", "completed", "failed")
_NOTIFICATION_FALLBACK_CODES = frozenset(
    {"attachment_detached", "mail_already_read", "notification_mode_changed"}
)


def _active_mailbox_is_valid(
    mailbox: Mailbox,
    workspaces: dict[int, Workspace],
    operators: dict[int, Operator],
) -> bool:
    """Validate persisted identity without returning any identity field."""
    if mailbox.status != "active":
        return True
    if mailbox.kind == "operator":
        operator = operators.get(mailbox.owner_operator_id)
        return bool(operator and mailbox.address == f"operator:{operator.slug}@brains")
    if mailbox.kind != "agent" or mailbox.workspace_id is None:
        return False
    workspace = workspaces.get(mailbox.workspace_id)
    if workspace is None or workspace.status != "active":
        return False
    try:
        from brains.control.durable_mailbox import (
            canonical_mailbox_tool,
            validate_native_tool_session_id,
        )

        tool = canonical_mailbox_tool(mailbox.tool or "")
        native_id = validate_native_tool_session_id(mailbox.native_tool_session_id or "")
    except ValueError:
        return False
    return mailbox.address == f"{tool}:{native_id}@{workspace.slug}"


def mailbox_health_report(
    *,
    now: datetime | None = None,
    aged_unread_hours: int = AGED_UNREAD_HOURS,
) -> dict[str, Any]:
    """Return count-only mailbox readiness without identities or message content."""
    if aged_unread_hours <= 0:
        raise ValueError("aged_unread_hours must be positive")
    observed_at = now or utc_now()
    unread_cutoff = observed_at - timedelta(hours=aged_unread_hours)
    notification_cutoff = observed_at - timedelta(minutes=STALLED_NOTIFICATION_MINUTES)
    smtp_cutoff = observed_at - timedelta(minutes=AGED_SMTP_OPEN_MINUTES)
    init_db()

    with SessionLocal() as session:
        mailboxes = session.query(Mailbox).all()
        workspaces = {row.id: row for row in session.query(Workspace).all()}
        operators = {row.id: row for row in session.query(Operator).all()}
        active_agent = sum(row.status == "active" and row.kind == "agent" for row in mailboxes)
        active_operator = sum(
            row.status == "active" and row.kind == "operator" for row in mailboxes
        )
        invalid_active = sum(
            row.status == "active" and not _active_mailbox_is_valid(row, workspaces, operators)
            for row in mailboxes
        )
        ambiguous_legacy = (
            session.query(MailLegacyRecord)
            .filter(MailLegacyRecord.disposition == "unverified")
            .count()
        )

        live_attachments = (
            session.query(MailboxAttachment, Mailbox, AgentSession)
            .outerjoin(Mailbox, Mailbox.id == MailboxAttachment.mailbox_id)
            .outerjoin(AgentSession, AgentSession.id == MailboxAttachment.session_id)
            .filter(MailboxAttachment.active_slot == 1)
            .all()
        )
        active_attachments = len(live_attachments)
        active_counts: dict[int, int] = {}
        invalid_live = 0
        for attachment, mailbox, agent in live_attachments:
            active_counts[attachment.mailbox_id] = active_counts.get(attachment.mailbox_id, 0) + 1
            session_current = False
            if (
                agent is not None
                and agent.ended_at is None
                and agent.state not in _TERMINAL_SESSION_STATES
            ):
                if agent.pid is not None:
                    session_current = True
                else:
                    from brains.control.session_liveness import lease_is_current

                    session_current = lease_is_current(session.get(SessionLease, agent.id))
            if (
                mailbox is None
                or mailbox.status != "active"
                or mailbox.kind != "agent"
                or agent is None
                or not session_current
                or agent.workspace_id != mailbox.workspace_id
            ):
                invalid_live += 1
        conflicting_live = sum(count > 1 for count in active_counts.values())

        delivery_total = session.query(MailDelivery).count()
        unread_query = session.query(MailDelivery).filter(MailDelivery.read_at.is_(None))
        unread = unread_query.count()
        aged_unread = unread_query.filter(MailDelivery.accepted_at <= unread_cutoff).count()
        active_attachment_mailboxes = session.query(MailboxAttachment.mailbox_id).filter(
            MailboxAttachment.active_slot == 1
        )
        offline_unread_query = (
            session.query(MailDelivery)
            .join(Mailbox, Mailbox.id == MailDelivery.recipient_mailbox_id)
            .filter(
                MailDelivery.read_at.is_(None),
                Mailbox.kind == "agent",
                Mailbox.status == "active",
                ~Mailbox.id.in_(active_attachment_mailboxes),
            )
        )
        offline_unread = offline_unread_query.count()
        offline_mailboxes = (
            offline_unread_query.with_entities(MailDelivery.recipient_mailbox_id).distinct().count()
        )

        notification_counts = {
            status: int(count)
            for status, count in session.query(
                MailNotificationAttempt.status,
                func.count(MailNotificationAttempt.id),
            )
            .group_by(MailNotificationAttempt.status)
            .all()
        }
        stalled_notifications = (
            session.query(MailNotificationAttempt)
            .filter(
                or_(
                    (
                        (MailNotificationAttempt.status == "queued")
                        & (MailNotificationAttempt.created_at <= notification_cutoff)
                    ),
                    (
                        (MailNotificationAttempt.status == "claimed")
                        & (MailNotificationAttempt.started_at <= notification_cutoff)
                    ),
                )
            )
            .count()
        )
        wakeup_failures = (
            session.query(MailNotificationAttempt)
            .join(MailDelivery, MailDelivery.id == MailNotificationAttempt.delivery_id)
            .filter(
                MailNotificationAttempt.status == "failed",
                MailDelivery.read_at.is_(None),
                ~MailNotificationAttempt.error_code.in_(_NOTIFICATION_FALLBACK_CODES),
            )
            .count()
        )
        notification_fallbacks = (
            session.query(MailNotificationAttempt)
            .filter(
                MailNotificationAttempt.status == "failed",
                MailNotificationAttempt.error_code.in_(_NOTIFICATION_FALLBACK_CODES),
            )
            .count()
        )

        smtp_counts = {
            status: int(count)
            for status, count in session.query(
                MailSmtpOutbox.status,
                func.count(MailSmtpOutbox.id),
            )
            .group_by(MailSmtpOutbox.status)
            .all()
        }
        aged_smtp_open = (
            session.query(MailSmtpOutbox)
            .filter(
                MailSmtpOutbox.status.in_(("queued", "retry")),
                MailSmtpOutbox.created_at <= smtp_cutoff,
            )
            .count()
        )
        expired_smtp_claims = (
            session.query(MailSmtpOutbox)
            .filter(
                MailSmtpOutbox.status == "sending",
                MailSmtpOutbox.lease_expires_at <= observed_at,
            )
            .count()
        )

    issue_counts = {
        "invalid_active_registration": invalid_active,
        "conflicting_live_attachment": conflicting_live,
        "invalid_live_attachment": invalid_live,
        "aged_unread_delivery": aged_unread,
        "stalled_notification": stalled_notifications,
        "wakeup_failure": wakeup_failures,
        "aged_smtp_backlog": aged_smtp_open,
        "expired_smtp_claim": expired_smtp_claims,
        "smtp_failed": smtp_counts.get("failed", 0),
        "smtp_uncertain": smtp_counts.get("uncertain", 0),
    }
    reasons = [code for code, count in issue_counts.items() if count]
    return {
        "state": "degraded" if reasons else "ready",
        "observed_at": observed_at.isoformat(),
        "thresholds": {
            "aged_unread_hours": aged_unread_hours,
            "stalled_notification_minutes": STALLED_NOTIFICATION_MINUTES,
            "aged_smtp_open_minutes": AGED_SMTP_OPEN_MINUTES,
        },
        "registration": {
            "active_agent": active_agent,
            "active_operator": active_operator,
            "invalid_active": invalid_active,
            "unverified_legacy": ambiguous_legacy,
            "unverified_legacy_is_degraded": False,
        },
        "attachments": {
            "active": active_attachments,
            "conflicting_live": conflicting_live,
            "invalid_live": invalid_live,
        },
        "delivery": {
            "total": delivery_total,
            "unread": unread,
            "aged_unread": aged_unread,
            "offline_unread": offline_unread,
            "offline_mailboxes_with_unread": offline_mailboxes,
            "offline_is_degraded": False,
        },
        "notification": {
            "queued": notification_counts.get("queued", 0),
            "claimed": notification_counts.get("claimed", 0),
            "delivered": notification_counts.get("delivered", 0),
            "failed": notification_counts.get("failed", 0),
            "wakeup_failures": wakeup_failures,
            "closed_by_pull_fallback": notification_fallbacks,
            "stalled": stalled_notifications,
        },
        "smtp": {
            "queued": smtp_counts.get("queued", 0),
            "sending": smtp_counts.get("sending", 0),
            "retry": smtp_counts.get("retry", 0),
            "sent": smtp_counts.get("sent", 0),
            "failed": smtp_counts.get("failed", 0),
            "uncertain": smtp_counts.get("uncertain", 0),
            "cancelled": smtp_counts.get("cancelled", 0),
            "aged_open": aged_smtp_open,
            "expired_claims": expired_smtp_claims,
        },
        "issue_count": sum(issue_counts.values()),
        "reasons": reasons,
    }


__all__ = [
    "AGED_SMTP_OPEN_MINUTES",
    "AGED_UNREAD_HOURS",
    "STALLED_NOTIFICATION_MINUTES",
    "mailbox_health_report",
]

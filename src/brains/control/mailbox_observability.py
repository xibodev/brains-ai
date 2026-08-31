"""Privacy-safe operational health for durable mailbox state."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_

from brains.control.common import utc_now
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    Event,
    Mailbox,
    MailboxAttachment,
    MailDelivery,
    MailLegacyRecord,
    MailMessage,
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
MINIMUM_ANALYTICS_GROUP_SIZE = 3
OUTCOME_RESULTS = ("success", "empty", "refused", "timeout", "failed", "uncertain")


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


def _count_bucket(count: int, minimum_group_size: int) -> dict[str, Any]:
    suppressed = 0 < count < minimum_group_size
    return {"count": None if suppressed else count, "suppressed": suppressed}


def _result_buckets(
    counts: dict[str, int],
    minimum_group_size: int,
) -> dict[str, dict[str, Any]]:
    return {
        result: _count_bucket(int(counts.get(result, 0)), minimum_group_size)
        for result in OUTCOME_RESULTS
    }


def _outcome_bucket(
    transport: str,
    eligible: int,
    counts: dict[str, int],
    minimum_group_size: int,
) -> dict[str, Any]:
    results = _result_buckets(counts, minimum_group_size)
    if any(result["suppressed"] for result in results.values()):
        # Hide the denominator and every non-zero peer bucket as well, otherwise
        # subtraction can recover the suppressed count.
        eligible_bucket = {"count": None, "suppressed": True}
        results = {
            result: (
                {"count": 0, "suppressed": False}
                if int(counts.get(result, 0)) == 0
                else {"count": None, "suppressed": True}
            )
            for result in OUTCOME_RESULTS
        }
    else:
        eligible_bucket = _count_bucket(eligible, minimum_group_size)
    return {"transport": transport, "eligible": eligible_bucket, "results": results}


def mailbox_outcome_report(
    *,
    window_minutes: int = 2,
    since_days: int = 14,
    workspace: str | None = None,
    minimum_group_size: int = MINIMUM_ANALYTICS_GROUP_SIZE,
) -> dict[str, Any]:
    """Aggregate mailbox lifecycle outcomes with right-censoring and suppression."""
    if window_minutes <= 0:
        raise ValueError("window_minutes must be positive")
    if since_days <= 0:
        raise ValueError("since_days must be positive")
    if minimum_group_size < 2:
        raise ValueError("minimum_group_size must be at least 2")

    observed_at = utc_now()
    started_at = observed_at - timedelta(days=since_days)
    eligible_before = observed_at - timedelta(minutes=window_minutes)
    init_db()
    with SessionLocal() as session:
        workspace_id: int | None = None
        if workspace:
            row = session.query(Workspace.id).filter(Workspace.slug == workspace).one_or_none()
            if row is None:
                raise ValueError(f"unknown workspace: {workspace}")
            workspace_id = int(row[0])

        registrations = session.query(Mailbox).filter(
            Mailbox.kind == "agent",
            Mailbox.created_at >= started_at,
        )
        if workspace_id is not None:
            registrations = registrations.filter(Mailbox.workspace_id == workspace_id)
        registration_count = registrations.count()

        messages = session.query(MailMessage).filter(
            MailMessage.created_at >= started_at,
            MailMessage.created_at <= eligible_before,
        )
        if workspace_id is not None:
            messages = messages.filter(MailMessage.origin_workspace_id == workspace_id)
        direct_sends = messages.filter(
            MailMessage.audience == "direct",
            MailMessage.in_reply_to_id.is_(None),
            MailMessage.forwarded_from_id.is_(None),
        ).count()
        replies = messages.filter(MailMessage.in_reply_to_id.isnot(None)).count()
        forwards = messages.filter(MailMessage.forwarded_from_id.isnot(None)).count()
        broadcasts = messages.filter(MailMessage.audience == "broadcast").count()

        deliveries = session.query(MailDelivery).join(
            MailMessage, MailMessage.id == MailDelivery.message_id
        )
        if workspace_id is not None:
            deliveries = deliveries.filter(MailMessage.origin_workspace_id == workspace_id)
        read_eligible_query = deliveries.filter(
            MailDelivery.accepted_at >= started_at,
            MailDelivery.accepted_at <= eligible_before,
        )
        read_eligible = read_eligible_query.count()
        read_success = 0
        for delivery in read_eligible_query.filter(MailDelivery.read_at.isnot(None)).all():
            assert delivery.read_at is not None
            read_at = delivery.read_at
            if read_at.tzinfo is None:
                read_at = read_at.replace(tzinfo=observed_at.tzinfo)
            accepted_at = delivery.accepted_at
            if accepted_at.tzinfo is None:
                accepted_at = accepted_at.replace(tzinfo=observed_at.tzinfo)
            if accepted_at <= read_at <= accepted_at + timedelta(minutes=window_minutes):
                read_success += 1

        notifications = (
            session.query(MailNotificationAttempt)
            .join(MailDelivery, MailDelivery.id == MailNotificationAttempt.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(
                MailNotificationAttempt.created_at >= started_at,
                MailNotificationAttempt.created_at <= eligible_before,
            )
        )
        if workspace_id is not None:
            notifications = notifications.filter(MailMessage.origin_workspace_id == workspace_id)
        notification_eligible = notifications.count()
        notification_success = 0
        notification_refused = 0
        notification_failed = 0
        for notification in notifications.all():
            if notification.completed_at is None:
                continue
            completed_at = notification.completed_at
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=observed_at.tzinfo)
            created_at = notification.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=observed_at.tzinfo)
            if not created_at <= completed_at <= created_at + timedelta(minutes=window_minutes):
                continue
            if notification.status == "delivered":
                notification_success += 1
            elif notification.status == "failed" and (
                notification.error_code in _NOTIFICATION_FALLBACK_CODES
            ):
                notification_refused += 1
            elif notification.status == "failed":
                notification_failed += 1

        smtp = (
            session.query(MailSmtpOutbox)
            .join(MailDelivery, MailDelivery.id == MailSmtpOutbox.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(
                MailSmtpOutbox.created_at >= started_at,
                MailSmtpOutbox.created_at <= eligible_before,
            )
        )
        if workspace_id is not None:
            smtp = smtp.filter(MailMessage.origin_workspace_id == workspace_id)
        smtp_eligible = smtp.count()
        smtp_success = 0
        smtp_refused = 0
        smtp_failed = 0
        smtp_uncertain = 0
        for copy in smtp.all():
            completed_smtp_at = copy.sent_at if copy.status == "sent" else copy.updated_at
            if completed_smtp_at is None:
                continue
            if completed_smtp_at.tzinfo is None:
                completed_smtp_at = completed_smtp_at.replace(tzinfo=observed_at.tzinfo)
            created_at = copy.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=observed_at.tzinfo)
            if (
                not created_at
                <= completed_smtp_at
                <= created_at + timedelta(minutes=window_minutes)
            ):
                continue
            if copy.status == "sent":
                smtp_success += 1
            elif copy.status == "cancelled":
                smtp_refused += 1
            elif copy.status == "failed":
                smtp_failed += 1
            elif copy.status == "uncertain":
                smtp_uncertain += 1

        refusals = session.query(Event).filter(
            Event.kind == "mailbox_delivery_refused",
            Event.created_at >= started_at,
        )
        if workspace_id is not None:
            refusals = refusals.filter(Event.workspace_id == workspace_id)
        refusal_rows = refusals.all()
        refusal_actions = {
            "send": 0,
            "reply": 0,
            "forward": 0,
            "broadcast": 0,
            "unclassified": 0,
        }
        refusal_reasons = {
            action: {"validation": 0, "unavailable": 0, "unclassified": 0}
            for action in refusal_actions
        }
        for refusal in refusal_rows:
            try:
                metadata = json.loads(refusal.metadata_json or "{}")
            except (TypeError, ValueError):
                metadata = {}
            action = metadata.get("action")
            reason = metadata.get("reason")
            action_key = action if action in refusal_actions else "unclassified"
            reason_key = reason if reason in refusal_reasons[action_key] else "unclassified"
            refusal_actions[action_key] += 1
            refusal_reasons[action_key][reason_key] += 1

    outcomes: dict[str, dict[str, Any]] = {
        "address_registration": _outcome_bucket(
            "local_database",
            registration_count,
            {"success": registration_count},
            minimum_group_size,
        ),
        "mail_acceptance": _outcome_bucket(
            "local_database",
            direct_sends + refusal_actions["send"],
            {"success": direct_sends, "refused": refusal_actions["send"]},
            minimum_group_size,
        ),
        "wakeup": _outcome_bucket(
            "adapter_notification",
            notification_eligible,
            {
                "success": notification_success,
                "refused": notification_refused,
                "failed": notification_failed,
                "timeout": max(
                    0,
                    notification_eligible
                    - notification_success
                    - notification_refused
                    - notification_failed,
                ),
            },
            minimum_group_size,
        ),
        "read": _outcome_bucket(
            "local_database",
            read_eligible,
            {"success": read_success, "timeout": max(0, read_eligible - read_success)},
            minimum_group_size,
        ),
        "reply": _outcome_bucket(
            "local_database",
            replies + refusal_actions["reply"],
            {"success": replies, "refused": refusal_actions["reply"]},
            minimum_group_size,
        ),
        "forward": _outcome_bucket(
            "local_database",
            forwards + refusal_actions["forward"],
            {"success": forwards, "refused": refusal_actions["forward"]},
            minimum_group_size,
        ),
        "broadcast": _outcome_bucket(
            "local_database",
            broadcasts + refusal_actions["broadcast"],
            {"success": broadcasts, "refused": refusal_actions["broadcast"]},
            minimum_group_size,
        ),
        "smtp_copy": _outcome_bucket(
            "smtp",
            smtp_eligible,
            {
                "success": smtp_success,
                "refused": smtp_refused,
                "failed": smtp_failed,
                "uncertain": smtp_uncertain,
                "timeout": max(
                    0,
                    smtp_eligible - smtp_success - smtp_refused - smtp_failed - smtp_uncertain,
                ),
            },
            minimum_group_size,
        ),
        "unclassified_refusal": _outcome_bucket(
            "local_database",
            refusal_actions["unclassified"],
            {"refused": refusal_actions["unclassified"]},
            minimum_group_size,
        ),
    }
    for outcome_name, action_name in (
        ("mail_acceptance", "send"),
        ("reply", "reply"),
        ("forward", "forward"),
        ("broadcast", "broadcast"),
        ("unclassified_refusal", "unclassified"),
    ):
        action_total = refusal_actions[action_name]
        action_reasons = refusal_reasons[action_name]
        if 0 < action_total < minimum_group_size:
            breakdown: dict[str, Any] = {
                "suppressed": True,
                "counts": None,
            }
        elif any(0 < count < minimum_group_size for count in action_reasons.values()):
            breakdown = {
                "suppressed": True,
                "counts": {
                    reason: (
                        {"count": 0, "suppressed": False}
                        if count == 0
                        else {"count": None, "suppressed": True}
                    )
                    for reason, count in action_reasons.items()
                },
            }
        else:
            breakdown = {
                "suppressed": False,
                "counts": {
                    reason: _count_bucket(count, minimum_group_size)
                    for reason, count in action_reasons.items()
                },
            }
        outcomes[outcome_name]["refusal_reasons"] = breakdown
    suppressed_groups = sum(
        int(bucket["eligible"]["suppressed"])
        + sum(int(result["suppressed"]) for result in bucket["results"].values())
        + int(bucket.get("refusal_reasons", {}).get("suppressed", False))
        for bucket in outcomes.values()
    )
    return {
        "window_minutes": window_minutes,
        "since_days": since_days,
        "workspace": workspace,
        "observed_at": observed_at.isoformat(),
        "observation_started_at": started_at.isoformat(),
        "eligible_before": eligible_before.isoformat(),
        "minimum_group_size": minimum_group_size,
        "privacy": {
            "suppressed_groups": suppressed_groups,
            "contains_content": False,
            "contains_address": False,
            "contains_source_path": False,
            "contains_native_session_id": False,
            "contains_native_object_id": False,
        },
        "interpretation": {
            "unit": "durable mailbox lifecycle row",
            "results": list(OUTCOME_RESULTS),
            "not_measured": ["task success", "user value", "causal impact"],
        },
        "outcomes": outcomes,
    }


__all__ = [
    "AGED_SMTP_OPEN_MINUTES",
    "AGED_UNREAD_HOURS",
    "MINIMUM_ANALYTICS_GROUP_SIZE",
    "OUTCOME_RESULTS",
    "STALLED_NOTIFICATION_MINUTES",
    "mailbox_health_report",
    "mailbox_outcome_report",
]

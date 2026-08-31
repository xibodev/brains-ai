from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

from brains.control.adoption import adoption_report
from brains.control.common import utc_now
from brains.control.durable_mail import reply_mailbox_message, send_mailbox_message
from brains.control.durable_mailbox import MailboxUnavailableError, register_agent_mailbox
from brains.control.mailbox_observability import mailbox_health_report
from brains.control.operators import ensure_admin_operator
from brains.control.sessions import start_session
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    Event,
    Mailbox,
    MailboxAttachment,
    MailDelivery,
    MailMessage,
    MailNotificationAttempt,
    MailSmtpOutbox,
    SessionLease,
)


@pytest.fixture(autouse=True)
def _clean_mailbox_observability_rows():
    init_db()
    ensure_admin_operator()
    with SessionLocal() as session:
        baseline_delivery_id = (
            session.query(MailDelivery.id).order_by(MailDelivery.id.desc()).limit(1).scalar() or 0
        )
    yield
    with SessionLocal() as session:
        session.query(MailSmtpOutbox).filter(
            MailSmtpOutbox.delivery_id > baseline_delivery_id
        ).delete(synchronize_session=False)
        session.query(MailNotificationAttempt).filter(
            MailNotificationAttempt.delivery_id > baseline_delivery_id
        ).delete(synchronize_session=False)
        session.query(MailDelivery).filter(MailDelivery.id > baseline_delivery_id).delete(
            synchronize_session=False
        )
        session.commit()


def _agent(path, *, tool: str = "opencode", mode: str = "pull") -> dict:
    started = start_session(
        str(path),
        tool=tool,
        mailbox_notification_mode=mode,
    )
    binding = f"binding-{uuid.uuid4().hex}"
    mailbox = register_agent_mailbox(
        str(path),
        tool,
        f"native-{uuid.uuid4().hex}",
        started["session_id"],
        binding,
        notification_mode=mode,
    )
    return {"session": started, "mailbox": mailbox, "binding": binding}


def test_mailbox_health_separates_offline_mail_from_degradation(tmp_path) -> None:
    sender = _agent(tmp_path / "health", tool="opencode")
    recipient = _agent(tmp_path / "health", tool="codex")
    sent = send_mailbox_message(
        str(tmp_path / "health"),
        [recipient["mailbox"]["address"]],
        "health private subject",
        f"health-{uuid.uuid4().hex}",
        body="health private body",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    with SessionLocal() as session:
        delivery = (
            session.query(MailDelivery)
            .filter(MailDelivery.delivery_id == sent["deliveries"][0]["delivery_id"])
            .one()
        )
        delivery.accepted_at = utc_now() - timedelta(hours=25)
        session.query(MailboxAttachment).filter(
            MailboxAttachment.session_id == recipient["session"]["session_id"]
        ).update(
            {
                "active_slot": None,
                "detached_at": utc_now(),
                "detach_reason": "test_offline",
            },
            synchronize_session=False,
        )
        session.commit()

    report = mailbox_health_report()
    assert report["state"] == "degraded"
    assert report["delivery"]["offline_unread"] >= 1
    assert report["delivery"]["offline_is_degraded"] is False
    assert report["delivery"]["aged_unread"] >= 1
    assert "aged_unread_delivery" in report["reasons"]
    assert "health private" not in repr(report)


def test_mailbox_health_reports_invalid_active_registration_without_echoing_it(tmp_path) -> None:
    agent = _agent(tmp_path / "invalid-registration", tool="opencode")
    private_value = f"malformed-{uuid.uuid4().hex}@private.invalid"
    with SessionLocal() as session:
        mailbox = (
            session.query(Mailbox).filter(Mailbox.address == agent["mailbox"]["address"]).one()
        )
        original = mailbox.address
        mailbox.address = private_value
        session.commit()
    try:
        report = mailbox_health_report()
        assert report["registration"]["invalid_active"] >= 1
        assert "invalid_active_registration" in report["reasons"]
        assert private_value not in json.dumps(report, sort_keys=True)
    finally:
        with SessionLocal() as session:
            mailbox = session.query(Mailbox).filter(Mailbox.address == private_value).one()
            mailbox.address = original
            session.commit()


def test_mailbox_health_detects_an_expired_live_attachment_lease(tmp_path) -> None:
    agent = _agent(tmp_path / "expired-attachment", tool="opencode")
    with SessionLocal() as session:
        lease = session.get(SessionLease, agent["session"]["session_id"])
        assert lease is not None
        lease.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    report = mailbox_health_report()

    assert report["attachments"]["invalid_live"] >= 1
    assert "invalid_live_attachment" in report["reasons"]


def test_mailbox_health_reports_wakeup_and_smtp_classes_without_identifiers(tmp_path) -> None:
    sender = _agent(tmp_path / "classes", tool="opencode")
    recipient = _agent(tmp_path / "classes", tool="codex", mode="turn_boundary")
    send_mailbox_message(
        str(tmp_path / "classes"),
        [recipient["mailbox"]["address"]],
        "classes private subject",
        f"classes-{uuid.uuid4().hex}",
        body="classes private body",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    with SessionLocal() as session:
        notification = (
            session.query(MailNotificationAttempt)
            .order_by(MailNotificationAttempt.id.desc())
            .first()
        )
        assert notification is not None
        notification.status = "failed"
        notification.attempt = 1
        notification.started_at = utc_now() - timedelta(minutes=6)
        notification.completed_at = utc_now()
        notification.error_code = "adapter_timeout"
        delivery = session.get(MailDelivery, notification.delivery_id)
        assert delivery is not None
        session.add(
            MailSmtpOutbox(
                outbox_id=f"smtp_{uuid.uuid4().hex}",
                idempotency_key=f"smtp-test-{uuid.uuid4().hex}",
                delivery_id=delivery.id,
                recipient_mailbox_id=delivery.recipient_mailbox_id,
                smtp_destination_ref=f"mailbox.smtp.1.{uuid.uuid4().hex}",
                copy_mode="notification",
                status="failed",
                attempt=1,
                error_code="synthetic_failure",
                created_at=utc_now() - timedelta(minutes=6),
                updated_at=utc_now() - timedelta(minutes=5),
            )
        )
        session.commit()

    report = mailbox_health_report()
    assert report["notification"]["wakeup_failures"] >= 1
    assert report["smtp"]["failed"] >= 1
    assert "wakeup_failure" in report["reasons"]
    assert "smtp_failed" in report["reasons"]
    text = json.dumps(report, sort_keys=True)
    assert recipient["mailbox"]["address"] not in text
    assert "smtp_destination_ref" not in text
    assert "classes private" not in text


def test_mailbox_outcomes_are_right_censored_suppressed_and_content_free(tmp_path) -> None:
    workspace = tmp_path / "analytics"
    sender = _agent(workspace, tool="opencode")
    recipient = _agent(workspace, tool="codex")
    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "analytics private subject",
        f"analytics-{uuid.uuid4().hex}",
        body="analytics private body",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    with SessionLocal() as session:
        delivery = (
            session.query(MailDelivery)
            .filter(MailDelivery.delivery_id == sent["deliveries"][0]["delivery_id"])
            .one()
        )
        delivery.accepted_at = utc_now() - timedelta(minutes=3)
        message = session.get(MailMessage, delivery.message_id)
        assert message is not None
        message.created_at = utc_now() - timedelta(minutes=3)
        session.commit()

    report = adoption_report(
        window_minutes=2,
        since_days=1,
        workspace=sender["session"]["workspace"],
    )["mailbox_outcomes"]
    acceptance = report["outcomes"]["mail_acceptance"]
    assert acceptance["eligible"]["suppressed"] is True
    assert acceptance["eligible"]["count"] is None
    assert report["outcomes"]["read"]["eligible"]["suppressed"] is True
    assert report["privacy"] == {
        "suppressed_groups": report["privacy"]["suppressed_groups"],
        "contains_content": False,
        "contains_address": False,
        "contains_source_path": False,
        "contains_native_session_id": False,
        "contains_native_object_id": False,
    }
    text = json.dumps(report, sort_keys=True)
    assert sent["message_id"] not in text
    assert sender["mailbox"]["address"] not in text
    assert str(workspace) not in text
    assert "analytics private" not in text


def test_mailbox_events_omit_address_and_native_object_ids(tmp_path) -> None:
    workspace = tmp_path / "events"
    sender = _agent(workspace, tool="opencode")
    recipient = _agent(workspace, tool="codex")
    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "event private subject",
        f"event-{uuid.uuid4().hex}",
        body="event private body",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    with SessionLocal() as session:
        events = (
            session.query(Event)
            .filter(
                Event.kind.in_(("mailbox_registered", "mailbox_attached", "mailbox_message_sent")),
                Event.workspace_id
                == session.query(MailDelivery.recipient_workspace_id)
                .filter(MailDelivery.delivery_id == sent["deliveries"][0]["delivery_id"])
                .scalar_subquery(),
            )
            .all()
        )
    text = "\n".join(f"{event.message}\n{event.metadata_json}" for event in events)
    assert sender["mailbox"]["address"] not in text
    assert recipient["mailbox"]["address"] not in text
    assert sent["message_id"] not in text
    assert "event private" not in text


def test_refusal_event_and_report_keep_recipient_input_private(tmp_path) -> None:
    workspace = tmp_path / "refusal"
    sender = _agent(workspace, tool="opencode")
    private_recipient = f"missing-{uuid.uuid4().hex}@private.invalid"
    with pytest.raises(MailboxUnavailableError):
        send_mailbox_message(
            str(workspace),
            [private_recipient],
            "refusal private subject",
            f"refusal-{uuid.uuid4().hex}",
            body="refusal private body",
            sender_session_id=sender["session"]["session_id"],
            binding_secret=sender["binding"],
        )
    with pytest.raises(MailboxUnavailableError):
        reply_mailbox_message(
            str(workspace),
            f"missing-{uuid.uuid4().hex}",
            f"reply-refusal-{uuid.uuid4().hex}",
            sender_session_id=sender["session"]["session_id"],
            binding_secret=sender["binding"],
        )

    with SessionLocal() as session:
        workspace_id = (
            session.query(Mailbox.workspace_id)
            .filter(Mailbox.id == sender["mailbox"]["mailbox_id"])
            .scalar()
        )
        event = (
            session.query(Event)
            .filter(
                Event.kind == "mailbox_delivery_refused",
                Event.workspace_id == workspace_id,
                Event.session_id.is_(None),
            )
            .all()
        )
    assert len(event) == 2
    assert all(row.session_id is None for row in event)
    assert private_recipient not in "".join(f"{row.message}{row.metadata_json}" for row in event)
    report = adoption_report(
        window_minutes=2,
        since_days=1,
        workspace=sender["session"]["workspace"],
    )["mailbox_outcomes"]
    refusal = report["outcomes"]["mail_acceptance"]
    assert refusal["results"]["refused"]["suppressed"] is True
    assert report["outcomes"]["reply"]["results"]["refused"]["suppressed"] is True
    assert refusal["refusal_reasons"] == {"suppressed": True, "counts": None}
    assert report["outcomes"]["reply"]["refusal_reasons"] == {
        "suppressed": True,
        "counts": None,
    }
    assert private_recipient not in json.dumps(report, sort_keys=True)

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine

from brains.config import settings
from brains.control.common import utc_now
from brains.control.durable_mail import send_mailbox_message
from brains.control.durable_mailbox import ensure_operator_mailboxes, register_agent_mailbox
from brains.control.mailbox_observability import mailbox_health_report
from brains.control.operations import readiness_report
from brains.control.operators import ensure_admin_operator
from brains.control.sessions import start_session
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    Mailbox,
    MailboxAttachment,
    MailDelivery,
    MailNotificationAttempt,
    MailSmtpOutbox,
    OperatorMailboxSetting,
    SessionLease,
)


@pytest.fixture(autouse=True)
def _clean_mailbox_observability_rows(tmp_path, monkeypatch):
    from brains.storage import db, migrations

    # Readiness needs exact counts, independent of other tests' historical rows.
    previous_bind = SessionLocal.kw["bind"]
    url = f"sqlite:///{(tmp_path / 'observability.sqlite').as_posix()}"
    engine = create_engine(url)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(migrations, "engine", engine)
    monkeypatch.setattr(settings, "db_url", url)
    SessionLocal.configure(bind=engine)
    try:
        init_db()
        ensure_admin_operator()
        yield
    finally:
        SessionLocal.configure(bind=previous_bind)
        engine.dispose()


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
    assert "smtp_failed" not in report["reasons"]
    assert report["issue_count"] == 1
    assert report["smtp"]["state"] == "degraded"
    assert report["smtp"]["issue_count"] == 1
    assert report["smtp"]["reasons"] == ["smtp_failed"]
    text = json.dumps(report, sort_keys=True)
    assert recipient["mailbox"]["address"] not in text
    assert "smtp_destination_ref" not in text
    assert "classes private" not in text


@pytest.fixture
def ready_protocols_and_recovery(monkeypatch):
    from brains.control import readiness, recovery_policy

    # Real storage, generic queue and durable-mail projections remain under test.
    for name in ("gateway_protocol_readiness", "mcp_protocol_readiness"):
        monkeypatch.setattr(readiness, name, lambda: {"ready": True})
    monkeypatch.setattr(
        recovery_policy,
        "recovery_readiness",
        lambda: {
            "ready": True,
            "policy": {"complete": True, "missing_fields": []},
            "candidate": {"ready": True},
            "last_drill": {"verified": True},
            "reasons": [],
        },
    )


@pytest.fixture
def historical_smtp_copy(tmp_path, monkeypatch):
    class FakeSMTP:
        connections = 0

        def __init__(self, *_args, **_kwargs):
            FakeSMTP.connections += 1
            raise AssertionError("observability must not connect to SMTP")

    monkeypatch.setattr("brains.control.mailer.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("brains.control.mailer.smtplib.SMTP_SSL", FakeSMTP)
    monkeypatch.setattr("brains.control.mailer._refresh_secure_settings", lambda: None)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.invalid")
    monkeypatch.setattr(settings, "smtp_from", "sender@example.invalid")
    workspace = tmp_path / "historical-smtp"
    sender = _agent(workspace)
    operator = ensure_admin_operator()
    address = f"operator:{operator['slug']}@brains"
    ensure_operator_mailboxes()
    with SessionLocal() as session:
        mailbox = session.query(Mailbox).filter(Mailbox.address == address).one()
        # Synthetic persisted, verified consent predates the core scheduler boundary.
        session.add(
            OperatorMailboxSetting(
                mailbox_id=mailbox.id,
                smtp_destination_ref="mailbox.smtp.synthetic-history",
                smtp_destination_verified_at=utc_now() - timedelta(days=1),
                smtp_copy_mode="notification",
            )
        )
        session.commit()
    sent = send_mailbox_message(
        str(workspace),
        [address],
        "historical private subject",
        f"historical-{uuid.uuid4().hex}",
        body="historical private body",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    assert sent["deliveries"][0]["state"] == "accepted"
    with SessionLocal() as session:
        row = session.query(MailSmtpOutbox).one()
        delivery = session.get(MailDelivery, row.delivery_id)
        assert delivery.delivery_id == sent["deliveries"][0]["delivery_id"]
        assert row.status == "queued"
        assert row.attempt == 0
        assert row.smtp_destination_ref == "mailbox.smtp.synthetic-history"
        outbox_id = row.id
    yield outbox_id
    assert FakeSMTP.connections == 0


@pytest.mark.parametrize(
    ("status", "aged", "expected_state", "expected_reasons"),
    [
        ("queued", False, "blocked", []),
        ("queued", True, "blocked", ["aged_smtp_backlog"]),
        ("retry", True, "blocked", ["aged_smtp_backlog"]),
        ("sending", False, "blocked", []),
        ("sending", True, "blocked", ["expired_smtp_claim"]),
        ("failed", True, "degraded", ["smtp_failed"]),
        ("uncertain", True, "degraded", ["smtp_uncertain"]),
        ("sent", True, "ready", []),
        ("cancelled", True, "ready", []),
    ],
)
def test_historical_smtp_diagnostics_preserve_local_readiness_and_rows(
    historical_smtp_copy,
    ready_protocols_and_recovery,
    status,
    aged,
    expected_state,
    expected_reasons,
):
    from brains.mcp.server import _scheduler_tick

    now = utc_now()
    with SessionLocal() as session:
        row = session.get(MailSmtpOutbox, historical_smtp_copy)
        row.status = status
        row.created_at = now - timedelta(minutes=6 if aged else 0)
        if status != "queued":
            row.attempt = 1
        if status == "sending":
            row.lease_owner = "synthetic-manual-processor"
            row.lease_expires_at = now + timedelta(minutes=-1 if aged else 1)
        if status == "retry":
            row.next_attempt_at = now + timedelta(minutes=1)
        if status in {"retry", "failed", "uncertain", "cancelled"}:
            row.error_code = "synthetic_history"
        if status == "sent":
            row.sent_at = now
        session.commit()
    with SessionLocal() as session:
        row = session.get(MailSmtpOutbox, historical_smtp_copy)
        before = tuple(getattr(row, column.name) for column in row.__table__.columns)
        delivery = session.get(MailDelivery, row.delivery_id)
        delivery_before = tuple(
            getattr(delivery, column.name) for column in delivery.__table__.columns
        )
    assert _scheduler_tick() == []
    for _ in range(2):
        report = mailbox_health_report(now=now)
        assert report["state"] == "ready"
        assert report["issue_count"] == 0
        assert report["reasons"] == []
        smtp = report["smtp"]
        assert smtp["state"] == expected_state
        assert smtp[status] == 1
        assert smtp["processor_state"] == "not_scheduled_by_core"
        assert smtp["core_scheduler_enabled"] is False
        assert smtp["affects_local_readiness"] is False
        blocked_reason = "processor_not_scheduled_by_core" if expected_state == "blocked" else None
        assert smtp["delivery_blocked_reason"] == blocked_reason
        assert smtp["reasons"] == ([blocked_reason] if blocked_reason else []) + expected_reasons
        assert smtp["issue_count"] == len(expected_reasons)
        assert smtp["aged_open"] == int(aged and status in {"queued", "retry"})
        assert smtp["expired_claims"] == int(aged and status == "sending")
        text = json.dumps(report)
        assert "historical private" not in text
        assert "mailbox.smtp.synthetic-history" not in text
        assert "synthetic-manual-processor" not in text

    readiness = readiness_report()
    assert readiness["status"] == "ready", readiness
    assert readiness["components"]["durable_mail"]["detail"]["smtp"] == smtp
    with SessionLocal() as session:
        row = session.query(MailSmtpOutbox).one()
        assert tuple(getattr(row, column.name) for column in row.__table__.columns) == before
        delivery = session.get(MailDelivery, row.delivery_id)
        assert (
            tuple(getattr(delivery, column.name) for column in delivery.__table__.columns)
            == delivery_before
        )


@pytest.mark.parametrize(
    "local_problem", ["invalid_active_registration", "invalid_live_attachment"]
)
def test_pending_smtp_does_not_mask_local_identity_failures(
    historical_smtp_copy,
    ready_protocols_and_recovery,
    local_problem,
):
    with SessionLocal() as session:
        mailbox = session.query(Mailbox).filter(Mailbox.kind == "agent").one()
        if local_problem == "invalid_active_registration":
            mailbox.address = "malformed@example.invalid"
        else:
            attachment = (
                session.query(MailboxAttachment)
                .filter(
                    MailboxAttachment.mailbox_id == mailbox.id,
                    MailboxAttachment.active_slot == 1,
                )
                .one()
            )
            lease = session.get(SessionLease, attachment.session_id)
            lease.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
    readiness = readiness_report()
    report = readiness["components"]["durable_mail"]["detail"]
    assert readiness["status"] == "degraded"
    assert report["state"] == "degraded"
    assert report["issue_count"] == 1
    assert report["reasons"] == [local_problem]
    assert report["smtp"]["state"] == "blocked"


@pytest.mark.parametrize("fallback", [False, True])
def test_notification_stall_and_pull_fallback_keep_existing_readiness_semantics(
    tmp_path,
    fallback,
):
    workspace = tmp_path / "notification-fallback"
    sender = _agent(workspace)
    recipient = _agent(workspace, tool="codex", mode="turn_boundary")
    send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "synthetic notification",
        f"notification-{uuid.uuid4().hex}",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    with SessionLocal() as session:
        notification = session.query(MailNotificationAttempt).one()
        notification.created_at = utc_now() - timedelta(minutes=6)
        if fallback:
            notification.status = "failed"
            notification.attempt = 1
            notification.started_at = notification.created_at
            notification.completed_at = utc_now()
            notification.error_code = "attachment_detached"
        session.commit()
    report = mailbox_health_report()
    assert report["state"] == ("ready" if fallback else "degraded")
    assert report["issue_count"] == (0 if fallback else 1)
    assert report["reasons"] == ([] if fallback else ["stalled_notification"])
    assert report["notification"]["closed_by_pull_fallback"] == int(fallback)
    assert report["smtp"]["state"] == "ready"
    assert report["smtp"]["delivery_blocked_reason"] is None
    assert report["smtp"]["issue_count"] == 0
    assert report["smtp"]["reasons"] == []

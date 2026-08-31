from __future__ import annotations

import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func

from brains.api.auth import mint_browser_token
from brains.authz.principal import CHANNEL_API, CHANNEL_BROWSER
from brains.authz.resolver import principal_for_operator_slug
from brains.config import settings
from brains.control.common import utc_now
from brains.control.durable_mail import send_mailbox_message
from brains.control.durable_mailbox import register_agent_mailbox
from brains.control.durable_smtp import (
    MailboxSmtpUnavailableError,
    MailboxSmtpValidationError,
    clear_smtp_destination,
    process_smtp_outbox,
    request_smtp_destination_verification,
    set_smtp_copy_mode,
    smtp_copy_status,
    verify_smtp_destination,
)
from brains.control.operators import ensure_admin_operator
from brains.control.secure_settings import get_scoped_value_in_transaction
from brains.control.sessions import start_session
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AuditLogEntry,
    Mailbox,
    MailDelivery,
    MailMessage,
    MailSmtpOutbox,
    OperatorMailboxSetting,
    SecureSetting,
)


class _FakeSMTP:
    sent: list[object] = []
    fail_before_send = False
    fail_during_send = False

    def __init__(self, _host, _port, timeout=None):
        if type(self).fail_before_send:
            raise OSError("synthetic connect failure")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        return None

    def starttls(self):
        return None

    def login(self, _user, _password):
        return None

    def send_message(self, message):
        if type(self).fail_during_send:
            raise __import__("smtplib").SMTPException("synthetic send failure")
        type(self).sent.append(message)


@pytest.fixture(autouse=True)
def _bootstrap(monkeypatch):
    init_db()
    ensure_admin_operator()
    with SessionLocal() as session:
        baseline_delivery_id = session.query(func.max(MailDelivery.id)).scalar() or 0
        session.query(MailSmtpOutbox).delete(synchronize_session=False)
        session.query(OperatorMailboxSetting).delete(synchronize_session=False)
        session.query(SecureSetting).filter(SecureSetting.name.like("mailbox.smtp.%")).delete(
            synchronize_session=False
        )
        session.commit()
    _FakeSMTP.sent = []
    _FakeSMTP.fail_before_send = False
    _FakeSMTP.fail_during_send = False
    monkeypatch.setattr("brains.control.mailer.smtplib.SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.invalid", raising=False)
    monkeypatch.setattr(settings, "smtp_port", 587, raising=False)
    monkeypatch.setattr(settings, "smtp_username", "", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "", raising=False)
    monkeypatch.setattr(settings, "smtp_from", "Brains <brains@example.invalid>", raising=False)
    monkeypatch.setattr(settings, "smtp_use_starttls", True, raising=False)
    yield
    with SessionLocal() as session:
        session.query(MailSmtpOutbox).delete(synchronize_session=False)
        session.query(OperatorMailboxSetting).delete(synchronize_session=False)
        session.query(SecureSetting).filter(SecureSetting.name.like("mailbox.smtp.%")).delete(
            synchronize_session=False
        )
        operator_mailboxes = session.query(Mailbox.id).filter(Mailbox.kind == "operator")
        session.query(MailDelivery).filter(
            MailDelivery.id > baseline_delivery_id,
            MailDelivery.recipient_mailbox_id.in_(operator_mailboxes),
        ).delete(synchronize_session=False)
        session.commit()


def _principal(channel: str = CHANNEL_BROWSER):
    principal = principal_for_operator_slug("admin")
    assert principal is not None
    return principal.with_channel(channel)


def _agent(path) -> dict:
    binding = f"binding-{uuid.uuid4().hex}"
    started = start_session(str(path), tool="opencode")
    mailbox = register_agent_mailbox(
        str(path),
        "opencode",
        f"opencode-{uuid.uuid4().hex}",
        started["session_id"],
        binding,
    )
    return {"session": started, "mailbox": mailbox, "binding": binding, "path": str(path)}


def _verification_code(message: object) -> str:
    match = re.search(r"\b([0-9]{6})\b", str(message.get_content()))
    assert match is not None
    return match.group(1)


def _verify_operator_destination() -> str:
    address = "operator:admin@brains"
    request_smtp_destination_verification(
        address,
        "human@example.invalid",
        principal=_principal(),
    )
    code = _verification_code(_FakeSMTP.sent[-1])
    verified = verify_smtp_destination(address, code, principal=_principal())
    assert verified["copy_mode"] == "notification"
    return address


def test_destination_is_encrypted_redacted_and_requires_human_verification() -> None:
    address = "operator:admin@brains"
    requested = request_smtp_destination_verification(
        address,
        "human@example.invalid",
        principal=_principal(),
    )
    assert requested["destination_hint"] != "human@example.invalid"
    assert "human@example.invalid" not in repr(requested)
    with pytest.raises(MailboxSmtpUnavailableError):
        smtp_copy_status(address, principal=_principal(CHANNEL_API))
    code = _verification_code(_FakeSMTP.sent[-1])
    verified = verify_smtp_destination(address, code, principal=_principal())
    assert verified["destination_state"] == "verified"

    with SessionLocal() as session:
        setting = session.query(OperatorMailboxSetting).one()
        raw = get_scoped_value_in_transaction(
            session,
            setting.smtp_destination_ref,
            admin_key=settings.api_key,
        )
        assert raw is not None
        assert "human@example.invalid" in raw
        assert code not in raw
        encrypted = session.get(SecureSetting, setting.smtp_destination_ref)
        assert encrypted is not None
        assert b"human@example.invalid" not in bytes(encrypted.ciphertext)
        audit_text = "\n".join(
            row.payload_json
            for row in session.query(AuditLogEntry)
            .filter(AuditLogEntry.action.like("mailbox.smtp%"))
            .all()
        )
        assert "human@example.invalid" not in audit_text


def test_notification_copy_is_body_free_and_local_delivery_stays_authoritative(tmp_path) -> None:
    operator_address = _verify_operator_destination()
    sender = _agent(tmp_path)
    secret_body = "mail body must not leave Brains by default"
    sent = send_mailbox_message(
        sender["path"],
        [operator_address],
        "Private subject",
        f"smtp-{uuid.uuid4().hex}",
        body=secret_body,
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    with SessionLocal() as session:
        row = (
            session.query(MailSmtpOutbox)
            .join(MailDelivery, MailDelivery.id == MailSmtpOutbox.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(MailMessage.message_id == sent["message_id"])
            .one()
        )
        assert row.copy_mode == "notification"
        delivery = session.get(MailDelivery, row.delivery_id)
        assert delivery is not None and delivery.read_at is None

    result = process_smtp_outbox(worker_id="test-notification")
    copied = _FakeSMTP.sent[-1]
    assert result["sent"] == 1
    assert secret_body not in str(copied)
    assert sent["subject"] not in str(copied)
    assert str(copied["Subject"]) == "New Brains mailbox message"
    assert str(copied["Message-ID"]).endswith("@brains.local>")
    with SessionLocal() as session:
        row = (
            session.query(MailSmtpOutbox)
            .join(MailDelivery, MailDelivery.id == MailSmtpOutbox.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(MailMessage.message_id == sent["message_id"])
            .one()
        )
        delivery = session.get(MailDelivery, row.delivery_id)
        assert row.status == "sent"
        assert delivery is not None and delivery.read_at is None


def test_smtp_outbox_collision_never_rolls_back_local_delivery(tmp_path, monkeypatch) -> None:
    import brains.control.durable_mail as durable_mail

    operator_address = _verify_operator_destination()
    sender = _agent(tmp_path)
    first = send_mailbox_message(
        sender["path"],
        [operator_address],
        "First local delivery",
        f"smtp-{uuid.uuid4().hex}",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    with SessionLocal() as session:
        existing_outbox_id = (
            session.query(MailSmtpOutbox)
            .join(MailDelivery, MailDelivery.id == MailSmtpOutbox.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(MailMessage.message_id == first["message_id"])
            .one()
            .outbox_id
        )

    original_public_id = durable_mail._public_id
    monkeypatch.setattr(
        durable_mail,
        "_public_id",
        lambda prefix: existing_outbox_id if prefix == "smtp" else original_public_id(prefix),
    )
    second = send_mailbox_message(
        sender["path"],
        [operator_address],
        "Second local delivery",
        f"smtp-{uuid.uuid4().hex}",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    assert second["created"] is True
    assert second["deliveries"][0]["state"] == "accepted"
    with SessionLocal() as session:
        message = (
            session.query(MailMessage).filter(MailMessage.message_id == second["message_id"]).one()
        )
        delivery = session.query(MailDelivery).filter(MailDelivery.message_id == message.id).one()
        assert (
            session.query(MailSmtpOutbox).filter(MailSmtpOutbox.delivery_id == delivery.id).count()
            == 0
        )


def test_full_body_requires_explicit_consent_and_revocation_cancels_queue(tmp_path) -> None:
    operator_address = _verify_operator_destination()
    with pytest.raises(MailboxSmtpValidationError, match="explicit consent"):
        set_smtp_copy_mode(operator_address, "full_body", principal=_principal())
    status = set_smtp_copy_mode(
        operator_address,
        "full_body",
        consent_full_body=True,
        principal=_principal(),
    )
    assert status["full_body_consented_at"] is not None

    sender = _agent(tmp_path)
    copied = send_mailbox_message(
        sender["path"],
        [operator_address],
        "Full copy",
        f"smtp-{uuid.uuid4().hex}",
        body="explicitly consented body",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    assert process_smtp_outbox(worker_id="test-full-body")["sent"] == 1
    message = _FakeSMTP.sent[-1]
    assert "explicitly consented body" in str(message)
    assert "Full copy" in str(message)
    assert str(message["Subject"]) == "Brains mailbox message"

    sent = send_mailbox_message(
        sender["path"],
        [operator_address],
        "Cancelled copy",
        f"smtp-{uuid.uuid4().hex}",
        body="must remain local",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    cleared = clear_smtp_destination(operator_address, principal=_principal())
    assert cleared["destination_state"] == "unconfigured"
    assert process_smtp_outbox(worker_id="test-revoked")["claimed"] == 0
    with SessionLocal() as session:
        assert (
            session.query(MailSmtpOutbox)
            .join(MailDelivery, MailDelivery.id == MailSmtpOutbox.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(MailMessage.message_id == sent["message_id"])
            .one()
            .status
            == "cancelled"
        )
        audit_text = "\n".join(
            row.payload_json
            for row in session.query(AuditLogEntry)
            .filter(AuditLogEntry.action.like("mailbox.smtp%"))
            .all()
        )
        assert "explicitly consented body" not in audit_text
        assert copied["subject"] not in audit_text


def test_copy_policy_cannot_change_while_a_send_claim_is_live(tmp_path) -> None:
    from brains.control.durable_smtp import _claim_outbox

    operator_address = _verify_operator_destination()
    sender = _agent(tmp_path)
    sent = send_mailbox_message(
        sender["path"],
        [operator_address],
        "Claimed copy",
        f"smtp-{uuid.uuid4().hex}",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    assert _claim_outbox(utc_now(), "held-worker") is not None

    with pytest.raises(MailboxSmtpValidationError, match="while an SMTP copy is sending"):
        set_smtp_copy_mode(operator_address, "disabled", principal=_principal())
    with pytest.raises(MailboxSmtpValidationError, match="while an SMTP copy is sending"):
        clear_smtp_destination(operator_address, principal=_principal())
    with pytest.raises(MailboxSmtpValidationError, match="while an SMTP copy is sending"):
        request_smtp_destination_verification(
            operator_address,
            "replacement@example.invalid",
            principal=_principal(),
        )
    with SessionLocal() as session:
        row = (
            session.query(MailSmtpOutbox)
            .join(MailDelivery, MailDelivery.id == MailSmtpOutbox.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(MailMessage.message_id == sent["message_id"])
            .one()
        )
        assert row.status == "sending"
        assert row.lease_owner == "held-worker"
        row.status = "uncertain"
        row.lease_owner = None
        row.lease_expires_at = None
        row.error_code = "test_cleanup"
        session.commit()


def test_retry_claim_is_single_and_send_stage_failure_is_uncertain(tmp_path) -> None:
    operator_address = _verify_operator_destination()
    sender = _agent(tmp_path)
    sent = send_mailbox_message(
        sender["path"],
        [operator_address],
        "Retry copy",
        f"smtp-{uuid.uuid4().hex}",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    _FakeSMTP.fail_before_send = True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda index: process_smtp_outbox(worker_id=f"worker-{index}"), range(2))
        )
    assert sum(result["claimed"] for result in results) == 1
    with SessionLocal() as session:
        row = (
            session.query(MailSmtpOutbox)
            .join(MailDelivery, MailDelivery.id == MailSmtpOutbox.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(MailMessage.message_id == sent["message_id"])
            .one()
        )
        assert row.status == "retry"
        row.next_attempt_at = utc_now() - timedelta(seconds=1)
        session.commit()

    _FakeSMTP.fail_before_send = False
    _FakeSMTP.fail_during_send = True
    result = process_smtp_outbox(worker_id="worker-uncertain")
    assert result["uncertain"] == 1
    with SessionLocal() as session:
        assert (
            session.query(MailSmtpOutbox)
            .join(MailDelivery, MailDelivery.id == MailSmtpOutbox.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(MailMessage.message_id == sent["message_id"])
            .one()
            .status
            == "uncertain"
        )


def test_expired_send_claim_is_uncertain_and_never_retried(tmp_path) -> None:
    from brains.control.durable_smtp import _claim_outbox

    operator_address = _verify_operator_destination()
    sender = _agent(tmp_path)
    sent = send_mailbox_message(
        sender["path"],
        [operator_address],
        "Expired claim",
        f"smtp-{uuid.uuid4().hex}",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    claimed_at = utc_now()
    assert _claim_outbox(claimed_at, "expired-worker") is not None

    result = process_smtp_outbox(
        worker_id="replacement-worker",
        now=claimed_at + timedelta(minutes=2),
    )
    assert result["claimed"] == 0
    assert _FakeSMTP.sent[-1]["Subject"] == "Verify your Brains mailbox email"
    with SessionLocal() as session:
        row = (
            session.query(MailSmtpOutbox)
            .join(MailDelivery, MailDelivery.id == MailSmtpOutbox.delivery_id)
            .join(MailMessage, MailMessage.id == MailDelivery.message_id)
            .filter(MailMessage.message_id == sent["message_id"])
            .one()
        )
        assert row.status == "uncertain"
        assert row.error_code == "lease_expired_after_send_claim"


def test_browser_smtp_routes_never_echo_destination(auth_headers) -> None:
    client = TestClient(app)
    address = "operator:admin@brains"
    raw = client.get(
        "/v1/operator/mailboxes/smtp", params={"address": address}, headers=auth_headers
    )
    assert raw.status_code == 404
    client.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    requested = client.post(
        "/v1/operator/mailboxes/smtp/destination",
        params={"address": address},
        json={"destination": "browser@example.invalid"},
    )
    assert requested.status_code == 200, requested.text
    assert "browser@example.invalid" not in requested.text
    code = _verification_code(_FakeSMTP.sent[-1])
    verified = client.post(
        "/v1/operator/mailboxes/smtp/verify",
        params={"address": address},
        json={"code": code},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["copy_mode"] == "notification"


def test_generic_secure_setting_projection_excludes_mailbox_destinations() -> None:
    from brains.control.secure_settings import status, values

    _verify_operator_destination()
    assert all(not name.startswith("mailbox.smtp.") for name in values(settings.api_key))
    assert all(
        not name.startswith("mailbox.smtp.") for name in status(settings.api_key)["settings"]
    )


def test_superseded_verification_returns_current_redacted_state(monkeypatch) -> None:
    import brains.control.durable_smtp as durable_smtp

    address = "operator:admin@brains"
    replacement = {"value": ""}
    nested = {"active": False}
    original_send = durable_smtp.send_email

    def supersede(*args, **kwargs):
        if not nested["active"]:
            nested["active"] = True
            result = request_smtp_destination_verification(
                address,
                "newer@example.invalid",
                principal=_principal(),
            )
            replacement["value"] = result["destination_hint"]
        return original_send(*args, **kwargs)

    monkeypatch.setattr(durable_smtp, "send_email", supersede)
    result = request_smtp_destination_verification(
        address,
        "older@example.invalid",
        principal=_principal(),
    )

    assert result["superseded"] is True
    assert result["destination_state"] == "pending"
    assert result["destination_hint"] == replacement["value"]
    assert "newer@example.invalid" not in repr(result)

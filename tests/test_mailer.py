"""Outbound email subsystem: config gating, send path, ASK courtesy copy."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from brains.config import reload_settings, settings
from brains.control.mailer import MailerError, mailer_status, notify_ask, send_email


class _FakeSMTP:
    """Captures one send; class attribute shared for assertions."""

    sent: list[tuple[str, str, str, object]] = []  # (host, from, to, msg)
    fail: bool = False

    def __init__(self, host, port, timeout=None):
        _FakeSMTP.sent.append((host, "", "", None))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, password):
        assert password == "s3cret", "password must reach SMTP but never logs"

    def send_message(self, msg):
        if _FakeSMTP.fail:
            raise __import__("smtplib").SMTPException("boom")
        host = _FakeSMTP.sent[-1][0]
        _FakeSMTP.sent[-1] = (host, str(msg["From"]), str(msg["To"]), msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.sent = []
    _FakeSMTP.fail = False
    monkeypatch.setattr("brains.control.mailer.smtplib.SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "smtp_port", 587, raising=False)
    monkeypatch.setattr(settings, "smtp_username", "brains@example.com", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "s3cret", raising=False)
    monkeypatch.setattr(settings, "smtp_from", "Brains <brains@example.com>", raising=False)
    monkeypatch.setattr(settings, "operator_notify_email", "ops@example.com", raising=False)
    yield _FakeSMTP


def test_mailer_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BRAINS_SMTP_HOST", raising=False)
    status = mailer_status()
    assert status["enabled"] is False
    with pytest.raises(MailerError, match="mailer is disabled"):
        send_email("a@b.c", "s", "b")


def test_send_email_via_smtp_and_audit(fake_smtp):
    out = send_email("dest@example.com", "Subject line", "Body text")
    assert out == {"sent": True, "to": "dest@example.com", "subject": "Subject line"}
    assert len(fake_smtp.sent) == 1
    assert fake_smtp.sent[0][0] == "smtp.example.com"
    assert fake_smtp.sent[0][2] == "dest@example.com"


def test_smtp_failure_raises_safe_message_without_password(fake_smtp):
    _FakeSMTP.fail = True
    with pytest.raises(MailerError) as excinfo:
        send_email("dest@example.com", "s", "b")
    assert "s3cret" not in str(excinfo.value)
    assert "smtp.example.com" not in str(excinfo.value)
    assert excinfo.value.delivery_uncertain is True


def test_notify_ask_sends_copy_but_never_blocks_on_failure(fake_smtp):
    ok = notify_ask("ASK-1", "Pick a color")
    assert ok["sent"] is True
    assert any("ASK-1" in str(msg["Subject"]) for _, _, _, msg in fake_smtp.sent)

    _FakeSMTP.fail = True
    degraded = notify_ask("ASK-2", "Pick another")
    assert degraded["sent"] is False
    assert degraded.get("error") == "MailerError"


def test_ask_filing_triggers_email_notification(tmp_path, fake_smtp):
    from brains.control.decisions import file_decision_request

    result = file_decision_request(str(tmp_path), "Need operator call", body="why")
    code = result["code"]
    subjects = [str(msg["Subject"]) for _, _, _, msg in fake_smtp.sent if msg]
    assert any(f"[brains ASK {code}]" in s for s in subjects)
    assert any("ops@example.com" in to for _, _, to, _ in fake_smtp.sent)


def test_mail_status_endpoint_is_redacted(monkeypatch):
    from brains.main import app

    client = TestClient(app)
    response = client.get("/health")  # liveness stays open; status via CLI/MCP only
    assert response.status_code == 200
    status = mailer_status()
    assert "has_credentials" in status and "smtp_password" not in status


def test_overlay_env_ref_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAINS_RUNTIME_OVERLAY", raising=False)
    overlay = tmp_path / "ov.yaml"
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay))
    reload_settings()
    assert settings.smtp_host == ""

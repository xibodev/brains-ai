"""Outbound email subsystem: config gating, send path, ASK courtesy copy."""

from __future__ import annotations

import json
import smtplib

import pytest
from fastapi.testclient import TestClient

from brains.config import Settings, load_settings, reload_settings, settings
from brains.control.mailer import MailerError, mailer_status, notify_ask, send_email


class _FakeSMTP:
    """Captures one send; class attribute shared for assertions."""

    sent: list[tuple[str, str, str, object]] = []  # (host, from, to, msg)
    fail: bool = False
    error: Exception | None = None
    quit_error: bool = False
    connect_error: bool = False
    refused: dict = {}

    def __init__(self, host, port, timeout=None):
        _FakeSMTP.sent.append((host, "", "", None))
        if _FakeSMTP.connect_error:
            raise OSError("private connection details")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if _FakeSMTP.quit_error:
            raise OSError("private quit details")
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, password):
        assert password == "s3cret", "password must reach SMTP but never logs"

    def send_message(self, msg):
        if _FakeSMTP.error:
            raise _FakeSMTP.error
        if _FakeSMTP.fail:
            raise __import__("smtplib").SMTPException("boom")
        host = _FakeSMTP.sent[-1][0]
        _FakeSMTP.sent[-1] = (host, str(msg["From"]), str(msg["To"]), msg)
        return _FakeSMTP.refused


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.sent = []
    _FakeSMTP.fail = False
    _FakeSMTP.error = None
    _FakeSMTP.quit_error = False
    _FakeSMTP.connect_error = False
    _FakeSMTP.refused = {}
    monkeypatch.setattr("brains.control.mailer.smtplib.SMTP", _FakeSMTP)
    monkeypatch.setattr("brains.control.mailer._refresh_secure_settings", lambda: None)
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", False)
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


def test_notify_ask_sends_copy_but_never_blocks_on_failure(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    ok = notify_ask("ASK-1", "Pick a color")
    assert ok["sent"] is True
    assert ok["status"] == "smtp_accepted"
    assert any("ASK-1" in str(msg["Subject"]) for _, _, _, msg in fake_smtp.sent)

    _FakeSMTP.fail = True
    degraded = notify_ask("ASK-2", "Pick another")
    assert degraded["sent"] is False
    assert degraded["status"] == "uncertain"
    assert degraded["error"] == "smtp_uncertain"


def test_ask_filing_triggers_email_notification(tmp_path, fake_smtp, monkeypatch):
    from brains.control.decisions import file_decision_request

    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    result = file_decision_request(str(tmp_path), "Need operator call", body="why")
    code = result["code"]
    subjects = [str(msg["Subject"]) for _, _, _, msg in fake_smtp.sent if msg]
    assert any(f"[brains ASK {code}]" in s for s in subjects)
    assert any("ops@example.com" in to for _, _, to, _ in fake_smtp.sent)
    assert result["notification"] == {"status": "smtp_accepted", "attempted": True, "sent": True}


def test_credentials_are_not_consent(fake_smtp, monkeypatch):
    def forbidden():
        pytest.fail("disabled notifications must not refresh encrypted settings")

    monkeypatch.setattr("brains.control.mailer._refresh_secure_settings", forbidden)
    assert notify_ask("ASK-1", "Decision") == {
        "status": "disabled",
        "attempted": False,
        "sent": False,
    }
    assert fake_smtp.sent == []


def test_consent_is_environment_only(tmp_path, monkeypatch):
    from brains.config import _SECURE_SETTING_FIELDS, ADMIN_EDITABLE_KEYS
    from brains.control.secure_settings import ALLOWED_NAMES

    flag = "ask_email_notifications_enabled"
    assert flag not in ADMIN_EDITABLE_KEYS
    assert flag not in _SECURE_SETTING_FIELDS
    assert flag not in ALLOWED_NAMES
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BRAINS_ASK_EMAIL_NOTIFICATIONS_ENABLED", raising=False)
    monkeypatch.setattr("brains.config.load_secrets_env", lambda: None)
    assert Settings(_env_file=None).ask_email_notifications_enabled is False
    config = tmp_path / "config.yaml"
    config.write_text(f"{flag}: true\n", encoding="utf-8")
    monkeypatch.setenv("BRAINS_CONFIG", str(config))
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(config))
    assert load_settings().ask_email_notifications_enabled is False
    monkeypatch.setenv("BRAINS_ASK_EMAIL_NOTIFICATIONS_ENABLED", "true")
    assert load_settings().ask_email_notifications_enabled is True
    monkeypatch.setenv("BRAINS_ASK_EMAIL_NOTIFICATIONS_ENABLED", "false")
    assert load_settings().ask_email_notifications_enabled is False


def test_refresh_precedes_owner_read_and_exports_minimal_content(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    monkeypatch.setattr(settings, "operator_notify_email", "")
    calls = []

    def refresh():
        calls.append("refresh")
        monkeypatch.setattr(settings, "operator_notify_email", "owner@example.com")

    monkeypatch.setattr("brains.control.mailer._refresh_secure_settings", refresh)
    result = notify_ask("ASK-55", "Choose", "private-workspace")
    assert result["status"] == "smtp_accepted"
    assert calls == ["refresh"]
    assert len(fake_smtp.sent) == 1
    msg = fake_smtp.sent[0][3]
    assert str(msg["To"]) == "owner@example.com"
    assert msg.get_content() == "ASK ASK-55: Choose\n\nhttp://127.0.0.1:8787/app\n"
    assert "private-workspace" not in msg.as_string()
    assert msg["Cc"] is None and msg["Bcc"] is None


def test_encrypted_owner_config_preserved_and_requires_new_opt_in(fake_smtp, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from brains.api.admin_key import ensure_admin_key
    from brains.control import secure_settings
    from brains.storage.models import SecureSetting

    engine = create_engine("sqlite://")
    SecureSetting.__table__.create(engine)
    local_session = sessionmaker(bind=engine)
    monkeypatch.setattr(secure_settings, "SessionLocal", local_session)
    monkeypatch.setattr(secure_settings, "init_db", lambda: None)
    monkeypatch.delenv("BRAINS_OPERATOR_NOTIFY_EMAIL", raising=False)
    monkeypatch.setattr(settings, "operator_notify_email", "")
    secure_settings.set_value(
        "operator_notify_email", "owner@example.com", admin_key=settings.api_key
    )
    with local_session() as db:
        before = bytes(db.get(SecureSetting, "operator_notify_email").ciphertext)
    monkeypatch.setattr(
        "brains.control.mailer._refresh_secure_settings",
        lambda: ensure_admin_key(print_banner=False),
    )
    assert notify_ask("ASK-1", "Choose")["status"] == "disabled"
    assert fake_smtp.sent == [] and settings.operator_notify_email == ""
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    assert notify_ask("ASK-2", "Choose")["status"] == "smtp_accepted"
    assert fake_smtp.sent[0][2] == "owner@example.com"
    with local_session() as db:
        assert bytes(db.get(SecureSetting, "operator_notify_email").ciphertext) == before
    engine.dispose()


@pytest.mark.parametrize("address", ["", "invalid", "a@b.test,c@d.test", "a@b.test\nBcc: c@d.test"])
def test_missing_or_multiple_owners_never_send(address, fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    monkeypatch.setattr(settings, "operator_notify_email", address)
    result = notify_ask("ASK-1", "Choose")
    assert result["status"] == "failed"
    assert result["attempted"] is False
    assert fake_smtp.sent == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (smtplib.SMTPRecipientsRefused({"owner@example.com": (550, b"private")}), "failed"),
        (smtplib.SMTPSenderRefused(550, b"private", "sender@example.com"), "failed"),
        (smtplib.SMTPDataError(550, b"private"), "failed"),
        (smtplib.SMTPServerDisconnected("private"), "uncertain"),
        (TimeoutError("private"), "uncertain"),
    ],
)
def test_smtp_outcomes_are_truthful_and_redacted(error, status, fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    fake_smtp.error = error
    events = []
    monkeypatch.setattr(
        "brains.control.mailer.append_event", lambda *a, **kw: events.append((a, kw))
    )
    result = notify_ask(
        "ASK-1", "private title", "private workspace", session_id="session", workspace_id=42
    )
    assert result["status"] == status
    assert result["attempted"] is True and result["sent"] is False
    assert len(fake_smtp.sent) == 1
    assert [kw["metadata"]["status"] for _, kw in events] == ["attempted", status]
    assert all(kw["workspace_id"] == 42 and kw["session_id"] == "session" for _, kw in events)
    assert "private" not in json.dumps([result, events])
    assert "example.com" not in json.dumps([result, events])


def test_smtp_refusal_return_is_not_acceptance(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    fake_smtp.refused = {"ops@example.com": (550, b"private")}
    assert notify_ask("ASK-1", "Choose")["status"] == "failed"


def test_connect_failure_and_quit_after_acceptance(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    fake_smtp.connect_error = True
    assert notify_ask("ASK-1", "Choose")["status"] == "failed"
    fake_smtp.connect_error = False
    fake_smtp.quit_error = True
    result = notify_ask("ASK-2", "Choose")
    assert result["status"] == "smtp_accepted" and result["sent"] is True


def test_audit_failure_before_send_fails_closed(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)

    def fail(*a, **kw):
        raise RuntimeError("private database details")

    monkeypatch.setattr("brains.control.mailer.append_event", fail)
    result = notify_ask("ASK-1", "Choose")
    assert result["status"] == "failed" and result["attempted"] is False
    assert result["audit_error"] == "outcome_record_failed"
    assert fake_smtp.sent == []


def test_terminal_audit_failure_preserves_acceptance(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)

    def record(*a, **kw):
        if kw["metadata"]["status"] != "attempted":
            raise RuntimeError("private database details")

    monkeypatch.setattr("brains.control.mailer.append_event", record)
    result = notify_ask("ASK-1", "Choose")
    assert result["status"] == "smtp_accepted" and result["sent"] is True
    assert result["audit_error"] == "outcome_record_failed"
    assert len(fake_smtp.sent) == 1


@pytest.mark.parametrize("failure", ["refresh", "smtp", "audit", "hook"])
def test_filing_survives_notification_failures(tmp_path, fake_smtp, monkeypatch, failure):
    from brains.control.decisions import file_decision_request
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest

    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)

    def fail(*a, **kw):
        raise RuntimeError("private details")

    if failure == "refresh":
        monkeypatch.setattr("brains.control.mailer._refresh_secure_settings", fail)
    elif failure == "smtp":
        fake_smtp.fail = True
    elif failure == "audit":
        monkeypatch.setattr("brains.control.mailer.append_event", fail)
    else:
        monkeypatch.setattr("brains.control.mailer.notify_ask", fail)
    result = file_decision_request(
        str(tmp_path), "Choose", body="private body", proposed_answer="private answer"
    )
    assert result["status"] == "open"
    notification = result["notification"]
    assert notification["status"] == ("uncertain" if failure in {"smtp", "hook"} else "failed")
    assert notification["attempted"] is (None if failure == "hook" else failure == "smtp")
    assert notification["sent"] is False
    assert "private" not in json.dumps(notification)
    with SessionLocal() as db:
        row = db.query(ApprovalRequest).filter_by(code=result["code"]).one()
        assert row.status == "open" and row.body == "private body"
        assert row.proposed_answer == "private answer"


def test_filing_records_attribution_without_exporting_context(tmp_path, fake_smtp, monkeypatch):
    from brains.control.decisions import file_decision_request
    from brains.control.sessions import start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest, Event

    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    sid = start_session(str(tmp_path), tool="codex")["session_id"]
    result = file_decision_request(str(tmp_path), "Choose", body="private body", session_id=sid)
    with SessionLocal() as db:
        row = db.query(ApprovalRequest).filter_by(code=result["code"]).one()
        events = db.query(Event).filter_by(kind="decision_email_notification", session_id=sid).all()
        assert [json.loads(e.metadata_json)["status"] for e in events] == [
            "attempted",
            "smtp_accepted",
        ]
        assert all(e.workspace_id == row.workspace_id for e in events)
    assert len(fake_smtp.sent) == 1
    message = fake_smtp.sent[0][3].as_string()
    assert "private body" not in message and sid not in message and str(tmp_path) not in message


def test_status_does_not_send_when_opted_in(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    mailer_status()
    assert fake_smtp.sent == []


@pytest.mark.parametrize("title", ["😀" * 100_000, "long private title " * 10_000])
def test_notification_title_is_bounded_without_changing_durable_ask(
    title, tmp_path, fake_smtp, monkeypatch
):
    from brains.control.decisions import file_decision_request
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest, Event

    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    result = file_decision_request(str(tmp_path), title, body="private context")
    assert result["notification"]["title_truncated"] is True
    msg = fake_smtp.sent[0][3]
    subject = str(msg["Subject"])
    preview = subject.removeprefix(f"[brains ASK {result['code']}] ")
    assert len(preview) == 200 and preview.endswith("…")
    assert len(preview.encode("utf-8")) <= 800
    assert len(subject.encode("utf-8")) <= 850
    assert len(msg.get_content().encode("utf-8")) <= 900
    assert len(msg.as_bytes()) < 4000
    assert "private context" not in msg.as_string()
    with SessionLocal() as db:
        row = db.query(ApprovalRequest).filter_by(code=result["code"]).one()
        assert row.title == title and row.body == "private context"
        events = (
            db.query(Event)
            .filter_by(kind="decision_email_notification", workspace_id=row.workspace_id)
            .all()
        )
        assert all(title not in e.message and title not in e.metadata_json for e in events)


@pytest.mark.parametrize("title", ["  Pick\t\t a\u2003color  ", "x" * 200])
def test_notification_normalizes_whitespace_and_preserves_title_at_limit(
    title, fake_smtp, monkeypatch
):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    result = notify_ask("ASK-" + "a" * 28, title)
    assert result["status"] == "smtp_accepted"
    assert "title_truncated" not in result
    assert str(fake_smtp.sent[0][3]["Subject"]).endswith(" ".join(title.split()))


@pytest.mark.parametrize("title", ["private\rBcc: other@example.com", "x" * 1000 + "\nprivate"])
def test_invalid_title_never_sends_but_ask_and_attribution_survive(
    title, tmp_path, fake_smtp, monkeypatch
):
    from brains.control.decisions import file_decision_request
    from brains.control.sessions import start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest, Event

    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    sid = start_session(str(tmp_path), tool="codex")["session_id"]
    result = file_decision_request(str(tmp_path), title, body="private context", session_id=sid)
    assert result["notification"] == {
        "status": "failed",
        "attempted": False,
        "sent": False,
        "error": "notification_failed",
    }
    assert fake_smtp.sent == []
    with SessionLocal() as db:
        row = db.query(ApprovalRequest).filter_by(code=result["code"]).one()
        assert row.title == title and row.body == "private context" and row.status == "open"
        event = db.query(Event).filter_by(kind="decision_email_notification", session_id=sid).one()
        assert event.workspace_id == row.workspace_id
        assert json.loads(event.metadata_json) == {"code": result["code"], "status": "failed"}
        assert "private" not in event.message + event.metadata_json


@pytest.mark.parametrize(
    "code", ["ASK-", "ASK-" + "a" * 29, "ASK-1\n", "ASK-1\r", "private-code", "ASK-😀"]
)
def test_notification_rejects_invalid_code_without_recording_it(code, fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "ask_email_notifications_enabled", True)
    events = []
    monkeypatch.setattr(
        "brains.control.mailer.append_event", lambda *a, **kw: events.append((a, kw))
    )
    result = notify_ask(code, "Choose", session_id="session", workspace_id=42)
    assert result["status"] == "failed" and result["attempted"] is False
    assert fake_smtp.sent == []
    assert events[0][1]["metadata"] == {"code": None, "status": "failed"}
    assert events[0][1]["session_id"] == "session" and events[0][1]["workspace_id"] == 42


def test_filing_notification_allowlist_drops_private_hook_fields(tmp_path, fake_smtp, monkeypatch):
    from brains.control.decisions import file_decision_request

    monkeypatch.setattr(
        "brains.control.mailer.notify_ask",
        lambda *a, **kw: {
            "status": "failed",
            "attempted": False,
            "sent": False,
            "error": "private error",
            "audit_error": "private audit error",
            "to": "private@example.com",
            "host": "private host",
            "title_truncated": True,
        },
    )
    assert file_decision_request(str(tmp_path), "Choose")["notification"] == {
        "status": "failed",
        "attempted": False,
        "sent": False,
        "title_truncated": True,
    }


def test_mcp_filing_returns_disabled_notification(tmp_path, fake_smtp):
    from brains.mcp.tools import file_decision_request_tool

    result = file_decision_request_tool(str(tmp_path), "Choose")
    assert result["code"].startswith("ASK-") and result["status"] == "open"
    assert result["workspace"]
    assert result["notification"] == {"status": "disabled", "attempted": False, "sent": False}
    assert fake_smtp.sent == []


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

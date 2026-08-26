"""Encrypted configuration, API redaction, re-keying, and email UI contract."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from brains.api.admin_key import rotate_admin_key
from brains.config import settings
from brains.control import secure_settings
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.models import SecureSetting

AUTH = {"Authorization": "Bearer local-dev-key"}


@pytest.fixture(autouse=True)
def clean_secure_settings(monkeypatch):
    for name in secure_settings.ALLOWED_NAMES:
        secure_settings.clear_value(name)
    monkeypatch.setattr(settings, "api_key", "local-dev-key")
    yield
    for name in secure_settings.ALLOWED_NAMES:
        secure_settings.clear_value(name)
    monkeypatch.setattr(settings, "api_key", "local-dev-key")


def test_ciphertext_at_rest_and_wrong_key_refusal():
    secure_settings.set_value("smtp_password", "super-secret", admin_key="key-one")
    with SessionLocal() as session:
        row = session.get(SecureSetting, "smtp_password")
        assert row is not None
        assert b"super-secret" not in bytes(row.ciphertext)
        assert len(row.nonce) == 12
        assert len(row.salt) == 16

    assert secure_settings.get_value("smtp_password", admin_key="key-one") == "super-secret"
    with pytest.raises(secure_settings.SecureSettingError):
        secure_settings.get_value("smtp_password", admin_key="wrong-key")


def test_admin_key_rotation_rekeys_before_replacing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAINS_API_KEY", raising=False)
    old_key = "old-admin-key"
    secure_settings.set_value("smtp_password", "secret", admin_key=old_key)
    monkeypatch.setattr(settings, "api_key", old_key)
    monkeypatch.setattr("brains.api.admin_key.admin_key_path", lambda: tmp_path / "admin-key")
    monkeypatch.setattr("brains.api.admin_key._supersede_admin_key", lambda *_args: None)

    new_key = rotate_admin_key()
    assert new_key != old_key
    assert secure_settings.get_value("smtp_password", admin_key=new_key) == "secret"
    with pytest.raises(secure_settings.SecureSettingError):
        secure_settings.get_value("smtp_password", admin_key=old_key)


def test_admin_key_rotation_refuses_environment_managed_key(monkeypatch):
    monkeypatch.setenv("BRAINS_API_KEY", "external")
    with pytest.raises(RuntimeError, match="authoritative store"):
        rotate_admin_key()


def test_email_configuration_api_never_returns_secret(monkeypatch):
    client = TestClient(app)
    put = client.put(
        "/v1/admin/configuration/email/smtp_password",
        json={"value": "api-secret"},
        headers=AUTH,
    )
    assert put.status_code == 200, put.text
    response = client.get("/v1/admin/configuration/email", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["secure"]["settings"]["smtp_password"] == {
        "set": True,
        "secret": True,
        "source": "encrypted",
    }
    assert "api-secret" not in response.text

    cleared = client.delete("/v1/admin/configuration/email/smtp_password", headers=AUTH)
    assert cleared.status_code == 200
    assert cleared.json()["set"] is False


def test_email_test_route_uses_effective_mailer(monkeypatch):
    client = TestClient(app)
    sent: list[str] = []

    monkeypatch.setattr(
        "brains.control.mailer.send_email",
        lambda to, subject, body: sent.append(to) or {"sent": True, "to": to, "subject": subject},
    )
    response = client.post(
        "/v1/admin/configuration/email/test",
        json={"to": "operator@example.com"},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert sent == ["operator@example.com"]


def test_configuration_routes_require_install_admin():
    client = TestClient(app)
    assert client.get("/v1/admin/configuration/email").status_code == 401
    assert client.get("/v1/admin/coordination/overview").status_code == 401


def test_integration_secret_api_is_redacted(monkeypatch):
    client = TestClient(app)
    name = "BRAINS_SLACK_BOT_TOKEN"
    response = client.put(
        f"/v1/admin/configuration/secrets/{name}",
        json={"value": "xoxb-secret"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    status = client.get("/v1/admin/configuration/secrets", headers=AUTH)
    assert status.status_code == 200
    assert status.json()["settings"][name]["set"] is True
    assert "xoxb-secret" not in status.text
    assert __import__("os").environ[name] == "xoxb-secret"
    cleared = client.delete(f"/v1/admin/configuration/secrets/{name}", headers=AUTH)
    assert cleared.status_code == 200
    assert name not in __import__("os").environ


def test_provider_secret_applies_to_settings_and_clears(monkeypatch):
    import os

    client = TestClient(app)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = client.put(
        "/v1/admin/configuration/secrets/OPENAI_API_KEY",
        json={"value": "provider-secret"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert settings.openai_compatible_api_key == "provider-secret"
    status = client.get("/v1/admin/configuration/secrets", headers=AUTH).json()
    assert status["settings"]["OPENAI_API_KEY"]["source"] == "encrypted"
    cleared = client.delete("/v1/admin/configuration/secrets/OPENAI_API_KEY", headers=AUTH)
    assert cleared.status_code == 200
    assert settings.openai_compatible_api_key is None
    assert "OPENAI_API_KEY" not in os.environ


def test_general_configuration_route_reuses_validated_overlay_writer(monkeypatch):
    client = TestClient(app)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "brains.admin.service.write_overlay",
        lambda updates: captured.update(updates) or updates,
    )
    monkeypatch.setattr(
        "brains.admin.service.current_config_view",
        lambda: {"models": {}, "rate_limit_per_minute": 60},
    )
    response = client.put(
        "/v1/admin/configuration/general",
        json={"updates": {"rate_limit_per_minute": 120}},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert captured == {"rate_limit_per_minute": 120}

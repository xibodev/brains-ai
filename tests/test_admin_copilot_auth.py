"""Tests for the dashboard-driven github_copilot management surface.

Covers the additions that let an operator enable + sign in to the
github_copilot provider entirely from ``/admin/config`` instead of the
CLI:

  * ``allow_copilot_proxy`` is overlay-editable (the safety gate can be
    flipped from the dashboard and persists across reloads);
  * the config page renders the "Enable Copilot proxy" toggle and the
    device-code sign-in UI;
  * ``GET  /admin/api/providers/github_copilot/auth-status``
  * ``POST /admin/api/providers/github_copilot/device/start``
  * ``POST /admin/api/providers/github_copilot/device/poll``
  * ``POST /admin/api/providers/github_copilot/logout``

Every GitHub network call is mocked — these tests never hit GitHub.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from brains.admin.service import current_config_view
from brains.api.auth import mint_browser_token, reset_rate_limit_state
from brains.auth.copilot import CopilotAuthError, DeviceCode
from brains.config import reload_settings, settings
from brains.dashboard.app import app
from brains.storage.migrations import init_db


@pytest.fixture(autouse=True)
def isolate_overlay(tmp_path: Path, monkeypatch):
    overlay_path = tmp_path / "brains.runtime.yaml"
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay_path))
    reload_settings()
    init_db()
    yield overlay_path
    if overlay_path.exists():
        overlay_path.unlink()
    reload_settings()


def _client() -> TestClient:
    reset_rate_limit_state()
    client = TestClient(app)
    client.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    return client


# ---------- allow_copilot_proxy is now overlay-editable ----------


def test_config_view_exposes_allow_copilot_proxy():
    assert "allow_copilot_proxy" in current_config_view()


def test_api_post_config_persists_allow_copilot_proxy(isolate_overlay: Path):
    assert settings.allow_copilot_proxy is False
    client = _client()
    r = client.post("/admin/api/config", json={"allow_copilot_proxy": True})
    assert r.status_code == 200, r.text
    # Settings reloaded from the overlay — the gate is now enabled.
    assert settings.allow_copilot_proxy is True


def test_config_page_renders_copilot_signin_ui():
    # The provider panes (incl. the Copilot sign-in UI) are built by
    # admin-config.js; the page just references the bundle. Assert the
    # bundle carries the new affordances and the page loads it.
    client = _client()
    body = client.get("/admin/config").text
    assert "admin-config.js" in body

    js = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "brains"
        / "web"
        / "static"
        / "admin-config.js"
    ).read_text(encoding="utf-8")
    assert "Enable Copilot proxy" in js
    assert "Login with device code" in js
    assert "/admin/api/providers/github_copilot/device/start" in js


# ---------- auth-status ----------


def test_auth_status_requires_auth():
    client = TestClient(app)
    r = client.get("/admin/api/providers/github_copilot/auth-status")
    assert r.status_code in (401, 403)


def test_auth_status_reports_gate_and_token_source():
    client = _client()
    fake = {
        "active_source": "cache",
        "env_present": False,
        "cache_present": True,
        "gh_cli_present": False,
        "use_gh_cli": True,
        "cache_dir": "/tmp/x",
    }
    with patch("brains.auth.copilot.auth_status", return_value=fake):
        r = client.get("/admin/api/providers/github_copilot/auth-status")
    assert r.status_code == 200
    body = r.json()
    assert body["active_source"] == "cache"
    # Default-OFF gate: proxy_enabled mirrors settings, proxy_allowed False.
    assert body["proxy_enabled"] is False
    assert body["proxy_allowed"] is False
    assert body["proxy_blocked_reason"]  # non-empty explanation


# ---------- device/start ----------


def test_device_start_returns_user_code():
    client = _client()
    fake = DeviceCode(
        device_code="dc-123",
        user_code="ABCD-1234",
        verification_uri="https://github.com/login/device",
        interval=5,
        expires_in=900,
    )
    with patch("brains.auth.copilot.start_device_flow", return_value=fake):
        r = client.post("/admin/api/providers/github_copilot/device/start")
    assert r.status_code == 200
    body = r.json()
    assert body["user_code"] == "ABCD-1234"
    assert body["verification_uri"].startswith("https://github.com/login/device")
    assert body["device_code"] == "dc-123"
    assert "error" not in body


def test_device_start_surfaces_error_envelope():
    client = _client()
    with patch(
        "brains.auth.copilot.start_device_flow",
        side_effect=CopilotAuthError("device-code request failed: boom"),
    ):
        r = client.post("/admin/api/providers/github_copilot/device/start")
    assert r.status_code == 200
    body = r.json()
    assert "boom" in body.get("error", "")


# ---------- device/poll ----------


def test_device_poll_requires_device_code():
    client = _client()
    r = client.post("/admin/api/providers/github_copilot/device/poll", json={})
    assert r.status_code == 400


def test_device_poll_reports_authorized():
    client = _client()
    with patch(
        "brains.auth.copilot.poll_device_flow_once",
        return_value={"status": "authorized"},
    ):
        r = client.post(
            "/admin/api/providers/github_copilot/device/poll",
            json={"device_code": "dc-123"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "authorized"


def test_device_poll_reports_pending():
    client = _client()
    with patch(
        "brains.auth.copilot.poll_device_flow_once",
        return_value={"status": "pending"},
    ):
        r = client.post(
            "/admin/api/providers/github_copilot/device/poll",
            json={"device_code": "dc-123"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


# ---------- logout ----------


def test_logout_returns_removed_map():
    client = _client()
    with patch(
        "brains.auth.copilot.clear_cached_credentials",
        return_value={"oauth": True, "session": False},
    ):
        r = client.post("/admin/api/providers/github_copilot/logout")
    assert r.status_code == 200
    assert r.json()["removed"] == {"oauth": True, "session": False}

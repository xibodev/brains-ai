"""Smoke tests for the structured admin config UI + secrets catalog.

Covers the post-redesign admin pages:
  * /admin/config — structured tier/route editor + per-provider panes +
    Advanced JSON fallback
  * /admin/secrets — env-var catalog with category/required/status
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brains.admin.service import (
    KNOWN_ENV_CATALOG,
    current_config_view,
    env_catalog_with_status,
)
from brains.api.auth import mint_browser_token, reset_rate_limit_state
from brains.config import reload_settings, settings
from brains.dashboard.app import app
from brains.storage.migrations import init_db


@pytest.fixture(autouse=True)
def isolate_overlay(tmp_path: Path, monkeypatch):
    overlay_path = tmp_path / "brains.runtime.yaml"
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay_path))
    reload_settings()
    init_db()  # ensure audit_log + workspaces tables exist for write_overlay
    yield overlay_path
    if overlay_path.exists():
        overlay_path.unlink()
    reload_settings()


def _client() -> TestClient:
    reset_rate_limit_state()
    client = TestClient(app)
    client.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    return client


# ---------- Config page: structured editor ----------


def test_config_page_renders_structured_editor():
    client = _client()
    r = client.get("/admin/config")
    assert r.status_code == 200
    body = r.text
    # Headers for the four new structured sections
    assert "Model tiers" in body
    assert "Routes" in body
    # Section heading was renamed from "Provider configuration" → "Providers"
    # in the provider-first Config UI refactor.
    assert "Providers" in body
    assert "Global limits" in body
    # Save buttons for each section
    assert 'id="save-tiers"' in body
    assert 'id="save-routes"' in body
    assert 'id="save-rate-limit"' in body
    # Mount points the external admin-config.js controllers attach to
    assert 'id="tiers-editor"' in body
    assert 'id="routes-editor"' in body
    assert 'id="provider-panes"' in body
    # Bootstrap blob is injected onto window so the JS controllers can read it
    assert "__BRAINS_CONFIG_BOOTSTRAP__" in body
    # External script that owns the editors
    assert "admin-config.js" in body


def test_config_page_keeps_advanced_raw_form_fallback():
    client = _client()
    r = client.get("/admin/config")
    assert r.status_code == 200
    body = r.text
    # The Advanced details block keeps the legacy textarea form alive
    assert "Advanced — raw overlay editor" in body
    assert 'name="models_json"' in body
    assert 'name="routes_json"' in body


def test_config_page_lists_known_provider_schemas():
    """Provider schemas live in the external admin-config.js controllers
    (loaded by the Config page). Verify each known editable provider has
    a schema entry there — that's what makes the per-provider panes
    render correctly."""
    js_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "brains"
        / "web"
        / "static"
        / "admin-config.js"
    )
    body = js_path.read_text(encoding="utf-8")
    for provider in ("ollama", "openai_compatible", "github_copilot", "litellm"):
        assert f"'{provider}': {{" in body, f"missing schema for {provider}"


def test_config_view_exposes_github_copilot_fields():
    view = current_config_view()
    # The new structured pane reads these directly from current_config_view
    assert "github_copilot_use_gh_cli" in view
    assert "github_copilot_timeout_seconds" in view
    assert "github_copilot_editor_version" in view
    assert "github_copilot_integration_id" in view


def test_config_view_redacts_raw_openai_key(monkeypatch):
    # When the key is a plain string (not an env ref), the view must
    # never leak it; it returns "***set***" instead.
    monkeypatch.setenv("OPENAI_API_KEY_TEST_RAW", "sk-supersecret")
    settings.openai_compatible_api_key = "sk-supersecret"
    try:
        view = current_config_view()
        assert view["openai_compatible_api_key"] == "***set***"
        assert view["openai_compatible_api_key_set"] is True
    finally:
        settings.openai_compatible_api_key = ""


def test_config_view_preserves_env_ref_for_openai_key():
    settings.openai_compatible_api_key = "${ENV:OPENAI_API_KEY}"
    try:
        view = current_config_view()
        assert view["openai_compatible_api_key"] == "${ENV:OPENAI_API_KEY}"
    finally:
        settings.openai_compatible_api_key = ""


# ---------- Structured editor saves work end-to-end ----------


def test_api_post_config_persists_github_copilot_scalar(isolate_overlay: Path):
    client = _client()
    r = client.post(
        "/admin/api/config",
        json={
            "github_copilot_timeout_seconds": 45.0,
            "github_copilot_use_gh_cli": False,
        },
    )
    assert r.status_code == 200, r.text
    # Settings reloaded — verify
    assert settings.github_copilot_timeout_seconds == 45.0
    assert settings.github_copilot_use_gh_cli is False


def test_api_post_config_persists_only_models(isolate_overlay: Path):
    # Simulates the "Save tiers" button — sends ONLY {models: ...}
    client = _client()
    r = client.post(
        "/admin/api/config",
        json={
            "models": {
                "default": {"provider": "echo", "model": "echo"},
                "fast": {"provider": "echo", "model": "echo"},
            }
        },
    )
    assert r.status_code == 200, r.text
    assert "default" in settings.models
    assert "fast" in settings.models


def test_api_post_config_persists_only_routes(isolate_overlay: Path):
    # First seed a tier so the routes validator accepts our key
    client = _client()
    client.post(
        "/admin/api/config",
        json={"models": {"default": {"provider": "echo", "model": "echo"}}},
    )
    r = client.post(
        "/admin/api/config",
        json={"routes": {"brain.plan": "default", "brain.execute": "default"}},
    )
    assert r.status_code == 200, r.text
    assert settings.routes.get("brain.plan") == "default"
    assert settings.routes.get("brain.execute") == "default"


def test_config_write_is_refused_when_it_cannot_be_recorded(isolate_overlay: Path, monkeypatch):
    """The overlay is a file: recording after the write would be too late."""
    import brains.audit as audit_module
    from brains.audit import AuditWriteError

    class _BrokenSession:
        def __enter__(self):
            raise RuntimeError("audit store is down")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(audit_module, "SessionLocal", lambda: _BrokenSession())
    client = _client()

    with pytest.raises(AuditWriteError):
        client.post("/admin/api/config", json={"github_copilot_timeout_seconds": 61.0})

    assert not isolate_overlay.exists(), "config was written with no record of the attempt"
    assert settings.github_copilot_timeout_seconds != 61.0


def test_env_override_is_refused_when_it_cannot_be_recorded(isolate_overlay: Path, monkeypatch):
    """Same ordering for a live-process override and its secrets file."""
    import os

    import brains.audit as audit_module
    from brains.audit import AuditWriteError

    class _BrokenSession:
        def __enter__(self):
            raise RuntimeError("audit store is down")

        def __exit__(self, *args):
            return False

    monkeypatch.delenv("BRAINS_ALLOW_PRIVATE_PROVIDERS", raising=False)
    monkeypatch.setattr(audit_module, "SessionLocal", lambda: _BrokenSession())
    client = _client()

    with pytest.raises(AuditWriteError):
        client.post(
            "/admin/api/env/set",
            json={"name": "BRAINS_ALLOW_PRIVATE_PROVIDERS", "value": "1", "persist": False},
        )

    assert os.environ.get("BRAINS_ALLOW_PRIVATE_PROVIDERS") is None, (
        "the override was applied unrecorded"
    )


# ---------- Secrets page: catalog ----------


def test_env_catalog_includes_core_vars():
    names = {entry["name"] for entry in KNOWN_ENV_CATALOG}
    # The catalog is the operator's "what can I set?" checklist — these
    # are the most important entries and must always be present.
    for required in (
        "BRAINS_API_KEY",
        "BRAINS_ADMIN_KEY",
        "BRAINS_DB_URL",
        "BRAINS_RUNTIME_OVERLAY",
        "BRAINS_GITHUB_COPILOT_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "BRAINS_ALLOW_PRIVATE_PROVIDERS",
    ):
        assert required in names, f"catalog missing {required}"


def test_env_catalog_entries_have_required_columns():
    for entry in KNOWN_ENV_CATALOG:
        assert "name" in entry
        assert "category" in entry
        assert "required" in entry
        assert "purpose" in entry


def test_env_catalog_with_status_reflects_environment(monkeypatch):
    monkeypatch.setenv("BRAINS_API_KEY", "test-value-for-catalog")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    enriched = env_catalog_with_status()
    by_name = {e["name"]: e for e in enriched}
    assert by_name["BRAINS_API_KEY"]["set"] is True
    assert by_name["OPENAI_API_KEY"]["set"] is False


def test_secrets_page_renders_catalog():
    client = _client()
    r = client.get("/admin/secrets")
    assert r.status_code == 200
    body = r.text
    # Headers from the new structure
    assert "Known env vars brains reads" in body
    assert "Currently referenced by the overlay" in body
    # At least the most important catalog rows show up
    assert "BRAINS_API_KEY" in body
    assert "BRAINS_ADMIN_KEY" in body
    assert "OPENAI_API_KEY" in body
    # Category badges render
    assert "auth" in body
    assert "github_copilot" in body

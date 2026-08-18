"""Tests for the admin API + HTML console."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brains.api.auth import mint_browser_token, reset_rate_limit_state
from brains.config import reload_settings, settings
from brains.dashboard.app import app


@pytest.fixture(autouse=True)
def isolate_overlay(tmp_path: Path, monkeypatch):
    overlay_path = tmp_path / "brains.runtime.yaml"
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay_path))
    reload_settings()
    yield overlay_path
    if overlay_path.exists():
        overlay_path.unlink()
    reload_settings()


def _client() -> TestClient:
    reset_rate_limit_state()
    client = TestClient(app)
    client.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    return client


def test_admin_login_rejects_bad_key():
    reset_rate_limit_state()
    client = TestClient(app)
    response = client.post("/admin/login", data={"key": "nope"}, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_admin_overview_lists_providers():
    client = _client()
    response = client.get("/admin/overview")
    assert response.status_code == 200
    # Ships with at least echo + ollama + openai + litellm pills.
    text = response.text
    assert "echo" in text
    assert "ollama" in text
    # New premium shell (Jinja2 templates + design system).
    assert "brand-mark" in text
    assert "stat-card" in text
    assert "/static/brains/brains.css" in text
    # Inline Lucide-style SVG icons are present in the new layout.
    assert "<svg" in text


def test_admin_api_providers_returns_known_set():
    client = _client()
    response = client.get("/admin/api/providers")
    assert response.status_code == 200
    payload = response.json()
    assert "echo" in payload["providers"]
    assert "openai_compatible" in payload["providers"]


def test_admin_api_providers_status_marks_echo_as_stub():
    client = _client()
    response = client.get("/admin/api/providers/status")
    assert response.status_code == 200
    payload = response.json()
    by_name = {p["name"]: p for p in payload["providers"]}
    assert "echo" in by_name
    echo = by_name["echo"]
    assert echo["is_stub"] is True
    assert echo["configured"] is True
    assert "stub" in echo["reason"].lower()


def test_admin_api_providers_status_flags_openai_without_base_url(
    isolate_overlay: Path, monkeypatch
):
    # Force openai_compatible to have no base URL so it should report
    # "not configured" with a precise reason — no socket opened.
    monkeypatch.setattr(settings, "openai_compatible_base_url", "")
    client = _client()
    response = client.get("/admin/api/providers/status")
    assert response.status_code == 200
    by_name = {p["name"]: p for p in response.json()["providers"]}
    if "openai_compatible" in by_name:
        entry = by_name["openai_compatible"]
        assert entry["is_stub"] is False
        assert entry["configured"] is False
        assert "base_url" in entry["reason"]


def test_admin_api_prices_returns_static_catalog():
    client = _client()
    response = client.get("/admin/api/prices")
    assert response.status_code == 200
    payload = response.json()
    assert "prices" in payload
    assert isinstance(payload["prices"], dict)
    # The static catalog ships with at least a handful of well-known
    # models; the exact ids are an implementation detail, but the
    # structure of each entry is part of the contract.
    assert payload["prices"], "static price catalog should not be empty"
    sample = next(iter(payload["prices"].values()))
    assert "input" in sample and "output" in sample
    assert isinstance(sample["input"], int | float)
    assert isinstance(sample["output"], int | float)
    assert payload["overlay_count"] == 0


def test_admin_api_prices_includes_overlay_override(isolate_overlay: Path):
    # Push an overlay price for a custom model id and verify it
    # surfaces with the right shape + overlay_count increments.
    settings.savings.price_catalog = {"custom-model-xyz": {"input": 1.25, "output": 7.5}}
    try:
        client = _client()
        response = client.get("/admin/api/prices")
        assert response.status_code == 200
        payload = response.json()
        assert payload["overlay_count"] >= 1
        assert payload["prices"]["custom-model-xyz"] == {
            "input": 1.25,
            "output": 7.5,
        }
    finally:
        settings.savings.price_catalog = {}


def test_admin_api_route_keys_returns_canonical_set():
    client = _client()
    response = client.get("/admin/api/route-keys")
    assert response.status_code == 200
    payload = response.json()
    keys = {entry["key"] for entry in payload["keys"]}
    # The classifier currently emits these task_types — the admin
    # autocomplete must list them so operators don't have to dig into
    # the source.
    assert {"code_fix", "code_explanation", "architecture", "docs_lookup", "research"} <= keys
    for entry in payload["keys"]:
        assert entry["description"], f"missing description for {entry['key']}"


def test_admin_api_savings_preview_with_no_ledger_returns_zeros():
    # Fresh test DB has no usage_ledger rows for this model -> the
    # endpoint must return rows_considered=0 without crashing.
    client = _client()
    response = client.get(
        "/admin/api/savings/preview",
        params={"model": "gpt-4o-mini", "current_model": "definitely-not-routed-anywhere"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["projected_model"] == "gpt-4o-mini"
    assert payload["current_model"] == "definitely-not-routed-anywhere"
    assert payload["rows_considered"] == 0
    assert payload["rows_repriced"] == 0
    assert payload["current_actual_usd"] == 0.0
    assert payload["projected_actual_usd"] == 0.0
    assert payload["delta_usd"] == 0.0
    # gpt-4o-mini is in the static catalog -> price should round-trip.
    assert payload["projected_price_per_million"] is not None
    assert payload["projected_price_per_million"]["input"] > 0


def test_admin_api_savings_preview_rejects_missing_model():
    client = _client()
    response = client.get("/admin/api/savings/preview", params={"model": ""})
    assert response.status_code == 400


def test_admin_api_savings_preview_reprices_without_writing():
    # Insert a single non-stub ledger row and verify the preview
    # endpoint reads it back without mutating the row.
    from datetime import UTC, datetime

    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import UsageLedgerEntry

    init_db()
    with SessionLocal() as session:
        row = UsageLedgerEntry(
            ts=datetime.now(UTC),
            endpoint="/v1/chat/completions",
            requested_model="auto",
            routed_model="preview-test-model",
            provider="openai_compatible",
            task_type="code_fix",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_actual_usd=10.0,
            cost_baseline_usd=15.0,
            savings_usd=5.0,
            is_stub=False,
        )
        session.add(row)
        session.commit()
        row_id = row.id

    try:
        client = _client()
        response = client.get(
            "/admin/api/savings/preview",
            params={
                "model": "gpt-4o-mini",
                "current_model": "preview-test-model",
                "days": 1,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["rows_considered"] == 1
        assert payload["rows_repriced"] == 1
        # gpt-4o-mini is much cheaper than the planted $10 actual,
        # so the delta should be a clear positive (we'd save money).
        assert payload["current_actual_usd"] == 10.0
        assert payload["projected_actual_usd"] < 1.0  # 1M @ $0.15 + 0.5M @ $0.60 = $0.45
        assert payload["delta_usd"] > 9.0
    finally:
        # Clean up the planted row so subsequent tests aren't polluted.
        from brains.storage.db import SessionLocal as _SL

        with _SL() as session:
            session.query(UsageLedgerEntry).filter(UsageLedgerEntry.id == row_id).delete()
            session.commit()


def test_admin_api_test_provider_echo_succeeds():
    client = _client()
    response = client.post(
        "/admin/api/providers/test",
        json={"provider": "echo", "model": "echo-default"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    # SEC-001: response body is NOT reflected. A fixed token + a
    # short sha256 fingerprint replace the upstream preview.
    assert payload["response"] == "ok"
    assert "response_preview" not in payload
    assert isinstance(payload.get("response_fingerprint"), str)


def test_admin_api_test_provider_unknown_fails_gracefully():
    client = _client()
    response = client.post(
        "/admin/api/providers/test",
        json={"provider": "does-not-exist", "model": "x"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["stage"] == "configuration"


def test_provider_status_requires_key_for_default_openai_endpoint(monkeypatch):
    from brains.admin.service import provider_status_view
    from brains.config import settings

    monkeypatch.setattr(settings, "openai_compatible_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "openai_compatible_api_key", None)
    rows = {row["name"]: row for row in provider_status_view()}
    assert rows["openai_compatible"]["configured"] is False
    assert rows["openai_compatible"]["reason"] == "OpenAI endpoint requires an API key"


def test_provider_status_allows_keyless_custom_compatible_endpoint(monkeypatch):
    from brains.admin.service import provider_status_view
    from brains.config import settings

    monkeypatch.setattr(settings, "openai_compatible_base_url", "http://127.0.0.1:8080/v1")
    monkeypatch.setattr(settings, "openai_compatible_api_key", None)
    rows = {row["name"]: row for row in provider_status_view()}
    assert rows["openai_compatible"]["configured"] is True


def test_admin_api_post_config_writes_overlay(isolate_overlay: Path):
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"rate_limit_per_minute": 17, "ollama_base_url": "http://example:11434"},
    )
    assert response.status_code == 200
    assert isolate_overlay.exists()
    body = isolate_overlay.read_text()
    assert "rate_limit_per_minute" in body
    # And the live settings reflect the change.
    assert settings.rate_limit_per_minute == 17
    assert settings.ollama_base_url == "http://example:11434"


def test_admin_api_post_config_rejects_bad_provider():
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"models": {"default": {"provider": "totally-fake", "model": "x"}}},
    )
    assert response.status_code == 400


def test_admin_api_post_config_rejects_route_without_tier():
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"routes": {"trivial": "nope"}},
    )
    assert response.status_code == 400


def test_admin_api_env_lists_referenced_names(isolate_overlay: Path, monkeypatch):
    isolate_overlay.write_text('openai_compatible_api_key: "${ENV:MY_OAI}"\n')
    reload_settings()
    monkeypatch.setenv("MY_OAI", "x")
    client = _client()
    response = client.get("/admin/api/env")
    assert response.status_code == 200
    payload = response.json()
    assert "MY_OAI" in payload["names"]
    assert payload["set"]["MY_OAI"] is True


def test_admin_config_form_post_persists_models_and_routes(isolate_overlay: Path):
    client = _client()
    response = client.post(
        "/admin/config",
        data={
            "models_json": json.dumps(
                {
                    "default": {"provider": "echo", "model": "echo-form"},
                    "cheap": {"provider": "echo", "model": "echo-cheap"},
                }
            ),
            "routes_json": json.dumps({"trivial": "cheap", "unknown": "default"}),
            "rate_limit_per_minute": 5,
            "ollama_base_url": "",
            "openai_compatible_base_url": "",
            "openai_compatible_api_key_ref": "MY_KEY",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert isolate_overlay.exists()
    assert settings.models["default"].model == "echo-form"
    assert settings.rate_limit_per_minute == 5
    # Key reference was wrapped in ${ENV:NAME} and stored only as a ref.
    raw = isolate_overlay.read_text()
    assert "${ENV:MY_KEY}" in raw


def test_admin_secrets_page_renders():
    client = _client()
    response = client.get("/admin/secrets")
    assert response.status_code == 200
    assert "Secrets" in response.text


def test_admin_routes_require_auth():
    reset_rate_limit_state()
    client = TestClient(app)
    # HTML pages now 303-redirect to /admin/login when unauthed; JSON
    # /admin/api/* still 401s so script clients see a real auth error.
    expected = {
        "/admin/overview": (303,),
        "/admin/config": (303,),
        "/admin/api/providers": (401,),
        "/admin/api/config": (401, 405),
    }
    for path, codes in expected.items():
        response = client.get(path, follow_redirects=False)
        assert response.status_code in codes, f"{path}: {response.status_code}"
        if response.status_code == 303:
            assert response.headers["location"].startswith("/admin/login")


# ---------- FIX-001: SSRF lockdown on base URLs + no upstream byte reflection ----------


def test_api_post_config_rejects_link_local_base_url():
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"ollama_base_url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert response.status_code == 400
    assert (
        "link-local" in response.json()["detail"].lower()
        or "metadata" in response.json()["detail"].lower()
    )


def test_api_post_config_rejects_non_http_scheme():
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"openai_compatible_base_url": "file:///etc/passwd"},
    )
    assert response.status_code == 400


def test_api_post_config_rejects_rfc1918_by_default():
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"ollama_base_url": "http://10.0.0.5:11434"},
    )
    assert response.status_code == 400
    assert "private" in response.json()["detail"].lower()


def test_api_post_config_accepts_loopback(isolate_overlay: Path):
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"ollama_base_url": "http://127.0.0.1:11434"},
    )
    assert response.status_code == 200
    assert settings.ollama_base_url == "http://127.0.0.1:11434"


def test_api_post_config_accepts_private_with_opt_in(isolate_overlay: Path, monkeypatch):
    monkeypatch.setenv("BRAINS_ALLOW_PRIVATE_PROVIDERS", "1")
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"ollama_base_url": "http://10.0.0.5:11434"},
    )
    assert response.status_code == 200


# ---------- FIX-002: env-refs only at allow-listed fields ----------


def test_api_post_config_rejects_env_ref_in_model_field():
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={
            "models": {
                "default": {
                    "provider": "echo",
                    "model": "${ENV:BRAINS_API_KEY}",
                }
            }
        },
    )
    assert response.status_code == 400
    assert "${ENV:" in response.json()["detail"] or "env" in response.json()["detail"].lower()


def test_api_post_config_rejects_env_ref_in_routes():
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"routes": {"trivial": "${ENV:SOMETHING}"}},
    )
    assert response.status_code == 400


def test_api_post_config_accepts_env_ref_in_api_key_field(isolate_overlay: Path, monkeypatch):
    monkeypatch.setenv("MY_OAI_KEY", "real-secret")
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"openai_compatible_api_key": "${ENV:MY_OAI_KEY}"},
    )
    assert response.status_code == 200
    # Resolved at load time, stored only as a reference on disk.
    assert settings.openai_compatible_api_key == "real-secret"
    assert "${ENV:MY_OAI_KEY}" in isolate_overlay.read_text()


# ---------- FIX-004: env-check is constrained to known names ----------


def test_env_check_rejects_unreferenced_name():
    client = _client()
    response = client.post(
        "/admin/api/env/check",
        json={"names": ["NEVER_REFERENCED_BY_OVERLAY"]},
    )
    assert response.status_code == 400


# ---------- env override (Environment page set/clear) ----------


def test_env_set_ephemeral_sets_process_without_persisting(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.env"
    monkeypatch.setattr("brains.config.secrets_env_path", lambda: secrets)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client()
    response = client.post(
        "/admin/api/env/set",
        json={"name": "OPENAI_API_KEY", "value": "sk-ephemeral", "persist": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["set"] is True and body["persisted"] is False
    import os

    assert os.environ.get("OPENAI_API_KEY") == "sk-ephemeral"
    assert not secrets.exists()
    os.environ.pop("OPENAI_API_KEY", None)


def test_env_set_persist_writes_gitignored_file(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.env"
    monkeypatch.setattr("brains.config.secrets_env_path", lambda: secrets)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client()
    response = client.post(
        "/admin/api/env/set",
        json={"name": "OPENAI_API_KEY", "value": "sk-persist", "persist": True},
    )
    assert response.status_code == 200
    assert response.json()["persisted"] is True
    assert secrets.exists()
    assert "OPENAI_API_KEY=sk-persist" in secrets.read_text()

    clear = client.post(
        "/admin/api/env/clear",
        json={"name": "OPENAI_API_KEY", "persist": True},
    )
    assert clear.status_code == 200
    assert clear.json()["removed_file"] is True
    assert "OPENAI_API_KEY=sk-persist" not in secrets.read_text()
    import os

    os.environ.pop("OPENAI_API_KEY", None)


def test_env_set_rejects_unknown_name():
    client = _client()
    response = client.post(
        "/admin/api/env/set",
        json={"name": "TOTALLY_UNKNOWN_VAR", "value": "x"},
    )
    assert response.status_code == 400


def test_env_set_rejects_empty_value():
    client = _client()
    response = client.post(
        "/admin/api/env/set",
        json={"name": "OPENAI_API_KEY", "value": ""},
    )
    assert response.status_code == 400


# ---------- FIX-006: env-ref shape validated in form handler ----------


def test_admin_config_form_rejects_lowercase_env_ref(isolate_overlay: Path):
    client = _client()
    response = client.post(
        "/admin/config",
        data={
            "models_json": "",
            "routes_json": "",
            "rate_limit_per_minute": 0,
            "ollama_base_url": "",
            "openai_compatible_base_url": "",
            "openai_compatible_api_key_ref": "lowercase_name",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=invalid" in response.headers["location"]


# ---------- FIX-003: /admin/test is POST-only ----------


def test_admin_test_endpoint_get_renders_form_only(isolate_overlay: Path):
    """GET shows the form but does not execute any provider call."""
    client = _client()
    response = client.get("/admin/test")
    assert response.status_code == 200
    # No result panel should be present (the form is empty).
    assert "Result" not in response.text or "Run ping" in response.text


def test_admin_test_endpoint_post_runs_echo():
    client = _client()
    response = client.post(
        "/admin/test",
        data={"provider": "echo", "model": "echo-default"},
    )
    assert response.status_code == 200
    # Pill rendered for the result.
    assert "ok" in response.text.lower()


# ---------- FIX-009 smoke tests: healthz + logout ----------


def test_admin_healthz_smoke():
    client = _client()
    response = client.get("/admin/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "overlay_path" in payload


def test_admin_logout_clears_cookie():
    client = _client()
    response = client.get("/admin/logout", follow_redirects=False)
    assert response.status_code == 303
    set_cookie = response.headers.get("set-cookie", "")
    # The cookie is expired (Max-Age=0 or empty value with past expires).
    assert "brains_admin_key" in set_cookie


# ---------- FIX-008: control payload happy + sad ----------


def test_api_post_config_accepts_valid_control(isolate_overlay: Path):
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"control": {"require_approval_for_deep": True}},
    )
    assert response.status_code == 200
    assert settings.control.require_approval_for_deep is True


def test_api_post_config_rejects_invalid_control_payload():
    client = _client()
    response = client.post(
        "/admin/api/config",
        json={"control": "not-a-mapping"},
    )
    assert response.status_code == 400

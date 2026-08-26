"""Tests for the experimental feature gate (``brains.experimental``).

The normal install hides and refuses non-mature surfaces; only the explicit
environment opt-ins reach them. These tests pin the gate itself: flag
parsing, the MCP tool filter, the refusal message naming its switch, and
the retired legacy browser surfaces on the gateway.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import brains.experimental as experimental
from brains.experimental import (
    EXPERIMENTAL_ENV,
    GATEWAY_ENV,
    LEGACY_SURFACES_ENV,
    UI_LABS_ENV,
    ExperimentalDisabledError,
    experimental_enabled,
    legacy_surfaces_enabled,
    require_experimental,
)

# --- flag parsing ----------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on", " yes "])
def test_truthy_values_enable_the_gate(raw, monkeypatch):
    monkeypatch.setenv(EXPERIMENTAL_ENV, raw)
    assert experimental_enabled() is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "junk"])
def test_non_truthy_values_do_not_enable_the_gate(raw, monkeypatch):
    monkeypatch.setenv(EXPERIMENTAL_ENV, raw)
    assert experimental_enabled() is False


def test_legacy_gate_is_independent(monkeypatch):
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    monkeypatch.delenv(LEGACY_SURFACES_ENV, raising=False)
    assert experimental_enabled() is False
    assert legacy_surfaces_enabled() is False
    monkeypatch.setenv(LEGACY_SURFACES_ENV, "1")
    assert legacy_surfaces_enabled() is True
    assert experimental_enabled() is False


# --- require_experimental --------------------------------------------------


def test_require_experimental_raises_and_names_the_switch(monkeypatch):
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    with pytest.raises(ExperimentalDisabledError, match=EXPERIMENTAL_ENV):
        require_experimental("code graph build")


def test_require_experimental_passes_when_opted_in(monkeypatch):
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    require_experimental("code graph build")  # must not raise


def test_every_registered_reason_has_a_tool_and_vice_versa():
    from brains.mcp.server import TOOL_REGISTRY

    assert set(experimental.EXPERIMENTAL_TOOL_REASONS) == set(experimental.EXPERIMENTAL_MCP_TOOLS)
    missing = experimental.EXPERIMENTAL_MCP_TOOLS - set(TOOL_REGISTRY)
    assert not missing, f"experimental tools no longer in TOOL_REGISTRY: {sorted(missing)}"


# --- gateway legacy-surface retirement -------------------------------------
#
# The dashboard app keeps its own HTML surface (it is already an explicit
# opt-in process); the gateway retires /admin HTML while leaving the JSON
# APIs under /admin/api/* alone.


@pytest.fixture
def gateway_client(monkeypatch):
    from brains.main import app

    monkeypatch.delenv(LEGACY_SURFACES_ENV, raising=False)
    return TestClient(app)


def test_gateway_admin_html_redirects_to_app_when_retired(gateway_client):
    response = gateway_client.get("/admin", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/app"


def test_gateway_legacy_admin_post_answers_404_when_retired(gateway_client):
    response = gateway_client.post("/admin/config", data={})
    assert response.status_code == 404


def test_gateway_admin_login_remains_core_when_legacy_ui_is_retired(gateway_client):
    response = gateway_client.post("/admin/login", data={"key": "nope"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login?error=")


def test_gateway_admin_html_served_when_opted_in(monkeypatch):
    from brains.main import app

    monkeypatch.setenv(LEGACY_SURFACES_ENV, "1")
    client = TestClient(app)
    # Unauthenticated GET reaches the page's own auth flow again (a redirect
    # to its login), proving the middleware no longer intercepts it.
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code in (303, 307)
    assert "/admin" in response.headers.get("location", "")


# --- model-gateway experimental gate ----------------------------------------
#
# The model-serving routes (/v1/chat/completions, /v1/messages, ...) are
# experimental; the native control-plane API is not.


@pytest.fixture
def gateway_off_client(monkeypatch):
    from brains.main import app

    monkeypatch.delenv(GATEWAY_ENV, raising=False)
    return TestClient(app)


def test_gateway_model_routes_answer_404_with_switch_when_disabled(gateway_off_client):
    for path in ("/v1/chat/completions", "/v1/messages", "/v1/models"):
        response = gateway_off_client.post(path, json={})
        assert response.status_code == 404, path
        assert GATEWAY_ENV in response.text, path


def test_gateway_copilot_bare_alias_hits_the_same_gate(gateway_off_client):
    # The copilot alias rewrites /chat/completions -> /v1/chat/completions
    # before the gate runs, so both spellings are gated.
    response = gateway_off_client.post("/chat/completions", json={})
    assert response.status_code == 404
    assert GATEWAY_ENV in response.text


def test_control_plane_routes_are_not_gated(gateway_off_client):
    # /v1/sessions without auth is 401 (auth), never the gateway-disabled 404.
    response = gateway_off_client.get("/v1/sessions")
    assert response.status_code == 401


def test_gateway_model_routes_serve_when_opted_in(monkeypatch):
    from brains.main import app

    monkeypatch.setenv(GATEWAY_ENV, "1")
    client = TestClient(app)
    # Pass-through proof: unauthenticated now reaches the route's own auth
    # (401), not the gate's 404.
    assert client.post("/v1/chat/completions", json={}).status_code == 401


def test_run_cli_refuses_without_gateway_opt_in(monkeypatch):
    import typer

    monkeypatch.delenv(GATEWAY_ENV, raising=False)
    from brains.cli.run import run_tool_cli

    with pytest.raises(typer.Exit) as excinfo:
        run_tool_cli(None, tool="claude")
    assert excinfo.value.exit_code == 2


def test_experimental_gates_registry_documents_every_switch():
    from brains.experimental import EXPERIMENTAL_GATES

    assert set(EXPERIMENTAL_GATES) == {
        EXPERIMENTAL_ENV,
        LEGACY_SURFACES_ENV,
        GATEWAY_ENV,
        UI_LABS_ENV,
    }
    assert all(EXPERIMENTAL_GATES.values())

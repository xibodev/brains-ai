"""HTTP tests for ``GET /admin/api/providers/{name}/models``."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from brains.dashboard.app import app


def test_models_endpoint_requires_auth():
    client = TestClient(app)
    response = client.get("/admin/api/providers/echo/models")
    # require_browser_auth returns 401 / 403 (depending on impl) when no key/cookie
    assert response.status_code in (401, 403)


def test_models_endpoint_returns_envelope_for_echo(auth_headers):
    client = TestClient(app)
    response = client.get("/admin/api/providers/echo/models", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "echo"
    assert body["error"] is None
    ids = [m["id"] for m in body["models"]]
    assert "echo-default" in ids
    # Schema: every row has id/vendor/label keys
    for row in body["models"]:
        assert set(row.keys()) >= {"id", "vendor", "label"}


def test_models_endpoint_returns_error_envelope_for_unknown_provider(auth_headers):
    client = TestClient(app)
    response = client.get("/admin/api/providers/nonexistent/models", headers=auth_headers)
    assert response.status_code == 200  # always 200 — UI degrades gracefully
    body = response.json()
    assert body["models"] == []
    assert body["error"]  # non-empty string


def test_models_endpoint_swallows_provider_invocation_errors(auth_headers):
    """If a provider's list_models() raises ProviderInvocationError mid-call,
    the endpoint reports the error in the envelope (not via HTTP 5xx)."""
    client = TestClient(app)
    from brains.providers.openai_compatible import ProviderInvocationError

    with patch(
        "brains.providers.openai_compatible.OpenAICompatibleProvider.list_models",
        side_effect=ProviderInvocationError("upstream broke"),
    ):
        response = client.get("/admin/api/providers/openai/models", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["models"] == []
    assert "upstream broke" in (body["error"] or "")

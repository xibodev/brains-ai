"""Tests for the MCP SSE auth middleware and bind enforcement.

The middleware is exercised against a minimal Starlette app instead of
booting the real FastMCP server — that keeps the test deterministic and
avoids needing an MCP client. Bind-host resolution is tested as a pure
function over the two env vars (``BRAINS_MCP_BIND`` and
``BRAINS_MCP_ALLOW_PUBLIC``).
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from brains.api.auth import reset_rate_limit_state
from brains.config import settings
from brains.mcp.sse_auth import (
    ALLOW_PUBLIC_ENV,
    BIND_ENV,
    LOOPBACK_HOSTS,
    MCPAuthMiddleware,
    host_allowlist_for,
    resolve_bind_host,
)


async def _ok(_request):
    return PlainTextResponse("ok")


def _client(*, allowed_hosts=LOOPBACK_HOSTS, base_url: str = "http://127.0.0.1"):
    reset_rate_limit_state()
    inner = Starlette(routes=[Route("/sse", _ok), Route("/sse/messages", _ok)])
    wrapped = MCPAuthMiddleware(inner, allowed_hosts=allowed_hosts)
    return TestClient(wrapped, base_url=base_url)


# --- middleware: auth gate ----------------------------------------------


def test_sse_request_without_key_is_rejected() -> None:
    with _client() as client:
        response = client.get("/sse")
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_sse_request_with_wrong_key_is_rejected() -> None:
    with _client() as client:
        response = client.get("/sse", headers={"Authorization": "Bearer not-the-key"})
    assert response.status_code == 401


def test_sse_request_with_valid_bearer_is_accepted() -> None:
    with _client() as client:
        response = client.get(
            "/sse",
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )
    assert response.status_code == 200
    assert response.text == "ok"


def test_sse_request_with_valid_x_api_key_is_accepted() -> None:
    with _client() as client:
        response = client.get("/sse", headers={"x-api-key": settings.api_key})
    assert response.status_code == 200


def test_sse_allows_unauthenticated_when_setting_is_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "allow_unauthenticated_api", True)
    with _client() as client:
        response = client.get("/sse")
    assert response.status_code == 200


# --- middleware: host header allowlist ----------------------------------


def test_sse_rejects_non_loopback_host_header_when_loopback_bound() -> None:
    with _client() as client:
        response = client.get(
            "/sse",
            headers={
                "Host": "attacker.example",
                "Authorization": f"Bearer {settings.api_key}",
            },
        )
    assert response.status_code == 400
    assert "Invalid Host header" in response.json()["detail"]


def test_sse_accepts_loopback_host_header_variants() -> None:
    for host_value in ("127.0.0.1", "127.0.0.1:9877", "localhost", "localhost:9877"):
        with _client() as client:
            response = client.get(
                "/sse",
                headers={
                    "Host": host_value,
                    "Authorization": f"Bearer {settings.api_key}",
                },
            )
        assert response.status_code == 200, f"failed for Host={host_value!r}"


def test_sse_skips_host_check_when_allowlist_is_none() -> None:
    with _client(allowed_hosts=None) as client:
        response = client.get(
            "/sse",
            headers={
                "Host": "brains.example.com",
                "Authorization": f"Bearer {settings.api_key}",
            },
        )
    assert response.status_code == 200


# --- resolve_bind_host --------------------------------------------------


def test_resolve_bind_host_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BIND_ENV, raising=False)
    monkeypatch.delenv(ALLOW_PUBLIC_ENV, raising=False)
    assert resolve_bind_host() == "127.0.0.1"


def test_resolve_bind_host_honors_explicit_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BIND_ENV, "localhost")
    monkeypatch.delenv(ALLOW_PUBLIC_ENV, raising=False)
    assert resolve_bind_host() == "localhost"


def test_resolve_bind_host_refuses_public_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BIND_ENV, "0.0.0.0")
    monkeypatch.delenv(ALLOW_PUBLIC_ENV, raising=False)
    with pytest.raises(RuntimeError, match="ALLOW_PUBLIC"):
        resolve_bind_host()


def test_resolve_bind_host_allows_public_with_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BIND_ENV, "0.0.0.0")
    monkeypatch.setenv(ALLOW_PUBLIC_ENV, "1")
    assert resolve_bind_host() == "0.0.0.0"


# --- host_allowlist_for -------------------------------------------------


def test_host_allowlist_returns_loopback_set_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ALLOW_PUBLIC_ENV, raising=False)
    allowed = host_allowlist_for("127.0.0.1")
    assert allowed is not None
    assert allowed == LOOPBACK_HOSTS


def test_host_allowlist_is_none_when_public_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_PUBLIC_ENV, "true")
    assert host_allowlist_for("0.0.0.0") is None

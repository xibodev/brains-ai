"""Auth + host-header allowlist middleware for MCP HTTP transports.

Stdio MCP is process-bounded and inherits the parent process's trust
boundary, so it does not need an auth layer. HTTP MCP, in contrast, exposes
every brains tool over HTTP and therefore needs the same gate the gateway
API enforces. This module reuses the gateway's constant-time key check and
sliding-window rate limiter so HTTP clients share one credential surface
with the rest of brains.

A Host header allowlist defends against DNS-rebinding attacks: when bound
to loopback, only loopback Host header values are accepted, so a browser
that resolves an attacker-controlled name to ``127.0.0.1`` cannot smuggle
requests to the MCP port. When the operator explicitly opts into a public
bind (``BRAINS_MCP_ALLOW_PUBLIC=1``), Host validation is skipped on the
assumption that a reverse proxy or load balancer is enforcing its own
hostname ACL upstream.

The presented key resolves to one principal (:mod:`brains.authz`), which is
published for the duration of the request so tool calls are attributed to that
actor and scoped reads see only that actor's Orgs and Workspaces. A
Runtime-narrow credential is refused outright: it exists to run work on one
machine, not to drive the tool surface.
"""

from __future__ import annotations

import os

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from brains.api.auth import (
    _check_rate_limit,
    _extract_api_key,
    _valid_keys,
)
from brains.config import settings

# Host header values we treat as loopback. Used both to validate Host
# headers on inbound requests and to validate the configured bind host.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# When set to a truthy value the operator has explicitly accepted that the
# MCP HTTP port may bind to a public interface and that Host header checks
# should be delegated to whatever sits in front of brains.
ALLOW_PUBLIC_ENV = "BRAINS_MCP_ALLOW_PUBLIC"

# Optional override of the bind host. Defaults to 127.0.0.1.
BIND_ENV = "BRAINS_MCP_BIND"


def _is_public_allowed() -> bool:
    raw = os.environ.get(ALLOW_PUBLIC_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def resolve_bind_host() -> str:
    """Return the host an HTTP MCP transport should bind to.

    Defaults to ``127.0.0.1``. If ``BRAINS_MCP_BIND`` is set to a
    non-loopback value, ``BRAINS_MCP_ALLOW_PUBLIC`` must also be set or
    startup is refused — exposing 59 tool endpoints to the network without
    that explicit opt-in is almost always an accident.
    """
    raw = os.environ.get(BIND_ENV, "").strip() or "127.0.0.1"
    if raw not in LOOPBACK_HOSTS and not _is_public_allowed():
        raise RuntimeError(
            f"{BIND_ENV}={raw!r} is not a loopback interface. "
            f"Set {ALLOW_PUBLIC_ENV}=1 to acknowledge that the MCP HTTP port "
            "(which exposes every brains tool) will be reachable beyond the "
            "local machine, and ensure you have terminated TLS and configured "
            "a Host header ACL upstream."
        )
    return raw


def host_allowlist_for(bind_host: str) -> frozenset[str] | None:
    """Return the set of Host header values the middleware will accept.

    ``None`` means *no validation* — used when the operator has opted into
    a public bind via ``BRAINS_MCP_ALLOW_PUBLIC``. The ``bind_host``
    argument is currently advisory (the policy is driven by the env flag),
    but it is part of the signature so callers can reason about why
    validation is on or off without re-reading the env var.
    """
    _ = bind_host  # kept for future per-bind tuning
    if _is_public_allowed():
        return None
    return LOOPBACK_HOSTS


class MCPAuthMiddleware:
    """Authenticate every HTTP MCP request with the gateway's API key.

    Implemented as a **pure ASGI middleware** (not ``BaseHTTPMiddleware``):
    ``BaseHTTPMiddleware`` buffers the downstream response through an async
    generator, which is incompatible with the long-lived streaming response of
    the MCP SSE transport — it raises ``AssertionError: Unexpected message ...
    http.response.start`` mid-stream and silently drops the connection, so MCP
    clients see zero tools. Operating directly on ``(scope, receive, send)``
    passes the stream through untouched.

    Skipped entirely when ``settings.allow_unauthenticated_api`` is true,
    matching the HTTP gateway's opt-out for single-user sealed networks.

    When ``allowed_hosts`` is non-``None``, requests whose Host header is
    not in the set are rejected with ``400`` so a browser tricked into a
    DNS rebind cannot reach the loopback-bound MCP port.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str] | None) -> None:
        self.app = app
        self._allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only HTTP requests carry auth; pass websocket/lifespan straight through.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if settings.allow_unauthenticated_api:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)

        if self._allowed_hosts is not None:
            host_only = request.headers.get("host", "").split(":", 1)[0]
            if host_only not in self._allowed_hosts:
                await JSONResponse(
                    {"detail": f"Invalid Host header: {host_only!r}"},
                    status_code=400,
                )(scope, receive, send)
                return

        valid_keys = _valid_keys()
        if not valid_keys:
            await JSONResponse(
                {"detail": "API key not configured"},
                status_code=500,
            )(scope, receive, send)
            return

        token = _extract_api_key(
            authorization=request.headers.get("authorization"),
            x_api_key_header=request.headers.get("x-api-key"),
        )
        if not token:
            await JSONResponse(
                {"detail": "Invalid API key"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        # Resolve the presented key to ONE principal and stamp it into
        # request-scoped ContextVars, so every tool call made during this
        # request is attributed to that actor and every scoped read is filtered
        # to what it may see. Imported lazily to keep the auth surface usable
        # before the database is initialised (e.g. very early in startup tests).
        try:
            from brains.authz.resolver import current_principal, principal_for_secret
            from brains.control.operators import current_operator
        except Exception:
            await self.app(scope, receive, send)
            return

        principal = principal_for_secret(token)
        if principal is None:
            await JSONResponse(
                {"detail": "Invalid API key"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        try:
            _check_rate_limit(token)
        except HTTPException as exc:
            await JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=dict(exc.headers or {}),
            )(scope, receive, send)
            return

        if principal.is_runtime:
            # A Runtime credential runs work on one machine; the MCP tool
            # surface is the operator/agent surface and is refused to it.
            await JSONResponse(
                {"detail": "Runtime credentials are not authorized for the MCP tool surface"},
                status_code=403,
            )(scope, receive, send)
            return

        principal_handle = current_principal.set(principal)
        operator_handle = current_operator.set(principal.operator_slug)
        try:
            await self.app(scope, receive, send)
        finally:
            current_operator.reset(operator_handle)
            current_principal.reset(principal_handle)

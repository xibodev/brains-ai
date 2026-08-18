"""FastAPI dependencies that turn a request into an authorized principal.

Three gates, one resolver:

``require_principal``
    The LLM-gateway gate. Accepts ``Authorization: Bearer`` / ``x-api-key``.
``require_console_principal``
    The operator-console gate. Additionally accepts the signed
    ``brains_admin_key`` cookie the SPA holds, and the legacy ``?key=`` script
    convenience.
``require_operator_principal``
    ``require_console_principal`` plus a refusal of Runtime credentials, for
    every surface that is not a Runtime operation.

Each stores the resolved principal on ``request.state.principal`` and publishes
it on the resolver ContextVar for the duration of the request, so downstream
control-layer reads scope to the same actor the route authenticated.
"""

from __future__ import annotations

from fastapi import Header
from fastapi.requests import Request

from brains.authz import policy
from brains.authz.principal import CHANNEL_API, CHANNEL_BROWSER, CHANNEL_LOCAL, Principal
from brains.authz.resolver import (
    bootstrap_principal,
    principal_for_secret,
    principal_slot,
    set_current_principal,
)
from brains.config import settings

#: Cookie name used by the dashboard + admin browser surfaces.
BROWSER_AUTH_COOKIE = "brains_admin_key"


def _extract_bearer(authorization: str | None, x_api_key_header: str | None) -> str | None:
    if x_api_key_header:
        return x_api_key_header
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _publish(request: Request | None, principal: Principal) -> Principal:
    if request is not None:
        request.state.principal = principal
    set_current_principal(principal)
    return principal


def resolve_request_principal(
    request: Request | None,
    *,
    authorization: str | None,
    x_api_key_header: str | None,
    allow_cookie: bool,
) -> Principal:
    """Resolve one request into a principal, or raise 401.

    Resolution order is credential-first: a presented secret wins over a
    cookie, and a cookie only resolves through the raw key that signed it.

    Every principal is tagged with the *channel* it arrived on. A raw secret on
    an HTTP request is :data:`~brains.authz.principal.CHANNEL_API` - an agent
    process holding a shared operator key presents exactly the same bytes as
    its owner - while the signed console cookie is
    :data:`~brains.authz.principal.CHANNEL_BROWSER`, which only a browser
    sign-in can mint. Approval separation of duty depends on that distinction
    instead of on a caller-declared Session id.
    """
    if settings.allow_unauthenticated_api:
        # Authentication is explicitly disabled for a sealed single-user
        # network, so the process boundary is the trust boundary, exactly as it
        # is for the CLI.
        return _publish(request, bootstrap_principal(channel=CHANNEL_LOCAL))

    token = _extract_bearer(authorization, x_api_key_header)
    if token:
        principal = principal_for_secret(token)
        if principal is not None:
            _check_rate_limit(token)
            return _publish(request, principal.with_channel(CHANNEL_API))
        raise policy.unauthenticated()

    if request is not None and allow_cookie:
        query_token = request.query_params.get("key")
        if query_token:
            principal = principal_for_secret(query_token)
            if principal is not None:
                _check_rate_limit(query_token)
                return _publish(request, principal.with_channel(CHANNEL_API))
            raise policy.unauthenticated("Sign in required")
        cookie_token = request.cookies.get(BROWSER_AUTH_COOKIE)
        if cookie_token:
            principal = principal_for_cookie(cookie_token)
            if principal is not None:
                _check_rate_limit(cookie_token)
                return _publish(request, principal.with_channel(CHANNEL_BROWSER))
        raise policy.unauthenticated("Sign in required")

    raise policy.unauthenticated()


def principal_for_cookie(cookie_token: str) -> Principal | None:
    """Resolve the signed console cookie to the principal it was minted for."""
    from brains.api.auth import resolve_browser_cookie

    raw_key = resolve_browser_cookie(cookie_token)
    if raw_key is None:
        return None
    return principal_for_secret(raw_key)


def _check_rate_limit(token: str) -> None:
    from brains.api.auth import check_rate_limit

    check_rate_limit(token)


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def require_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key_header: str | None = Header(default=None, alias="x-api-key"),
) -> Principal:
    """Gateway gate: a presented API key resolves to one principal."""
    return resolve_request_principal(
        request,
        authorization=authorization,
        x_api_key_header=x_api_key_header,
        allow_cookie=False,
    )


def require_console_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key_header: str | None = Header(default=None, alias="x-api-key"),
) -> Principal:
    """Console gate: API key or the signed console cookie."""
    return resolve_request_principal(
        request,
        authorization=authorization,
        x_api_key_header=x_api_key_header,
        allow_cookie=True,
    )


def require_operator_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key_header: str | None = Header(default=None, alias="x-api-key"),
) -> Principal:
    """Console gate that refuses Runtime-narrow credentials."""
    principal = resolve_request_principal(
        request,
        authorization=authorization,
        x_api_key_header=x_api_key_header,
        allow_cookie=True,
    )
    policy.require_operator(principal, operation="this operator API")
    return principal


class PrincipalContextMiddleware:
    """Bind one principal slot per request, at the ASGI boundary.

    Implemented as a **pure ASGI middleware** rather than a
    ``BaseHTTPMiddleware``: ``__call__`` awaits the downstream app in the same
    ``contextvars`` context it set the slot in, so every context FastAPI copies
    from it - the dependency, the endpoint, the threadpool worker - shares the
    same slot object and sees the principal the gate resolved. A dependency's
    own ``ContextVar.set`` would not survive that copy, and its token could not
    be reset from another context.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        with principal_slot():
            await self.app(scope, receive, send)


def install_principal_context(app) -> None:
    """Install :class:`PrincipalContextMiddleware` on ``app``."""
    app.add_middleware(PrincipalContextMiddleware)


__all__ = [
    "BROWSER_AUTH_COOKIE",
    "PrincipalContextMiddleware",
    "install_principal_context",
    "principal_for_cookie",
    "require_console_principal",
    "require_operator_principal",
    "require_principal",
    "resolve_request_principal",
]

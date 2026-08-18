"""API authentication: every accepted credential resolves to one principal.

Authentication is a lookup in the credential store
(:mod:`brains.authz.credentials`), not membership in a broad key set. A
presented secret is hashed and matched against ``api_credentials``; the row
names the actor, its credential kind, its Org roles and, for a daemon, its
Runtime binding. Revocation and expiry are honoured on every request.

The keys an install already holds on disk (``settings.api_key``,
``settings.api_keys`` and ``~/.brains/operator-keys/*.key``) are *adopted* into
the store the first time they are seen, so an existing single-operator install
keeps working and still resolves to an explicit principal.

Rate limiting is a per-credential sliding-window counter scoped to
``settings.rate_limit_per_minute`` - disabled when 0. The window is keyed by
the secret's hash rather than by the secret itself, so no raw credential sits
in process memory for the lifetime of the window.

Browser surfaces (``/admin/*``, ``/dashboard/*``, ``/app``) accept an *opaque*
signed token in the ``brains_admin_key`` cookie instead of the raw API key, so
a leaked cookie can be expired without rotating the key itself. The cookie is
signed with the key it was minted for, so it resolves to that key's principal
and to no other.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException
from fastapi.requests import Request

from brains.config import settings


def _valid_keys() -> tuple[str, ...]:
    """Raw keys this process can read from settings and disk.

    This is **not** the authorization surface - a key here is accepted only if
    it also resolves to an active credential row. It exists because the console
    cookie is an HMAC keyed by the raw API key, so verifying a cookie requires
    the raw key, which the credential store deliberately does not hold.

    Sources, in priority order:

    1. ``settings.api_key`` - the admin key (auto-generated on first run,
       persisted at ``~/.brains/admin-key``).
    2. ``settings.api_keys`` - the optional rotation tuple.
    3. Every per-operator key on disk under ``~/.brains/operator-keys/*.key``.

    The list comes from :func:`brains.authz.credentials.local_key_sources`,
    which is ``stat``-cached, so verifying a forged cookie does not re-read
    every operator key file.
    """
    try:
        from brains.authz.credentials import local_key_sources

        entries = local_key_sources()
    except Exception:
        # Never let a missing operators table / DB error fail cookie
        # verification against the admin key.
        entries = ()
        if settings.api_key:
            entries = ((settings.api_key, "admin", "local:admin_key"),)
    seen: set[str] = set()
    unique: list[str] = []
    for raw, _kind, _source in entries:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        unique.append(raw)
    return tuple(unique)


def _extract_api_key(authorization: str | None, x_api_key_header: str | None) -> str | None:
    if x_api_key_header:
        return x_api_key_header
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _matches_any_key(token: str, valid_keys: tuple[str, ...]) -> bool:
    # Always iterate every key so attackers can't time which slot matched.
    matched = False
    for key in valid_keys:
        if hmac.compare_digest(token, key):
            matched = True
    return matched


_rate_lock = threading.Lock()
_rate_history: dict[str, deque[float]] = defaultdict(deque)


def _check_rate_limit(token: str) -> None:
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return
    # Key the window by digest, never by the raw secret.
    bucket = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    window_start = now - 60.0
    with _rate_lock:
        history = _rate_history[bucket]
        while history and history[0] < window_start:
            history.popleft()
        if len(history) >= limit:
            retry_after = max(1, int(60 - (now - history[0])))
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({limit} req/min)",
                headers={"Retry-After": str(retry_after)},
            )
        history.append(now)


def check_rate_limit(token: str) -> None:
    """Public alias used by :mod:`brains.authz.deps`."""
    _check_rate_limit(token)


def reset_rate_limit_state() -> None:
    """Test-only helper to clear sliding-window state between tests."""
    with _rate_lock:
        _rate_history.clear()


def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key_header: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """Gateway gate. Resolves one operator principal or raises 401/403.

    A Runtime-narrow credential is refused here too: it exists to run work on
    one machine, not to spend the install's provider budget or enumerate its
    configured providers.
    """
    from brains.authz import policy
    from brains.authz.deps import resolve_request_principal

    principal = resolve_request_principal(
        request,
        authorization=authorization,
        x_api_key_header=x_api_key_header,
        allow_cookie=False,
    )
    policy.require_operator(principal, operation="the model gateway")


# Cookie name used by the dashboard + admin browser surfaces.
BROWSER_AUTH_COOKIE = "brains_admin_key"

# Session lifetime for the signed browser cookie.
BROWSER_COOKIE_TTL_SECONDS = 60 * 60 * 12  # 12h


def _kid(key: str) -> str:
    """A short, non-secret fingerprint we can embed in the cookie."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_browser_token(api_key: str, *, issued_at: int | None = None) -> str:
    """Mint a signed opaque browser cookie value for ``api_key``.

    Format: ``v1.<kid>.<issued_at>.<sig>``. The signature is HMAC-SHA256
    over ``f"{kid}.{issued_at}"`` keyed by the API key itself, so no
    additional server secret is required, and so the cookie can only ever be
    resolved back to the one key that minted it. The cookie reveals only a
    truncated fingerprint of the key, not the key itself.
    """
    ts = int(issued_at if issued_at is not None else time.time())
    kid = _kid(api_key)
    msg = f"{kid}.{ts}".encode()
    sig = hmac.new(api_key.encode("utf-8"), msg, hashlib.sha256).digest()
    return f"v1.{kid}.{ts}.{_b64u(sig)}"


def _resolve_browser_token(token: str, valid_keys: tuple[str, ...]) -> str | None:
    """Return the API key matching a browser cookie, or ``None``."""
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return None
    _, kid, ts_str, sig_b64 = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    if ts <= 0 or time.time() - ts > BROWSER_COOKIE_TTL_SECONDS:
        return None
    msg = f"{kid}.{ts}".encode()
    matched: str | None = None
    # Iterate every key so timing can't reveal which slot matched.
    for key in valid_keys:
        candidate_kid = _kid(key)
        candidate_sig = hmac.new(key.encode("utf-8"), msg, hashlib.sha256).digest()
        candidate_sig_b64 = _b64u(candidate_sig)
        kid_ok = hmac.compare_digest(candidate_kid, kid)
        sig_ok = hmac.compare_digest(candidate_sig_b64, sig_b64)
        if kid_ok and sig_ok and matched is None:
            matched = key
    return matched


def resolve_browser_cookie(token: str) -> str | None:
    """Resolve a console cookie to the raw key that signed it, or ``None``."""
    return _resolve_browser_token(token, _valid_keys())


def require_browser_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key_header: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """Auth gate for HTML + console surfaces.

    Accepts the same Authorization/x-api-key headers as the API. In
    addition, it accepts a signed opaque token in the ``brains_admin_key``
    cookie (minted by ``mint_browser_token`` on ``/admin/login``). The
    legacy ``?key=`` query parameter is still honored as a script
    convenience but should not be used by browsers - see SECURITY.md.

    Authentication is not enough on these surfaces. Two principals are refused
    outright:

    * a **Runtime** credential, which exists to run work on one machine and
      must never reach a console or admin page; and
    * a principal that holds **no Org role at all**, which would otherwise see
      an install-wide HTML surface while every scoped API answers it "nothing".

    Both are ``403``: the caller authenticated, it simply may not be here.
    """
    from brains.authz import policy
    from brains.authz.deps import resolve_request_principal

    principal = resolve_request_principal(
        request,
        authorization=authorization,
        x_api_key_header=x_api_key_header,
        allow_cookie=True,
    )
    policy.require_operator(principal, operation="the console")
    policy.require_scoped_principal(principal, operation="the console")


def require_install_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key_header: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """Gate for install-level configuration surfaces.

    ``/admin/*`` edits provider credentials, environment overrides, router
    policy and the process configuration itself. None of that is Org-scoped:
    an Org ``owner`` that could reach it would be reading and rewriting the
    whole install's secrets. So these routes require the **bootstrap admin**
    principal - the operator that holds the install's own key - and answer
    every other authenticated principal ``403``.
    """
    from brains.authz import policy
    from brains.authz.deps import resolve_request_principal

    principal = resolve_request_principal(
        request,
        authorization=authorization,
        x_api_key_header=x_api_key_header,
        allow_cookie=True,
    )
    policy.require_install_admin(principal, operation="the admin configuration console")


def require_install_admin_html(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key_header: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """HTML variant of :func:`require_install_admin` (401 -> login redirect)."""
    try:
        require_install_admin(
            request,
            authorization=authorization,
            x_api_key_header=x_api_key_header,
        )
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        raise _admin_login_redirect(request) from exc


def _admin_login_redirect(request: Request):
    """Build the 303-to-login exception for an unauthenticated HTML request."""
    from urllib.parse import quote

    from brains.api.errors import AdminLoginRequired

    # Build a "next" URL pointing back to whatever the user was trying to
    # reach. Drop any existing ?key= so the redirect doesn't carry an invalid
    # token forward.
    target = request.url.path
    query = "&".join(f"{k}={v}" for k, v in request.query_params.multi_items() if k != "key")
    if query:
        target = f"{target}?{query}"
    return AdminLoginRequired(location=f"/admin/login?next={quote(target, safe='')}")


def require_browser_auth_html(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key_header: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """HTML variant of :func:`require_browser_auth`.

    Identical auth logic, but on failure raises :class:`AdminLoginRequired`
    which is converted to a 303 redirect to ``/admin/login?next=…`` by the
    handler installed via :func:`register_admin_redirect_handler`. Use this
    on HTML admin routes so a browser visiting an authed page when logged
    out goes to the login form instead of seeing a bare 401 JSON error.

    JSON routes under ``/admin/api/*`` should keep using
    :func:`require_browser_auth` (which returns 401) so script clients
    get a proper auth error instead of a redirect.
    """
    try:
        require_browser_auth(
            request,
            authorization=authorization,
            x_api_key_header=x_api_key_header,
        )
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        raise _admin_login_redirect(request) from exc


def require_console_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key_header: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """Auth gate for the operator-console API (`/v1/orgs`, `/v1/runtimes`, ...).

    Accepts EITHER a raw API key (daemon + scripts) OR the signed
    ``brains_admin_key`` cookie minted at ``/admin/login`` (the SPA, which
    cannot hold a raw key in JS). Authentication alone is not authorization:
    routers additionally require a capability against a resolved Org through
    :mod:`brains.authz.policy`.
    """

    require_browser_auth(
        request,
        authorization=authorization,
        x_api_key_header=x_api_key_header,
    )

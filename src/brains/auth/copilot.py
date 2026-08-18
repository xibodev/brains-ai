"""GitHub Copilot OAuth + session-token resolution for the github_copilot provider.

Three OAuth-token sources, tried in order:
    1. ``settings.github_copilot_oauth_token`` (env override)
    2. cached device-code token at ``<cache_dir>/github_copilot_oauth.json``
    3. ``gh auth token`` shell-out (when ``use_gh_cli=True``)

The GitHub OAuth token is exchanged at
``https://api.github.com/copilot_internal/v2/token`` for a short-lived
Copilot session token (~30 min). That token is cached per-OAuth-token
fingerprint so concurrent calls don't thrash the exchange endpoint.

Same client_id and integration headers the official Copilot for Neovim
plugin uses — this is the path OpenCode, aider, and Cline take.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from brains.config import settings

CLIENT_ID = "Iv1.b507a08c87ecfe98"  # Copilot-for-Neovim OAuth app
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
SESSION_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"

_DEFAULT_CHAT_BASE = "https://api.githubcopilot.com"
_OAUTH_CACHE_FILE = "github_copilot_oauth.json"
_SESSION_CACHE_FILE = "github_copilot_session.json"
_REFRESH_BEFORE_EXPIRY_SECONDS = 60

# Shared grey-area notice surfaced by the CLI (`copilot-login`) and logged once
# at runtime when the provider is first used. GitHub's Copilot terms scope it to
# "code suggestions in editors"; using it as a general gateway provider is a
# personal-use grey area, not a sanctioned public API, and the upstream endpoint
# and headers can change without notice.
COPILOT_TOS_WARNING = (
    "GitHub Copilot is licensed for code suggestions in editors. Using it as a "
    "general gateway provider is a personal-use grey area, not a sanctioned "
    "public API \u2014 keep it on your own loopback gateway, do not expose it as a "
    "shared/hosted relay, and expect the upstream endpoint/headers to change "
    "without notice."
)


class CopilotAuthError(RuntimeError):
    """Raised when no usable Copilot OAuth token can be resolved."""


@dataclass(frozen=True)
class CopilotSession:
    """A resolved Copilot session token + the chat endpoint it unlocks."""

    token: str
    chat_base_url: str
    expires_at: int


# ---------------------------------------------------------------------------
# cache paths
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    override = (settings.github_copilot_cache_dir or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".brains" / "cache"


def _ensure_cache_dir() -> Path:
    path = _cache_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _oauth_cache_path() -> Path:
    return _cache_dir() / _OAUTH_CACHE_FILE


def _session_cache_path() -> Path:
    return _cache_dir() / _SESSION_CACHE_FILE


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_secret(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    # Best-effort 0600. On Windows this is a no-op behaviorally but harmless.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# OAuth token resolution
# ---------------------------------------------------------------------------


def _from_env() -> str | None:
    token = (settings.github_copilot_oauth_token or "").strip()
    return token or None


def _from_cache() -> str | None:
    data = _read_json(_oauth_cache_path())
    if not data:
        return None
    token = str(data.get("access_token") or "").strip()
    return token or None


def _from_gh_cli() -> str | None:
    if not settings.github_copilot_use_gh_cli:
        return None
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        result = subprocess.run(  # noqa: S603 — gh is resolved via shutil.which
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    token = (result.stdout or "").strip()
    return token or None


def _configured_operator_count() -> int:
    """Best-effort count of operators on this brain (0 if unknown).

    Used only by :func:`assert_copilot_proxy_allowed`. Failing to read the
    table (e.g. pristine install before migrations) returns 0, which keeps the
    single-operator local path working.
    """
    try:
        from brains.storage.db import SessionLocal
        from brains.storage.models import Operator

        with SessionLocal() as session:
            return session.query(Operator).count()
    except Exception:
        return 0


def assert_copilot_proxy_allowed() -> None:
    """Raise :class:`CopilotAuthError` unless the github_copilot proxy is safe.

    The provider is intended for a single operator's own loopback gateway, not
    a shared / hosted / multi-tenant relay. It is therefore:

    * default-OFF — enable with ``allow_copilot_proxy: true`` or
      ``BRAINS_EXPERIMENTAL_COPILOT_PROVIDER=1``; and
    * refused outright on a shared Postgres backend or when more than one
      operator is configured, because the OAuth/session token cache is
      per-machine (not per-operator) and would silently be shared.
    """
    enabled = settings.allow_copilot_proxy or os.environ.get(
        "BRAINS_EXPERIMENTAL_COPILOT_PROVIDER", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        raise CopilotAuthError(
            "github_copilot provider is disabled by default (personal-use grey "
            "area; see docs/OPERATIONS.md). Enable it for your own loopback "
            "gateway with `allow_copilot_proxy: true` or "
            "BRAINS_EXPERIMENTAL_COPILOT_PROVIDER=1."
        )
    backend = getattr(getattr(settings, "subsystems", None), "storage", None)
    if getattr(backend, "backend", "sqlite") == "postgres":
        raise CopilotAuthError(
            "github_copilot proxy is unavailable on a shared Postgres backend: "
            "its token cache is per-machine and would be shared across operators."
        )
    if _configured_operator_count() > 1:
        raise CopilotAuthError(
            "github_copilot proxy is unavailable when multiple operators are "
            "configured: the per-machine token cache would be shared. Use a "
            "per-operator OpenAI-compatible key instead."
        )


def resolve_oauth_token() -> str:
    """Find a usable GitHub OAuth token or raise :class:`CopilotAuthError`.

    Search order is documented at module level. The returned token is
    whatever value the source provided; it is not validated against
    GitHub here — the session-token exchange does that.
    """
    for source in (_from_env, _from_cache, _from_gh_cli):
        token = source()
        if token:
            return token
    raise CopilotAuthError(
        "no GitHub Copilot OAuth token available. "
        "Run `brains-ai copilot-login`, set BRAINS_GITHUB_COPILOT_OAUTH_TOKEN, "
        "or `gh auth refresh -s copilot` then `gh auth token`."
    )


# ---------------------------------------------------------------------------
# session token (the short-lived Copilot bearer)
# ---------------------------------------------------------------------------


def _copilot_headers() -> dict[str, str]:
    """Headers the Copilot endpoints expect on every request.

    The Editor-Version + integration-id pair is what unlocks the chat
    endpoint for an OAuth token issued to the Copilot-for-Neovim app.
    """
    return {
        "Accept": "application/json",
        "Editor-Version": settings.github_copilot_editor_version,
        "Editor-Plugin-Version": "brains-gateway/0.1",
        "User-Agent": "GithubCopilot/brains-gateway",
    }


def _exchange_oauth_for_session(oauth_token: str) -> CopilotSession:
    headers = {
        **_copilot_headers(),
        "Authorization": f"token {oauth_token}",
    }
    try:
        response = httpx.get(
            SESSION_TOKEN_URL,
            headers=headers,
            timeout=settings.github_copilot_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise CopilotAuthError("Copilot session-token exchange timed out") from exc
    except httpx.HTTPError as exc:
        raise CopilotAuthError(f"Copilot session-token transport error: {exc}") from exc

    if response.status_code == 401:
        raise CopilotAuthError(
            "Copilot session-token exchange returned 401 — the OAuth token "
            "is invalid or lacks Copilot access. Try `brains-ai copilot-login`."
        )
    if response.status_code >= 400:
        raise CopilotAuthError(
            f"Copilot session-token exchange failed ({response.status_code}): {response.text[:200]}"
        )
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise CopilotAuthError("Copilot session-token response was not JSON") from exc
    if not isinstance(payload, dict):
        raise CopilotAuthError("Copilot session-token response had unexpected shape")
    token = str(payload.get("token") or "").strip()
    expires_at = int(payload.get("expires_at") or 0)
    if not token or not expires_at:
        raise CopilotAuthError("Copilot session-token response missing token/expires_at")
    endpoints = payload.get("endpoints") or {}
    chat_base = str(endpoints.get("api") or _DEFAULT_CHAT_BASE).rstrip("/")
    return CopilotSession(token=token, chat_base_url=chat_base, expires_at=expires_at)


def _cached_session_for(oauth_token: str) -> CopilotSession | None:
    data = _read_json(_session_cache_path())
    if not data:
        return None
    if data.get("fingerprint") != _fingerprint(oauth_token):
        return None
    try:
        expires_at = int(data["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if expires_at - int(time.time()) < _REFRESH_BEFORE_EXPIRY_SECONDS:
        return None
    token = str(data.get("token") or "").strip()
    if not token:
        return None
    chat_base = str(data.get("chat_base_url") or _DEFAULT_CHAT_BASE).rstrip("/")
    return CopilotSession(token=token, chat_base_url=chat_base, expires_at=expires_at)


def _store_session(oauth_token: str, session: CopilotSession) -> None:
    _ensure_cache_dir()
    _write_json_secret(
        _session_cache_path(),
        {
            "fingerprint": _fingerprint(oauth_token),
            "token": session.token,
            "chat_base_url": session.chat_base_url,
            "expires_at": session.expires_at,
        },
    )


def get_session(*, force_refresh: bool = False) -> CopilotSession:
    """Return a fresh-enough Copilot session token, refreshing on demand."""
    oauth_token = resolve_oauth_token()
    if not force_refresh:
        cached = _cached_session_for(oauth_token)
        if cached is not None:
            return cached
    session = _exchange_oauth_for_session(oauth_token)
    _store_session(oauth_token, session)
    return session


# ---------------------------------------------------------------------------
# device-code flow (used by `brains copilot-login`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


def start_device_flow() -> DeviceCode:
    """Kick off the GitHub device-code flow against the Copilot OAuth app."""
    try:
        response = httpx.post(
            DEVICE_CODE_URL,
            headers={"Accept": "application/json"},
            data={"client_id": CLIENT_ID, "scope": "read:user"},
            timeout=settings.github_copilot_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise CopilotAuthError(f"device-code request failed: {exc}") from exc
    if response.status_code >= 400:
        raise CopilotAuthError(
            f"device-code request returned {response.status_code}: {response.text[:200]}"
        )
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise CopilotAuthError("device-code response was not JSON") from exc
    if not isinstance(payload, dict):
        raise CopilotAuthError("device-code response had unexpected shape")
    return DeviceCode(
        device_code=str(payload.get("device_code") or ""),
        user_code=str(payload.get("user_code") or ""),
        verification_uri=str(payload.get("verification_uri") or "https://github.com/login/device"),
        interval=int(payload.get("interval") or 5),
        expires_in=int(payload.get("expires_in") or 900),
    )


def poll_device_flow(device_code: str, interval: int, expires_in: int) -> str:
    """Poll GitHub's token endpoint until the user authorizes the device.

    Returns the OAuth access token. Raises :class:`CopilotAuthError` on
    explicit denial, network errors, or expiry.
    """
    deadline = time.time() + max(60, expires_in)
    poll_interval = max(1, interval)
    while time.time() < deadline:
        try:
            response = httpx.post(
                ACCESS_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                timeout=settings.github_copilot_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise CopilotAuthError(f"device-code poll transport error: {exc}") from exc
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        token = str(payload.get("access_token") or "").strip()
        if token:
            _persist_oauth_token(token)
            return token
        error = str(payload.get("error") or "").strip()
        if error in {"authorization_pending", "slow_down"}:
            if error == "slow_down":
                poll_interval += 5
            time.sleep(poll_interval)
            continue
        # access_denied, expired_token, unsupported_grant_type, etc.
        raise CopilotAuthError(f"device-code flow failed: {error or 'unknown error'}")
    raise CopilotAuthError("device-code flow expired before user completed it")


def poll_device_flow_once(device_code: str) -> dict[str, Any]:
    """Single, non-blocking poll of the device-code token endpoint.

    Unlike :func:`poll_device_flow` (which blocks until the user finishes
    or the code expires), this performs exactly one poll so a caller — e.g.
    the admin dashboard — can drive the flow with its own timer. Returns a
    JSON-safe status envelope::

        {"status": "authorized" | "pending" | "slow_down" | "denied"
                   | "error", "error"?: str}

    On ``authorized`` the OAuth token is persisted to the cache exactly as
    the blocking poller would, so subsequent requests pick it up.
    """
    try:
        response = httpx.post(
            ACCESS_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=settings.github_copilot_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return {"status": "error", "error": f"transport error: {exc}"}
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    token = str(payload.get("access_token") or "").strip()
    if token:
        _persist_oauth_token(token)
        return {"status": "authorized"}
    error = str(payload.get("error") or "").strip()
    if error == "authorization_pending":
        return {"status": "pending"}
    if error == "slow_down":
        return {"status": "slow_down"}
    return {"status": "denied", "error": error or "unknown error"}


def _persist_oauth_token(token: str) -> None:
    _ensure_cache_dir()
    _write_json_secret(
        _oauth_cache_path(),
        {"access_token": token, "stored_at": int(time.time())},
    )


def clear_cached_credentials() -> dict[str, bool]:
    """Remove cached OAuth + session tokens. Used by ``brains copilot-logout``."""
    removed: dict[str, bool] = {}
    for label, path in (
        ("oauth", _oauth_cache_path()),
        ("session", _session_cache_path()),
    ):
        try:
            path.unlink()
            removed[label] = True
        except FileNotFoundError:
            removed[label] = False
        except OSError:
            removed[label] = False
    return removed


def auth_status() -> dict[str, Any]:
    """Diagnostic snapshot used by ``brains copilot-status``."""
    env_token = _from_env()
    cache_token = _from_cache()
    gh_token = _from_gh_cli()
    active_source: str | None = None
    if env_token:
        active_source = "env"
    elif cache_token:
        active_source = "cache"
    elif gh_token:
        active_source = "gh-cli"
    return {
        "active_source": active_source,
        "env_present": bool(env_token),
        "cache_present": bool(cache_token),
        "gh_cli_present": bool(gh_token),
        "use_gh_cli": settings.github_copilot_use_gh_cli,
        "cache_dir": str(_cache_dir()),
    }


__all__ = [
    "ACCESS_TOKEN_URL",
    "CLIENT_ID",
    "COPILOT_TOS_WARNING",
    "CopilotAuthError",
    "CopilotSession",
    "DEVICE_CODE_URL",
    "DeviceCode",
    "SESSION_TOKEN_URL",
    "assert_copilot_proxy_allowed",
    "auth_status",
    "clear_cached_credentials",
    "get_session",
    "poll_device_flow",
    "poll_device_flow_once",
    "resolve_oauth_token",
    "start_device_flow",
]

"""Unit tests for brains.auth.copilot."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from brains.auth import copilot as auth
from brains.config import settings


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point every cache + setting at a per-test tempdir.

    Every test gets a fresh cache_dir, no env token, and gh-cli enabled
    by default. Individual tests override what they need to.
    """
    monkeypatch.setattr(settings, "github_copilot_cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "")
    monkeypatch.setattr(settings, "github_copilot_use_gh_cli", True)
    monkeypatch.setattr(settings, "github_copilot_timeout_seconds", 5.0)
    # Default: pretend gh is not on PATH so tests start from a clean slate.
    monkeypatch.setattr(auth.shutil, "which", lambda _name: None)
    yield


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        payload: dict | None = None,
        status_code: int = 200,
        text: str | None = None,
    ):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


# ---------------------------------------------------------------------------
# resolve_oauth_token: priority + fallbacks
# ---------------------------------------------------------------------------


def test_resolve_prefers_env_over_cache_and_gh(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "env-token")
    # cache + gh both present but env must win
    (Path(tmp_path) / "github_copilot_oauth.json").write_text(
        json.dumps({"access_token": "cache-token"})
    )
    monkeypatch.setattr(auth.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "gh-token\n", ""),
    )
    assert auth.resolve_oauth_token() == "env-token"


def test_resolve_falls_back_to_cache_when_env_empty(tmp_path):
    (Path(tmp_path) / "github_copilot_oauth.json").write_text(
        json.dumps({"access_token": "cache-token"})
    )
    assert auth.resolve_oauth_token() == "cache-token"


def test_resolve_falls_back_to_gh_cli_when_other_sources_empty(monkeypatch):
    monkeypatch.setattr(auth.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "gh-token\n", ""),
    )
    assert auth.resolve_oauth_token() == "gh-token"


def test_resolve_skips_gh_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "github_copilot_use_gh_cli", False)
    monkeypatch.setattr(auth.shutil, "which", lambda _n: "/usr/bin/gh")
    captured = {"called": False}

    def _fail(*_a, **_k):
        captured["called"] = True
        return subprocess.CompletedProcess([], 0, "gh-token", "")

    monkeypatch.setattr(auth.subprocess, "run", _fail)
    with pytest.raises(auth.CopilotAuthError, match="no GitHub Copilot OAuth token"):
        auth.resolve_oauth_token()
    assert captured["called"] is False


def test_resolve_handles_gh_failure_gracefully(monkeypatch):
    monkeypatch.setattr(auth.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "not logged in"),
    )
    with pytest.raises(auth.CopilotAuthError):
        auth.resolve_oauth_token()


def test_resolve_handles_gh_timeout_gracefully(monkeypatch):
    monkeypatch.setattr(auth.shutil, "which", lambda _n: "/usr/bin/gh")

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=5)

    monkeypatch.setattr(auth.subprocess, "run", _timeout)
    with pytest.raises(auth.CopilotAuthError):
        auth.resolve_oauth_token()


def test_resolve_raises_when_no_source_available():
    with pytest.raises(auth.CopilotAuthError, match="no GitHub Copilot OAuth token"):
        auth.resolve_oauth_token()


# ---------------------------------------------------------------------------
# session-token exchange + caching
# ---------------------------------------------------------------------------


def test_get_session_exchanges_and_caches(monkeypatch):
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "oauth-A")
    future = int(time.time()) + 600
    captured: dict = {}

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return _FakeResponse(
            {
                "token": "session-A",
                "expires_at": future,
                "endpoints": {"api": "https://api.individual.githubcopilot.com"},
            }
        )

    monkeypatch.setattr(auth.httpx, "get", _fake_get)

    session = auth.get_session()
    assert session.token == "session-A"
    assert session.chat_base_url == "https://api.individual.githubcopilot.com"
    assert session.expires_at == future
    assert captured["url"] == auth.SESSION_TOKEN_URL
    assert captured["headers"]["Authorization"] == "token oauth-A"
    # cache file written for the oauth fingerprint
    cached = json.loads((auth._session_cache_path()).read_text())
    assert cached["token"] == "session-A"
    assert cached["fingerprint"] == auth._fingerprint("oauth-A")


def test_get_session_reuses_cached_when_fresh(monkeypatch):
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "oauth-A")
    future = int(time.time()) + 600
    auth._ensure_cache_dir()
    auth._session_cache_path().write_text(
        json.dumps(
            {
                "fingerprint": auth._fingerprint("oauth-A"),
                "token": "cached-session",
                "chat_base_url": "https://api.githubcopilot.com",
                "expires_at": future,
            }
        )
    )

    def _explode(*_a, **_k):
        raise AssertionError("should not call httpx.get when cache is fresh")

    monkeypatch.setattr(auth.httpx, "get", _explode)
    session = auth.get_session()
    assert session.token == "cached-session"


def test_get_session_refreshes_when_cache_near_expiry(monkeypatch):
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "oauth-A")
    near = int(time.time()) + 10  # under the 60s safety margin
    auth._ensure_cache_dir()
    auth._session_cache_path().write_text(
        json.dumps(
            {
                "fingerprint": auth._fingerprint("oauth-A"),
                "token": "stale-session",
                "chat_base_url": "https://api.githubcopilot.com",
                "expires_at": near,
            }
        )
    )
    monkeypatch.setattr(
        auth.httpx,
        "get",
        lambda *_a, **_k: _FakeResponse(
            {
                "token": "fresh-session",
                "expires_at": int(time.time()) + 600,
                "endpoints": {"api": "https://api.githubcopilot.com"},
            }
        ),
    )
    assert auth.get_session().token == "fresh-session"


def test_cache_invalidated_when_oauth_token_changes(monkeypatch):
    """Rotating the OAuth token must force a re-exchange (fingerprint mismatch)."""
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "oauth-OLD")
    auth._ensure_cache_dir()
    auth._session_cache_path().write_text(
        json.dumps(
            {
                "fingerprint": auth._fingerprint("oauth-OLD"),
                "token": "old-session",
                "chat_base_url": "https://api.githubcopilot.com",
                "expires_at": int(time.time()) + 600,
            }
        )
    )
    # Rotate.
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "oauth-NEW")
    monkeypatch.setattr(
        auth.httpx,
        "get",
        lambda *_a, **_k: _FakeResponse(
            {
                "token": "new-session",
                "expires_at": int(time.time()) + 600,
                "endpoints": {"api": "https://api.githubcopilot.com"},
            }
        ),
    )
    assert auth.get_session().token == "new-session"


def test_session_exchange_401_raises_auth_error(monkeypatch):
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "bad-token")
    monkeypatch.setattr(
        auth.httpx,
        "get",
        lambda *_a, **_k: _FakeResponse({"message": "Bad credentials"}, status_code=401),
    )
    with pytest.raises(auth.CopilotAuthError, match="401"):
        auth.get_session()


def test_session_exchange_500_raises_auth_error(monkeypatch):
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "tok")
    monkeypatch.setattr(
        auth.httpx,
        "get",
        lambda *_a, **_k: _FakeResponse({"error": "boom"}, status_code=502, text="bad gateway"),
    )
    with pytest.raises(auth.CopilotAuthError, match="502"):
        auth.get_session()


def test_session_exchange_timeout_raises_auth_error(monkeypatch):
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "tok")

    def _timeout(*_a, **_k):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(auth.httpx, "get", _timeout)
    with pytest.raises(auth.CopilotAuthError, match="timed out"):
        auth.get_session()


def test_session_exchange_missing_fields_raises(monkeypatch):
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "tok")
    monkeypatch.setattr(
        auth.httpx,
        "get",
        lambda *_a, **_k: _FakeResponse({"token": "", "expires_at": 0}),
    )
    with pytest.raises(auth.CopilotAuthError, match="missing token"):
        auth.get_session()


# ---------------------------------------------------------------------------
# clear + status
# ---------------------------------------------------------------------------


def test_clear_cached_credentials_removes_both_files(tmp_path):
    auth._ensure_cache_dir()
    auth._oauth_cache_path().write_text("{}")
    auth._session_cache_path().write_text("{}")
    removed = auth.clear_cached_credentials()
    assert removed == {"oauth": True, "session": True}
    assert not auth._oauth_cache_path().exists()
    assert not auth._session_cache_path().exists()


def test_clear_returns_false_for_missing_files(tmp_path):
    removed = auth.clear_cached_credentials()
    assert removed == {"oauth": False, "session": False}


def test_auth_status_reports_active_source(monkeypatch):
    monkeypatch.setattr(settings, "github_copilot_oauth_token", "env-token")
    status = auth.auth_status()
    assert status["active_source"] == "env"
    assert status["env_present"] is True
    assert status["use_gh_cli"] is True


def test_auth_status_reports_no_source_when_empty():
    status = auth.auth_status()
    assert status["active_source"] is None
    assert status["env_present"] is False
    assert status["cache_present"] is False
    assert status["gh_cli_present"] is False

"""Unit tests for brains.providers.github_copilot."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from brains.auth import copilot as auth
from brains.config import settings
from brains.providers.github_copilot import GitHubCopilotProvider
from brains.providers.openai_compatible import ProviderInvocationError
from brains.providers.registry import get_provider


@pytest.fixture(autouse=True)
def _enable_copilot_proxy(monkeypatch):
    """The github_copilot proxy is default-OFF (a safety gate). Enable it for
    the provider unit tests, which exercise the happy path. Gate-refusal tests
    re-disable it (or change backend / operator count) explicitly."""
    monkeypatch.setattr(settings, "allow_copilot_proxy", True, raising=False)
    monkeypatch.delenv("BRAINS_EXPERIMENTAL_COPILOT_PROVIDER", raising=False)
    # The proxy gate also refuses when >1 operator is configured. The test DB
    # is shared across the session, so an earlier test creating operators must
    # not flip this gate — pin the happy-path count so these tests stay
    # order-independent. (Gate-refusal tests patch this themselves.)
    monkeypatch.setattr(auth, "_configured_operator_count", lambda: 1)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        payload: dict | Exception | None = None,
        status_code: int = 200,
        text: str | None = None,
    ):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        if text is not None:
            self.text = text
        elif isinstance(self._payload, Exception):
            self.text = ""
        else:
            self.text = json.dumps(self._payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeStream:
    def __init__(
        self,
        lines: list[str],
        status_code: int = 200,
        error_payload: dict | None = None,
    ):
        self._lines = lines
        self.status_code = status_code
        self._error_payload = error_payload or {}
        self.text = json.dumps(self._error_payload) if error_payload else ""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self):
        return b""

    def json(self):
        return self._error_payload

    def iter_lines(self):
        yield from self._lines


@pytest.fixture
def _stub_session(monkeypatch):
    """Replace the auth resolver with a fixed in-memory session."""

    def _fake(*, force_refresh: bool = False):  # noqa: ARG001
        return auth.CopilotSession(
            token="sess-tok",
            chat_base_url="https://api.githubcopilot.com",
            expires_at=int(time.time()) + 600,
        )

    monkeypatch.setattr("brains.providers.github_copilot.get_session", _fake)
    return _fake


# ---------------------------------------------------------------------------
# registry wiring
# ---------------------------------------------------------------------------


def test_registry_exposes_github_copilot_provider():
    provider = get_provider("github_copilot")
    assert isinstance(provider, GitHubCopilotProvider)


# ---------------------------------------------------------------------------
# safety gate (default-off; refused in shared contexts)
# ---------------------------------------------------------------------------


def test_complete_refused_when_proxy_disabled(monkeypatch, _stub_session):
    monkeypatch.setattr(settings, "allow_copilot_proxy", False, raising=False)
    with pytest.raises(ProviderInvocationError, match="disabled by default"):
        GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])


def test_experimental_env_flag_enables_proxy(monkeypatch, _stub_session):
    monkeypatch.setattr(settings, "allow_copilot_proxy", False, raising=False)
    monkeypatch.setenv("BRAINS_EXPERIMENTAL_COPILOT_PROVIDER", "1")
    monkeypatch.setattr(
        "brains.providers.github_copilot.httpx.post",
        lambda *_a, **_k: _FakeResponse({"choices": [{"message": {"content": "ok"}}]}),
    )
    out = GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])
    assert out["choices"][0]["message"]["content"] == "ok"


def test_complete_refused_on_postgres_backend(monkeypatch, _stub_session):
    # Enabled (autouse) but a shared Postgres backend must refuse: the token
    # cache is per-machine and would be shared across operators.
    monkeypatch.setattr(settings.subsystems.storage, "backend", "postgres", raising=False)
    with pytest.raises(ProviderInvocationError, match="shared Postgres"):
        GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])


def test_complete_refused_when_multiple_operators(monkeypatch, _stub_session):
    monkeypatch.setattr("brains.auth.copilot._configured_operator_count", lambda: 2)
    with pytest.raises(ProviderInvocationError, match="multiple operators"):
        GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


def test_complete_forwards_to_session_endpoint(monkeypatch, _stub_session):
    captured: dict = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers", {})
        return _FakeResponse(
            {
                "id": "chatcmpl-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    monkeypatch.setattr("brains.providers.github_copilot.httpx.post", _fake_post)

    response = GitHubCopilotProvider().complete(
        "gpt-5", [{"role": "user", "content": "hello"}], temperature=0.2
    )

    assert response["choices"][0]["message"]["content"] == "hi"
    assert captured["url"] == "https://api.githubcopilot.com/chat/completions"
    assert captured["json"]["model"] == "gpt-5"
    assert captured["json"]["temperature"] == 0.2
    assert captured["json"]["stream"] is False
    # Required Copilot identity headers
    assert captured["headers"]["Authorization"] == "Bearer sess-tok"
    assert captured["headers"]["Copilot-Integration-Id"] == "vscode-chat"
    assert "Editor-Version" in captured["headers"]


def test_complete_omits_unset_optional_kwargs(monkeypatch, _stub_session):
    captured: dict = {}

    def _fake_post(url, **kwargs):
        captured["json"] = kwargs.get("json")
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("brains.providers.github_copilot.httpx.post", _fake_post)
    GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])
    payload = captured["json"]
    for key in ("temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice"):
        assert key not in payload, f"{key} should be omitted when not provided"


def test_complete_refreshes_session_on_401(monkeypatch):
    """A first-call 401 must trigger a forced session refresh + retry."""
    sessions: list[auth.CopilotSession] = [
        auth.CopilotSession(
            token="stale",
            chat_base_url="https://api.githubcopilot.com",
            expires_at=int(time.time()) + 600,
        ),
        auth.CopilotSession(
            token="fresh",
            chat_base_url="https://api.githubcopilot.com",
            expires_at=int(time.time()) + 600,
        ),
    ]
    calls: list[bool] = []

    def _fake_session(*, force_refresh: bool = False):
        calls.append(force_refresh)
        return sessions.pop(0) if sessions else sessions[-1]

    monkeypatch.setattr("brains.providers.github_copilot.get_session", _fake_session)

    posted_tokens: list[str] = []

    def _fake_post(_url, **kwargs):
        posted_tokens.append(kwargs["headers"]["Authorization"])
        if kwargs["headers"]["Authorization"].endswith("stale"):
            return _FakeResponse({"error": {"message": "expired"}}, status_code=401)
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("brains.providers.github_copilot.httpx.post", _fake_post)

    GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])
    assert posted_tokens == ["Bearer stale", "Bearer fresh"]
    assert calls == [False, True]


def test_complete_401_after_refresh_raises(monkeypatch):
    fixed = auth.CopilotSession(
        token="t",
        chat_base_url="https://api.githubcopilot.com",
        expires_at=int(time.time()) + 600,
    )
    monkeypatch.setattr(
        "brains.providers.github_copilot.get_session",
        lambda *, force_refresh=False: fixed,
    )
    monkeypatch.setattr(
        "brains.providers.github_copilot.httpx.post",
        lambda *_a, **_k: _FakeResponse({"error": {"message": "no"}}, status_code=401),
    )
    with pytest.raises(ProviderInvocationError, match="401"):
        GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])


def test_complete_500_raises_structured_error(monkeypatch, _stub_session):
    monkeypatch.setattr(
        "brains.providers.github_copilot.httpx.post",
        lambda *_a, **_k: _FakeResponse({"error": {"message": "boom"}}, status_code=500),
    )
    with pytest.raises(ProviderInvocationError, match=r"returned 500: boom"):
        GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])


def test_complete_redacts_sensitive_upstream_error(monkeypatch, _stub_session):
    token = "gho_abcdefghijklmnopqrstuvwxyz123456"
    monkeypatch.setattr(
        "brains.providers.github_copilot.httpx.post",
        lambda *_a, **_k: _FakeResponse(
            {
                "error": {
                    "message": f"account user@example.com rejected token {token} for subscription"
                }
            },
            status_code=403,
        ),
    )
    with pytest.raises(ProviderInvocationError) as exc:
        GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])
    message = str(exc.value)
    assert "user@example.com" not in message
    assert token not in message
    assert "account [redacted] rejected token [redacted] for subscription" in message


def test_complete_timeout_raises_structured_error(monkeypatch, _stub_session):
    def _timeout(*_a, **_k):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr("brains.providers.github_copilot.httpx.post", _timeout)
    with pytest.raises(ProviderInvocationError, match="timed out"):
        GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])


def test_complete_translates_auth_error_to_provider_invocation_error(monkeypatch):
    def _no_token(*, force_refresh: bool = False):  # noqa: ARG001
        raise auth.CopilotAuthError("no token")

    monkeypatch.setattr("brains.providers.github_copilot.get_session", _no_token)
    with pytest.raises(ProviderInvocationError, match="github_copilot: no token"):
        GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])


def test_complete_invalid_json_raises(monkeypatch, _stub_session):
    monkeypatch.setattr(
        "brains.providers.github_copilot.httpx.post",
        lambda *_a, **_k: _FakeResponse(ValueError("nope"), text="not-json-body"),
    )
    with pytest.raises(ProviderInvocationError, match="invalid JSON"):
        GitHubCopilotProvider().complete("gpt-5", [{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


def test_stream_yields_sse_payloads(monkeypatch, _stub_session):
    lines = [
        "",
        "data: " + json.dumps({"choices": [{"delta": {"content": "Hel"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"content": "lo"}}]}),
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        "brains.providers.github_copilot.httpx.stream",
        lambda *_a, **_k: _FakeStream(lines),
    )
    chunks = list(GitHubCopilotProvider().stream("gpt-5", [{"role": "user", "content": "hi"}]))
    # SSE data-prefix stripped, [DONE] filtered out, only real chunks remain
    assert len(chunks) == 2
    decoded = [json.loads(c) for c in chunks]
    assert decoded[0]["choices"][0]["delta"]["content"] == "Hel"
    assert decoded[1]["choices"][0]["delta"]["content"] == "lo"


def test_stream_4xx_raises_structured(monkeypatch, _stub_session):
    monkeypatch.setattr(
        "brains.providers.github_copilot.httpx.stream",
        lambda *_a, **_k: _FakeStream([], status_code=400, error_payload={"error": "bad"}),
    )
    with pytest.raises(ProviderInvocationError, match=r"returned 400: bad"):
        list(GitHubCopilotProvider().stream("gpt-5", [{"role": "user", "content": "hi"}]))


def test_stream_timeout_raises_structured(monkeypatch, _stub_session):
    def _timeout(*_a, **_k):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr("brains.providers.github_copilot.httpx.stream", _timeout)
    with pytest.raises(ProviderInvocationError, match="timed out"):
        list(GitHubCopilotProvider().stream("gpt-5", [{"role": "user", "content": "hi"}]))


def test_extract_capabilities_distills_reasoning_levels():
    """The Copilot ``capabilities`` block is distilled to the operator-
    relevant bits: the reasoning_effort (thinking) levels, headline flags,
    context window, and family."""
    from brains.providers.github_copilot import _extract_capabilities

    caps = {
        "family": "gpt-5.4",
        "limits": {"max_context_window_tokens": 264000, "max_output_tokens": 64000},
        "supports": {
            "reasoning_effort": ["none", "low", "medium", "high", "xhigh"],
            "vision": True,
            "streaming": True,
            "tool_calls": True,
        },
    }
    out = _extract_capabilities(caps)
    assert out["reasoning_effort"] == ["none", "low", "medium", "high", "xhigh"]
    assert out["context_window"] == 264000
    assert out["max_output_tokens"] == 64000
    assert out["vision"] is True
    assert out["family"] == "gpt-5.4"


def test_extract_capabilities_omits_reasoning_for_non_reasoning_models():
    from brains.providers.github_copilot import _extract_capabilities

    out = _extract_capabilities({"supports": {"vision": True, "tool_calls": True}})
    assert "reasoning_effort" not in out
    assert out["vision"] is True
    assert _extract_capabilities(None) == {}


def test_build_payload_forwards_reasoning_effort():
    """A client can pick a thinking level by passing reasoning_effort —
    it must reach the upstream payload (it used to be dropped)."""
    from brains.providers.github_copilot import _build_payload

    payload = _build_payload(
        "gpt-5.4",
        [{"role": "user", "content": "hi"}],
        stream=False,
        kwargs={"reasoning_effort": "high"},
    )
    assert payload["reasoning_effort"] == "high"

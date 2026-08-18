"""Tests for the OpenAI-compatible HTTP provider.

We never hit a real endpoint. ``httpx.MockTransport`` lets us craft the
exact upstream response (status code, headers, body) for each branch:
success, timeout, transport error, HTTP error, malformed JSON, and SSE
streaming. We also test the small helpers in isolation - header
construction, payload kwargs filtering, and error-detail extraction -
because those are the parts that silently mangle real requests if they
regress.

The provider is instantiated with explicit constructor args so the
process-global ``settings`` is never read; this keeps the tests
hermetic and parallel-safe.
"""

from __future__ import annotations

import json

import httpx
import pytest

from brains.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderInvocationError,
)


def _provider_with(transport: httpx.BaseTransport, **kw: object) -> OpenAICompatibleProvider:
    """Build a provider whose httpx calls go through ``transport``.

    We monkeypatch httpx.post / httpx.stream inside each test rather than
    at construction time because the provider calls those module-level
    functions directly. See individual tests for the pattern.
    """
    return OpenAICompatibleProvider(
        base_url=kw.get("base_url", "http://upstream.test/v1"),
        api_key=kw.get("api_key", "sk-test"),
        timeout=kw.get("timeout", 5.0),
    )


# --- header + payload helpers -------------------------------------------


def test_headers_include_bearer_when_api_key_set() -> None:
    p = OpenAICompatibleProvider(base_url="http://x", api_key="sk-abc", timeout=1)
    assert p._headers() == {
        "content-type": "application/json",
        "authorization": "Bearer sk-abc",
    }


def test_headers_omit_authorization_when_api_key_blank() -> None:
    p = OpenAICompatibleProvider(base_url="http://x", api_key="", timeout=1)
    assert p._headers() == {"content-type": "application/json"}


def test_build_payload_filters_unknown_kwargs_and_drops_none() -> None:
    p = OpenAICompatibleProvider(base_url="http://x", api_key="k", timeout=1)
    payload = p._build_payload(
        "gpt-4o-mini",
        [{"role": "user", "content": "hi"}],
        stream=True,
        temperature=0.2,
        top_p=None,  # explicit None must be dropped
        max_tokens=128,
        stop=["\n"],
        tools=[{"type": "function"}],
        tool_choice="auto",
        extra_kwarg_that_should_be_ignored="nope",
    )
    assert payload == {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "temperature": 0.2,
        "max_tokens": 128,
        "stop": ["\n"],
        "tools": [{"type": "function"}],
        "tool_choice": "auto",
    }


# --- _extract_error edge cases ------------------------------------------


def test_extract_error_pulls_nested_message_when_present() -> None:
    resp = httpx.Response(400, json={"error": {"message": "bad model"}})
    assert OpenAICompatibleProvider._extract_error(resp) == "bad model"


def test_extract_error_pulls_string_error_field() -> None:
    resp = httpx.Response(400, json={"error": "rate limited"})
    assert OpenAICompatibleProvider._extract_error(resp) == "rate limited"


def test_extract_error_falls_back_to_text_when_not_json() -> None:
    resp = httpx.Response(500, text="upstream exploded")
    assert OpenAICompatibleProvider._extract_error(resp) == "upstream exploded"


def test_extract_error_truncates_long_unstructured_json() -> None:
    body = {"weird": "x" * 1000}
    resp = httpx.Response(400, json=body)
    detail = OpenAICompatibleProvider._extract_error(resp)
    assert len(detail) <= 512


# --- complete() ----------------------------------------------------------


def _patch_post(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Route every ``httpx.post`` call through a MockTransport handler."""
    seen: list[httpx.Request] = []

    def _capturing_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_capturing_handler)

    def _post(url, **kw):
        # Honor json= / headers= / timeout= just like httpx.post does.
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kw)

    monkeypatch.setattr("brains.providers.openai_compatible.httpx.post", _post)
    return seen


def test_complete_returns_upstream_json_on_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    seen = _patch_post(monkeypatch, _handler)
    p = _provider_with(httpx.MockTransport(_handler))
    result = p.complete("gpt-4o-mini", [{"role": "user", "content": "ping"}])
    assert result == {"choices": [{"message": {"content": "hi"}}]}
    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == "http://upstream.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["stream"] is False
    assert body["model"] == "gpt-4o-mini"


def test_complete_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _post(*_a, **_kw):
        raise httpx.ReadTimeout("simulated timeout")

    monkeypatch.setattr("brains.providers.openai_compatible.httpx.post", _post)
    p = OpenAICompatibleProvider(base_url="http://x", api_key="k", timeout=1)
    with pytest.raises(ProviderInvocationError, match="timed out"):
        p.complete("m", [{"role": "user", "content": "x"}])


def test_complete_raises_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _post(*_a, **_kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("brains.providers.openai_compatible.httpx.post", _post)
    p = OpenAICompatibleProvider(base_url="http://x", api_key="k", timeout=1)
    with pytest.raises(ProviderInvocationError, match="transport error"):
        p.complete("m", [{"role": "user", "content": "x"}])


def test_complete_raises_with_status_and_message_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    _patch_post(monkeypatch, _handler)
    p = _provider_with(httpx.MockTransport(_handler))
    with pytest.raises(ProviderInvocationError, match="429.*rate limited"):
        p.complete("m", [{"role": "user", "content": "x"}])


def test_complete_raises_on_invalid_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all", headers={"content-type": "text/plain"})

    _patch_post(monkeypatch, _handler)
    p = _provider_with(httpx.MockTransport(_handler))
    with pytest.raises(ProviderInvocationError, match="invalid JSON"):
        p.complete("m", [{"role": "user", "content": "x"}])


# --- stream() ------------------------------------------------------------


class _FakeStreamingResponse:
    """Context-managed stand-in for httpx.stream's response object."""

    def __init__(self, status_code: int, lines: list[bytes], body_text: str = "") -> None:
        self.status_code = status_code
        self._lines = lines
        self.text = body_text

    def __enter__(self) -> _FakeStreamingResponse:
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def iter_lines(self):
        return iter(self._lines)

    def json(self):
        return json.loads(self.text) if self.text else {}


def test_stream_yields_sse_data_payloads_and_skips_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_lines = [
        b'data: {"choices":[{"delta":{"content":"hel"}}]}',
        b"",  # blank line should be skipped
        b'data: {"choices":[{"delta":{"content":"lo"}}]}',
        b"data: [DONE]",
    ]

    def _stream(_method, _url, **_kw):
        return _FakeStreamingResponse(200, payload_lines)

    monkeypatch.setattr("brains.providers.openai_compatible.httpx.stream", _stream)
    p = OpenAICompatibleProvider(base_url="http://x", api_key="k", timeout=1)
    chunks = list(p.stream("m", [{"role": "user", "content": "x"}]))
    assert chunks == [
        '{"choices":[{"delta":{"content":"hel"}}]}',
        '{"choices":[{"delta":{"content":"lo"}}]}',
    ]


def test_stream_raises_on_upstream_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _stream(_method, _url, **_kw):
        return _FakeStreamingResponse(503, [], body_text='{"error":{"message":"down"}}')

    monkeypatch.setattr("brains.providers.openai_compatible.httpx.stream", _stream)
    p = OpenAICompatibleProvider(base_url="http://x", api_key="k", timeout=1)
    with pytest.raises(ProviderInvocationError, match="503.*down"):
        list(p.stream("m", [{"role": "user", "content": "x"}]))


def test_stream_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _stream(*_a, **_kw):
        raise httpx.ReadTimeout("slow upstream")

    monkeypatch.setattr("brains.providers.openai_compatible.httpx.stream", _stream)
    p = OpenAICompatibleProvider(base_url="http://x", api_key="k", timeout=1)
    with pytest.raises(ProviderInvocationError, match="timed out"):
        list(p.stream("m", [{"role": "user", "content": "x"}]))


def test_stream_raises_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _stream(*_a, **_kw):
        raise httpx.ConnectError("dns failure")

    monkeypatch.setattr("brains.providers.openai_compatible.httpx.stream", _stream)
    p = OpenAICompatibleProvider(base_url="http://x", api_key="k", timeout=1)
    with pytest.raises(ProviderInvocationError, match="transport error"):
        list(p.stream("m", [{"role": "user", "content": "x"}]))


def test_base_url_trailing_slash_is_stripped() -> None:
    p = OpenAICompatibleProvider(base_url="http://x/v1/", api_key="k", timeout=1)
    assert p._base_url == "http://x/v1"


class _UnreadStreamingResponse:
    """Mimics a real httpx streaming response: ``.json()`` / ``.text`` raise
    ``httpx.ResponseNotRead`` until ``.read()`` is called. This reproduces the
    bug where an upstream error on a *streaming* request crashed the gateway
    with ResponseNotRead (hiding the real 4xx) because ``_extract_error``
    touched the body before it was loaded."""

    def __init__(self, status_code: int, payload: str) -> None:
        self.status_code = status_code
        self._payload = payload
        self._read = False

    def __enter__(self) -> _UnreadStreamingResponse:
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def iter_lines(self):
        return iter([])

    def read(self) -> bytes:
        self._read = True
        return self._payload.encode()

    def json(self):
        if not self._read:
            raise httpx.ResponseNotRead()
        return json.loads(self._payload)

    @property
    def text(self) -> str:
        if not self._read:
            raise httpx.ResponseNotRead()
        return self._payload


def test_stream_error_reads_unread_body_before_extracting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: a 413 (or any 4xx) on a streaming request must surface the
    # real upstream message, not crash with httpx.ResponseNotRead.
    def _stream(_method, _url, **_kw):
        return _UnreadStreamingResponse(413, '{"error":{"message":"request too large"}}')

    monkeypatch.setattr("brains.providers.openai_compatible.httpx.stream", _stream)
    p = OpenAICompatibleProvider(base_url="http://x", api_key="k", timeout=1)
    with pytest.raises(ProviderInvocationError, match="413.*request too large"):
        list(p.stream("m", [{"role": "user", "content": "x"}]))

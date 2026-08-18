import json

import httpx
import pytest

from brains.providers.ollama import (
    OllamaProvider,
    OllamaProviderError,
    _normalize_messages,
)
from brains.providers.registry import get_provider


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "bad status",
                request=httpx.Request("POST", "http://localhost"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeStream:
    """Minimal stand-in for the ``httpx.stream`` context manager."""

    def __init__(self, lines, status_code: int = 200, error_payload: dict | None = None):
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


def test_registry_exposes_ollama_provider():
    provider = get_provider("ollama")
    assert isinstance(provider, OllamaProvider)


def test_ollama_complete_success(monkeypatch):
    def _fake_post(*args, **kwargs):
        return _FakeResponse({"message": {"role": "assistant", "content": "hello from ollama"}})

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _fake_post)

    provider = OllamaProvider()
    response = provider.complete("llama3.1", [{"role": "user", "content": "hi"}])

    assert response["object"] == "chat.completion"
    assert response["model"] == "llama3.1"
    assert response["choices"][0]["message"]["content"] == "hello from ollama"
    assert response["choices"][0]["finish_reason"] == "stop"


def test_ollama_timeout_is_structured_error(monkeypatch):
    def _timeout(*args, **kwargs):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _timeout)

    provider = OllamaProvider()
    with pytest.raises(OllamaProviderError, match="timed out"):
        provider.complete("llama3.1", [{"role": "user", "content": "hi"}])


# --- message normalization ----------------------------------------------


def test_normalize_messages_drops_only_truly_empty():
    """Empty-content messages are dropped — unless they carry tool data."""
    out = _normalize_messages(
        [
            {"role": "system", "content": "be concise"},
            {"role": "user", "content": ""},
            {"role": "user", "content": "   "},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "x"}}],
            },
            {"role": "tool", "content": "", "tool_call_id": "call_0"},
            {"role": "assistant", "content": "ok"},
        ]
    )
    assert [m["role"] for m in out] == ["system", "assistant", "tool", "assistant"]
    # native roles preserved (not flattened to a text prompt)
    assert out[0] == {"role": "system", "content": "be concise"}
    assert out[1]["tool_calls"] == [{"function": {"name": "x"}}]
    assert out[2]["tool_call_id"] == "call_0"


def test_normalize_messages_defaults_missing_role():
    assert _normalize_messages([{"content": "hi"}]) == [{"role": "user", "content": "hi"}]


# --- payload shaping -----------------------------------------------------


def test_ollama_complete_forwards_generation_options(monkeypatch):
    """temperature/top_p/max_tokens map into the Ollama ``options`` block."""
    captured: dict = {}

    def _capture(*_args, **kwargs):
        captured.update(kwargs)
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _capture)

    provider = OllamaProvider()
    provider.complete(
        "llama3.1",
        [{"role": "user", "content": "hi"}],
        temperature=0.3,
        max_tokens=128,
        top_p=0.9,
    )

    payload = captured["json"]
    assert payload["model"] == "llama3.1"
    assert payload["stream"] is False
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert "prompt" not in payload  # /api/chat, not /api/generate
    assert payload["options"] == {
        "temperature": 0.3,
        "num_predict": 128,  # mapped from max_tokens
        "top_p": 0.9,
    }


def test_ollama_complete_omits_options_when_unspecified(monkeypatch):
    """No kwargs means no ``options`` key — keeps Ollama on its defaults."""
    captured: dict = {}

    def _capture(*_args, **kwargs):
        captured.update(kwargs)
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _capture)

    OllamaProvider().complete("llama3.1", [{"role": "user", "content": "hi"}])
    assert "options" not in captured["json"]


def test_ollama_forwards_tools_in_payload(monkeypatch):
    captured: dict = {}

    def _capture(*_args, **kwargs):
        captured.update(kwargs)
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _capture)
    tools = [{"type": "function", "function": {"name": "x"}}]
    OllamaProvider().complete("m", [{"role": "user", "content": "hi"}], tools=tools)
    assert captured["json"]["tools"] == tools


# --- tool calling --------------------------------------------------------


def test_ollama_complete_maps_tool_calls(monkeypatch):
    def _fake(*_a, **_k):
        return _FakeResponse(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}
                    ],
                }
            }
        )

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _fake)
    resp = OllamaProvider().complete("llama3.1", [{"role": "user", "content": "weather?"}])
    choice = resp["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tc = choice["message"]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["id"]  # synthesized when Ollama omits one
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"city": "Paris"}


# --- error handling ------------------------------------------------------


def test_ollama_http_500_with_json_error_field(monkeypatch):
    def _fake(*_args, **_kwargs):
        return _FakeResponse({"error": "model not found"}, status_code=500)

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _fake)

    provider = OllamaProvider()
    with pytest.raises(OllamaProviderError, match=r"failed \(500\): model not found"):
        provider.complete("missing-model", [{"role": "user", "content": "hi"}])


def test_ollama_http_400_with_non_json_body(monkeypatch):
    def _fake(*_args, **_kwargs):
        return _FakeResponse(ValueError("not json"), status_code=400, text="bad request")

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _fake)

    provider = OllamaProvider()
    with pytest.raises(OllamaProviderError, match=r"failed \(400\): bad request"):
        provider.complete("m", [{"role": "user", "content": "hi"}])


def test_ollama_invalid_response_payload_is_structured_error(monkeypatch):
    def _fake(*_args, **_kwargs):
        return _FakeResponse(ValueError("not json"), status_code=200, text="garbage")

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _fake)

    provider = OllamaProvider()
    with pytest.raises(OllamaProviderError, match="Invalid Ollama response payload"):
        provider.complete("m", [{"role": "user", "content": "hi"}])


def test_ollama_non_dict_payload_is_structured_error(monkeypatch):
    def _fake(*_args, **_kwargs):
        return _FakeResponse(["not", "a", "dict"])

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _fake)

    with pytest.raises(OllamaProviderError, match="Invalid Ollama response payload"):
        OllamaProvider().complete("m", [{"role": "user", "content": "hi"}])


def test_ollama_empty_message_content_is_structured_error(monkeypatch):
    """A 200 whose message has no content and no tool_calls must raise."""

    def _fake(*_args, **_kwargs):
        return _FakeResponse({"message": {"role": "assistant", "content": ""}})

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _fake)

    provider = OllamaProvider()
    with pytest.raises(OllamaProviderError, match="empty response"):
        provider.complete("m", [{"role": "user", "content": "hi"}])


def test_ollama_generic_http_error_translated(monkeypatch):
    def _fake(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("brains.providers.ollama.httpx.post", _fake)

    provider = OllamaProvider()
    with pytest.raises(OllamaProviderError, match="failed: connection refused"):
        provider.complete("m", [{"role": "user", "content": "hi"}])


# --- streaming -----------------------------------------------------------


def test_ollama_stream_emits_token_deltas_then_stop(monkeypatch):
    """Real token streaming: per-line content deltas, then a final stop chunk."""
    lines = [
        json.dumps({"message": {"content": "Hel"}, "done": False}),
        json.dumps({"message": {"content": "lo"}, "done": False}),
        json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"}),
    ]
    monkeypatch.setattr("brains.providers.ollama.httpx.stream", lambda *a, **k: _FakeStream(lines))

    chunks = [
        json.loads(c)
        for c in OllamaProvider().stream("llama3.1", [{"role": "user", "content": "hi"}])
    ]
    text = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"][0]["delta"]
    )
    assert text == "Hello"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"][0]["delta"] == {}


def test_ollama_stream_maps_tool_calls(monkeypatch):
    lines = [
        json.dumps(
            {
                "message": {
                    "content": "",
                    "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}],
                },
                "done": True,
            }
        )
    ]
    monkeypatch.setattr("brains.providers.ollama.httpx.stream", lambda *a, **k: _FakeStream(lines))

    chunks = [
        json.loads(c) for c in OllamaProvider().stream("m", [{"role": "user", "content": "hi"}])
    ]
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert any("tool_calls" in d for d in deltas)
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_ollama_stream_http_error_is_structured(monkeypatch):
    monkeypatch.setattr(
        "brains.providers.ollama.httpx.stream",
        lambda *a, **k: _FakeStream([], status_code=500, error_payload={"error": "boom"}),
    )
    with pytest.raises(OllamaProviderError, match=r"failed \(500\): boom"):
        list(OllamaProvider().stream("m", [{"role": "user", "content": "hi"}]))

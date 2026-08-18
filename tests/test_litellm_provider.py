"""Tests for :mod:`brains.providers.litellm_provider`.

LiteLLM is an optional extra. We install a fake ``litellm`` module into
``sys.modules`` and reload the provider so its constructor's deferred
import resolves to our stub. This lets the test cover every code path
(success, kwargs filtering, exception translation, streaming, response
normalization for both ``model_dump`` and ``dict`` shapes) without
requiring the real LiteLLM package or hitting any upstream API.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from typing import Any

import pytest

from brains.providers.openai_compatible import ProviderInvocationError


class _FakeResp:
    """Mimics a litellm ModelResponse with ``model_dump``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


class _LegacyFakeResp:
    """Mimics an older litellm response that only has ``dict``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def dict(self) -> dict[str, Any]:  # noqa: A003 - mirrors litellm's API
        return dict(self._payload)


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch):
    """Install a fake litellm module and reload the provider against it.

    Yields a controller object the test uses to (a) set the next
    completion response or exception, and (b) inspect every call the
    provider made through us.
    """
    calls: list[dict[str, Any]] = []
    state: dict[str, Any] = {"next": None, "exc": None}

    def _completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if state["exc"] is not None:
            raise state["exc"]
        return state["next"]

    fake = types.ModuleType("litellm")
    fake.completion = _completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)

    # Provider was likely already imported with the real (missing) module
    # cached in sys.modules; reload so it re-runs its deferred import.
    import brains.providers.litellm_provider as ll_mod

    ll_mod = importlib.reload(ll_mod)

    controller = types.SimpleNamespace(
        calls=calls,
        set_response=lambda payload: state.update(next=payload, exc=None),
        set_exception=lambda exc: state.update(exc=exc),
        module=ll_mod,
    )
    yield controller


def test_complete_forwards_filtered_kwargs(fake_litellm) -> None:
    fake_litellm.set_response(_FakeResp({"choices": [{"message": {"content": "hi"}}]}))
    provider = fake_litellm.module.LiteLLMProvider(timeout=5.0)
    out = provider.complete(
        "openai/gpt-4o-mini",
        [{"role": "user", "content": "ping"}],
        temperature=0.3,
        top_p=None,  # explicit None must be dropped
        max_tokens=64,
        unsupported_kw="ignored",
    )
    assert out == {"choices": [{"message": {"content": "hi"}}]}
    assert len(fake_litellm.calls) == 1
    call = fake_litellm.calls[0]
    assert call["model"] == "openai/gpt-4o-mini"
    assert call["stream"] is False
    assert call["timeout"] == 5.0
    assert call["temperature"] == 0.3
    assert call["max_tokens"] == 64
    assert "top_p" not in call  # filtered because value was None
    assert "unsupported_kw" not in call  # filtered because not in allowlist


def test_complete_normalizes_legacy_dict_response(fake_litellm) -> None:
    fake_litellm.set_response(_LegacyFakeResp({"choices": [{"text": "hello"}]}))
    provider = fake_litellm.module.LiteLLMProvider()
    assert provider.complete("m", []) == {"choices": [{"text": "hello"}]}


def test_complete_accepts_plain_dict_response(fake_litellm) -> None:
    fake_litellm.set_response({"choices": [{"text": "raw"}]})
    provider = fake_litellm.module.LiteLLMProvider()
    assert provider.complete("m", []) == {"choices": [{"text": "raw"}]}


def test_complete_translates_upstream_exception(fake_litellm) -> None:
    fake_litellm.set_exception(RuntimeError("bad credentials"))
    provider = fake_litellm.module.LiteLLMProvider()
    with pytest.raises(ProviderInvocationError, match="bad credentials"):
        provider.complete("m", [])


def test_stream_yields_json_chunks(fake_litellm) -> None:
    chunks = [
        _FakeResp({"id": "1", "choices": [{"delta": {"content": "he"}}]}),
        _FakeResp({"id": "2", "choices": [{"delta": {"content": "llo"}}]}),
    ]
    fake_litellm.set_response(iter(chunks))

    provider = fake_litellm.module.LiteLLMProvider()
    out = list(provider.stream("m", [{"role": "user", "content": "x"}]))
    assert [json.loads(s) for s in out] == [
        {"id": "1", "choices": [{"delta": {"content": "he"}}]},
        {"id": "2", "choices": [{"delta": {"content": "llo"}}]},
    ]
    assert fake_litellm.calls[0]["stream"] is True


def test_stream_normalizes_legacy_chunks(fake_litellm) -> None:
    fake_litellm.set_response(iter([_LegacyFakeResp({"x": 1})]))
    provider = fake_litellm.module.LiteLLMProvider()
    out = list(provider.stream("m", []))
    assert out == [json.dumps({"x": 1})]


def test_stream_translates_creation_exception(fake_litellm) -> None:
    fake_litellm.set_exception(RuntimeError("rate limited"))
    provider = fake_litellm.module.LiteLLMProvider()
    with pytest.raises(ProviderInvocationError, match="rate limited"):
        list(provider.stream("m", []))


def test_stream_translates_iteration_exception(fake_litellm) -> None:
    def _bad_iter():
        yield _FakeResp({"ok": True})
        raise RuntimeError("connection reset")

    fake_litellm.set_response(_bad_iter())
    provider = fake_litellm.module.LiteLLMProvider()
    out_iter = provider.stream("m", [])
    next(out_iter)  # first chunk fine
    with pytest.raises(ProviderInvocationError, match="connection reset"):
        next(out_iter)

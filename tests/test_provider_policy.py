"""Tests for the per-provider retry + circuit-breaker policy layer.

Covers :mod:`brains.providers.policy`, the new
:class:`brains.config.ProviderPolicyConfig` overlay surface, and the
``get_provider`` wrap-with-policy behaviour. The module is the
implementation of the Phase 6 (README) / Phase 3 (roadmap) hardening
bullet ``Per-provider retry / timeout / failover + circuit-breakers``.

Off-by-default is critical: every existing test in the suite assumes
``get_provider`` returns a raw provider, so the policy block must be a
no-op when no entry is configured.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from brains.config import ProviderPolicyConfig, settings
from brains.providers.base import Provider
from brains.providers.policy import (
    CircuitOpenError,
    ProviderPolicy,
    ResilientProvider,
    _next_backoff,
    reset_circuit,
    resolve_policy,
)
from brains.providers.registry import (
    ProviderInvocationError,
    get_provider,
)


@pytest.fixture(autouse=True)
def _isolate_circuits_and_policies(monkeypatch):
    """Each test gets clean breaker state and an empty policy map.

    We poke ``settings.provider_policies`` directly because Pydantic
    settings models accept attribute assignment for mutable fields.
    """
    reset_circuit()
    monkeypatch.setattr(settings, "provider_policies", {}, raising=False)
    yield
    reset_circuit()


@pytest.fixture
def fast_sleep(monkeypatch):
    """Replace the policy module's sleep with a no-op so tests don't wait."""
    slept: list[float] = []

    def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("brains.providers.policy._sleep", _record)
    return slept


class _FakeProvider(Provider):
    """Configurable test double: queue up complete/stream behaviours."""

    def __init__(
        self,
        complete_results: list[Any] | None = None,
        stream_results: list[Any] | None = None,
    ) -> None:
        self._complete_results = list(complete_results or [])
        self._stream_results = list(stream_results or [])
        self.complete_calls = 0
        self.stream_calls = 0

    def complete(self, model: str, messages: list[dict], **kwargs: Any) -> dict:
        self.complete_calls += 1
        if not self._complete_results:
            raise AssertionError("complete called more times than configured")
        outcome = self._complete_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stream(self, model: str, messages: list[dict], **kwargs: Any) -> Iterator[str]:
        self.stream_calls += 1
        if not self._stream_results:
            raise AssertionError("stream called more times than configured")
        outcome = self._stream_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        # outcome is a list of chunks (strings) or a callable returning an iterator
        if callable(outcome):
            return outcome()
        return iter(outcome)


# ----------------------------------------------------------------------
# ProviderPolicy defaults
# ----------------------------------------------------------------------


def test_provider_policy_defaults_are_off():
    p = ProviderPolicy()
    assert p.retry_enabled is False
    assert p.circuit_enabled is False


def test_provider_policy_enables_when_threshold_set():
    p = ProviderPolicy(retry_max_attempts=3, circuit_failure_threshold=2)
    assert p.retry_enabled is True
    assert p.circuit_enabled is True


def test_resolve_policy_returns_defaults_when_missing():
    p = resolve_policy("openai_compatible", settings)
    assert isinstance(p, ProviderPolicy)
    assert p.retry_enabled is False
    assert p.circuit_enabled is False


def test_resolve_policy_maps_config_fields(monkeypatch):
    cfg = ProviderPolicyConfig(
        retry_max_attempts=4,
        retry_initial_backoff_seconds=0.25,
        retry_max_backoff_seconds=2.0,
        retry_backoff_multiplier=3.0,
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=15.0,
    )
    monkeypatch.setattr(settings, "provider_policies", {"ollama": cfg}, raising=False)
    p = resolve_policy("ollama", settings)
    assert p.retry_max_attempts == 4
    assert p.retry_initial_backoff_seconds == 0.25
    assert p.retry_max_backoff_seconds == 2.0
    assert p.retry_backoff_multiplier == 3.0
    assert p.circuit_failure_threshold == 2
    assert p.circuit_cooldown_seconds == 15.0
    assert p.retry_enabled is True
    assert p.circuit_enabled is True


# ----------------------------------------------------------------------
# Exponential backoff math
# ----------------------------------------------------------------------


def test_next_backoff_grows_then_caps():
    p = ProviderPolicy(
        retry_max_attempts=10,
        retry_initial_backoff_seconds=0.5,
        retry_backoff_multiplier=2.0,
        retry_max_backoff_seconds=4.0,
    )
    assert _next_backoff(p, 1) == pytest.approx(0.5)
    assert _next_backoff(p, 2) == pytest.approx(1.0)
    assert _next_backoff(p, 3) == pytest.approx(2.0)
    assert _next_backoff(p, 4) == pytest.approx(4.0)
    # capped
    assert _next_backoff(p, 5) == pytest.approx(4.0)
    assert _next_backoff(p, 10) == pytest.approx(4.0)


# ----------------------------------------------------------------------
# Retry behaviour: complete()
# ----------------------------------------------------------------------


def test_complete_succeeds_first_attempt_no_retry(fast_sleep):
    inner = _FakeProvider(complete_results=[{"ok": True}])
    rp = ResilientProvider(
        inner,
        provider_name="t",
        policy=ProviderPolicy(retry_max_attempts=3),
    )
    assert rp.complete("m", []) == {"ok": True}
    assert inner.complete_calls == 1
    assert fast_sleep == []


def test_complete_retries_until_success(fast_sleep):
    inner = _FakeProvider(
        complete_results=[
            ProviderInvocationError("boom 1"),
            ProviderInvocationError("boom 2"),
            {"ok": True},
        ]
    )
    rp = ResilientProvider(
        inner,
        provider_name="t",
        policy=ProviderPolicy(
            retry_max_attempts=3,
            retry_initial_backoff_seconds=0.1,
            retry_backoff_multiplier=2.0,
            retry_max_backoff_seconds=1.0,
        ),
    )
    assert rp.complete("m", []) == {"ok": True}
    assert inner.complete_calls == 3
    assert fast_sleep == [pytest.approx(0.1), pytest.approx(0.2)]


def test_complete_exhausts_attempts_then_raises_original(fast_sleep):
    final = ProviderInvocationError("final")
    inner = _FakeProvider(
        complete_results=[
            ProviderInvocationError("boom 1"),
            final,
        ]
    )
    rp = ResilientProvider(
        inner,
        provider_name="t",
        policy=ProviderPolicy(retry_max_attempts=2),
    )
    with pytest.raises(ProviderInvocationError) as ei:
        rp.complete("m", [])
    assert ei.value is final
    assert inner.complete_calls == 2


def test_complete_default_policy_does_not_retry(fast_sleep):
    """retry_max_attempts=1 must mean exactly one attempt."""
    inner = _FakeProvider(complete_results=[ProviderInvocationError("boom")])
    rp = ResilientProvider(
        inner,
        provider_name="t",
        policy=ProviderPolicy(retry_max_attempts=1),
    )
    with pytest.raises(ProviderInvocationError):
        rp.complete("m", [])
    assert inner.complete_calls == 1
    assert fast_sleep == []


# ----------------------------------------------------------------------
# Circuit breaker behaviour
# ----------------------------------------------------------------------


def test_circuit_opens_after_threshold_failures(fast_sleep):
    inner = _FakeProvider(
        complete_results=[
            ProviderInvocationError("1"),
            ProviderInvocationError("2"),
        ]
    )
    policy = ProviderPolicy(
        retry_max_attempts=1,
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=30.0,
    )
    rp = ResilientProvider(inner, provider_name="t", policy=policy)

    with pytest.raises(ProviderInvocationError):
        rp.complete("m", [])
    with pytest.raises(ProviderInvocationError):
        rp.complete("m", [])
    # Third call short-circuits
    with pytest.raises(CircuitOpenError):
        rp.complete("m", [])
    assert inner.complete_calls == 2


def test_circuit_open_error_is_provider_invocation_error():
    assert issubclass(CircuitOpenError, ProviderInvocationError)


def test_circuit_closes_after_cooldown(monkeypatch, fast_sleep):
    fake_now = {"t": 100.0}

    def _now() -> float:
        return fake_now["t"]

    monkeypatch.setattr("brains.providers.policy._now", _now)

    inner = _FakeProvider(
        complete_results=[
            ProviderInvocationError("1"),
            ProviderInvocationError("2"),
            {"ok": True},
        ]
    )
    policy = ProviderPolicy(
        retry_max_attempts=1,
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=5.0,
    )
    rp = ResilientProvider(inner, provider_name="t", policy=policy)
    with pytest.raises(ProviderInvocationError):
        rp.complete("m", [])
    with pytest.raises(ProviderInvocationError):
        rp.complete("m", [])
    # Within cooldown -> short circuit
    fake_now["t"] = 101.0
    with pytest.raises(CircuitOpenError):
        rp.complete("m", [])
    # After cooldown -> allowed
    fake_now["t"] = 200.0
    assert rp.complete("m", []) == {"ok": True}


def test_circuit_resets_on_success(fast_sleep):
    inner = _FakeProvider(
        complete_results=[
            ProviderInvocationError("1"),
            {"ok": True},
            ProviderInvocationError("2"),
        ]
    )
    policy = ProviderPolicy(
        retry_max_attempts=1,
        circuit_failure_threshold=2,
    )
    rp = ResilientProvider(inner, provider_name="t", policy=policy)
    with pytest.raises(ProviderInvocationError):
        rp.complete("m", [])
    assert rp.complete("m", []) == {"ok": True}
    # Counter was reset by the success, so one more failure must NOT open
    with pytest.raises(ProviderInvocationError):
        rp.complete("m", [])
    # And the next call goes through (would short-circuit if breaker was open)
    inner._complete_results.append({"ok": True})
    assert rp.complete("m", []) == {"ok": True}


def test_circuit_disabled_means_threshold_zero(fast_sleep):
    """retry_max_attempts=1 + threshold=0 = always pass through, never open."""
    inner = _FakeProvider(
        complete_results=[
            ProviderInvocationError("1"),
            ProviderInvocationError("2"),
            ProviderInvocationError("3"),
        ]
    )
    policy = ProviderPolicy(retry_max_attempts=1, circuit_failure_threshold=0)
    rp = ResilientProvider(inner, provider_name="t", policy=policy)
    for _ in range(3):
        with pytest.raises(ProviderInvocationError):
            rp.complete("m", [])
    # No CircuitOpenError even after many failures
    assert inner.complete_calls == 3


# ----------------------------------------------------------------------
# Streaming behaviour
# ----------------------------------------------------------------------


def test_stream_retries_at_connection_open(fast_sleep):
    inner = _FakeProvider(
        stream_results=[
            ProviderInvocationError("conn refused"),
            ["chunk1", "chunk2"],
        ]
    )
    rp = ResilientProvider(
        inner,
        provider_name="t",
        policy=ProviderPolicy(retry_max_attempts=2, retry_initial_backoff_seconds=0.1),
    )
    chunks = list(rp.stream("m", []))
    assert chunks == ["chunk1", "chunk2"]
    assert inner.stream_calls == 2
    assert fast_sleep == [pytest.approx(0.1)]


def test_stream_does_not_retry_mid_stream(fast_sleep):
    """Once chunks have started flowing we can't replay."""

    def _failing_iterator() -> Iterator[str]:
        yield "good"
        raise ProviderInvocationError("died mid-stream")

    inner = _FakeProvider(stream_results=[_failing_iterator])
    rp = ResilientProvider(
        inner,
        provider_name="t",
        policy=ProviderPolicy(retry_max_attempts=3),
    )
    it = rp.stream("m", [])
    assert next(it) == "good"
    with pytest.raises(ProviderInvocationError, match="died mid-stream"):
        next(it)
    # Inner was called exactly once: no replay after first chunk
    assert inner.stream_calls == 1


def test_stream_mid_stream_failure_counts_toward_breaker(fast_sleep):
    """A failure after chunks started must still bump the breaker counter."""

    def _failing_iterator() -> Iterator[str]:
        yield "good"
        raise ProviderInvocationError("died mid-stream")

    inner = _FakeProvider(
        stream_results=[_failing_iterator, _failing_iterator],
        complete_results=[],
    )
    policy = ProviderPolicy(
        retry_max_attempts=1,
        circuit_failure_threshold=2,
    )
    rp = ResilientProvider(inner, provider_name="t", policy=policy)

    # First stream: starts, fails mid-stream
    it1 = rp.stream("m", [])
    assert next(it1) == "good"
    with pytest.raises(ProviderInvocationError):
        next(it1)

    # Second stream: starts, fails mid-stream -> threshold reached
    it2 = rp.stream("m", [])
    assert next(it2) == "good"
    with pytest.raises(ProviderInvocationError):
        next(it2)

    # Third call short-circuits because breaker is now open
    with pytest.raises(CircuitOpenError):
        rp.complete("m", [])


def test_stream_exhausts_retries(fast_sleep):
    inner = _FakeProvider(
        stream_results=[
            ProviderInvocationError("1"),
            ProviderInvocationError("2"),
        ]
    )
    rp = ResilientProvider(
        inner,
        provider_name="t",
        policy=ProviderPolicy(retry_max_attempts=2),
    )
    with pytest.raises(ProviderInvocationError):
        list(rp.stream("m", []))
    assert inner.stream_calls == 2


# ----------------------------------------------------------------------
# Registry integration
# ----------------------------------------------------------------------


def test_get_provider_returns_raw_when_no_policy(monkeypatch):
    """Off-by-default: every existing test in the suite relies on this."""
    monkeypatch.setattr(settings, "provider_policies", {}, raising=False)
    p = get_provider("echo")
    assert not isinstance(p, ResilientProvider)


def test_get_provider_wraps_when_policy_configured(monkeypatch):
    monkeypatch.setattr(
        settings,
        "provider_policies",
        {"echo": ProviderPolicyConfig(retry_max_attempts=2)},
        raising=False,
    )
    p = get_provider("echo")
    assert isinstance(p, ResilientProvider)
    assert p.policy.retry_max_attempts == 2
    # Inner is the raw provider
    from brains.providers.echo import EchoProvider

    assert isinstance(p.inner, EchoProvider)


def test_get_provider_wraps_when_only_circuit_configured(monkeypatch):
    monkeypatch.setattr(
        settings,
        "provider_policies",
        {"echo": ProviderPolicyConfig(circuit_failure_threshold=3)},
        raising=False,
    )
    p = get_provider("echo")
    assert isinstance(p, ResilientProvider)


# ----------------------------------------------------------------------
# Pydantic validation
# ----------------------------------------------------------------------


def test_provider_policy_config_rejects_negative_attempts():
    with pytest.raises(Exception):  # pydantic ValidationError
        ProviderPolicyConfig(retry_max_attempts=0)


def test_provider_policy_config_rejects_excessive_attempts():
    with pytest.raises(Exception):
        ProviderPolicyConfig(retry_max_attempts=999)


def test_provider_policy_config_rejects_negative_threshold():
    with pytest.raises(Exception):
        ProviderPolicyConfig(circuit_failure_threshold=-1)

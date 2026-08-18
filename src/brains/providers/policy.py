"""Per-provider retry + circuit-breaker policies.

This module is the missing Phase 6 (README) / Phase 3 (roadmap) hardening
layer: ``Per-provider retry / timeout / failover + circuit-breakers``.
Timeouts are already enforced inside each provider class
(``ollama_timeout_seconds``, ``openai_compatible_timeout_seconds``,
``litellm_timeout_seconds``); this module adds the missing two pieces:

* **Retry** with exponential backoff on transient
  :class:`brains.providers.registry.ProviderInvocationError` instances.
  Streaming requests only retry at the connection-open phase \u2014 once the
  first chunk has been forwarded to the client we cannot replay.
* **Circuit breaker**: after ``failure_threshold`` consecutive failures
  for a given (provider-name) the breaker opens and short-circuits
  subsequent calls for ``cooldown_seconds``. A single successful call
  (or the cooldown expiring) closes the breaker again.

Policies are looked up by provider name and are off-by-default \u2014 the
out-of-the-box behaviour is identical to today's "one attempt, no
breaker, surface the error" path, so existing tests stay green without
touching the policy block.

Configuration lives under :class:`brains.config.ProviderPolicyConfig`
and is exposed as :attr:`brains.config.Settings.provider_policies`.
Sample overlay::

    provider_policies:
      openai_compatible:
        retry_max_attempts: 3
        retry_initial_backoff_seconds: 0.5
        retry_backoff_multiplier: 2.0
        retry_max_backoff_seconds: 8.0
        circuit_failure_threshold: 5
        circuit_cooldown_seconds: 30
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from brains.providers.base import Provider
from brains.providers.registry import ProviderInvocationError

logger = logging.getLogger("brains.providers.policy")


@dataclass(frozen=True)
class ProviderPolicy:
    """Resolved retry + circuit-breaker policy for one provider name."""

    retry_max_attempts: int = 1
    retry_initial_backoff_seconds: float = 0.5
    retry_max_backoff_seconds: float = 8.0
    retry_backoff_multiplier: float = 2.0
    circuit_failure_threshold: int = 0
    circuit_cooldown_seconds: float = 30.0

    @property
    def retry_enabled(self) -> bool:
        return self.retry_max_attempts > 1

    @property
    def circuit_enabled(self) -> bool:
        return self.circuit_failure_threshold > 0


@dataclass
class _CircuitState:
    """Per-provider failure counter + open-until timestamp.

    ``open_until`` is the monotonic clock value after which the breaker
    closes again. Zero means the breaker is closed.
    """

    consecutive_failures: int = 0
    open_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


# Module-level state keyed by provider name. FastAPI runs requests on
# the same process so a plain dict + per-entry lock is enough.
_CIRCUITS: dict[str, _CircuitState] = {}
_CIRCUITS_LOCK = threading.Lock()


def _get_circuit(provider_name: str) -> _CircuitState:
    with _CIRCUITS_LOCK:
        state = _CIRCUITS.get(provider_name)
        if state is None:
            state = _CircuitState()
            _CIRCUITS[provider_name] = state
        return state


def reset_circuit(provider_name: str | None = None) -> None:
    """Test helper: clear circuit-breaker state.

    Pass a name to reset one provider, or ``None`` to reset every
    provider seen so far. Production callers never need this.
    """
    with _CIRCUITS_LOCK:
        if provider_name is None:
            _CIRCUITS.clear()
        else:
            _CIRCUITS.pop(provider_name, None)


class CircuitOpenError(ProviderInvocationError):
    """Raised when the circuit breaker is open and short-circuits the call.

    Subclasses :class:`ProviderInvocationError` so the existing gateway
    error path (HTTP 502 + structured SSE error chunk) covers it without
    a new branch.
    """


def _now() -> float:
    return time.monotonic()


def _check_circuit(provider_name: str, policy: ProviderPolicy) -> None:
    if not policy.circuit_enabled:
        return
    state = _get_circuit(provider_name)
    with state.lock:
        if state.open_until and _now() < state.open_until:
            remaining = state.open_until - _now()
            raise CircuitOpenError(
                f"{provider_name}: circuit breaker open for"
                f" another {remaining:.1f}s after"
                f" {policy.circuit_failure_threshold} consecutive failures"
            )
        # Cooldown elapsed \u2014 close the breaker so the next call gets a
        # real attempt. The failure counter is preserved until the next
        # success so a flaky upstream that recovers briefly then fails
        # again doesn't reset the threshold count.
        if state.open_until and _now() >= state.open_until:
            state.open_until = 0.0


def _record_success(provider_name: str, policy: ProviderPolicy) -> None:
    if not policy.circuit_enabled:
        return
    state = _get_circuit(provider_name)
    with state.lock:
        state.consecutive_failures = 0
        state.open_until = 0.0


def _record_failure(provider_name: str, policy: ProviderPolicy) -> None:
    if not policy.circuit_enabled:
        return
    state = _get_circuit(provider_name)
    with state.lock:
        state.consecutive_failures += 1
        if state.consecutive_failures >= policy.circuit_failure_threshold:
            state.open_until = _now() + policy.circuit_cooldown_seconds
            logger.warning(
                "circuit_open provider=%s failures=%d cooldown_s=%.1f",
                provider_name,
                state.consecutive_failures,
                policy.circuit_cooldown_seconds,
            )


def _sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch sleep without touching time.sleep globally."""
    time.sleep(seconds)


def _next_backoff(policy: ProviderPolicy, attempt: int) -> float:
    """attempt is 1-indexed; backoff is computed for the wait BEFORE the next attempt."""
    raw = policy.retry_initial_backoff_seconds * (policy.retry_backoff_multiplier ** (attempt - 1))
    return min(raw, policy.retry_max_backoff_seconds)


class ResilientProvider(Provider):
    """Wraps another :class:`Provider` with retry + circuit-breaker behaviour.

    Streaming retries only at connection-open: once
    ``inner.stream(...)`` has yielded the first chunk we can no longer
    safely replay, so a mid-stream failure surfaces immediately and
    counts as one failure toward the breaker threshold.
    """

    def __init__(
        self,
        inner: Provider,
        *,
        provider_name: str,
        policy: ProviderPolicy,
    ) -> None:
        self._inner = inner
        self._name = provider_name
        self._policy = policy

    @property
    def inner(self) -> Provider:
        return self._inner

    @property
    def policy(self) -> ProviderPolicy:
        return self._policy

    def complete(self, model: str, messages: list[dict], **kwargs: Any) -> dict:
        _check_circuit(self._name, self._policy)
        attempts = max(1, self._policy.retry_max_attempts)
        last_exc: ProviderInvocationError | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = self._inner.complete(model, messages, **kwargs)
            except ProviderInvocationError as exc:
                last_exc = exc
                if attempt < attempts:
                    backoff = _next_backoff(self._policy, attempt)
                    logger.info(
                        "provider_retry provider=%s attempt=%d/%d backoff_s=%.2f error=%s",
                        self._name,
                        attempt,
                        attempts,
                        backoff,
                        exc,
                    )
                    _sleep(backoff)
                    continue
                _record_failure(self._name, self._policy)
                raise
            else:
                _record_success(self._name, self._policy)
                return result
        # Unreachable \u2014 the loop either returns or raises \u2014 but keep an
        # explicit safety net so type checkers don't flag a missing return.
        assert last_exc is not None  # noqa: S101  (defensive)
        raise last_exc

    def stream(self, model: str, messages: list[dict], **kwargs: Any) -> Iterator[str]:
        _check_circuit(self._name, self._policy)
        attempts = max(1, self._policy.retry_max_attempts)
        iterator: Iterator[str] | None = None
        last_exc: ProviderInvocationError | None = None
        for attempt in range(1, attempts + 1):
            try:
                iterator = self._inner.stream(model, messages, **kwargs)
            except ProviderInvocationError as exc:
                last_exc = exc
                if attempt < attempts:
                    backoff = _next_backoff(self._policy, attempt)
                    logger.info(
                        "provider_retry_stream provider=%s attempt=%d/%d backoff_s=%.2f error=%s",
                        self._name,
                        attempt,
                        attempts,
                        backoff,
                        exc,
                    )
                    _sleep(backoff)
                    continue
                _record_failure(self._name, self._policy)
                raise
            else:
                break
        if iterator is None:
            assert last_exc is not None  # noqa: S101
            raise last_exc
        return self._wrap_stream(iterator)

    def _wrap_stream(self, iterator: Iterator[str]) -> Iterator[str]:
        """Forward chunks while tracking success/failure for the breaker.

        Mid-stream failures are NOT retried (the client has already seen
        partial data) but they DO count as a failure for the breaker.
        """
        try:
            chunk_seen = False
            for chunk in iterator:
                chunk_seen = True
                yield chunk
        except ProviderInvocationError:
            _record_failure(self._name, self._policy)
            raise
        else:
            # Empty stream still counts as a success \u2014 the upstream
            # actually replied, even if with nothing.
            if not chunk_seen:
                logger.debug(
                    "provider_stream_empty provider=%s model_class=%s",
                    self._name,
                    type(self._inner).__name__,
                )
            _record_success(self._name, self._policy)


def resolve_policy(provider_name: str, settings_obj: Any) -> ProviderPolicy:
    """Look up a policy by provider name from ``settings.provider_policies``.

    Returns the off-by-default policy when no entry exists, so callers
    can always wrap providers unconditionally and pay only the cost of
    one method call when no policy was configured.
    """
    container = getattr(settings_obj, "provider_policies", {}) or {}
    raw = container.get(provider_name)
    if raw is None:
        return ProviderPolicy()
    # ``raw`` is a Pydantic ProviderPolicyConfig; map its fields onto
    # the frozen dataclass so the hot path doesn't touch Pydantic.
    return ProviderPolicy(
        retry_max_attempts=int(getattr(raw, "retry_max_attempts", 1)),
        retry_initial_backoff_seconds=float(getattr(raw, "retry_initial_backoff_seconds", 0.5)),
        retry_max_backoff_seconds=float(getattr(raw, "retry_max_backoff_seconds", 8.0)),
        retry_backoff_multiplier=float(getattr(raw, "retry_backoff_multiplier", 2.0)),
        circuit_failure_threshold=int(getattr(raw, "circuit_failure_threshold", 0)),
        circuit_cooldown_seconds=float(getattr(raw, "circuit_cooldown_seconds", 30.0)),
    )


__all__ = [
    "CircuitOpenError",
    "ProviderPolicy",
    "ResilientProvider",
    "reset_circuit",
    "resolve_policy",
]

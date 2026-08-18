"""Tiny in-house metrics registry with bounded labels."""

from __future__ import annotations

from collections.abc import Mapping

LabelValue = str | int | float | bool | None
Labels = Mapping[str, LabelValue] | None
MetricKey = tuple[str, tuple[tuple[str, str], ...]]

COUNTERS: dict[MetricKey, float] = {}
GAUGES: dict[MetricKey, float] = {}

_SENTINEL_LABEL = "_series"
_SENTINEL_VALUE = "boot"
PASSTHROUGH_INTEGRITY = "passthrough_integrity"
_PASSTHROUGH_REASONS = {"model", "body", "model_and_body", "other"}


def bounded_label(value: str, allowed: set[str], default: str = "other") -> str:
    """Return *value* only when it is in the bounded allowlist."""

    if isinstance(value, str) and value in allowed:
        return value
    return default


def _normalise_labels(labels: Labels = None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def _key(name: str, labels: Labels = None) -> MetricKey:
    return (str(name), _normalise_labels(labels))


def init_counter(name: str, labels: Labels = None) -> None:
    """Force-zero a counter series and a boot sentinel for dashboard discovery."""

    labels_dict = {str(k): str(v) for k, v in (labels or {}).items()}
    COUNTERS[_key(name, labels_dict)] = 0.0
    sentinel = dict(labels_dict)
    sentinel[_SENTINEL_LABEL] = _SENTINEL_VALUE
    COUNTERS[_key(name, sentinel)] = 0.0


def increment_counter(name: str, labels: Labels = None, amount: float = 1.0) -> float:
    key = _key(name, labels)
    COUNTERS[key] = COUNTERS.get(key, 0.0) + float(amount)
    return COUNTERS[key]


def counter_value(name: str, labels: Labels = None) -> float:
    return COUNTERS.get(_key(name, labels), 0.0)


def record_passthrough_mutation(reason: str) -> None:
    """Increment the passthrough-integrity alarm counter.

    This counter should stay at zero in healthy faithful-proxy operation.
    """

    bounded = bounded_label(reason, _PASSTHROUGH_REASONS)
    increment_counter(PASSTHROUGH_INTEGRITY, {"reason": bounded})


for _reason in sorted(_PASSTHROUGH_REASONS):
    init_counter(PASSTHROUGH_INTEGRITY, {"reason": _reason})


__all__ = [
    "COUNTERS",
    "GAUGES",
    "PASSTHROUGH_INTEGRITY",
    "bounded_label",
    "counter_value",
    "increment_counter",
    "init_counter",
    "record_passthrough_mutation",
]

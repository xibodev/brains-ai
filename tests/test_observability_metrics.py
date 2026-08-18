from __future__ import annotations

import pytest

from brains.observability import metrics


@pytest.fixture(autouse=True)
def restore_counters():
    snapshot = dict(metrics.COUNTERS)
    metrics.COUNTERS.clear()
    yield
    metrics.COUNTERS.clear()
    metrics.COUNTERS.update(snapshot)


def test_bounded_label_maps_unknown_to_other():
    assert metrics.bounded_label("known", {"known"}) == "known"
    assert metrics.bounded_label("tenant-supplied-value", {"known"}) == "other"


def test_init_counter_force_zeros_series_and_boot_sentinel():
    metrics.COUNTERS[("gateway_requests", (("route", "direct"),))] = 99.0
    metrics.init_counter("gateway_requests", {"route": "direct"})

    assert metrics.counter_value("gateway_requests", {"route": "direct"}) == 0.0
    assert (
        metrics.counter_value(
            "gateway_requests",
            {"route": "direct", "_series": "boot"},
        )
        == 0.0
    )


def test_record_passthrough_mutation_increments_integrity_counter():
    metrics.init_counter(metrics.PASSTHROUGH_INTEGRITY, {"reason": "other"})

    metrics.record_passthrough_mutation("user-supplied-unbounded")
    assert metrics.counter_value(metrics.PASSTHROUGH_INTEGRITY, {"reason": "other"}) == 1.0

    metrics.record_passthrough_mutation("model")
    assert metrics.counter_value(metrics.PASSTHROUGH_INTEGRITY, {"reason": "model"}) == 1.0

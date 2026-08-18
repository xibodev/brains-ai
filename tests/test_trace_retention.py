"""Trace retention: payload truncation + row pruning.

write_trace() stores a redacted copy of each gateway request for the admin
"Recent traces" view. Left unbounded, the traces table dominates the DB
(observed: 700MB / 99.7% of a box0 brain, ~331KB avg per row). These tests
pin the two bounds that keep it in check:

  * payloads are truncated at settings.trace_max_payload_bytes;
  * only the most recent settings.trace_retention_max_rows rows are kept.
"""

from __future__ import annotations

import pytest

from brains.config import settings
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Trace
from brains.storage.repositories import list_traces, write_trace


def _trace_count() -> int:
    with SessionLocal() as session:
        return session.query(Trace).count()


@pytest.fixture(autouse=True)
def _clean_traces():
    init_db()
    with SessionLocal() as session:
        session.query(Trace).delete()
        session.commit()
    yield
    with SessionLocal() as session:
        session.query(Trace).delete()
        session.commit()


def test_payload_truncated_at_cap(monkeypatch):
    monkeypatch.setattr(settings, "trace_max_payload_bytes", 100)
    monkeypatch.setattr(settings, "trace_retention_max_rows", 0)  # isolate truncation
    big = "x" * 5000
    write_trace("test", big)
    row = list_traces(limit=1)[0]
    # Head slice (100) + truncation marker; far smaller than the 5000-byte input.
    assert len(row.payload) < 300
    assert "truncated" in row.payload
    assert "5000 bytes original" in row.payload


def test_payload_under_cap_is_untouched(monkeypatch):
    monkeypatch.setattr(settings, "trace_max_payload_bytes", 10_000)
    monkeypatch.setattr(settings, "trace_retention_max_rows", 0)
    payload = '{"messages": "short"}'
    write_trace("test", payload)
    assert list_traces(limit=1)[0].payload == payload


def test_truncation_disabled_when_cap_zero(monkeypatch):
    monkeypatch.setattr(settings, "trace_max_payload_bytes", 0)
    monkeypatch.setattr(settings, "trace_retention_max_rows", 0)
    big = "y" * 4000
    write_trace("test", big)
    assert list_traces(limit=1)[0].payload == big


def test_retention_prunes_to_cap(monkeypatch):
    monkeypatch.setattr(settings, "trace_max_payload_bytes", 0)
    monkeypatch.setattr(settings, "trace_retention_max_rows", 5)
    for i in range(20):
        write_trace("test", f"payload-{i}")
    assert _trace_count() == 5
    # The 5 survivors are the most recent (highest ids / latest payloads).
    kept = {t.payload for t in list_traces(limit=10)}
    assert kept == {f"payload-{i}" for i in range(15, 20)}


def test_retention_disabled_when_cap_zero(monkeypatch):
    monkeypatch.setattr(settings, "trace_max_payload_bytes", 0)
    monkeypatch.setattr(settings, "trace_retention_max_rows", 0)
    for i in range(12):
        write_trace("test", f"p-{i}")
    assert _trace_count() == 12


def test_defaults_are_bounded():
    """Out of the box, both bounds are active so a fresh deployment can't
    silently balloon the way box0 did."""
    assert settings.trace_max_payload_bytes > 0
    assert settings.trace_retention_max_rows > 0


def test_prune_traces_now_backfills_existing(monkeypatch):
    """One-shot maintenance enforces retention on an ALREADY-bloated table
    (the box0 case), honoring explicit overrides over settings."""
    from brains.storage.repositories import prune_traces_now

    monkeypatch.setattr(settings, "trace_max_payload_bytes", 0)  # build unbounded
    monkeypatch.setattr(settings, "trace_retention_max_rows", 0)
    for _ in range(30):
        write_trace("test", "z" * 1000)
    assert _trace_count() == 30

    result = prune_traces_now(max_rows=10, max_payload_bytes=50)
    assert result["deleted"] == 20
    assert result["remaining"] == 10
    # All 10 survivors truncated from 1000 → head(50) + marker.
    assert result["truncated"] == 10
    for row in list_traces(limit=10):
        assert "truncated" in row.payload
        assert len(row.payload) < 200

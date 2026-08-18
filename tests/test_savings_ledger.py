"""Tests for the cost-savings ledger (Phase C).

Covers:
  * ``brains.router.prices`` — exact, longest-prefix, override, miss.
  * ``brains.router.savings`` — record_usage round-trip, baseline
    fallback to ``default`` tier model, NULL-cost rows for unpriced
    models, aggregation helpers (totals / daily / top).
  * End-to-end: a POST to ``/v1/chat/completions`` writes a ledger
    row with the routed model + non-zero cost. Same for
    ``/v1/messages`` (streaming and non-streaming).
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from brains.config import settings
from brains.main import app
from brains.router import savings as savings_mod
from brains.router.prices import (
    DEFAULT_PRICES,
    compute_cost,
    lookup_price,
)
from brains.storage.db import SessionLocal
from brains.storage.models import UsageLedgerEntry

# ---------- price catalog ----------


def test_lookup_price_exact_match():
    assert lookup_price("gpt-4o") == DEFAULT_PRICES["gpt-4o"]


def test_lookup_price_longest_prefix():
    # The catalog has "claude-opus-4" but not the dated suffix.
    assert lookup_price("claude-opus-4-20250514") == DEFAULT_PRICES["claude-opus-4"]


def test_lookup_price_case_insensitive():
    assert lookup_price("GPT-4O-mini") == DEFAULT_PRICES["gpt-4o-mini"]


def test_lookup_price_unknown_returns_none():
    assert lookup_price("totally-made-up-model-9000") is None


def test_lookup_price_overlay_beats_default():
    overrides = {"gpt-4o": {"input": 99.0, "output": 88.0}}
    assert lookup_price("gpt-4o", overrides=overrides) == (99.0, 88.0)


def test_lookup_price_overlay_longest_prefix():
    overrides = {"acme-": {"input": 1.0, "output": 2.0}}
    assert lookup_price("acme-foo-2025", overrides=overrides) == (1.0, 2.0)


def test_compute_cost_known_model():
    # 1M input + 1M output of gpt-4o = $2.50 + $10.00 = $12.50
    assert compute_cost("gpt-4o", 1_000_000, 1_000_000) == pytest.approx(12.50)


def test_compute_cost_unknown_returns_none():
    assert compute_cost("nope", 1, 1) is None


def test_compute_cost_zero_for_local_models():
    # Local / open-weights families bill at $0; the savings panel uses
    # this to show "saved 100%" against a hosted alternative.
    assert compute_cost("llama-3.1-70b-instruct", 1000, 1000) == 0.0


# ---------- record_usage round-trip ----------


@pytest.fixture
def isolated_ledger(monkeypatch):
    """Force the savings module to re-init the DB and start with a
    clean ledger table for each test."""
    monkeypatch.setattr(savings_mod, "_db_initialized", False)
    yield
    # Clean up so the next test sees an empty ledger.
    with SessionLocal() as session:
        session.query(UsageLedgerEntry).delete()
        session.commit()


def test_record_usage_writes_row(isolated_ledger):
    entry = savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="openai",
        input_tokens=1000,
        output_tokens=500,
        task_type="trivial",
    )
    assert entry is not None
    assert entry.routed_model == "gpt-4o-mini"
    assert entry.input_tokens == 1000
    assert entry.output_tokens == 500
    # gpt-4o-mini: 0.15*1000/1e6 + 0.60*500/1e6 = 0.00015 + 0.0003 = 0.00045
    assert entry.cost_actual_usd == pytest.approx(0.00045)


def test_record_usage_savings_against_default_tier(isolated_ledger, monkeypatch):
    """Baseline defaults to the ``default`` tier model when
    ``savings.baseline_model`` is empty."""
    from brains.config import ModelRoute

    monkeypatch.setattr(settings.savings, "baseline_model", "")
    monkeypatch.setitem(settings.models, "default", ModelRoute(provider="openai", model="gpt-4o"))
    entry = savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="openai",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert entry is not None
    # actual gpt-4o-mini: 0.15 + 0.60 = 0.75
    # baseline gpt-4o:    2.50 + 10.00 = 12.50
    # savings:            12.50 - 0.75 = 11.75
    assert entry.cost_actual_usd == pytest.approx(0.75)
    assert entry.cost_baseline_usd == pytest.approx(12.50)
    assert entry.savings_usd == pytest.approx(11.75)


def test_record_usage_explicit_baseline_overrides_default(isolated_ledger, monkeypatch):
    monkeypatch.setattr(settings.savings, "baseline_model", "claude-opus-4")
    entry = savings_mod.record_usage(
        endpoint="anthropic.messages",
        requested_model="claude-opus-4",
        routed_model="claude-haiku-4",
        provider="anthropic",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # haiku-4:  0.80 + 4.00 =  4.80
    # opus-4:  15.00 + 75.00 = 90.00
    # savings: 90.00 - 4.80 = 85.20
    assert entry is not None
    assert entry.cost_baseline_usd == pytest.approx(90.00)
    assert entry.savings_usd == pytest.approx(85.20)


def test_record_usage_unknown_model_writes_null_costs(isolated_ledger):
    entry = savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="totally-unknown",
        routed_model="totally-unknown",
        provider="openai",
        input_tokens=10,
        output_tokens=10,
    )
    assert entry is not None
    assert entry.cost_actual_usd is None
    assert entry.cost_baseline_usd is None or entry.savings_usd is None


def test_record_usage_disabled_returns_none(isolated_ledger, monkeypatch):
    monkeypatch.setattr(settings.savings, "enabled", False)
    entry = savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o",
        provider="openai",
        input_tokens=1,
        output_tokens=1,
    )
    assert entry is None
    # Nothing in the table.
    with SessionLocal() as session:
        assert session.query(UsageLedgerEntry).count() == 0


# ---------- aggregation ----------


def test_totals_and_daily_series(isolated_ledger):
    now = datetime.now(UTC)
    for i in range(3):
        savings_mod.record_usage(
            endpoint="openai.chat",
            requested_model="gpt-4o",
            routed_model="gpt-4o-mini",
            provider="openai",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            occurred_at=now - timedelta(days=i),
        )
    t = savings_mod.totals(days=7)
    assert t["calls"] == 3
    assert t["input_tokens"] == 3_000_000
    assert t["output_tokens"] == 3_000_000
    # gpt-4o-mini cost per call: 0.15 + 0.60 = 0.75. 3 calls = 2.25.
    assert t["cost_actual_usd"] == pytest.approx(2.25)

    series = savings_mod.daily_series(days=7)
    assert len(series) == 7
    # Total calls across the series equals the total in `totals`.
    assert sum(row["calls"] for row in series) == 3
    # The three calls land on three distinct days (one each) — this guards
    # the dialect-free Python day-bucketing that replaced SQLite's strftime,
    # which does not exist on Postgres and silently hid the savings panel.
    assert sorted(row["calls"] for row in series) == [0, 0, 0, 0, 1, 1, 1]


def test_savings_summary_ci_math(isolated_ledger, monkeypatch):
    monkeypatch.setattr(settings, "savings_holdout_fraction", 0.0)
    now = datetime.now(UTC)
    values = [1.0, 2.0, 3.0, 4.0]
    with SessionLocal() as session:
        for value in values:
            session.add(
                UsageLedgerEntry(
                    ts=now,
                    endpoint="openai.chat",
                    requested_model="gpt-4o",
                    routed_model="gpt-4o-mini",
                    provider="openai",
                    input_tokens=1,
                    output_tokens=1,
                    cost_actual_usd=0.0,
                    cost_baseline_usd=value,
                    savings_usd=value,
                    is_stub=False,
                    is_holdout=False,
                )
            )
        session.commit()

    summary = savings_mod.savings_summary(days=7)
    expected_margin = 1.96 * statistics.stdev(values) * (len(values) ** 0.5)
    assert summary["request_count"] == 4
    assert summary["total_savings_usd"] == pytest.approx(sum(values))
    assert summary["confidence_interval_95_usd"]["low"] == pytest.approx(
        sum(values) - expected_margin
    )
    assert summary["confidence_interval_95_usd"]["high"] == pytest.approx(
        sum(values) + expected_margin
    )
    assert summary["basis"] == "estimated"


def test_savings_summary_basis_requires_configured_holdout_and_rows(isolated_ledger, monkeypatch):
    now = datetime.now(UTC)
    with SessionLocal() as session:
        session.add(
            UsageLedgerEntry(
                ts=now,
                endpoint="openai.chat",
                requested_model="gpt-4o",
                routed_model="gpt-4o-mini",
                provider="openai",
                input_tokens=1,
                output_tokens=1,
                cost_actual_usd=0.0,
                cost_baseline_usd=1.0,
                savings_usd=1.0,
                is_stub=False,
                is_holdout=True,
            )
        )
        session.commit()

    monkeypatch.setattr(settings, "savings_holdout_fraction", 0.0)
    assert savings_mod.savings_summary(days=7)["basis"] == "estimated"

    monkeypatch.setattr(settings, "savings_holdout_fraction", 0.25)
    measured = savings_mod.savings_summary(days=7)
    assert measured["basis"] == "measured"
    assert measured["holdout_count"] == 1

    with SessionLocal() as session:
        session.query(UsageLedgerEntry).update({"is_holdout": False})
        session.commit()
    assert savings_mod.savings_summary(days=7)["basis"] == "estimated"


def test_record_usage_tags_configured_holdout(isolated_ledger, monkeypatch):
    monkeypatch.setattr(settings, "savings_holdout_fraction", 1.0)
    entry = savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="openai",
        input_tokens=1,
        output_tokens=1,
    )
    assert entry is not None
    assert entry.is_holdout is True


def test_top_routed_models(isolated_ledger):
    for _ in range(3):
        savings_mod.record_usage(
            endpoint="openai.chat",
            requested_model="x",
            routed_model="gpt-4o-mini",
            provider="openai",
            input_tokens=100,
            output_tokens=100,
        )
    savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="x",
        routed_model="claude-haiku-4",
        provider="anthropic",
        input_tokens=100,
        output_tokens=100,
    )
    top = savings_mod.top_routed_models(days=7, limit=5)
    assert top[0]["routed_model"] == "gpt-4o-mini"
    assert top[0]["calls"] == 3
    assert top[1]["routed_model"] == "claude-haiku-4"
    assert top[1]["calls"] == 1


# ---------- end-to-end through the gateway ----------


def test_openai_chat_completion_writes_ledger_row(isolated_ledger, auth_headers):
    client = TestClient(app)
    # Use the explicit classifier-driven alias — under the new
    # faithful-proxy contract an arbitrary id like ``gpt-4o`` returns
    # 404 (we don't silently classify on the client's behalf). The
    # ledger semantics being tested here (one row per call, stub flag,
    # provider/model attribution) are unaffected by which alias the
    # caller used to opt into routing.
    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "brains/auto", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200
    with SessionLocal() as session:
        rows = session.query(UsageLedgerEntry).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "openai.chat"
    assert row.requested_model == "brains/auto"
    # routed_model is whatever tier the classifier picked; echo provider
    # is the only one wired up in tests so it's always an echo-* model.
    assert row.routed_model.startswith("echo-")
    assert row.provider == "echo"
    # Echo is a stub: the ledger row must be flagged so it never shows
    # up on the dashboard headline.
    assert row.is_stub is True


def test_anthropic_messages_writes_ledger_row(isolated_ledger, auth_headers):
    client = TestClient(app)
    # ``claude-opus-4`` isn't in any test catalog → would 404. Route
    # through the explicit deep-tier alias which the Anthropic facade
    # accepts and Echo backs in tests.
    resp = client.post(
        "/v1/messages",
        headers=auth_headers,
        json={
            "model": "brains/deep",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 200
    with SessionLocal() as session:
        rows = session.query(UsageLedgerEntry).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "anthropic.messages"
    assert row.requested_model == "brains/deep"


def test_anthropic_streaming_writes_ledger_row(isolated_ledger, auth_headers):
    client = TestClient(app)
    with client.stream(
        "POST",
        "/v1/messages",
        headers=auth_headers,
        json={
            # As above — ``claude-opus-4`` is not a real catalog id in
            # tests; the streaming ledger contract is what we're
            # asserting, not the alias mapping.
            "model": "brains/deep",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "hello world"}],
        },
    ) as r:
        assert r.status_code == 200
        # Drain so the generator's tail (which records the ledger row) runs.
        for _ in r.iter_text():
            pass
    with SessionLocal() as session:
        rows = session.query(UsageLedgerEntry).all()
    assert len(rows) == 1
    assert rows[0].endpoint == "anthropic.messages"


def test_admin_savings_endpoint(isolated_ledger):
    savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="openai",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    client = TestClient(app)
    resp = client.get(
        "/admin/api/savings?days=7",
        headers={"Authorization": "Bearer local-dev-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["totals"]["calls"] == 1
    assert body["totals"]["cost_actual_usd"] == pytest.approx(0.75)
    assert len(body["daily"]) == 7
    assert body["top_routed_models"][0]["routed_model"] == "gpt-4o-mini"


# ---------- stub-provider filtering ----------


def test_is_stub_provider_lookup():
    from brains.providers.registry import is_stub_provider, stub_provider_names

    assert is_stub_provider("echo") is True
    assert is_stub_provider("openai") is False
    assert is_stub_provider("unknown-thing") is False
    assert "echo" in stub_provider_names()


def test_record_usage_marks_stub_for_echo_provider(isolated_ledger):
    entry = savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="echo-default",
        routed_model="echo-default",
        provider="echo",
        input_tokens=1,
        output_tokens=1,
    )
    assert entry is not None
    assert entry.is_stub is True


def test_record_usage_does_not_mark_stub_for_real_providers(isolated_ledger):
    entry = savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="openai",
        input_tokens=1,
        output_tokens=1,
    )
    assert entry is not None
    assert entry.is_stub is False


def test_totals_excludes_stub_traffic_by_default(isolated_ledger, monkeypatch):
    from brains.config import ModelRoute

    monkeypatch.setitem(settings.models, "default", ModelRoute(provider="openai", model="gpt-4o"))
    # Real call: should count.
    savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="openai",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # Stub call: should be hidden from the headline but tracked under
    # ``stub_calls``.
    savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="echo-default",
        provider="echo",
        input_tokens=1,
        output_tokens=1,
    )
    t = savings_mod.totals(days=7)
    assert t["calls"] == 1, "stub row must not inflate the headline call count"
    assert t["stub_calls"] == 1, "stub_calls must surface the hidden row"
    assert t["cost_actual_usd"] == pytest.approx(0.75)

    t_with_stub = savings_mod.totals(days=7, include_stubs=True)
    assert t_with_stub["calls"] == 2


def test_top_routed_models_excludes_stub_traffic_by_default(isolated_ledger):
    savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="openai",
        input_tokens=10,
        output_tokens=10,
    )
    for _ in range(5):
        savings_mod.record_usage(
            endpoint="openai.chat",
            requested_model="gpt-4o",
            routed_model="echo-default",
            provider="echo",
            input_tokens=1,
            output_tokens=1,
        )
    top = savings_mod.top_routed_models(days=7, limit=5)
    routed_models = [row["routed_model"] for row in top]
    assert "echo-default" not in routed_models, (
        "stub provider models must not appear in the dashboard's top-models table"
    )
    assert "gpt-4o-mini" in routed_models

    top_with_stub = savings_mod.top_routed_models(days=7, limit=5, include_stubs=True)
    routed_with = [row["routed_model"] for row in top_with_stub]
    assert "echo-default" in routed_with


def test_daily_series_excludes_stub_traffic_by_default(isolated_ledger):
    savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="openai",
        input_tokens=10,
        output_tokens=10,
    )
    savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="echo-default",
        provider="echo",
        input_tokens=1,
        output_tokens=1,
    )
    series = savings_mod.daily_series(days=7)
    total_calls = sum(row["calls"] for row in series)
    assert total_calls == 1

    series_with_stub = savings_mod.daily_series(days=7, include_stubs=True)
    assert sum(row["calls"] for row in series_with_stub) == 2


def test_admin_savings_endpoint_include_stubs_param(isolated_ledger):
    savings_mod.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="echo-default",
        provider="echo",
        input_tokens=1,
        output_tokens=1,
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer local-dev-key"}

    # Default: stub call hidden from headline, surfaced under stub_calls.
    resp = client.get("/admin/api/savings?days=7", headers=headers)
    body = resp.json()
    assert body["totals"]["calls"] == 0
    assert body["totals"]["stub_calls"] == 1
    assert "echo" in body["stub_providers"]

    # Opt-in: stub call folded back in.
    resp2 = client.get("/admin/api/savings?days=7&include_stubs=1", headers=headers)
    body2 = resp2.json()
    assert body2["totals"]["calls"] == 1
    assert body2["include_stubs"] is True

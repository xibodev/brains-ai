"""Cost-savings ledger writer + read-side aggregator.

This module is intentionally small and side-effect-free until the
caller invokes :func:`record_usage`. The provider-flow code paths in
:mod:`brains.api.openai` and :mod:`brains.api.anthropic` call
:func:`record_usage` after a successful response (streaming or
non-streaming) with the routed model + token counts pulled from the
provider's ``usage`` block. Everything else — price lookup, baseline
selection, DB write — happens here so the gateway code stays clean.

The read-side helpers (:func:`totals`, :func:`daily_series`) provide local
usage and savings reporting for supported callers.
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from brains.config import settings
from brains.providers.registry import is_stub_provider
from brains.router.prices import compute_cost
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import UsageAttribution, UsageLedgerEntry

_db_initialized = False


def _ensure_db() -> None:
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True


def _resolve_baseline_model() -> str:
    """Pick the model we'll quote as the "without brains" baseline.

    Operator-supplied ``settings.savings.baseline_model`` wins. Empty
    string (the default) falls back to the model bound to the
    ``default`` tier, which represents "what the operator would have
    pointed clients at without brains in front".
    """
    explicit = (settings.savings.baseline_model or "").strip()
    if explicit:
        return explicit
    default_tier = settings.models.get("default")
    if default_tier is not None:
        return default_tier.model
    return ""


def _extract_usage(provider_response: Mapping[str, Any] | None) -> tuple[int, int]:
    """Pull ``(input_tokens, output_tokens)`` from an OpenAI-shaped
    ``usage`` block. Missing fields default to 0 so streamed responses
    that never reported usage are still recordable (with zero cost)."""
    if not provider_response:
        return (0, 0)
    usage = provider_response.get("usage") or {}
    if not isinstance(usage, Mapping):
        return (0, 0)
    inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return (inp, out)


def _holdout_fraction() -> float:
    try:
        return max(0.0, min(1.0, float(settings.savings_holdout_fraction or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _is_holdout_id(entry_id: int | None, fraction: float | None = None) -> bool:
    if entry_id is None:
        return False
    frac = _holdout_fraction() if fraction is None else max(0.0, min(1.0, float(fraction)))
    if frac <= 0.0:
        return False
    if frac >= 1.0:
        return True
    digest = hashlib.sha256(str(entry_id).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    return bucket < frac


def record_usage(
    *,
    endpoint: str,
    requested_model: str,
    routed_model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    task_type: str | None = None,
    occurred_at: datetime | None = None,
    session_id: str | None = None,
) -> UsageLedgerEntry | None:
    """Persist one row in the usage ledger.

    Returns the inserted row (with computed cost / savings) for
    test-introspection, or ``None`` if the savings ledger is disabled.
    Failures are swallowed (a savings-ledger write must never break
    the user-facing request) — callers do not need to wrap this in a
    try/except.

    ``session_id`` attributes the call to a Brains Session (BL-P1-02) so an
    Issue rollup can sum real persisted cost. It is honoured only when the
    Session row exists: an unknown id records the ledger entry unattributed
    rather than fabricating a link.
    """
    if not settings.savings.enabled:
        return None
    try:
        _ensure_db()
        overrides = settings.savings.price_catalog or {}
        actual_cost = compute_cost(routed_model, input_tokens, output_tokens, overrides=overrides)
        baseline_model = _resolve_baseline_model()
        baseline_cost = (
            compute_cost(baseline_model, input_tokens, output_tokens, overrides=overrides)
            if baseline_model
            else None
        )
        savings = (
            baseline_cost - actual_cost
            if (actual_cost is not None and baseline_cost is not None)
            else None
        )
        entry = UsageLedgerEntry(
            ts=occurred_at or datetime.now(UTC),
            endpoint=endpoint,
            requested_model=requested_model or "",
            routed_model=routed_model or "",
            provider=provider or "",
            task_type=task_type,
            input_tokens=max(0, int(input_tokens or 0)),
            output_tokens=max(0, int(output_tokens or 0)),
            cost_actual_usd=actual_cost,
            cost_baseline_usd=baseline_cost,
            savings_usd=savings,
            is_stub=is_stub_provider(provider or ""),
        )
        with SessionLocal() as session:
            session.add(entry)
            session.flush()
            entry_id = entry.id
            entry.is_holdout = _is_holdout_id(entry.id)
            session.flush()
            session.expunge(entry)
            session.commit()
        if session_id:
            _attribute_usage(entry_id, session_id)
        return entry
    except Exception:
        # The savings ledger is observability, not a hot-path
        # dependency. Never let it break a user request.
        return None


def _attribute_usage(usage_entry_id: int, session_id: str) -> None:
    """Link one ledger row to the Session (and its Issue/Persona/Org).

    ``usage_attributions.usage_entry_id`` is unique, so this is idempotent: a
    retried write attributes the same call once and an Issue rollup can never
    double-count it. An unknown Session writes nothing - an unattributed call
    is reported as unattributed, never guessed onto a Session.
    """
    from brains.authz import policy
    from brains.storage.models import AgentSession

    try:
        with SessionLocal() as session:
            agent_session = session.get(AgentSession, session_id)
            if agent_session is None:
                return
            existing = (
                session.query(UsageAttribution)
                .filter(UsageAttribution.usage_entry_id == usage_entry_id)
                .one_or_none()
            )
            if existing is not None:
                return
            session.add(
                UsageAttribution(
                    usage_entry_id=usage_entry_id,
                    session_id=agent_session.id,
                    issue_id=agent_session.issue_id,
                    persona_id=agent_session.persona_id,
                    org_id=policy.workspace_org_id(agent_session.workspace_id),
                )
            )
            session.commit()
    except Exception:
        # Attribution is evidence, not a hot-path dependency.
        return


def record_usage_from_response(
    *,
    endpoint: str,
    requested_model: str,
    routed_model: str,
    provider: str,
    provider_response: Mapping[str, Any] | None,
    task_type: str | None = None,
    session_id: str | None = None,
) -> UsageLedgerEntry | None:
    """Convenience wrapper that extracts token counts from a provider
    response's ``usage`` block before calling :func:`record_usage`."""
    inp, out = _extract_usage(provider_response)
    return record_usage(
        endpoint=endpoint,
        requested_model=requested_model,
        routed_model=routed_model,
        provider=provider,
        input_tokens=inp,
        output_tokens=out,
        task_type=task_type,
        session_id=session_id,
    )


def totals(days: int = 7, *, include_stubs: bool = False) -> dict[str, Any]:
    """Aggregate the ledger over the trailing *days* window.

    Stub-provider rows (e.g. ``echo``) are excluded by default so the
    reported total reflects real upstream traffic. The count of
    excluded stub calls is still returned under ``stub_calls`` so the
    operator can see what's hidden. Pass ``include_stubs=True`` to
    fold them back in (e.g. for dev smoke tests where echo IS the
    traffic).

    Returned shape:
        {
            "window_days": 7,
            "calls": <int>,
            "input_tokens": <int>,
            "output_tokens": <int>,
            "cost_actual_usd": <float>,
            "cost_baseline_usd": <float>,
            "savings_usd": <float>,
            "unpriced_calls": <int>,
            "stub_calls": <int>,
        }
    """
    _ensure_db()
    since = datetime.now(UTC) - timedelta(days=max(1, int(days)))
    with SessionLocal() as session:
        base_filters = [UsageLedgerEntry.ts >= since]
        if not include_stubs:
            base_filters.append(UsageLedgerEntry.is_stub.is_(False))
        rows = session.execute(
            select(
                func.count(UsageLedgerEntry.id),
                func.coalesce(func.sum(UsageLedgerEntry.input_tokens), 0),
                func.coalesce(func.sum(UsageLedgerEntry.output_tokens), 0),
                func.coalesce(func.sum(UsageLedgerEntry.cost_actual_usd), 0.0),
                func.coalesce(func.sum(UsageLedgerEntry.cost_baseline_usd), 0.0),
                func.coalesce(func.sum(UsageLedgerEntry.savings_usd), 0.0),
            ).where(*base_filters)
        ).one()
        unpriced = session.execute(
            select(func.count(UsageLedgerEntry.id)).where(
                *base_filters,
                UsageLedgerEntry.cost_actual_usd.is_(None),
            )
        ).scalar_one()
        stub_calls = session.execute(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.ts >= since,
                UsageLedgerEntry.is_stub.is_(True),
            )
        ).scalar_one()
    calls, in_tok, out_tok, c_actual, c_baseline, savings = rows
    return {
        "window_days": int(days),
        "calls": int(calls or 0),
        "input_tokens": int(in_tok or 0),
        "output_tokens": int(out_tok or 0),
        "cost_actual_usd": float(c_actual or 0.0),
        "cost_baseline_usd": float(c_baseline or 0.0),
        "savings_usd": float(savings or 0.0),
        "unpriced_calls": int(unpriced or 0),
        "stub_calls": int(stub_calls or 0),
    }


def savings_summary(days: int = 30) -> dict[str, Any]:
    """Savings rigor summary with a 95% aggregate confidence interval.

    The interval is a normal approximation over per-request ``savings_usd``:
    mean ± 1.96 * stdev/sqrt(n), scaled back to the aggregate total.
    Rows without priced savings are excluded; stub rows are excluded to match
    the reported total.
    """

    _ensure_db()
    window = max(1, int(days))
    since = datetime.now(UTC) - timedelta(days=window)
    filters = [
        UsageLedgerEntry.ts >= since,
        UsageLedgerEntry.is_stub.is_(False),
        UsageLedgerEntry.savings_usd.is_not(None),
    ]
    with SessionLocal() as session:
        values = [
            float(value)
            for (value,) in session.execute(
                select(UsageLedgerEntry.savings_usd).where(*filters)
            ).all()
            if value is not None
        ]
        holdout_count = session.execute(
            select(func.count(UsageLedgerEntry.id)).where(
                *filters,
                UsageLedgerEntry.is_holdout.is_(True),
            )
        ).scalar_one()

    request_count = len(values)
    total = float(sum(values))
    margin = 1.96 * statistics.stdev(values) * (request_count**0.5) if request_count > 1 else 0.0
    low = float(total - margin)
    high = float(total + margin)
    basis = "measured" if _holdout_fraction() > 0.0 and int(holdout_count or 0) > 0 else "estimated"
    return {
        "window_days": window,
        "request_count": request_count,
        "total_savings_usd": total,
        "confidence_interval_95_usd": {"low": low, "high": high},
        "ci95_low_usd": low,
        "ci95_high_usd": high,
        "basis": basis,
        "holdout_count": int(holdout_count or 0),
    }


def daily_series(days: int = 7, *, include_stubs: bool = False) -> list[dict[str, Any]]:
    """One row per day for the trailing *days* window, oldest first.

    Each row is ``{date: 'YYYY-MM-DD', calls, savings_usd,
    cost_actual_usd}``. Days with zero calls are included as zero rows
    so consumers receive a contiguous time series. Stub-provider rows
    are excluded by default; see :func:`totals` for the same toggle.
    """
    _ensure_db()
    now = datetime.now(UTC)
    span = max(1, int(days))
    since = (now - timedelta(days=span - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as session:
        # Bucket by day in Python rather than a SQL date function: SQLite has
        # strftime() but Postgres does not, so a SQL-side day expression made
        # the savings panel crash (and silently hide) on a Postgres backend.
        # The ledger is small (one row per routed call) so pulling the window
        # and grouping in memory is cheap and fully dialect-portable.
        filters = [UsageLedgerEntry.ts >= since]
        if not include_stubs:
            filters.append(UsageLedgerEntry.is_stub.is_(False))
        rows = session.execute(
            select(
                UsageLedgerEntry.ts,
                UsageLedgerEntry.savings_usd,
                UsageLedgerEntry.cost_actual_usd,
            ).where(*filters)
        ).all()
    by_day: dict[str, tuple[int, float, float]] = {}
    for ts, savings_usd, cost_actual_usd in rows:
        if ts is None:
            continue
        day = ts.strftime("%Y-%m-%d")
        calls, savings, actual = by_day.get(day, (0, 0.0, 0.0))
        by_day[day] = (
            calls + 1,
            savings + float(savings_usd or 0.0),
            actual + float(cost_actual_usd or 0.0),
        )
    series: list[dict[str, Any]] = []
    for i in range(span):
        day = (since + timedelta(days=i)).strftime("%Y-%m-%d")
        calls, savings, actual = by_day.get(day, (0, 0.0, 0.0))
        series.append(
            {
                "date": day,
                "calls": calls,
                "savings_usd": savings,
                "cost_actual_usd": actual,
            }
        )
    return series


def top_routed_models(
    days: int = 7, limit: int = 5, *, include_stubs: bool = False
) -> list[dict[str, Any]]:
    """Top *limit* routed models by call count over the window. Stub-provider
    rows are excluded by default so the table reflects real upstreams."""
    _ensure_db()
    since = datetime.now(UTC) - timedelta(days=max(1, int(days)))
    with SessionLocal() as session:
        filters = [UsageLedgerEntry.ts >= since]
        if not include_stubs:
            filters.append(UsageLedgerEntry.is_stub.is_(False))
        results = session.execute(
            select(
                UsageLedgerEntry.routed_model,
                func.count(UsageLedgerEntry.id),
                func.coalesce(func.sum(UsageLedgerEntry.savings_usd), 0.0),
            )
            .where(*filters)
            .group_by(UsageLedgerEntry.routed_model)
            .order_by(func.count(UsageLedgerEntry.id).desc())
            .limit(int(limit))
        ).all()
    return [
        {
            "routed_model": row[0] or "",
            "calls": int(row[1] or 0),
            "savings_usd": float(row[2] or 0.0),
        }
        for row in results
    ]


# --------------------------------------------------------------------------- #
# Org-scoped usage (BL-P1-07 / AC-F9-04)
# --------------------------------------------------------------------------- #
#
# ``usage_ledger`` is install-wide and carries no product attribution of its
# own; ``totals``/``top_routed_models`` above answer "what did the whole
# gateway do" and are restricted to the bootstrap admin for exactly that
# reason. The functions below instead join through ``usage_attributions``
# (migration 136) and filter on ``org_id`` at the SQL level, so a call this
# Org's Session never identified, or that belongs to another Org's Session,
# is excluded by construction rather than filtered after the fact. The
# returned shape mirrors ``totals``/``top_routed_models`` so the Org-scoped
# and install-wide Usage surfaces render identically.


def org_totals(org_id: int, days: int = 30, *, include_stubs: bool = False) -> dict[str, Any]:
    """Aggregate the ledger over the trailing *days* window, scoped to one Org."""
    _ensure_db()
    since = datetime.now(UTC) - timedelta(days=max(1, int(days)))
    with SessionLocal() as session:
        base_filters = [UsageAttribution.org_id == org_id, UsageLedgerEntry.ts >= since]
        if not include_stubs:
            base_filters.append(UsageLedgerEntry.is_stub.is_(False))

        def _scoped(*extra):
            return (
                select(*extra)
                .select_from(UsageLedgerEntry)
                .join(UsageAttribution, UsageAttribution.usage_entry_id == UsageLedgerEntry.id)
            )

        rows = session.execute(
            _scoped(
                func.count(UsageLedgerEntry.id),
                func.coalesce(func.sum(UsageLedgerEntry.input_tokens), 0),
                func.coalesce(func.sum(UsageLedgerEntry.output_tokens), 0),
                func.coalesce(func.sum(UsageLedgerEntry.cost_actual_usd), 0.0),
                func.coalesce(func.sum(UsageLedgerEntry.cost_baseline_usd), 0.0),
                func.coalesce(func.sum(UsageLedgerEntry.savings_usd), 0.0),
            ).where(*base_filters)
        ).one()
        unpriced = session.execute(
            _scoped(func.count(UsageLedgerEntry.id)).where(
                *base_filters, UsageLedgerEntry.cost_actual_usd.is_(None)
            )
        ).scalar_one()
        stub_calls = session.execute(
            _scoped(func.count(UsageLedgerEntry.id)).where(
                UsageAttribution.org_id == org_id,
                UsageLedgerEntry.ts >= since,
                UsageLedgerEntry.is_stub.is_(True),
            )
        ).scalar_one()
    calls, in_tok, out_tok, c_actual, c_baseline, savings = rows
    return {
        "window_days": int(days),
        "calls": int(calls or 0),
        "input_tokens": int(in_tok or 0),
        "output_tokens": int(out_tok or 0),
        "cost_actual_usd": float(c_actual or 0.0),
        "cost_baseline_usd": float(c_baseline or 0.0),
        "savings_usd": float(savings or 0.0),
        "unpriced_calls": int(unpriced or 0),
        "stub_calls": int(stub_calls or 0),
    }


def org_top_routed_models(
    org_id: int, days: int = 7, limit: int = 5, *, include_stubs: bool = False
) -> list[dict[str, Any]]:
    """Top *limit* routed models by call count over the window, scoped to one Org."""
    _ensure_db()
    since = datetime.now(UTC) - timedelta(days=max(1, int(days)))
    with SessionLocal() as session:
        filters = [UsageAttribution.org_id == org_id, UsageLedgerEntry.ts >= since]
        if not include_stubs:
            filters.append(UsageLedgerEntry.is_stub.is_(False))
        results = session.execute(
            select(
                UsageLedgerEntry.routed_model,
                func.count(UsageLedgerEntry.id),
                func.coalesce(func.sum(UsageLedgerEntry.savings_usd), 0.0),
            )
            .select_from(UsageLedgerEntry)
            .join(UsageAttribution, UsageAttribution.usage_entry_id == UsageLedgerEntry.id)
            .where(*filters)
            .group_by(UsageLedgerEntry.routed_model)
            .order_by(func.count(UsageLedgerEntry.id).desc())
            .limit(int(limit))
        ).all()
    return [
        {
            "routed_model": row[0] or "",
            "calls": int(row[1] or 0),
            "savings_usd": float(row[2] or 0.0),
        }
        for row in results
    ]

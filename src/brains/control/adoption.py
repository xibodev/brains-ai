"""Adoption telemetry: did the agent actually use what the welcome packet offered?

The welcome packet returned by :func:`brains.control.sessions.start_session`
surfaces unread mail, applicable patterns, relevant memories, missing/unverified
tools, and index hints. Every session_start event now carries a snapshot of those
counts in its ``metadata_json`` (see ``control/sessions.py``).

This module closes the measurement loop by joining the *offer* (session_start)
against the *action* (follow-up event with the same session_id, within a short
window). The result is a per-surface hit-rate that answers:

    "Of N sessions where the welcome packet offered <surface>, how many
    actually called the matching tool within <window> minutes?"

Surface → follow-up event kind:

* ``unread_messages``     → ``message_read``     (mailbox.read_messages)
* ``applicable_patterns`` → ``pattern_used``     (patterns.use_pattern)
* ``relevant_memories``   → ``memory_retrieved`` (repositories.retrieve_memory)
* ``tools_missing``       → ``tool_verified``    (tool_registry.verify_tool)
* ``tools_unverified``    → ``tool_verified``    (tool_registry.verify_tool)

All follow-up event sites now thread ``session_id`` through; without it the
self-join cannot tie the action back to the session that was offered the cue.
Every externally returned aggregate enforces a minimum group size; this module
never returns a per-session row or native identifier.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func

from brains.control.common import utc_now
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Event, Workspace

MINIMUM_ANALYTICS_GROUP_SIZE = 3

# Stable list so callers and tests have a single source of truth.
SURFACES: list[tuple[str, str]] = [
    ("unread_messages", "message_read"),
    ("applicable_patterns", "pattern_used"),
    ("relevant_memories", "memory_retrieved"),
    ("tools_missing", "tool_verified"),
    ("tools_unverified", "tool_verified"),
]
AGGREGATED_EVENT_KINDS = frozenset(
    {"session_start", *(follow_kind for _key, follow_kind in SURFACES)}
)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _suppress_related_counts(*counts: int) -> tuple[list[int | None], bool]:
    suppressed = any(0 < count < MINIMUM_ANALYTICS_GROUP_SIZE for count in counts)
    if not suppressed:
        return list(counts), False
    return [0 if count == 0 else None for count in counts], True


def adoption_report(
    window_minutes: int = 2,
    since_days: int = 14,
    workspace: str | None = None,
) -> dict:
    """Compute per-surface adoption hit-rates over the recent event log.

    Parameters
    ----------
    window_minutes:
        Maximum time between the session_start event and the follow-up event
        for the follow-up to count as adoption. Default 2 minutes.
    since_days:
        Lookback window for ``session_start`` events (and follow-ups). Default
        14 days.
    workspace:
        Optional workspace slug filter. ``None`` means all workspaces.

    Returns
    -------
    dict with shape::

        {
          "window_minutes": int,
          "since_days": int,
          "workspace": str | None,
          "sessions_started": int | None,    # suppressed below the minimum group size
          "surfaces": {
            "<surface_key>": {
              "follow_kind": str,
               "offered": int | None,         # sessions where this surface was non-zero
               "acted":   int | None,         # matching follow-up in the window
              "rate":    float | None,       # acted / offered, None if offered == 0
            },
            ...
          },
          "totals_by_kind": [{"kind": str, "count": int | None}, ...],
        }
    """
    if window_minutes <= 0:
        raise ValueError("window_minutes must be positive")
    if since_days <= 0:
        raise ValueError("since_days must be positive")

    init_db()
    now = utc_now()
    cutoff = now - timedelta(days=since_days)
    window = timedelta(minutes=window_minutes)
    eligible_before = now - window

    with SessionLocal() as session:
        workspace_id: int | None = None
        if workspace:
            ws = session.query(Workspace).filter(Workspace.slug == workspace).one_or_none()
            if ws is None:
                raise ValueError(f"unknown workspace: {workspace}")
            workspace_id = ws.id

        starts_q = session.query(Event).filter(
            Event.kind == "session_start",
            Event.created_at >= cutoff,
        )
        if workspace_id is not None:
            starts_q = starts_q.filter(Event.workspace_id == workspace_id)
        starts = starts_q.all()
        sessions_observed = len(starts)
        eligible_starts = [
            event for event in starts if _as_utc(event.created_at) <= eligible_before
        ]
        sessions_excluded_incomplete_window = sessions_observed - len(eligible_starts)

        # Per surface, collect (session_id, start_at). A session can theoretically
        # appear more than once if start_session is called twice for the same id
        # (we do not do this today, but the dedup below is defensive: keep the
        # earliest start, so the action window is conservative).
        offered_by_surface: dict[str, dict[str, datetime]] = {key: {} for key, _ in SURFACES}
        for ev in eligible_starts:
            if not ev.session_id or not ev.metadata_json:
                continue
            try:
                meta = json.loads(ev.metadata_json)
            except (TypeError, ValueError):
                continue
            welcome = meta.get("welcome") or {}
            for key, _ in SURFACES:
                try:
                    value = int(welcome.get(key, 0) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    start_at = _as_utc(ev.created_at)
                    existing = offered_by_surface[key].get(ev.session_id)
                    if existing is None or start_at < existing:
                        offered_by_surface[key][ev.session_id] = start_at

        surface_results: dict[str, dict] = {}
        for key, follow_kind in SURFACES:
            start_by_session = offered_by_surface[key]
            offered_count = len(start_by_session)
            if offered_count == 0:
                surface_results[key] = {
                    "follow_kind": follow_kind,
                    "offered": 0,
                    "acted": 0,
                    "rate": None,
                    "suppressed": False,
                }
                continue
            session_ids = list(start_by_session.keys())
            follows = (
                session.query(Event.session_id, Event.created_at)
                .filter(
                    Event.kind == follow_kind,
                    Event.session_id.in_(session_ids),
                    Event.created_at >= cutoff,
                )
                .all()
            )
            acted: set[str] = set()
            for sid, follow_at in follows:
                if sid is None or follow_at is None:
                    continue
                offered_at = start_by_session.get(sid)
                if offered_at is None:
                    continue
                normalized_follow = _as_utc(follow_at)
                if offered_at <= normalized_follow <= offered_at + window:
                    acted.add(sid)
            acted_count = len(acted)
            visible_counts, suppressed = _suppress_related_counts(offered_count, acted_count)
            visible_offered, visible_acted = visible_counts
            surface_results[key] = {
                "follow_kind": follow_kind,
                "offered": visible_offered,
                "acted": visible_acted,
                "rate": (
                    (acted_count / offered_count) if offered_count and not suppressed else None
                ),
                "suppressed": suppressed,
            }

        totals_q = session.query(Event.kind, func.count(Event.id)).filter(
            Event.created_at >= cutoff,
            Event.kind.in_(AGGREGATED_EVENT_KINDS),
        )
        if workspace_id is not None:
            totals_q = totals_q.filter(Event.workspace_id == workspace_id)
        totals_rows = totals_q.group_by(Event.kind).all()
        totals = [
            {
                "kind": kind,
                "count": None if 0 < int(count) < MINIMUM_ANALYTICS_GROUP_SIZE else int(count),
                "suppressed": 0 < int(count) < MINIMUM_ANALYTICS_GROUP_SIZE,
            }
            for kind, count in totals_rows
        ]
        totals.sort(key=lambda row: int(row["count"] or 0), reverse=True)

    from brains.control.mailbox_observability import mailbox_outcome_report

    mailbox_outcomes = mailbox_outcome_report(
        window_minutes=window_minutes,
        since_days=since_days,
        workspace=workspace,
    )
    session_counts, sessions_suppressed = _suppress_related_counts(
        sessions_observed,
        len(eligible_starts),
        sessions_excluded_incomplete_window,
    )
    suppressed_total_kinds = {
        follow_kind for key, follow_kind in SURFACES if surface_results[key]["suppressed"]
    }
    if sessions_suppressed:
        suppressed_total_kinds.add("session_start")
    for total in totals:
        if total["kind"].startswith("mailbox_") or total["kind"] in suppressed_total_kinds:
            total["count"] = None
            total["suppressed"] = True
    return {
        "window_minutes": window_minutes,
        "since_days": since_days,
        "workspace": workspace,
        "observed_at": now.isoformat(),
        "observation_started_at": cutoff.isoformat(),
        "eligible_before": eligible_before.isoformat(),
        "minimum_group_size": MINIMUM_ANALYTICS_GROUP_SIZE,
        "sessions_started": session_counts[0],
        "sessions_eligible": session_counts[1],
        "sessions_excluded_incomplete_window": session_counts[2],
        "sessions_suppressed": sessions_suppressed,
        "interpretation": {
            "unit": "session_start event",
            "rate": "sessions with a matching follow-up event divided by eligible sessions offered the surface",
            "not_measured": [
                "task success",
                "user value",
                "causal impact",
                "actions outside the configured follow-up window",
            ],
        },
        "surfaces": surface_results,
        "totals_by_kind": totals,
        "mailbox_outcomes": mailbox_outcomes,
    }

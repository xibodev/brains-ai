"""Tests for the Tier 3 TTL hygiene additions.

* ``mark_stale_handoffs`` flips active handoffs older than the configured
  age to ``stale``.
* ``list_handoffs`` opportunistically sweeps so the read path is honest.
* ``start_session`` runs the sweep so its returned ``active_handoff`` is
  never an ancient row.
"""

from __future__ import annotations

from datetime import timedelta

from brains.control.common import utc_now
from brains.control.handoffs import (
    list_handoffs,
    mark_stale_handoffs,
    set_handoff,
)
from brains.control.sessions import start_session
from brains.storage.db import SessionLocal
from brains.storage.models import Handoff


def _force_handoff_age(handoff_id: int, age_hours: int) -> None:
    """Backdate a handoff so the staleness sweep treats it as old."""
    with SessionLocal() as session:
        row = session.query(Handoff).filter(Handoff.id == handoff_id).one()
        row.set_at = utc_now() - timedelta(hours=age_hours)
        session.commit()


def test_mark_stale_flips_old_active_handoffs(tmp_path):
    handoff = set_handoff(str(tmp_path), title="ttl-test-old", body="x")
    _force_handoff_age(handoff["handoff_id"], age_hours=48)
    flipped = mark_stale_handoffs(max_age_hours=24)
    assert flipped >= 1
    # The row should no longer surface as active.
    actives = list_handoffs(workspace_path=str(tmp_path), active_only=True)
    assert all(h["handoff_id"] != handoff["handoff_id"] for h in actives)


def test_list_handoffs_sweeps_opportunistically(tmp_path):
    handoff = set_handoff(str(tmp_path), title="ttl-test-list", body="x")
    _force_handoff_age(handoff["handoff_id"], age_hours=48)
    # No explicit mark — list_handoffs should sweep on its own.
    actives = list_handoffs(workspace_path=str(tmp_path), active_only=True)
    assert all(h["handoff_id"] != handoff["handoff_id"] for h in actives)
    # The row is still inspectable with active_only=False.
    everything = list_handoffs(workspace_path=str(tmp_path), active_only=False)
    matched = [h for h in everything if h["handoff_id"] == handoff["handoff_id"]]
    assert matched, "stale handoff must still be inspectable"
    assert matched[0]["status"] == "stale"


def test_start_session_does_not_surface_stale_handoff(tmp_path):
    handoff = set_handoff(str(tmp_path), title="ttl-test-start", body="x")
    _force_handoff_age(handoff["handoff_id"], age_hours=48)
    started = start_session(str(tmp_path), tool="pytest")
    # The previous behavior would have returned the ancient handoff as
    # active_handoff. The TTL hygiene must suppress it.
    if started["active_handoff"] is not None:
        assert started["active_handoff"]["id"] != handoff["handoff_id"]

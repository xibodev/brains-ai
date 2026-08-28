"""Tests for ``brains.control.adoption.adoption_report``.

These tests exercise the full offer → action join: seed a session whose
welcome packet flags a surface as non-zero, optionally have the agent
perform the matching follow-up tool call, then assert the report counts
the session under ``offered`` and (only if the follow-up happened in
the window) under ``acted``.

Shared dev DB caveat (same as the rest of the suite): we namespace by
``tmp_path`` and unique slugs so we don't collide with other tests'
event rows. The report's ``since_days`` defaults to 14 days, more than
wide enough for everything written in a single suite run.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from brains.control.adoption import SURFACES, adoption_report
from brains.control.common import utc_now
from brains.control.mailbox import read_messages, send_message
from brains.control.patterns import approve_pattern, propose_pattern, use_pattern
from brains.control.sessions import register_workspace, start_session
from brains.control.tool_registry import register_tool, verify_tool
from brains.storage.db import SessionLocal
from brains.storage.models import Event
from brains.storage.repositories import retrieve_memory, store_memory


def _make_workspace(tmp_path, prefix: str):
    leaf = f"{prefix}-{uuid.uuid4().hex[:8]}"
    target = tmp_path / leaf
    target.mkdir()
    return target, register_workspace(str(target))


def test_report_shape_is_stable():
    """Even on an empty workspace the report shape must be the contract."""
    report = adoption_report(window_minutes=1, since_days=1, workspace=None)
    assert set(report.keys()) == {
        "window_minutes",
        "since_days",
        "workspace",
        "sessions_started",
        "surfaces",
        "totals_by_kind",
    }
    # Every declared surface must appear, even when offered == 0.
    for key, follow_kind in SURFACES:
        assert key in report["surfaces"], f"missing surface {key}"
        bucket = report["surfaces"][key]
        assert bucket["follow_kind"] == follow_kind
        assert "offered" in bucket
        assert "acted" in bucket
        assert "rate" in bucket
    assert isinstance(report["totals_by_kind"], list)


def test_invalid_window_minutes_raises():
    import pytest

    with pytest.raises(ValueError):
        adoption_report(window_minutes=0)
    with pytest.raises(ValueError):
        adoption_report(window_minutes=-1)


def test_invalid_since_days_raises():
    import pytest

    with pytest.raises(ValueError):
        adoption_report(since_days=0)


def test_unknown_workspace_raises(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        adoption_report(workspace=f"missing-{uuid.uuid4().hex}")


def test_unread_mail_offered_but_not_acted(tmp_path):
    target, workspace = _make_workspace(tmp_path, "adopt-mail-skip")
    # Workspace-broadcast mail: visible to any session in the workspace,
    # not just a single named recipient. (Direct mail to_session_id is
    # private to that recipient; we want a new session to see it.)
    send_message(
        subject=f"hey-{uuid.uuid4().hex}",
        body="needs eyes",
        workspace_path=str(target),
    )
    silent = start_session(str(target), tool="pytest")
    sid = silent["session_id"]
    welcome = silent.get("welcome") or {}
    assert (welcome.get("unread_messages") or {}).get("count", 0) >= 1, (
        "test precondition: session must see the broadcast mail"
    )
    report = adoption_report(window_minutes=1, since_days=1, workspace=workspace.slug)
    bucket = report["surfaces"]["unread_messages"]
    assert bucket["offered"] >= 1
    # The silent session contributes to offered but NOT to acted.
    # (Acted may be > 0 if other sessions in same workspace read mail —
    # so we assert acted < offered, the strict inequality is what we
    # actually care about.)
    assert bucket["acted"] < bucket["offered"]
    # And the silent session id specifically is not in the acted set:
    # verify by checking there is no message_read event for sid.
    with SessionLocal() as db:
        n = db.query(Event).filter(Event.session_id == sid, Event.kind == "message_read").count()
        assert n == 0


def test_unread_mail_offered_and_acted_in_window(tmp_path):
    target, workspace = _make_workspace(tmp_path, "adopt-mail-act")
    # Workspace-broadcast mail so any new session sees it as unread.
    send_message(
        subject=f"act-{uuid.uuid4().hex}",
        body="x",
        workspace_path=str(target),
    )
    actor = start_session(str(target), tool="pytest")
    aid = actor["session_id"]
    welcome = actor.get("welcome") or {}
    assert (welcome.get("unread_messages") or {}).get("count", 0) >= 1
    # Follow-up call.
    read_messages(aid)
    report = adoption_report(window_minutes=2, since_days=1, workspace=workspace.slug)
    bucket = report["surfaces"]["unread_messages"]
    assert bucket["offered"] >= 1
    assert bucket["acted"] >= 1
    assert bucket["rate"] is not None
    assert 0.0 < bucket["rate"] <= 1.0


def test_pattern_offered_and_acted(tmp_path):
    target, workspace = _make_workspace(tmp_path, "adopt-pat")
    # Seed an approved pattern matching this workspace BEFORE starting
    # the session so the welcome packet picks it up.
    name = f"adopt-pat-{uuid.uuid4().hex}"
    propose_pattern(
        name=name,
        category="testing",
        description="adoption probe",
        applies_to=f"adopt-pat-*,{workspace.slug}",
    )
    approve_pattern(name)
    session = start_session(str(target), tool="pytest")
    sid = session["session_id"]
    welcome = session.get("welcome") or {}
    assert len(welcome.get("applicable_patterns") or []) >= 1
    # The agent uses the pattern, threading session_id through (the
    # whole point of the threading change is this join works).
    use_pattern(name, session_id=sid)
    report = adoption_report(window_minutes=2, since_days=1, workspace=workspace.slug)
    bucket = report["surfaces"]["applicable_patterns"]
    assert bucket["offered"] >= 1
    assert bucket["acted"] >= 1


def test_memory_offered_and_acted(tmp_path):
    target, workspace = _make_workspace(tmp_path, "adopt-mem")
    # Seed a workspace-keyed memory.
    key = f"workspace:{workspace.slug}:probe"
    store_memory(key, "value")
    session = start_session(str(target), tool="pytest")
    sid = session["session_id"]
    welcome = session.get("welcome") or {}
    assert len(welcome.get("relevant_memories") or []) >= 1
    # Follow-up: agent fetches the memory and attributes to its session.
    retrieve_memory(key, session_id=sid)
    report = adoption_report(window_minutes=2, since_days=1, workspace=workspace.slug)
    bucket = report["surfaces"]["relevant_memories"]
    assert bucket["offered"] >= 1
    assert bucket["acted"] >= 1


def test_tools_missing_offered_and_acted(tmp_path):
    target, workspace = _make_workspace(tmp_path, "adopt-tool")
    # Register a tool we know is missing (unique nonsense binary name).
    tool_name = f"adopt-missing-{uuid.uuid4().hex[:8]}"
    register_tool(
        name=tool_name,
        display_name=tool_name,
        cli_command=f"def_not_real_{uuid.uuid4().hex[:6]}",
    )
    session = start_session(str(target), tool="pytest")
    sid = session["session_id"]
    welcome = session.get("welcome") or {}
    ts = welcome.get("tool_status") or {}
    assert int(ts.get("missing", 0)) >= 1
    # Follow-up: agent re-verifies the tool, attributes to its session.
    verify_tool(tool_name, session_id=sid)
    report = adoption_report(window_minutes=2, since_days=1, workspace=workspace.slug)
    bucket = report["surfaces"]["tools_missing"]
    assert bucket["offered"] >= 1
    assert bucket["acted"] >= 1


def test_followup_outside_window_does_not_count(tmp_path):
    """Synthetic test: backdate a session_start by one hour, leave the
    follow-up at "now". With a small window, the action is OUTSIDE the
    join window so the session counts as offered-not-acted.
    """
    target, workspace = _make_workspace(tmp_path, "adopt-late")
    # Seed broadcast mail and start session (offered).
    send_message(
        subject=f"late-{uuid.uuid4().hex}",
        body="x",
        workspace_path=str(target),
    )
    actor = start_session(str(target), tool="pytest")
    aid = actor["session_id"]
    welcome = actor.get("welcome") or {}
    assert (welcome.get("unread_messages") or {}).get("count", 0) >= 1
    # Mutate the session_start event's created_at backwards so the
    # follow-up (added next) lands well outside any reasonable window.
    with SessionLocal() as db:
        start_ev = (
            db.query(Event)
            .filter(Event.session_id == aid, Event.kind == "session_start")
            .one_or_none()
        )
        assert start_ev is not None
        start_ev.created_at = utc_now() - timedelta(hours=1)
        db.commit()
    # Now the follow-up.
    read_messages(aid)
    # Tight 1-minute window: session_start is 1h old, message_read is
    # ~now → 60 minutes apart → outside the window.
    report = adoption_report(window_minutes=1, since_days=1, workspace=workspace.slug)
    bucket = report["surfaces"]["unread_messages"]
    # Specifically: aid was offered, but did NOT act inside the window.
    # We can't easily assert globally because other tests in the workspace
    # may have offered+acted. So verify by re-running with a generous
    # window: acted should rise when the window widens enough.
    report_wide = adoption_report(window_minutes=120, since_days=1, workspace=workspace.slug)
    bucket_wide = report_wide["surfaces"]["unread_messages"]
    assert bucket_wide["acted"] >= bucket["acted"], (
        "widening the window must monotonically increase acted count"
    )
    # And the strict version: aid contributed to wide-acted but not
    # narrow-acted, so wide > narrow at least when aid is the only
    # difference. Use that as the assertion.
    assert bucket_wide["acted"] > bucket["acted"] or bucket["offered"] == 0


def test_workspace_filter_isolates_scope(tmp_path):
    """Sessions in workspace A must not pollute the report for workspace B."""
    target_a, ws_a = _make_workspace(tmp_path, "adopt-iso-a")
    target_b, ws_b = _make_workspace(tmp_path, "adopt-iso-b")
    # Seed broadcast mail + acted session in A.
    send_message(
        subject=f"iso-{uuid.uuid4().hex}",
        body="x",
        workspace_path=str(target_a),
    )
    actor = start_session(str(target_a), tool="pytest")
    read_messages(actor["session_id"])
    # No activity in B.
    report_a = adoption_report(window_minutes=2, since_days=1, workspace=ws_a.slug)
    report_b = adoption_report(window_minutes=2, since_days=1, workspace=ws_b.slug)
    assert report_a["surfaces"]["unread_messages"]["offered"] >= 1
    assert report_b["surfaces"]["unread_messages"]["offered"] == 0
    assert report_b["surfaces"]["unread_messages"]["acted"] == 0
    assert report_b["sessions_started"] == 0


def test_totals_by_kind_is_ordered_and_includes_session_start(tmp_path):
    target, workspace = _make_workspace(tmp_path, "adopt-totals")
    # Drive a few events through the workspace.
    s = start_session(str(target), tool="pytest")
    send_message(
        subject="x",
        body="y",
        from_session_id=s["session_id"],
        workspace_path=str(target),
    )
    read_messages(s["session_id"])
    report = adoption_report(window_minutes=2, since_days=1, workspace=workspace.slug)
    totals = report["totals_by_kind"]
    assert totals, "expected at least one event kind"
    # Sorted descending by count.
    for prev, nxt in zip(totals, totals[1:], strict=False):
        assert prev["count"] >= nxt["count"]
    kinds = {row["kind"] for row in totals}
    assert "session_start" in kinds
    assert "message_sent" in kinds
    assert "message_read" in kinds


def test_emitter_threads_session_id_for_pattern_used(tmp_path):
    """Regression guard: ``pattern_used`` events must carry session_id.

    If this stops being true the adoption query silently degrades —
    pattern usage continues to be logged but cannot be joined back to
    the offering session.
    """
    target, workspace = _make_workspace(tmp_path, "adopt-emit-pat")
    name = f"emit-pat-{uuid.uuid4().hex}"
    propose_pattern(
        name=name,
        category="testing",
        description="emitter guard",
        applies_to=f"emit-pat-*,{workspace.slug}",
    )
    approve_pattern(name)
    s = start_session(str(target), tool="pytest")
    sid = s["session_id"]
    use_pattern(name, session_id=sid)
    with SessionLocal() as db:
        ev = (
            db.query(Event)
            .filter(Event.kind == "pattern_used", Event.session_id == sid)
            .one_or_none()
        )
        assert ev is not None, "pattern_used must carry session_id"


def test_emitter_threads_session_id_for_tool_verified(tmp_path):
    target, _ = _make_workspace(tmp_path, "adopt-emit-tool")
    tool_name = f"emit-tool-{uuid.uuid4().hex[:8]}"
    register_tool(
        name=tool_name,
        display_name=tool_name,
        cli_command=f"def_not_real_{uuid.uuid4().hex[:6]}",
    )
    s = start_session(str(target), tool="pytest")
    sid = s["session_id"]
    verify_tool(tool_name, session_id=sid)
    with SessionLocal() as db:
        ev = (
            db.query(Event)
            .filter(Event.kind == "tool_verified", Event.session_id == sid)
            .one_or_none()
        )
        assert ev is not None, "tool_verified must carry session_id"


def test_emitter_emits_message_read_event(tmp_path):
    target, _ = _make_workspace(tmp_path, "adopt-emit-read")
    s = start_session(str(target), tool="pytest")
    sid = s["session_id"]
    send_message("read me", to_session_id=sid)
    read_messages(sid)
    with SessionLocal() as db:
        ev = (
            db.query(Event)
            .filter(Event.kind == "message_read", Event.session_id == sid)
            .one_or_none()
        )
        assert ev is not None, "a non-empty read_messages call must emit message_read"


def test_emitter_emits_memory_retrieved_event(tmp_path):
    target, workspace = _make_workspace(tmp_path, "adopt-emit-mem")
    key = f"workspace:{workspace.slug}:emit-probe"
    store_memory(key, "x")
    s = start_session(str(target), tool="pytest")
    sid = s["session_id"]
    retrieve_memory(key, session_id=sid)
    with SessionLocal() as db:
        ev = (
            db.query(Event)
            .filter(Event.kind == "memory_retrieved", Event.session_id == sid)
            .one_or_none()
        )
        assert ev is not None, "retrieve_memory must emit a memory_retrieved event"

"""Tests for the brain-session resume layer.

Covers the three coupled pieces shipped together: the
``tool_session_links`` mapping table, the ``session_checkpoints``
table, and the ``AgentSession.last_activity_at`` heartbeat that
``append_event`` stamps automatically whenever a brain tool call carries
a ``session_id``.

All tests namespace by ``tmp_path`` because the suite shares the dev DB
(no per-test isolation). Identifiers are randomised with ``uuid`` so
parallel runs and reruns don't collide.
"""

from __future__ import annotations

import time
import uuid

import pytest

from brains.control.events import append_event
from brains.control.handoffs import set_handoff
from brains.control.mailbox import send_message
from brains.control.resume import (
    checkpoint,
    find_brain_sessions,
    latest_checkpoint,
    link_tool_session,
    list_checkpoints,
    list_tool_session_links,
    resume_brain_session,
)
from brains.control.sessions import (
    AgentSessionNotFoundError,
    start_session,
)
from brains.storage.db import SessionLocal
from brains.storage.models import AgentSession

# ---------------------------------------------------------------- linking


def test_link_tool_session_records_triple(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    tool_id = f"copilot-{uuid.uuid4().hex}"
    row = link_tool_session(sid, "copilot-cli", tool_id, linked_by="operator")
    assert row["brain_session_id"] == sid
    assert row["tool"] == "copilot-cli"
    assert row["tool_session_id"] == tool_id
    assert row["linked_by"] == "operator"


def test_link_tool_session_is_idempotent(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    tool_id = f"claude-{uuid.uuid4().hex}"
    first = link_tool_session(sid, "claude-code", tool_id)
    second = link_tool_session(sid, "claude-code", tool_id)
    assert first["id"] == second["id"], "re-linking the same triple must be a no-op"


def test_link_tool_session_rejects_unknown_brain_session():
    with pytest.raises(AgentSessionNotFoundError):
        link_tool_session("ses_does_not_exist", "claude-code", "x")


def test_link_tool_session_validates_inputs(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    with pytest.raises(ValueError):
        link_tool_session(sid, "", "abc")
    with pytest.raises(ValueError):
        link_tool_session(sid, "claude-code", "abc", linked_by="bogus")


def test_list_tool_session_links_orders_oldest_first(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    first = f"a-{uuid.uuid4().hex}"
    second = f"b-{uuid.uuid4().hex}"
    link_tool_session(sid, "claude-code", first)
    link_tool_session(sid, "claude-code", second)
    rows = list_tool_session_links(sid)
    ids = [r["tool_session_id"] for r in rows]
    assert ids.index(first) < ids.index(second)


def test_find_brain_sessions_reverse_lookup(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    tool_id = f"codex-{uuid.uuid4().hex}"
    link_tool_session(sid, "codex", tool_id)
    rows = find_brain_sessions("codex", tool_id)
    assert any(r["brain_session_id"] == sid for r in rows)
    # Workspace slug is surfaced so the operator can disambiguate.
    assert all("workspace" in r for r in rows)


# ------------------------------------------------------------- checkpoints


def test_checkpoint_requires_summary(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    with pytest.raises(ValueError):
        checkpoint(sid, "   ")


def test_checkpoint_persists_full_payload(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    row = checkpoint(
        sid,
        summary="finished migrations, about to compact",
        next_action="run pytest on the resume layer",
        blockers="none",
        scratchpad_path=".agents/scratch.md",
    )
    assert row["summary"] == "finished migrations, about to compact"
    assert row["next_action"] == "run pytest on the resume layer"
    assert row["blockers"] == "none"
    assert row["scratchpad_path"] == ".agents/scratch.md"


def test_checkpoint_normalises_empty_fields_to_none(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    row = checkpoint(sid, summary="quick cairn")
    assert row["next_action"] is None
    assert row["blockers"] is None
    assert row["scratchpad_path"] is None


def test_list_checkpoints_returns_newest_first(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    checkpoint(sid, summary=f"first-{uuid.uuid4().hex}")
    # Small sleep is fine — the suite uses tmp_path namespacing and the
    # ordering query also breaks ties on id desc, but the timestamp
    # difference is the primary signal.
    time.sleep(0.01)
    checkpoint(sid, summary=f"second-{uuid.uuid4().hex}")
    rows = list_checkpoints(sid, limit=10)
    assert len(rows) >= 2
    assert rows[0]["summary"].startswith("second-")


def test_latest_checkpoint_returns_none_when_empty(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    assert latest_checkpoint(sid) is None
    checkpoint(sid, summary="now there is one")
    assert latest_checkpoint(sid) is not None


def test_checkpoint_rejects_unknown_session():
    with pytest.raises(AgentSessionNotFoundError):
        checkpoint("ses_does_not_exist", summary="orphan")


# ----------------------------------------------------------------- resume


def test_resume_brain_session_returns_full_packet(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    checkpoint(sid, summary="anchor for resume", next_action="continue")
    fresh_tool_id = f"copilot-fresh-{uuid.uuid4().hex}"
    packet = resume_brain_session(
        sid,
        tool="copilot-cli",
        tool_session_id=fresh_tool_id,
        operator=True,
    )
    # Top-level shape is the durable contract the agent reads on
    # restart, so each key is asserted.
    for key in (
        "brain_session",
        "last_checkpoint",
        "checkpoints_total",
        "active_handoffs",
        "active_claims",
        "open_tasks",
        "unread_messages",
        "tool_session_links",
        "recent_events",
    ):
        assert key in packet, f"resume packet missing {key}"
    assert packet["brain_session"]["id"] == sid
    assert packet["checkpoints_total"] >= 1
    assert packet["last_checkpoint"]["summary"] == "anchor for resume"
    # The fresh tool-side id passed in must have been linked in the same
    # call.
    linked_ids = [link["tool_session_id"] for link in packet["tool_session_links"]]
    assert fresh_tool_id in linked_ids
    # Operator-driven resume must record linked_by="operator".
    assert any(
        link["tool_session_id"] == fresh_tool_id and link["linked_by"] == "operator"
        for link in packet["tool_session_links"]
    )


def test_resume_brain_session_surfaces_active_handoff(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    title = f"do-the-thing-{uuid.uuid4().hex}"
    set_handoff(
        workspace_path=str(tmp_path),
        title=title,
        session_id=sid,
    )
    packet = resume_brain_session(sid)
    assert any(h.get("title") == title for h in packet["active_handoffs"])


def test_resume_brain_session_surfaces_unread_mail_preview(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    subj = f"please-resume-{uuid.uuid4().hex}"
    send_message(
        subject=subj,
        body="ping",
        to_session_id=sid,
        workspace_path=str(tmp_path),
    )
    packet = resume_brain_session(sid)
    # Resume must NOT mark mail read — the agent decides.
    assert packet["unread_messages"]["count"] >= 1
    assert any(m["subject"] == subj for m in packet["unread_messages"]["preview"])


def test_resume_brain_session_rejects_unknown_session():
    with pytest.raises(AgentSessionNotFoundError):
        resume_brain_session("ses_does_not_exist")


# ------------------------------------------------------------- heartbeat


def _read_last_activity(sid: str):
    with SessionLocal() as db:
        return db.query(AgentSession.last_activity_at).filter(AgentSession.id == sid).scalar()


def test_append_event_stamps_last_activity_at(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    # start_session also writes an event, so the column should already
    # be non-null. Capture it as the baseline.
    baseline = _read_last_activity(sid)
    assert baseline is not None
    time.sleep(0.01)
    append_event(
        "test_event",
        "heartbeat ping",
        workspace_id=None,
        session_id=sid,
    )
    after = _read_last_activity(sid)
    assert after is not None
    assert after >= baseline, "last_activity_at must monotonically advance"


def test_append_event_without_session_id_skips_heartbeat(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    before = _read_last_activity(sid)
    time.sleep(0.01)
    # Event without session_id must not touch any session row.
    append_event("test_event_no_session", "no heartbeat", workspace_id=None)
    after = _read_last_activity(sid)
    assert after == before


def test_append_event_normalizes_blank_session_id_to_null() -> None:
    row = append_event("blank_session", "legacy caller supplied blank id", session_id="  ")
    with SessionLocal() as db:
        persisted = db.get(type(row), row.id)
        assert persisted.session_id is None


def test_resume_packet_surfaces_last_activity(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    append_event(
        "test_event",
        "warm up",
        session_id=sid,
    )
    packet = resume_brain_session(sid)
    assert packet["brain_session"]["last_activity_at"] is not None

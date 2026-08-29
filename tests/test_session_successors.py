"""Explicit handle continuity and recency-based liveness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brains.control.mailbox import read_messages, send_message
from brains.control.sessions import (
    SESSION_LIVE_TTL_SECONDS,
    end_session,
    link_session_successor,
    live_replacement_session_ids,
    require_live_session,
    start_session,
)
from brains.control.topics import live_agent_sessions
from brains.storage.db import SessionLocal
from brains.storage.models import AgentSession, MailboxMessage


def test_successor_reads_unread_predecessor_mail_without_recipient_rewrite(tmp_path):
    workspace = str(tmp_path / "repo")
    old = start_session(workspace, tool="opencode")
    sent = send_message("important", to_session_id=old["session_id"])
    end_session(old["session_id"], summary="restarted")
    new = start_session(
        workspace,
        tool="opencode",
        predecessor_session_id=old["session_id"],
    )

    messages = read_messages(new["session_id"])
    assert [row["id"] for row in messages] == [sent["id"]]
    with SessionLocal() as session:
        row = session.get(MailboxMessage, sent["id"])
        assert row.to_session_id == old["session_id"]


def test_explicit_successor_link_recovers_mail(tmp_path):
    workspace = str(tmp_path / "repo")
    old = start_session(workspace, tool="opencode")
    sent = send_message("recover me", to_session_id=old["session_id"])
    end_session(old["session_id"])
    new = start_session(workspace, tool="opencode")
    link_session_successor(old["session_id"], new["session_id"])
    assert [row["id"] for row in read_messages(new["session_id"])] == [sent["id"]]


def test_explicit_successor_link_is_idempotent_but_refuses_conflicting_relink(tmp_path):
    workspace = str(tmp_path / "repo")
    old = start_session(workspace, tool="opencode")
    first = start_session(workspace, tool="opencode")
    other = start_session(workspace, tool="opencode")

    initial = link_session_successor(old["session_id"], first["session_id"])
    retry = link_session_successor(old["session_id"], first["session_id"])
    with pytest.raises(ValueError, match="refusing conflicting relink"):
        link_session_successor(old["session_id"], other["session_id"])

    assert initial["duplicate"] is False
    assert retry["duplicate"] is True


def test_replacement_candidates_and_roster_require_recent_activity(tmp_path):
    workspace = str(tmp_path / "repo")
    stale = start_session(workspace, tool="copilot")
    live = start_session(workspace, tool="opencode")
    old = datetime.now(UTC) - timedelta(seconds=SESSION_LIVE_TTL_SECONDS + 60)
    with SessionLocal() as session:
        stale_row = session.get(AgentSession, stale["session_id"])
        stale_row.last_activity_at = old
        stale_row.started_at = old
        session.commit()
        assert live_replacement_session_ids(session, stale_row.workspace_id) == [live["session_id"]]

    roster = {row["session_id"] for row in live_agent_sessions()}
    assert live["session_id"] in roster
    assert stale["session_id"] not in roster


def test_post_end_activity_heals_false_terminal_state(tmp_path):
    session_result = start_session(str(tmp_path / "repo"), tool="opencode")
    with SessionLocal() as session:
        row = session.get(AgentSession, session_result["session_id"])
        row.state = "failed"
        row.ended_at = datetime.now(UTC) - timedelta(minutes=5)
        row.last_activity_at = datetime.now(UTC)
        session.commit()

    with SessionLocal() as session:
        healed = require_live_session(session, session_result["session_id"], action="test")
        session.commit()
        assert healed.state == "running"
        assert healed.ended_at is None

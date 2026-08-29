"""Renewable coordination Session leases and canonical handle reuse."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from brains.control.claims import claim_workspace
from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.mailbox import read_messages
from brains.control.resume import resume_brain_session
from brains.control.session_commands import KIND_STOP, enqueue, get
from brains.control.sessions import (
    heartbeat_session,
    start_session,
    sweep_stale_session_leases,
)
from brains.control.tasks import claim_task, create_task
from brains.control.topics import live_agent_sessions
from brains.mcp.tools import start_session_tool
from brains.storage.db import SessionLocal
from brains.storage.models import (
    AgentSession,
    AgentTask,
    Event,
    EventContext,
    SessionCommand,
    SessionLease,
    SessionSuccessor,
    WorkspaceClaim,
)


def _expire(session_id: str) -> None:
    with SessionLocal() as session:
        lease = session.get(SessionLease, session_id)
        assert lease is not None
        lease.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()


def test_pidless_session_starts_with_renewable_lease(tmp_path) -> None:
    result = start_session(str(tmp_path / "repo"), tool="opencode")
    with SessionLocal() as session:
        lease = session.get(SessionLease, result["session_id"])
        assert lease is not None
        assert lease.lease_expires_at > lease.renewed_at
    assert result["lease_expires_at"] is not None


def test_heartbeat_renews_without_journal_event(tmp_path) -> None:
    result = start_session(str(tmp_path / "repo"), tool="opencode")
    with SessionLocal() as session:
        lease = session.get(SessionLease, result["session_id"])
        before = lease.lease_expires_at
        from brains.storage.models import Event

        events_before = (
            session.query(Event).filter(Event.session_id == result["session_id"]).count()
        )

    heartbeat = heartbeat_session(result["session_id"])

    with SessionLocal() as session:
        lease = session.get(SessionLease, result["session_id"])
        from brains.storage.models import Event

        events_after = session.query(Event).filter(Event.session_id == result["session_id"]).count()
        assert lease.lease_expires_at >= before
    assert heartbeat["state"] == "running"
    assert events_after == events_before


def test_expired_lease_becomes_dormant_and_releases_ownership(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    result = start_session(workspace, tool="opencode")
    claim_workspace(workspace, result["session_id"])
    task = create_task(workspace, "owned task")
    claim_task(task["code"], result["session_id"])
    _expire(result["session_id"])

    assert sweep_stale_session_leases() == [result["session_id"]]

    with SessionLocal() as session:
        row = session.get(AgentSession, result["session_id"])
        assert row.state == "dormant"
        assert row.ended_at is None
        assert session.query(WorkspaceClaim).filter_by(session_id=row.id).count() == 0
        task_row = session.query(AgentTask).filter_by(code=task["code"]).one()
        assert task_row.status == "available"
        assert task_row.claimed_by_session_id is None
        command_events = (
            session.query(Event, EventContext)
            .join(EventContext, EventContext.event_id == Event.id)
            .filter(Event.session_id == row.id, Event.kind == "session_dormant")
            .all()
        )
        assert len(command_events) == 1
        assert command_events[0][1].scope == "workspace"
    assert result["session_id"] not in {row["session_id"] for row in live_agent_sessions()}


def test_dormancy_cancels_open_commands_in_the_same_transition(tmp_path) -> None:
    started = start_session(str(tmp_path / "repo"), tool="opencode")
    command, created = enqueue(started["session_id"], KIND_STOP)
    assert created is True
    _expire(started["session_id"])

    sweep_stale_session_leases()

    with SessionLocal() as session:
        row = session.query(SessionCommand).filter_by(command_id=command["command_id"]).one()
        assert row.status == "cancelled"
        assert row.result == "session_dormant"


def test_resume_winning_dormancy_race_prevents_event_and_command_cancellation(
    tmp_path, monkeypatch
) -> None:
    from brains.control import session_liveness

    started = start_session(str(tmp_path / "repo"), tool="opencode")
    command, created = enqueue(started["session_id"], KIND_STOP)
    assert created is True
    _expire(started["session_id"])
    heartbeat_locked = threading.Event()
    finish_heartbeat = threading.Event()
    original_renew = session_liveness.renew_session_lease

    def paused_renew(*args, **kwargs):
        heartbeat_locked.set()
        assert finish_heartbeat.wait(timeout=5)
        return original_renew(*args, **kwargs)

    monkeypatch.setattr(session_liveness, "renew_session_lease", paused_renew)
    heartbeat_result: list[dict] = []
    sweep_result: list[list[str]] = []
    heartbeat = threading.Thread(
        target=lambda: heartbeat_result.append(heartbeat_session(started["session_id"]))
    )
    heartbeat.start()
    assert heartbeat_locked.wait(timeout=5)
    sweep = threading.Thread(target=lambda: sweep_result.append(sweep_stale_session_leases()))
    sweep.start()
    time.sleep(0.05)
    finish_heartbeat.set()
    heartbeat.join(timeout=10)
    sweep.join(timeout=10)

    assert not heartbeat.is_alive()
    assert not sweep.is_alive()
    assert heartbeat_result[0]["state"] == "running"
    assert sweep_result == [[]]
    with SessionLocal() as session:
        assert session.get(AgentSession, started["session_id"]).state == "running"
        assert (
            session.query(Event)
            .filter_by(session_id=started["session_id"], kind="session_dormant")
            .count()
            == 0
        )
        assert (
            session.query(SessionCommand).filter_by(command_id=command["command_id"]).one().status
            == "requested"
        )


def test_canonical_start_reuses_dormant_handle(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    first = start_session(workspace, tool="opencode", reuse_existing=True)
    _expire(first["session_id"])
    sweep_stale_session_leases()

    second = start_session(workspace, tool="opencode", reuse_existing=True)

    assert second["session_id"] == first["session_id"]
    assert second["reused"] is True
    with SessionLocal() as session:
        assert session.get(AgentSession, first["session_id"]).state == "running"


def test_resume_reactivates_dormant_handle_and_returns_current_state(tmp_path) -> None:
    first = start_session(str(tmp_path / "repo"), tool="opencode")
    _expire(first["session_id"])
    sweep_stale_session_leases()

    packet = resume_brain_session(first["session_id"])

    assert packet["brain_session"]["state"] == "running"
    assert packet["brain_session"]["lease_expires_at"] is not None


def test_event_on_dormant_handle_does_not_reactivate_or_renew_it(tmp_path) -> None:
    first = start_session(str(tmp_path / "repo"), tool="opencode")
    _expire(first["session_id"])
    sweep_stale_session_leases()
    with SessionLocal() as session:
        lease = session.get(SessionLease, first["session_id"])
        expired_at = lease.lease_expires_at

    append_event("late_result", "arrived after dormancy", session_id=first["session_id"])

    with SessionLocal() as session:
        row = session.get(AgentSession, first["session_id"])
        lease = session.get(SessionLease, first["session_id"])
        assert row.state == "dormant"
        assert lease.lease_expires_at == expired_at


def test_mcp_start_reuses_sole_live_handle(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    first = start_session_tool(workspace, tool="opencode")
    second = start_session_tool(workspace, tool="opencode")
    assert second["session_id"] == first["session_id"]
    assert second["reused"] is True


def test_canonical_start_refuses_ambiguous_live_handles(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    first = start_session(workspace, tool="opencode")
    second = start_session(workspace, tool="opencode")
    assert first["session_id"] != second["session_id"]

    with pytest.raises(ValueError, match="multiple live coordination sessions"):
        start_session(workspace, tool="opencode", reuse_existing=True)


def test_canonical_start_reuses_freshest_ambiguous_dormant_handle(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    first = start_session(workspace, tool="opencode")
    second = start_session(workspace, tool="opencode")
    _expire(first["session_id"])
    _expire(second["session_id"])
    sweep_stale_session_leases()

    reused = start_session(workspace, tool="opencode", reuse_existing=True)

    assert reused["session_id"] == second["session_id"]
    assert reused["reused"] is True


def test_new_canonical_handle_auto_links_latest_predecessor(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    first = start_session(workspace, tool="opencode")
    claim_workspace(workspace, first["session_id"])
    task = create_task(workspace, "transfer me")
    claim_task(task["code"], first["session_id"])
    second = start_session(
        workspace,
        tool="opencode",
        pid=999_999,
        auto_link_predecessor=True,
    )

    assert second["predecessor_session_id"] == first["session_id"]
    with SessionLocal() as session:
        link = session.get(SessionSuccessor, first["session_id"])
        assert link.successor_session_id == second["session_id"]
        assert session.get(AgentSession, first["session_id"]).state == "dormant"
        transferred = session.query(WorkspaceClaim).filter_by(session_id=second["session_id"]).one()
        assert transferred.session_id == second["session_id"]
        task_row = session.query(AgentTask).filter_by(code=task["code"]).one()
        assert task_row.claimed_by_session_id == second["session_id"]


def test_auto_link_prefers_the_live_leaf_and_cancels_its_commands(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    historical = start_session(workspace, tool="opencode")
    _expire(historical["session_id"])
    sweep_stale_session_leases()
    live = start_session(workspace, tool="opencode")
    command, created = enqueue(live["session_id"], KIND_STOP)
    assert created is True

    with SessionLocal() as session:
        old = session.get(AgentSession, historical["session_id"])
        old.last_activity_at = utc_now() + timedelta(seconds=1)
        session.commit()

    replacement = start_session(
        workspace,
        tool="opencode",
        pid=999_999,
        auto_link_predecessor=True,
    )

    assert replacement["predecessor_session_id"] == live["session_id"]
    settled = get(command["command_id"])
    assert settled is not None
    assert settled["status"] == "cancelled"
    assert settled["result"] == "superseded"


def test_superseded_handle_poll_and_heartbeat_name_successor(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    old = start_session(workspace, tool="opencode")
    new = start_session(
        workspace,
        tool="opencode",
        pid=999_999,
        predecessor_session_id=old["session_id"],
    )

    with pytest.raises(ValueError, match=new["session_id"]):
        read_messages(old["session_id"])
    with pytest.raises(ValueError, match=new["session_id"]):
        heartbeat_session(old["session_id"])


def test_runner_owned_session_does_not_receive_a_coordination_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from brains.exec import guard as guard_module
    from brains.exec import runner

    monkeypatch.setattr(
        guard_module,
        "run",
        lambda *_args, **_kwargs: guard_module.GovernedRun(
            allowed=True,
            action_id="lease-test",
            status="applied",
            tier="local",
            returncode=0,
        ),
    )

    result = runner.run_session(
        ["lease-test-tool"],
        str(tmp_path / "repo"),
        tool="lease-test-tool",
    )

    with SessionLocal() as session:
        assert session.get(SessionLease, result["session_id"]) is None


def test_dormant_session_has_truthful_derived_status(tmp_path) -> None:
    from brains.control.sessions import get_agent_session

    result = start_session(str(tmp_path / "repo"), tool="opencode")
    _expire(result["session_id"])
    sweep_stale_session_leases()

    assert get_agent_session(result["session_id"])["status"] == "dormant"

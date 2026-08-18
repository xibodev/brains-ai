"""Tests for the zombie session reaper ported from agent-hivemind.

Covers:

* ``_pid_alive`` returns sensible truthiness for sentinel/self/missing PIDs.
* ``reap_zombie_sessions`` ends sessions whose PIDs are dead, deletes their
  workspace claims, and releases tasks they had ``in_progress``.
* ``start_session`` invokes the reaper so a new agent inherits a clean
  slate even after the previous agent crashed.

Tests use a per-test SQLite DB so they don't interfere with the suite-wide
``brains.db`` and don't see zombie rows left over from other tests.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import brains.control.events as events_module
import brains.control.sessions as sessions_module
import brains.storage.db as db_module
import brains.storage.migrations as migrations_module


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "reaper.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(sessions_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(events_module, "SessionLocal", SessionLocal)
    yield db_path


def _dead_pid() -> int:
    """Pick a PID that almost certainly isn't running.

    Linux PID_MAX_LIMIT is 2^22 by default and Windows allocates much smaller
    PIDs, so 999_999 is safely outside the live range on every platform we
    target.
    """
    return 999_999


def test_pid_alive_basic_cases() -> None:
    from brains.control.sessions import _pid_alive

    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False
    assert _pid_alive(None) is False  # type: ignore[arg-type]
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(_dead_pid()) is False


def test_reaper_marks_dead_session_ended_and_releases_claims(isolated_db) -> None:
    from brains.control.sessions import reap_zombie_sessions, register_workspace
    from brains.storage.models import (
        AgentSession,
        AgentTask,
        WorkspaceClaim,
    )

    workspace = register_workspace(str(isolated_db.parent / "wkspace"))

    zombie_id = f"ses_zombie_{uuid.uuid4().hex[:8]}"
    live_id = f"ses_live_{uuid.uuid4().hex[:8]}"

    with db_module.SessionLocal() as db_session:
        db_session.add_all(
            [
                AgentSession(
                    id=zombie_id,
                    workspace_id=workspace.id,
                    tool="codex",
                    pid=_dead_pid(),
                ),
                AgentSession(
                    id=live_id,
                    workspace_id=workspace.id,
                    tool="codex",
                    pid=os.getpid(),
                ),
            ]
        )
        db_session.flush()
        from brains.control.common import utc_now

        db_session.add(
            WorkspaceClaim(
                workspace_id=workspace.id,
                session_id=zombie_id,
                scope="code",
                expires_at=utc_now() + timedelta(hours=1),
            )
        )
        db_session.add(
            AgentTask(
                code=f"T-{uuid.uuid4().hex[:8]}",
                workspace_id=workspace.id,
                title="held by zombie",
                status="in_progress",
                claimed_by_session_id=zombie_id,
                claimed_at=utc_now(),
            )
        )
        db_session.add(
            AgentTask(
                code=f"T-{uuid.uuid4().hex[:8]}",
                workspace_id=workspace.id,
                title="completed earlier — must not be touched",
                status="completed",
                claimed_by_session_id=zombie_id,
                claimed_at=utc_now(),
                completed_at=utc_now(),
            )
        )
        db_session.commit()

    reaped = reap_zombie_sessions()
    assert zombie_id in reaped
    assert live_id not in reaped

    with db_module.SessionLocal() as db_session:
        zombie_row = db_session.get(AgentSession, zombie_id)
        assert zombie_row.ended_at is not None
        assert "zombie reaped" in (zombie_row.summary or "")

        live_row = db_session.get(AgentSession, live_id)
        assert live_row.ended_at is None

        remaining_claims = (
            db_session.query(WorkspaceClaim).filter(WorkspaceClaim.session_id == zombie_id).count()
        )
        assert remaining_claims == 0

        statuses = {
            row.status: row.claimed_by_session_id
            for row in db_session.query(AgentTask)
            .filter(AgentTask.claimed_by_session_id.in_([zombie_id]))
            .all()
        }
        # Completed task should still reference the zombie; in_progress
        # should have been released.
        assert "completed" in statuses
        assert "in_progress" not in statuses

        released = db_session.query(AgentTask).filter(AgentTask.status == "available").all()
        assert len(released) == 1
        assert released[0].claimed_by_session_id is None
        assert released[0].claimed_at is None


def test_reaper_is_noop_on_clean_db(isolated_db) -> None:
    from brains.control.sessions import reap_zombie_sessions

    assert reap_zombie_sessions() == []


def test_start_session_reaps_prior_zombies(isolated_db) -> None:
    from brains.control.sessions import register_workspace, start_session
    from brains.storage.models import AgentSession

    workspace = register_workspace(str(isolated_db.parent / "wkspace"))
    zombie_id = f"ses_zombie_{uuid.uuid4().hex[:8]}"
    with db_module.SessionLocal() as db_session:
        db_session.add(
            AgentSession(
                id=zombie_id,
                workspace_id=workspace.id,
                tool="codex",
                pid=_dead_pid(),
            )
        )
        db_session.commit()

    result = start_session(str(isolated_db.parent / "wkspace"), tool="codex")
    assert result["session_id"].startswith("ses_")

    with db_module.SessionLocal() as db_session:
        zombie_row = db_session.get(AgentSession, zombie_id)
        assert zombie_row.ended_at is not None

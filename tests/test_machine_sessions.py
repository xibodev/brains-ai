"""Tests for Phase 4 machine-aware sessions and tenant indexes."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import brains.control.events as events_module
import brains.control.sessions as sessions_module
import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.storage.migrations import _list_disk_migrations, init_db


@pytest.fixture
def isolated_brains(tmp_path, monkeypatch) -> Path:
    """Per-test DB + state isolation (mirrors memberships/knowledge tests)."""
    db_path = tmp_path / "isolated.sqlite"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    monkeypatch.setenv("BRAINS_AUDIT_KEY_FILE", str(tmp_path / "audit-key"))
    monkeypatch.delenv("BRAINS_AUDIT_KEY", raising=False)
    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)

    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(sessions_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(events_module, "SessionLocal", SessionLocal)

    import brains.audit as audit_module

    monkeypatch.setattr(audit_module, "SessionLocal", SessionLocal, raising=False)
    audit_module._reset_key_cache()
    yield tmp_path
    audit_module._reset_key_cache()


def _dead_pid() -> int:
    return 999_999


def test_machine_id_column_and_migration_discoverable(isolated_brains: Path) -> None:
    init_db()
    conn = sqlite3.connect(str(isolated_brains / "isolated.sqlite"))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_sessions)")}
        assert "machine_id" in cols
    finally:
        conn.close()

    names = {p.name for p in _list_disk_migrations()}
    assert "100_session_machine_id.py" in names


def test_start_session_stamps_machine_id_and_current_is_stable(
    isolated_brains: Path, tmp_path
) -> None:
    from brains.control.sessions import current_machine_id, start_session
    from brains.storage.models import AgentSession

    first = current_machine_id()
    assert current_machine_id() == first
    assert (isolated_brains / "state" / "machine-id").read_text(encoding="utf-8").strip() == first

    workspace = tmp_path / "payments-api"
    workspace.mkdir()
    result = start_session(str(workspace), tool="pytest", pid=_dead_pid())

    with db_module.SessionLocal() as session:
        row = session.get(AgentSession, result["session_id"])
        assert row is not None
        assert row.machine_id == first


def test_reaper_uses_ttl_for_foreign_sessions_and_pid_for_own_machine(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.common import utc_now
    from brains.control.sessions import (
        STALE_SESSION_TTL_SECONDS,
        current_machine_id,
        reap_zombie_sessions,
        register_workspace,
    )
    from brains.storage.models import AgentSession

    workspace = tmp_path / "payments-api"
    workspace.mkdir()
    registered = register_workspace(str(workspace))
    own_machine = current_machine_id()
    now = utc_now()
    foreign_recent_id = f"ses_foreign_recent_{uuid.uuid4().hex[:8]}"
    foreign_old_id = f"ses_foreign_old_{uuid.uuid4().hex[:8]}"
    own_dead_id = f"ses_own_dead_{uuid.uuid4().hex[:8]}"
    foreign_recent_pid = 111_111
    foreign_old_pid = 222_222
    own_dead_pid = 333_333

    with db_module.SessionLocal() as session:
        session.add_all(
            [
                AgentSession(
                    id=foreign_recent_id,
                    workspace_id=registered.id,
                    tool="codex",
                    pid=foreign_recent_pid,
                    machine_id="foreign-machine",
                    last_activity_at=now,
                ),
                AgentSession(
                    id=foreign_old_id,
                    workspace_id=registered.id,
                    tool="codex",
                    pid=foreign_old_pid,
                    machine_id="foreign-machine",
                    last_activity_at=now - timedelta(seconds=STALE_SESSION_TTL_SECONDS + 1),
                ),
                AgentSession(
                    id=own_dead_id,
                    workspace_id=registered.id,
                    tool="codex",
                    pid=own_dead_pid,
                    machine_id=own_machine,
                    # Dead pid alone is not enough anymore (field report:
                    # the recorded pid is often the stdio child). The reaper
                    # also requires a stale heartbeat.
                    last_activity_at=now - timedelta(seconds=STALE_SESSION_TTL_SECONDS + 1),
                ),
            ]
        )
        session.commit()

    checked_pids: list[int] = []

    def fake_pid_alive(pid: int) -> bool:
        checked_pids.append(pid)
        if pid in {foreign_recent_pid, foreign_old_pid}:
            raise AssertionError("foreign-machine PID should not be probed")
        return False

    monkeypatch.setattr(sessions_module, "_pid_alive", fake_pid_alive)

    reaped = reap_zombie_sessions()
    assert foreign_recent_id not in reaped
    assert foreign_old_id in reaped
    assert own_dead_id in reaped
    assert checked_pids == [own_dead_pid]

    with db_module.SessionLocal() as session:
        foreign_recent = session.get(AgentSession, foreign_recent_id)
        foreign_old = session.get(AgentSession, foreign_old_id)
        own_dead = session.get(AgentSession, own_dead_id)
        assert foreign_recent is not None
        assert foreign_old is not None
        assert own_dead is not None
        assert foreign_recent.ended_at is None
        assert foreign_old.ended_at is not None
        assert own_dead.ended_at is not None


def test_tenant_composite_indexes_exist_after_init_db(isolated_brains: Path) -> None:
    init_db()
    conn = sqlite3.connect(str(isolated_brains / "isolated.sqlite"))
    try:
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
    finally:
        conn.close()

    assert {
        "ix_agent_sessions_ws_activity",
        "ix_agent_tasks_ws_status",
        "ix_events_ws_created",
        "ix_knowledge_ws_status",
    } <= indexes

"""What the gate records once an action has been released to run.

The gate's record used to stop at the release. On POSIX it left the row
``executing`` and replaced its own process, so the only thing that ever touched
the row again was the stale sweep - which settled it ``failed`` with "abandoned
while executing" although the command had been authorised, released and run.
On the local tier it did not even mark the release, so a command that ran to
completion was swept as "abandoned in authorized before any effect".

Both are the same bug: a record that says something happened which did not.
These tests assert the truthful shape instead - what this process observed, and
nothing beyond it.
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import brains.govern as govern
from brains.exec import gate
from brains.govern import (
    STATUS_FAILED,
    STATUS_RELEASED,
    STATUS_SUCCEEDED,
    expire_stale_pending,
    list_governed_actions,
)


class _ProcessReplaced(BaseException):
    """Stands in for ``os.execv`` never returning: control does not come back."""


def _fake_bin(tmp_path: Path, name: str) -> Path:
    bindir = tmp_path / "realbin"
    bindir.mkdir(exist_ok=True)
    for filename in (name, f"{name}.cmd"):
        target = bindir / filename
        target.write_text("@echo off\n" if filename.endswith(".cmd") else "#!/bin/sh\n")
        if not filename.endswith(".cmd"):
            target.chmod(0o755)
    return bindir


@pytest.fixture
def gated_env(tmp_path, monkeypatch):
    """A resolvable fake ``git`` and a workspace, with no shim recursion."""
    bindir = _fake_bin(tmp_path, "git")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("BRAINS_GATE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("BRAINS_GATE_SHIM_DIR", raising=False)
    monkeypatch.delenv(gate.GATE_DEPTH_ENV, raising=False)
    return tmp_path


def _newest(tool: str = "git") -> dict:
    rows = [row for row in list_governed_actions(limit=50) if row["tool"] == tool]
    assert rows, "the gate recorded no governed action at all"
    return rows[0]


def _audit_actions(action_id: str) -> list[str]:
    from brains.audit import list_entries

    return [
        entry["action"]
        for entry in list_entries(action_prefix="governed.", limit=200)
        if entry["payload"].get("action_id") == action_id
    ]


def _posix(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_handoff_replaces_process", lambda: True)


def _windows(monkeypatch, returncode: int) -> None:
    monkeypatch.setattr(gate, "_handoff_replaces_process", lambda: False)

    def _fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(gate.subprocess, "run", _fake_run)


def _backdate(action_id: str, *, seconds: int) -> None:
    """Age the row's attempt past its lease, so the sweep would settle it."""
    from brains.storage.db import SessionLocal
    from brains.storage.models import GovernedAction

    stale = datetime.now(UTC) - timedelta(seconds=seconds)
    with SessionLocal() as session:
        row = session.query(GovernedAction).filter_by(action_id=action_id).one()
        row.attempt_started_at = stale
        row.created_at = stale
        session.commit()


# ----------------------------------------------------------------------
# POSIX: the handoff is the last observable fact
# ----------------------------------------------------------------------


def test_execv_handoff_is_recorded_as_released_not_as_a_completion(gated_env, monkeypatch):
    """``execv`` never returns, so the record must stop where knowledge stops."""
    _posix(monkeypatch)
    captured: dict = {}

    def _fake_execv(real, argv):
        captured["argv"] = argv
        raise _ProcessReplaced

    monkeypatch.setattr(gate.os, "execv", _fake_execv)

    with pytest.raises(_ProcessReplaced):
        gate.gate_main(["git", "status"])

    assert captured["argv"][1:] == ["status"]
    row = _newest()
    assert row["status"] == STATUS_RELEASED
    assert row["result"] == STATUS_RELEASED
    assert row["error"] is None, "a released handoff is not an error"
    assert row["executed_at"] and row["completed_at"]
    actions = _audit_actions(row["action_id"])
    assert "governed.executing" in actions
    assert "governed.released" in actions
    assert "governed.succeeded" not in actions, "the child's outcome was never observed"
    assert "governed.failed" not in actions


def test_the_sweep_never_turns_a_released_handoff_into_a_failure(gated_env, monkeypatch):
    """The bug this closes: the sweep was the only thing that touched the row."""
    _posix(monkeypatch)
    monkeypatch.setattr(gate.os, "execv", lambda *_a: (_ for _ in ()).throw(_ProcessReplaced()))

    with pytest.raises(_ProcessReplaced):
        gate.gate_main(["git", "status"])

    action_id = _newest()["action_id"]
    _backdate(action_id, seconds=govern.attempt_lease_seconds() * 4)

    expire_stale_pending()

    row = govern.get_governed_action(action_id)
    assert row["status"] == STATUS_RELEASED
    assert row["result"] == STATUS_RELEASED
    assert row["error"] is None
    assert "governed.failed" not in _audit_actions(action_id)


def test_a_failed_execv_is_recorded_as_a_failure(gated_env, monkeypatch):
    """If the replacement never happens, this process is still here to say so."""
    _posix(monkeypatch)

    def _broken_execv(_real, _argv):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(gate.os, "execv", _broken_execv)

    assert gate.gate_main(["git", "status"]) == 13

    row = _newest()
    assert row["status"] == STATUS_FAILED
    assert "execv failed" in (row["error"] or "")
    assert "governed.failed" in _audit_actions(row["action_id"])


# ----------------------------------------------------------------------
# Windows: the outcome is observable, so it is recorded
# ----------------------------------------------------------------------


def test_windows_records_the_real_exit_status(gated_env, monkeypatch):
    _windows(monkeypatch, returncode=0)

    assert gate.gate_main(["git", "status"]) == 0

    row = _newest()
    assert row["status"] == STATUS_SUCCEEDED
    assert row["result"] == "exit 0"
    assert "governed.succeeded" in _audit_actions(row["action_id"])


def test_windows_records_a_non_zero_exit_as_a_failure(gated_env, monkeypatch):
    _windows(monkeypatch, returncode=3)

    assert gate.gate_main(["git", "status"]) == 3

    row = _newest()
    assert row["status"] == STATUS_FAILED
    assert row["result"] == "exit 3"
    assert "exited 3" in (row["error"] or "")


def test_a_windows_child_that_outlives_the_lease_is_not_swept(gated_env, monkeypatch):
    """The released child is waited on here, and the wait has no upper bound.

    A `git rebase -i`, a deploy, an agent CLI: the gate sits in
    ``subprocess.run`` for as long as the operator's own tool takes. The row
    used to be judged on elapsed runtime, so a long command was settled
    ``failed`` with "abandoned while executing" underneath a process that was
    still waiting for it. The execution lease is what keeps it truthful.
    """
    monkeypatch.setenv(govern.EXECUTION_LEASE_ENV, "1")
    monkeypatch.setenv(govern.EXECUTION_HEARTBEAT_ENV, "0.05")
    monkeypatch.setattr(gate, "_handoff_replaces_process", lambda: False)
    observed: list[str] = []

    def _slow_run(argv, **_kwargs):
        deadline = time.monotonic() + 1.4  # past the 1s lease, several times over
        while time.monotonic() < deadline:
            expire_stale_pending()
            observed.append(_newest()["status"])
            time.sleep(0.05)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(gate.subprocess, "run", _slow_run)

    assert gate.gate_main(["git", "status"]) == 0

    assert observed and set(observed) == {"executing"}, (
        f"a running child was swept as abandoned: {set(observed)}"
    )
    row = _newest()
    assert row["status"] == STATUS_SUCCEEDED
    assert row["error"] is None
    assert row["heartbeat_at"] is None


# ----------------------------------------------------------------------
# Both tiers use the same lifecycle
# ----------------------------------------------------------------------


def test_a_local_command_is_settled_rather_than_left_authorized(gated_env, monkeypatch):
    """A local command that ran must not be swept as "never ran"."""
    _windows(monkeypatch, returncode=0)

    assert gate.gate_main(["git", "status"]) == 0

    action_id = _newest()["action_id"]
    _backdate(action_id, seconds=govern.attempt_lease_seconds() * 4)
    expire_stale_pending()

    row = govern.get_governed_action(action_id)
    assert row["status"] == STATUS_SUCCEEDED
    assert row["error"] is None


def test_an_approved_outward_command_takes_the_same_lifecycle(gated_env, monkeypatch):
    """The gated tier records executing -> released exactly as the local one."""
    from brains.control.decisions import list_open_decisions, resolve_decision

    _posix(monkeypatch)
    monkeypatch.setattr(gate.os, "execv", lambda *_a: (_ for _ in ()).throw(_ProcessReplaced()))

    workspace = str(gated_env)

    def _approve_soon() -> None:
        deadline = time.time() + 20
        while time.time() < deadline:
            pending = list_open_decisions(workspace_path=workspace)
            if pending:
                resolve_decision(pending[0]["code"], chosen="approve", reasoning="ok")
                return
            time.sleep(0.05)

    resolver = threading.Thread(target=_approve_soon, daemon=True)
    resolver.start()
    try:
        with pytest.raises(_ProcessReplaced):
            gate.gate_main(["git", "push", "origin", "main"])
    finally:
        resolver.join(timeout=5)

    row = _newest()
    assert row["tier"] == govern.TIER_OUTWARD
    assert row["status"] == STATUS_RELEASED
    assert row["approval_code"]
    actions = _audit_actions(row["action_id"])
    assert "governed.executing" in actions
    assert "governed.released" in actions

    _backdate(row["action_id"], seconds=govern.attempt_lease_seconds() * 4)
    expire_stale_pending()
    assert govern.get_governed_action(row["action_id"])["status"] == STATUS_RELEASED

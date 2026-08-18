"""Tests for the recurring-task auto-spawn pipeline (PR-2 of the\nconsolidation plan).\n\nValidates the env gate, missing-tool fallback, JSON arg handling, and the\nhappy path via a monkeypatched ``subprocess.Popen`` so we never actually\nfork a real agent CLI in CI.\n"""

from __future__ import annotations

import json
import sys
import uuid

import pytest

import brains.control.recurring as recurring_module
from brains.control.recurring import (
    SPAWN_ENV_VAR,
    _auto_spawn,
    create_recurring_task,
    fire_recurring_task,
    list_recurring_runs,
)
from brains.storage.db import SessionLocal
from brains.storage.models import RecurringTaskDefinition


def _row(**kwargs) -> RecurringTaskDefinition:
    """Build a detached ``RecurringTaskDefinition`` for direct _auto_spawn calls."""
    defaults = dict(
        name="x",
        workspace_id=1,
        title_template="T",
        spawn_tool=None,
        spawn_args=None,
        spawn_prompt=None,
    )
    defaults.update(kwargs)
    return RecurringTaskDefinition(**defaults)


def test_auto_spawn_skipped_without_spawn_tool(tmp_path) -> None:
    result = _auto_spawn(
        _row(spawn_tool=None),
        workspace_path=str(tmp_path),
        task_code="T-1",
        rendered_title="title",
        rendered_body="",
    )
    assert result["status"] == "skipped"
    assert "no spawn_tool" in result["reason"]


def test_auto_spawn_skipped_when_env_gate_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(SPAWN_ENV_VAR, raising=False)
    result = _auto_spawn(
        _row(spawn_tool="claude"),
        workspace_path=str(tmp_path),
        task_code="T-1",
        rendered_title="title",
        rendered_body="",
    )
    assert result["status"] == "skipped"
    assert SPAWN_ENV_VAR in result["reason"]


def test_auto_spawn_skipped_when_tool_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SPAWN_ENV_VAR, "1")
    monkeypatch.setattr(recurring_module.shutil, "which", lambda _: None)
    result = _auto_spawn(
        _row(spawn_tool="claude-not-installed"),
        workspace_path=str(tmp_path),
        task_code="T-1",
        rendered_title="title",
        rendered_body="",
    )
    assert result["status"] == "skipped"
    assert "not found on PATH" in result["reason"]


def test_auto_spawn_errors_on_invalid_spawn_args(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SPAWN_ENV_VAR, "1")
    monkeypatch.setattr(recurring_module.shutil, "which", lambda _: "/usr/bin/fake")
    result = _auto_spawn(
        _row(spawn_tool="fake", spawn_args="not-json"),
        workspace_path=str(tmp_path),
        task_code="T-1",
        rendered_title="title",
        rendered_body="",
    )
    assert result["status"] == "error"
    assert "invalid spawn_args JSON" in result["reason"]


def test_auto_spawn_errors_when_spawn_args_not_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SPAWN_ENV_VAR, "1")
    monkeypatch.setattr(recurring_module.shutil, "which", lambda _: "/usr/bin/fake")
    result = _auto_spawn(
        _row(spawn_tool="fake", spawn_args='{"k": 1}'),
        workspace_path=str(tmp_path),
        task_code="T-1",
        rendered_title="title",
        rendered_body="",
    )
    assert result["status"] == "error"
    assert "must be a JSON array" in result["reason"]


class _FakeProc:
    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid


def test_auto_spawn_happy_path_invokes_popen(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(pid=99)

    monkeypatch.setenv(SPAWN_ENV_VAR, "1")
    monkeypatch.setattr(recurring_module.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(recurring_module.subprocess, "Popen", fake_popen)

    result = _auto_spawn(
        _row(
            spawn_tool="claude",
            spawn_args=json.dumps(["--print", "--allowed-tools", "Edit,Write"]),
            spawn_prompt="custom prompt",
        ),
        workspace_path=str(tmp_path),
        task_code="T-42",
        rendered_title="title",
        rendered_body="body line",
    )

    assert result["status"] == "spawned"
    assert result["pid"] == 99
    assert result["tool"] == "claude"
    assert result["task_code"] == "T-42"

    # The prompt arg should include both the configured prompt and the body.
    assert captured["cmd"][0] == "/usr/bin/claude"
    assert "--print" in captured["cmd"]
    assert "--allowed-tools" in captured["cmd"]
    prompt_arg = captured["cmd"][-1]
    assert "custom prompt" in prompt_arg
    assert "body line" in prompt_arg
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_create_recurring_persists_spawn_columns(tmp_path) -> None:
    name = f"pytest-spawn-create-{uuid.uuid4().hex[:8]}"
    created = create_recurring_task(
        str(tmp_path),
        name=name,
        title_template="t",
        cron_expr="manual",
        spawn_tool="claude",
        spawn_args=["--print", "-p", "hello"],
        spawn_prompt="Run audit",
    )
    assert created["spawn_tool"] == "claude"
    assert json.loads(created["spawn_args"]) == ["--print", "-p", "hello"]
    assert created["spawn_prompt"] == "Run audit"

    with SessionLocal() as session:
        row = (
            session.query(RecurringTaskDefinition)
            .filter(RecurringTaskDefinition.name == name)
            .one()
        )
        assert row.spawn_tool == "claude"


def test_create_recurring_rejects_non_array_spawn_args(tmp_path) -> None:
    name = f"pytest-spawn-bad-{uuid.uuid4().hex[:8]}"
    with pytest.raises(ValueError):
        create_recurring_task(
            str(tmp_path),
            name=name,
            title_template="t",
            spawn_args='{"k": 1}',
        )


def test_fire_recurring_returns_auto_spawn_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SPAWN_ENV_VAR, "1")
    monkeypatch.setattr(recurring_module.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(recurring_module.subprocess, "Popen", lambda *a, **kw: _FakeProc(pid=777))
    name = f"pytest-spawn-fire-{uuid.uuid4().hex[:8]}"
    create_recurring_task(
        str(tmp_path),
        name=name,
        title_template="Audit {date}",
        cron_expr="manual",
        spawn_tool="claude",
        spawn_args=["--print"],
    )
    fired = fire_recurring_task(name)
    assert fired["auto_spawn"]["status"] == "spawned"
    assert fired["auto_spawn"]["pid"] == 777


def test_auto_spawn_is_a_governed_action(tmp_path, monkeypatch) -> None:
    """The recurring path must use the same gate as a manual command.

    Before BL-P0-04 this called ``subprocess.Popen`` directly, so a scheduled
    fire could launch an agent CLI with no classification, no approval and no
    record. The spawn now produces a governed action with a durable decision.
    """
    from brains.govern import STATUS_SUCCEEDED, list_governed_actions

    monkeypatch.setenv(SPAWN_ENV_VAR, "1")
    monkeypatch.setattr(recurring_module.shutil, "which", lambda _: sys.executable)
    name = f"pytest-governed-spawn-{uuid.uuid4().hex[:8]}"
    create_recurring_task(
        str(tmp_path),
        name=name,
        title_template="Governed {date}",
        cron_expr="manual",
        spawn_tool="python",
        spawn_args=["-c", "pass"],
    )

    fired = fire_recurring_task(name)

    assert fired["auto_spawn"]["status"] == "spawned"
    action_id = fired["auto_spawn"]["action_id"]
    row = next(
        r
        for r in list_governed_actions(limit=50, action_prefix="recurring.")
        if r["action_id"] == action_id
    )
    assert row["status"] == STATUS_SUCCEEDED
    assert row["idempotency_key"] == f"recurring.spawn:{fired['task']['code']}"
    assert row["actor"] == f"recurring:{name}"


def test_outward_spawn_is_blocked_without_approval(tmp_path, monkeypatch) -> None:
    """An outward-classified spawn files an approval instead of running."""
    monkeypatch.setenv(SPAWN_ENV_VAR, "1")
    monkeypatch.setattr(recurring_module.shutil, "which", lambda _: "/usr/bin/vercel")
    launched: list = []
    monkeypatch.setattr(
        recurring_module.subprocess,
        "Popen",
        lambda *a, **kw: launched.append(a) or _FakeProc(pid=1),
    )
    name = f"pytest-outward-spawn-{uuid.uuid4().hex[:8]}"
    create_recurring_task(
        str(tmp_path),
        name=name,
        title_template="Deploy {date}",
        cron_expr="manual",
        spawn_tool="vercel",
        spawn_args=["deploy"],
    )

    fired = fire_recurring_task(name)

    assert fired["auto_spawn"]["status"] == "pending"
    assert fired["auto_spawn"]["approval_code"]
    assert launched == [], "an unapproved outward spawn reached the process launcher"


def test_fire_fails_closed_when_the_run_cannot_be_recorded(tmp_path, monkeypatch) -> None:
    """A fire that cannot be recorded does not happen - and does not advance."""
    import brains.audit as audit_module
    from brains.audit import AuditWriteError
    from brains.storage.db import SessionLocal

    name = f"pytest-fire-audit-{uuid.uuid4().hex[:8]}"
    create_recurring_task(
        str(tmp_path),
        name=name,
        title_template="Never fired {date}",
        cron_expr="manual",
    )

    def _explode(*_args, **_kwargs):
        raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr(audit_module, "append_in_session", _explode)
    with pytest.raises(AuditWriteError):
        fire_recurring_task(name)
    monkeypatch.undo()

    with SessionLocal() as session:
        row = (
            session.query(RecurringTaskDefinition)
            .filter(RecurringTaskDefinition.name == name)
            .one()
        )
        assert row.last_fired_at is None, "the schedule advanced on an unrecorded fire"
    assert list_recurring_runs(name=name) == []


def test_fire_records_a_durable_run_and_audit_entry(tmp_path) -> None:
    from brains.audit import list_entries

    name = f"pytest-fire-run-{uuid.uuid4().hex[:8]}"
    create_recurring_task(
        str(tmp_path),
        name=name,
        title_template="Recorded {date}",
        cron_expr="manual",
    )

    fired = fire_recurring_task(name, source="schedule")

    runs = list_recurring_runs(name=name)
    assert len(runs) == 1
    assert runs[0]["source"] == "schedule"
    assert runs[0]["task_code"] == fired["task"]["code"]
    audited = [
        entry
        for entry in list_entries(action_prefix="recurring.fired", limit=50)
        if entry["payload"].get("definition") == name
    ]
    assert len(audited) == 1
    assert audited[0]["payload"]["run_id"] == fired["run_id"]

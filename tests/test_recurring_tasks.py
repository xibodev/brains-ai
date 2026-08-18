import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from brains.control.recurring import (
    RecurringFireAlreadyClaimed,
    create_recurring_task,
    fire_recurring_task,
    list_recurring_tasks,
    set_recurring_enabled,
)
from brains.control.tasks import list_tasks
from brains.mcp.server import call_tool, list_tools


def test_recurring_task_manual_fire_creates_advisory_task(tmp_path):
    name = f"pytest-recurring-{uuid.uuid4().hex}"
    today = datetime.now(UTC).date().isoformat()

    created = create_recurring_task(
        str(tmp_path),
        name=name,
        title_template="Daily audit {date}",
        body_template="Review state for {date}",
        priority="p1",
        tags="audit,manual",
        cron_expr="daily",
    )
    assert created["enabled"] is True
    assert created["last_fired_at"] is None

    fired = fire_recurring_task(name)
    task = fired["task"]
    assert task["title"] == f"Daily audit {today}"
    assert task["body"] == f"Review state for {today}"
    assert task["priority"] == "p1"
    assert task["status"] == "available"
    # No spawn_tool configured -> auto_spawn payload says skipped.
    assert fired["auto_spawn"]["status"] == "skipped"
    assert "no spawn_tool" in fired["auto_spawn"]["reason"]

    rows = list_recurring_tasks(str(tmp_path))
    assert any(row["name"] == name and row["last_fired_at"] for row in rows)
    assert any(row["code"] == task["code"] for row in list_tasks(str(tmp_path)))


def test_disabled_recurring_task_cannot_fire(tmp_path):
    name = f"pytest-disabled-recurring-{uuid.uuid4().hex}"
    create_recurring_task(
        str(tmp_path),
        name=name,
        title_template="Disabled task",
    )

    disabled = set_recurring_enabled(name, False)
    assert disabled["enabled"] is False

    with pytest.raises(ValueError, match="disabled"):
        fire_recurring_task(name)

    enabled_rows = list_recurring_tasks(str(tmp_path), enabled=True)
    assert all(row["name"] != name for row in enabled_rows)


def test_concurrent_schedule_fire_claims_one_due_occurrence(tmp_path):
    name = f"pytest-concurrent-recurring-{uuid.uuid4().hex}"
    create_recurring_task(
        str(tmp_path),
        name=name,
        title_template="Claim once",
        cron_expr="hourly",
    )

    def _fire(_index: int) -> str:
        try:
            fire_recurring_task(
                name,
                source="schedule",
                expected_last_fired_at=None,
            )
        except RecurringFireAlreadyClaimed:
            return "claimed"
        return "fired"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(_fire, range(2)))
    assert sorted(outcomes) == ["claimed", "fired"]
    assert len([row for row in list_tasks(str(tmp_path)) if row["title"] == "Claim once"]) == 1


def test_recurring_task_mcp_registry(tmp_path):
    name = f"pytest-mcp-recurring-{uuid.uuid4().hex}"

    assert "brains_create_recurring_task" in list_tools()
    assert "brains_fire_recurring_task" in list_tools()

    created = call_tool(
        "brains_create_recurring_task",
        workspace_path=str(tmp_path),
        name=name,
        title_template="MCP recurring {date}",
    )
    assert created["name"] == name

    fired = call_tool("brains_fire_recurring_task", name=name)
    assert fired["task"]["title"].startswith("MCP recurring ")

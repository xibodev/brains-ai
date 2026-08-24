"""Loudness of the experimental auto-fire notice at Autopilot surfaces."""

from __future__ import annotations

import pytest

from brains.experimental import EXPERIMENTAL_ENV
from brains.mcp import tools as mcp_tools


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    """A throwaway workspace path with the experimental gate off.

    The conftest already redirects the DB to a process-local store, and each
    test here uses a distinct definition name, so no explicit teardown is
    needed.
    """
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    return tmp_path


def test_create_with_schedule_warns_auto_fire_is_disabled(isolated_workspace):
    result = mcp_tools.create_recurring_task_tool(
        str(isolated_workspace),
        name="exp-gate-hourly",
        title_template="gate probe {date}",
        cron_expr="hourly",
    )
    assert result["experimental_auto_fire_disabled"] is True
    assert EXPERIMENTAL_ENV in result["notice"]


def test_create_manual_definition_stays_silent(isolated_workspace):
    result = mcp_tools.create_recurring_task_tool(
        str(isolated_workspace),
        name="exp-gate-manual",
        title_template="manual probe {date}",
        cron_expr="manual",
    )
    assert "notice" not in result
    assert "experimental_auto_fire_disabled" not in result


def test_notice_disappears_when_opted_in(isolated_workspace, monkeypatch):
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    result = mcp_tools.create_recurring_task_tool(
        str(isolated_workspace),
        name="exp-gate-optin",
        title_template="opt-in probe {date}",
        cron_expr="hourly",
    )
    assert "notice" not in result

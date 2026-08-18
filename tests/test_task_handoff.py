"""Tests for the agent-to-agent task handoff primitive.

``handoff_task`` is the explicit roadmap item from Phase 6 (README) /
Phase 6 (roadmap): one agent finishes chunk A, hands the diff plus the
chunk-B brief to the next agent in a single atomic call. The function
combines :func:`complete_task` and :func:`create_task` and auto-prepends
the predecessor code to the follow-up's ``depends_on``.
"""

from __future__ import annotations

import pytest

from brains.control.sessions import start_session
from brains.control.tasks import (
    TASK_PRIORITIES,
    claim_task,
    create_task,
    handoff_task,
    list_tasks,
)


def test_handoff_marks_predecessor_done_and_creates_follow_up(tmp_path):
    workspace = str(tmp_path)
    handoffer = start_session(workspace, tool="pytest")
    predecessor = create_task(
        workspace,
        title="Chunk A",
        priority="p1",
        session_id=handoffer["session_id"],
    )
    claim_task(predecessor["code"], handoffer["session_id"])

    result = handoff_task(
        predecessor["code"],
        title="Chunk B",
        body="Pick up from chunk A's diff",
        priority="p1",
        tags="follow-up",
        completion_summary="diff at refs/heads/chunk-a",
        session_id=handoffer["session_id"],
    )

    assert result["completed"]["code"] == predecessor["code"]
    assert result["completed"]["status"] == "done"
    assert result["completed"]["completion_summary"] == "diff at refs/heads/chunk-a"

    follow_up = result["next"]
    assert follow_up["title"] == "Chunk B"
    assert follow_up["body"] == "Pick up from chunk A's diff"
    assert follow_up["priority"] == "p1"
    assert follow_up["status"] == "available"
    assert follow_up["tags"] == "follow-up"
    assert follow_up["depends_on"] == predecessor["code"]
    assert follow_up["created_by_session_id"] == handoffer["session_id"]
    assert follow_up["claimed_by_session_id"] is None


def test_handoff_preserves_extra_dependencies_and_deduplicates(tmp_path):
    workspace = str(tmp_path)
    session = start_session(workspace, tool="pytest")
    predecessor = create_task(workspace, title="A", session_id=session["session_id"])
    other = create_task(workspace, title="Other dep", session_id=session["session_id"])

    result = handoff_task(
        predecessor["code"],
        title="B",
        session_id=session["session_id"],
        extra_depends_on=f"{other['code']},{predecessor['code']}",  # duplicate predecessor
    )
    expected = f"{predecessor['code']},{other['code']}"
    assert result["next"]["depends_on"] == expected


def test_handoff_blocks_follow_up_until_predecessor_done(tmp_path):
    """The auto-prepended dependency must actually gate ``claim_task``."""
    workspace = str(tmp_path)
    handoffer = start_session(workspace, tool="pytest")
    receiver = start_session(workspace, tool="pytest")
    predecessor = create_task(workspace, title="A", session_id=handoffer["session_id"])

    # Fresh predecessor with no claim -> handoff still allowed.
    result = handoff_task(
        predecessor["code"],
        title="B",
        session_id=handoffer["session_id"],
        completion_summary="A is done",
    )
    follow_up_code = result["next"]["code"]

    # Predecessor is recorded done, so the gate clears and the receiver
    # can claim the follow-up immediately.
    claimed = claim_task(follow_up_code, receiver["session_id"])
    assert claimed["status"] == "in_progress"
    assert claimed["claimed_by_session_id"] == receiver["session_id"]


def test_handoff_rejects_unknown_predecessor(tmp_path):
    workspace = str(tmp_path)
    session = start_session(workspace, tool="pytest")
    with pytest.raises(ValueError, match="unknown task"):
        handoff_task(
            "TASK-9999",
            title="B",
            session_id=session["session_id"],
        )


def test_handoff_rejects_predecessor_already_done(tmp_path):
    workspace = str(tmp_path)
    session = start_session(workspace, tool="pytest")
    predecessor = create_task(workspace, title="A", session_id=session["session_id"])
    handoff_task(
        predecessor["code"],
        title="B",
        session_id=session["session_id"],
    )
    with pytest.raises(ValueError, match="already done"):
        handoff_task(
            predecessor["code"],
            title="C",
            session_id=session["session_id"],
        )


def test_handoff_rejects_predecessor_claimed_by_other_session(tmp_path):
    workspace = str(tmp_path)
    owner = start_session(workspace, tool="pytest")
    intruder = start_session(workspace, tool="pytest")
    predecessor = create_task(workspace, title="A", session_id=owner["session_id"])
    claim_task(predecessor["code"], owner["session_id"])

    with pytest.raises(ValueError, match="claimed by"):
        handoff_task(
            predecessor["code"],
            title="B",
            session_id=intruder["session_id"],
        )


def test_handoff_rejects_invalid_priority(tmp_path):
    workspace = str(tmp_path)
    session = start_session(workspace, tool="pytest")
    predecessor = create_task(workspace, title="A", session_id=session["session_id"])
    with pytest.raises(ValueError, match="priority must be one of"):
        handoff_task(
            predecessor["code"],
            title="B",
            session_id=session["session_id"],
            priority="urgent",
        )
    # Sanity: the predecessor must NOT have been mutated by the rejected call.
    assert "urgent" not in TASK_PRIORITIES
    surviving = next(row for row in list_tasks(workspace) if row["code"] == predecessor["code"])
    assert surviving["status"] == "available"


def test_handoff_follow_up_inherits_predecessor_workspace(tmp_path):
    """The new task lives in the predecessor's workspace, never the caller's CWD."""
    workspace_a = str(tmp_path / "ws-a")
    workspace_b = str(tmp_path / "ws-b")
    session = start_session(workspace_a, tool="pytest")
    # Initialize the second workspace so the visibility join works.
    start_session(workspace_b, tool="pytest")
    predecessor = create_task(workspace_a, title="A", session_id=session["session_id"])

    result = handoff_task(
        predecessor["code"],
        title="B",
        session_id=session["session_id"],
    )

    a_codes = {row["code"] for row in list_tasks(workspace_a)}
    b_codes = {row["code"] for row in list_tasks(workspace_b)}
    assert result["next"]["code"] in a_codes
    assert result["next"]["code"] not in b_codes


def test_handoff_registered_as_mcp_tool() -> None:
    from brains.mcp import tools as mcp_tools
    from brains.mcp.server import TOOL_REGISTRY

    assert "handoff_task" in TOOL_REGISTRY
    assert TOOL_REGISTRY["handoff_task"] is mcp_tools.handoff_task_tool

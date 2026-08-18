"""Schema-roundtrip tests for the MCP and control-plane Pydantic models.

These files were 0% covered because nothing in the existing suite ever
imports the schema modules directly. Field renames, type changes, or
deletions would slip past every test until an MCP client crashed.

The check is intentionally coarse: instantiate each model from a minimal
valid dict, round-trip through ``model_dump`` / ``model_validate``, and
assert the validator rejects the wrong type. The goal is refactor-break
detection, not exhaustive validation; the model bodies are thin enough
that field-by-field assertions would just duplicate the class
definitions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from brains.control import schemas as control_schemas
from brains.mcp import schemas as mcp_schemas

# --- MCP input schemas ---------------------------------------------------

# (cls, minimal valid kwargs, a tweak that MUST fail validation)
_MCP_CASES: list[tuple[type[BaseModel], dict[str, Any], dict[str, Any]]] = [
    (mcp_schemas.PlanRequestInput, {"prompt": "x"}, {"prompt": 123}),
    (mcp_schemas.SearchRepoInput, {}, {"limit": "not-an-int"}),
    (mcp_schemas.StateInput, {}, {"limit": "nope"}),
    (mcp_schemas.SessionStartInput, {}, {"tool": 123}),
    (mcp_schemas.SessionEndInput, {"session_id": "s1"}, {"session_id": 7}),
    (
        mcp_schemas.DecisionRequestInput,
        {"title": "t"},
        {"title": None},
    ),
    (
        mcp_schemas.DecisionResolveInput,
        {"code": "D-1", "chosen": "yes"},
        {"chosen": None},
    ),
    (mcp_schemas.HandoffInput, {"title": "h"}, {"title": None}),
    (
        mcp_schemas.TaskCreateInput,
        {"title": "do thing"},
        {"title": None},
    ),
    (
        mcp_schemas.TaskActionInput,
        {"task_code": "T-1", "session_id": "s1"},
        {"task_code": None},
    ),
    (
        mcp_schemas.WorkspaceClaimInput,
        {"session_id": "s1"},
        {"duration_minutes": "thirty"},
    ),
    (mcp_schemas.MessageSendInput, {"subject": "hi"}, {"subject": None}),
    (
        mcp_schemas.MessageReadInput,
        {"session_id": "s1"},
        {"session_id": None},
    ),
    (
        mcp_schemas.SnapshotInput,
        {"kind": "k", "data": {"a": 1}},
        {"kind": None},
    ),
    (
        mcp_schemas.PatternProposeInput,
        {"name": "p", "category": "c", "description": "d"},
        {"name": None},
    ),
    (
        mcp_schemas.PatternApproveInput,
        {"name": "p"},
        {"approved": "definitely"},
    ),
    (mcp_schemas.PatternListInput, {}, {"limit": "many"}),
    (
        mcp_schemas.ToolRegisterInput,
        {"name": "n", "display_name": "N", "cli_command": "echo"},
        {"name": None},
    ),
    (mcp_schemas.ToolVerifyInput, {"name": "n"}, {"name": None}),
    (
        mcp_schemas.RecurringCreateInput,
        {"name": "r", "title_template": "tmpl"},
        {"name": None},
    ),
    (mcp_schemas.RecurringListInput, {}, {"limit": "lots"}),
    (mcp_schemas.RecurringEnableInput, {"name": "r"}, {"enabled": "yes please"}),
    (mcp_schemas.RecurringFireInput, {"name": "r"}, {"name": None}),
]


@pytest.mark.parametrize(
    "cls,minimal,_bad",
    _MCP_CASES,
    ids=[c[0].__name__ for c in _MCP_CASES],
)
def test_mcp_input_minimal_roundtrip(
    cls: type[BaseModel], minimal: dict[str, Any], _bad: dict[str, Any]
) -> None:
    """Every MCP input model must accept its declared minimal payload and round-trip cleanly."""
    instance = cls(**minimal)
    dumped = instance.model_dump()
    restored = cls.model_validate(dumped)
    assert restored.model_dump() == dumped


@pytest.mark.parametrize(
    "cls,minimal,bad",
    _MCP_CASES,
    ids=[c[0].__name__ for c in _MCP_CASES],
)
def test_mcp_input_rejects_bad_field_type(
    cls: type[BaseModel], minimal: dict[str, Any], bad: dict[str, Any]
) -> None:
    """A type-incorrect override must be rejected, proving validation is wired."""
    payload = {**minimal, **bad}
    with pytest.raises(ValidationError):
        cls(**payload)


def test_mcp_search_repo_supports_both_query_aliases() -> None:
    """Both ``query`` and ``q`` are valid, distinct fields — neither is required."""
    assert mcp_schemas.SearchRepoInput(query="foo").query == "foo"
    assert mcp_schemas.SearchRepoInput(q="bar").q == "bar"
    assert mcp_schemas.SearchRepoInput().limit == 10


def test_mcp_recurring_list_enabled_is_tristate() -> None:
    """``enabled`` must accept None/True/False to support 'list all' vs 'list enabled only'."""
    assert mcp_schemas.RecurringListInput(enabled=None).enabled is None
    assert mcp_schemas.RecurringListInput(enabled=True).enabled is True
    assert mcp_schemas.RecurringListInput(enabled=False).enabled is False


# --- Control-plane info schemas ------------------------------------------


def test_workspace_info_minimal_and_roundtrip() -> None:
    info = control_schemas.WorkspaceInfo(id=1, slug="ws-1", path="/tmp/ws", status="active")
    assert info.last_touched_at is None
    assert control_schemas.WorkspaceInfo.model_validate(info.model_dump()) == info


def test_workspace_info_rejects_wrong_id_type() -> None:
    with pytest.raises(ValidationError):
        control_schemas.WorkspaceInfo(
            id="not-an-int",  # type: ignore[arg-type]
            slug="ws-1",
            path="/tmp/ws",
            status="active",
        )


def test_session_info_keeps_started_at_as_datetime() -> None:
    started = datetime(2026, 5, 30, 12, 0, 0)
    info = control_schemas.SessionInfo(
        id="s-1",
        workspace_slug="ws-1",
        tool="codex",
        started_at=started,
    )
    assert info.started_at == started
    assert info.ended_at is None
    assert info.active_handoff is None


def test_decision_request_info_minimal() -> None:
    created = datetime(2026, 5, 30, 12, 0, 0)
    info = control_schemas.DecisionRequestInfo(
        code="D-1",
        workspace_slug="ws-1",
        title="t",
        status="open",
        created_at=created,
    )
    assert info.resolved_at is None
    assert info.proposed_answer is None


def test_handoff_info_minimal() -> None:
    info = control_schemas.HandoffInfo(
        id=1,
        workspace_slug="ws-1",
        title="hand-off",
        status="open",
        set_at=datetime(2026, 5, 30, 12, 0, 0),
    )
    assert info.body is None
    assert control_schemas.HandoffInfo.model_validate(info.model_dump()) == info

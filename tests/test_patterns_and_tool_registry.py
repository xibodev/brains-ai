import uuid

import pytest

from brains.control.patterns import (
    approve_pattern,
    list_patterns,
    propose_pattern,
    use_pattern,
)
from brains.control.sessions import start_session
from brains.control.tool_registry import (
    list_registered_tools,
    register_tool,
    verify_tool,
)
from brains.mcp.server import call_tool, list_tools


def test_pattern_proposal_approval_and_use(tmp_path):
    session = start_session(str(tmp_path), tool="pytest")
    name = f"pytest-pattern-{uuid.uuid4().hex}"

    proposed = propose_pattern(
        name=name,
        category="testing",
        description="Use deterministic tests for tool availability.",
        session_id=session["session_id"],
    )
    assert proposed["status"] == "proposed"
    assert any(row["name"] == name for row in list_patterns(status="proposed"))

    with pytest.raises(ValueError, match="approved pattern"):
        use_pattern(name)

    approved = approve_pattern(name)
    assert approved["status"] == "approved"
    assert any(row["name"] == name for row in list_patterns(category="testing"))

    used = use_pattern(name)
    assert used["usage_count"] == 1

    with pytest.raises(ValueError, match="already exists"):
        propose_pattern(
            name=name,
            category="testing",
            description="duplicate",
        )


def test_tool_registry_register_list_and_verify(monkeypatch):
    name = f"pytest-tool-{uuid.uuid4().hex}"
    availability = {"known-tool": "C:/fake/known-tool.exe"}

    monkeypatch.setattr(
        "brains.control.tool_registry.shutil.which",
        lambda command: availability.get(command),
    )

    registered = register_tool(
        name=name,
        display_name="Known Tool",
        cli_command="known-tool --headless",
        capabilities="chat,terminal",
    )
    assert registered["is_available"] is True
    assert registered["on_path_now"] is True

    rows = list_registered_tools(verify_now=True)
    assert any(row["name"] == name and row["on_path_now"] is True for row in rows)

    availability.clear()
    verified = verify_tool(name)
    assert verified["is_available"] is False
    assert verified["on_path_now"] is False


def test_list_verify_now_persists_local_readiness(monkeypatch):
    name = f"pytest-refresh-tool-{uuid.uuid4().hex}"
    availability = {"refresh-tool": "C:/fake/refresh-tool.exe"}
    monkeypatch.setattr(
        "brains.control.tool_registry.shutil.which",
        lambda command: availability.get(command),
    )
    register_tool(name, "Refresh Tool", "refresh-tool", verify=False)

    refreshed = next(row for row in list_registered_tools(verify_now=True) if row["name"] == name)
    assert refreshed["is_available"] is True
    assert refreshed["last_verified_at"] is not None
    persisted = next(row for row in list_registered_tools() if row["name"] == name)
    assert persisted["is_available"] is True


def test_tool_registry_handles_quoted_commands_and_unverified_state(monkeypatch):
    quoted_name = f"pytest-quoted-tool-{uuid.uuid4().hex}"
    unchecked_name = f"pytest-unchecked-tool-{uuid.uuid4().hex}"
    quoted_path = r"C:\Program Files\Tool\tool.exe"

    monkeypatch.setattr(
        "brains.control.tool_registry.shutil.which",
        lambda command: quoted_path if command == quoted_path else None,
    )

    registered = register_tool(
        name=quoted_name,
        display_name="Quoted Tool",
        cli_command=f'"{quoted_path}" --version',
    )
    assert registered["is_available"] is True
    assert registered["on_path_now"] is True

    unchecked = register_tool(
        name=unchecked_name,
        display_name="Unchecked Tool",
        cli_command="missing-tool",
        verify=False,
    )
    assert unchecked["is_available"] is False
    assert "on_path_now" not in unchecked


def test_pattern_and_tool_mcp_registry(monkeypatch):
    pattern_name = f"pytest-mcp-pattern-{uuid.uuid4().hex}"
    tool_name = f"pytest-mcp-tool-{uuid.uuid4().hex}"

    assert "brains_propose_pattern" in list_tools()
    assert "brains_register_tool" in list_tools()

    proposed = call_tool(
        "brains_propose_pattern",
        name=pattern_name,
        category="workflow",
        description="Route through the MCP callable registry.",
    )
    assert proposed["status"] == "proposed"

    monkeypatch.setattr(
        "brains.control.tool_registry.shutil.which",
        lambda command: "C:/fake/tool.exe" if command == "mcp-tool" else None,
    )
    registered = call_tool(
        "brains_register_tool",
        name=tool_name,
        display_name="MCP Tool",
        cli_command="mcp-tool",
    )
    assert registered["is_available"] is True


def test_search_repo_mcp_tool_accepts_search_style_args(tmp_path):
    (tmp_path / "README.md").write_text("retry webhook marker", encoding="utf-8")

    result = call_tool(
        "brains_search_repo",
        q="retry",
        path=str(tmp_path),
    )

    assert result["query"] == "retry"
    assert result["results"]

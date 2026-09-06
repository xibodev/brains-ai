"""Tests for the retained MCP registry and dispatch boundary."""

from __future__ import annotations

import re

import pytest

from brains.capabilities import CORE_MCP_TOOLS
from brains.mcp import server as mcp_server


def test_list_tools_returns_only_prefixed_core_names() -> None:
    names = mcp_server.list_tools()
    assert names
    assert all(name.startswith("brains_") for name in names)
    assert {name.removeprefix("brains_") for name in names} == set(CORE_MCP_TOOLS)


def test_registered_mcp_tool_names_are_anthropic_safe() -> None:
    names = [tool.name for tool in mcp_server.mcp._tool_manager.list_tools()]
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]+", name) for name in names)
    assert "brains_start_session" in names


def test_tool_registry_every_value_is_callable() -> None:
    assert set(mcp_server.TOOL_REGISTRY) == set(CORE_MCP_TOOLS)
    assert all(callable(handler) for handler in mcp_server.TOOL_REGISTRY.values())


def test_call_tool_normalizes_supported_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(mcp_server.TOOL_REGISTRY, "pytest_probe", lambda **kw: kw)
    assert mcp_server.call_tool("brains_pytest_probe", a=1) == {"a": 1}
    assert mcp_server.call_tool("pytest_probe", a=2) == {"a": 2}
    assert mcp_server.call_tool("brains.pytest_probe", a=3) == {"a": 3}
    with pytest.raises(ValueError, match="unknown Brains tool"):
        mcp_server.call_tool("brains.does_not_exist")

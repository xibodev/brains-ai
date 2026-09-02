from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from scripts.check_core_surface import inventory, violations


def test_generated_core_surface_inventory_has_no_withdrawn_activation() -> None:
    assert violations(inventory()) == []


@pytest.mark.parametrize(
    "name",
    ["search_semantic", "graph_query", "feedback_report", "session_message", "register_tool"],
)
def test_withdrawn_mcp_direct_dispatch_fails_closed(monkeypatch, name: str) -> None:
    monkeypatch.setenv("BRAINS_MCP_EXPERIMENTAL", "1")
    import brains.mcp.server as server

    server = importlib.reload(server)
    with pytest.raises(ValueError, match="unknown Brains tool"):
        server.call_tool(f"brains_{name}")


@pytest.mark.parametrize("name", ["features", "dashboard", "search-semantic", "recurring-fire"])
def test_withdrawn_cli_command_is_unknown(name: str) -> None:
    from brains.cli.app import app

    result = CliRunner().invoke(app, [name])
    assert result.exit_code != 0
    assert "No such command" in result.output


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/models"),
        ("post", "/v1/runtimes/enrol"),
        ("get", "/v1/personas/example"),
        ("get", "/v1/projects/example"),
        ("get", "/v1/issues/example"),
        ("post", "/hooks/example"),
        ("post", "/relay/reply"),
        ("get", "/v1/config/summary"),
        ("get", "/admin"),
        ("get", "/static/brains/brains.css"),
    ],
)
def test_withdrawn_http_direct_calls_fail_closed(method: str, path: str) -> None:
    from brains.main import app

    with TestClient(app) as client:
        response = client.request(method, path)
    assert response.status_code == 404

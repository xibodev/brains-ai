"""Supported CLI/MCP exact-code peer-help claim surfaces over synthetic state."""

from __future__ import annotations

import inspect
import json

import pytest
from typer.testing import CliRunner

from brains.capabilities import CORE_MCP_TOOLS
from brains.cli.app import app
from brains.control.sessions import start_session
from brains.mcp import server as mcp_server
from brains.mcp import tools


def test_cli_claim_code_lifecycle_preserves_queue_command(tmp_path) -> None:
    asker = start_session(str(tmp_path / "asker"), tool="opencode")["session_id"]
    peer = start_session(str(tmp_path / "peer"), tool="codex")["session_id"]
    runner = CliRunner()
    codes = []
    for subject in ("queued first", "exact target"):
        filed = runner.invoke(
            app,
            [
                "help-file",
                "--subject",
                subject,
                "--question",
                "Review the synthetic fixture?",
                "--from-session",
                asker,
                "--to-session",
                peer,
                "--timeout-ms",
                "60000",
            ],
        )
        assert filed.exit_code == 0, filed.output
        codes.append(json.loads(filed.stdout)["code"])

    claim_args = ["help-claim-code", codes[1], "--session", peer]
    claimed = runner.invoke(app, claim_args)
    assert claimed.exit_code == 0, claimed.output
    claim = json.loads(claimed.stdout)
    assert claim["code"] == codes[1]
    assert claim["status"] == "claimed"
    assert claim["claimed_by_session_id"] == peer
    retried = runner.invoke(app, claim_args)
    assert retried.exit_code == 0, retried.output
    retry = json.loads(retried.stdout)
    assert retry["claimed_at"] == claim["claimed_at"]
    assert retry["expires_at"] == claim["expires_at"]

    answered = runner.invoke(
        app,
        [
            "help-answer",
            codes[1],
            "--session",
            peer,
            "--answer",
            "Reviewed",
            "--evidence",
            "synthetic fixture",
        ],
    )
    assert answered.exit_code == 0, answered.output
    got = runner.invoke(app, ["help-get", codes[1], "--session", asker])
    assert got.exit_code == 0, got.output
    assert json.loads(got.stdout)["status"] == "answered"

    queued = runner.invoke(app, ["help-claim", "--session", peer, "--timeout-ms", "100"])
    assert queued.exit_code == 0, queued.output
    assert json.loads(queued.stdout)["code"] == codes[0]


def test_mcp_claim_code_lifecycle_and_no_queue_fallback(tmp_path) -> None:
    asker = start_session(str(tmp_path / "asker"), tool="opencode")["session_id"]
    peer = start_session(str(tmp_path / "peer"), tool="codex")["session_id"]
    filed = [
        mcp_server.call_tool(
            "brains_file_help_request",
            subject=subject,
            question="Review the synthetic fixture?",
            from_session_id=asker,
            to_session_id=peer,
            timeout_ms=60000,
        )
        for subject in ("queued first", "exact target")
    ]
    with pytest.raises(ValueError):
        mcp_server.call_tool("brains_claim_help_request", code="HR-missing", session_id=peer)
    claimed = mcp_server.call_tool(
        "brains_claim_help_request", code=filed[1]["code"], session_id=peer
    )
    assert isinstance(claimed, dict)
    assert claimed["code"] == filed[1]["code"]
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by_session_id"] == peer
    retry = mcp_server.call_tool(
        "brains_claim_help_request", code=filed[1]["code"], session_id=peer
    )
    assert retry["claimed_at"] == claimed["claimed_at"]
    assert retry["expires_at"] == claimed["expires_at"]
    queued = mcp_server.call_tool(
        "brains_get_help_request", code=filed[0]["code"], session_id=asker
    )
    assert queued["status"] == "open"
    mcp_server.call_tool(
        "brains_answer_request",
        code=filed[1]["code"],
        answer="Reviewed",
        evidence="synthetic fixture",
        session_id=peer,
    )
    answered = mcp_server.call_tool(
        "brains_get_help_request", code=filed[1]["code"], session_id=asker
    )
    assert answered["status"] == "answered"
    assert answered["answer"] == "Reviewed"
    assert answered["evidence"] == "synthetic fixture"


def test_claim_code_mcp_advertisement_signature_and_registration(monkeypatch) -> None:
    name = "claim_help_request"
    assert len(CORE_MCP_TOOLS) == 81
    assert name in CORE_MCP_TOOLS
    assert name in mcp_server.LEAN_TOOLS
    assert mcp_server.TOOL_REGISTRY[name] is tools.claim_help_request_tool
    for selection in ("full", "lean", name):
        monkeypatch.setenv("BRAINS_MCP_TOOLS", selection)
        assert name in mcp_server._resolve_active_tools()
    signature = inspect.signature(tools.claim_help_request_tool)
    assert list(signature.parameters) == ["code", "session_id"]
    assert signature.parameters["session_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(p.default is inspect.Parameter.empty for p in signature.parameters.values())
    assert inspect.get_annotations(tools.claim_help_request_tool, eval_str=True) == {
        "code": str,
        "session_id": str,
        "return": dict,
    }
    registered = {tool.name: tool for tool in mcp_server.mcp._tool_manager.list_tools()}
    tool = registered["brains_claim_help_request"]
    assert "brains_claim_help_request" in mcp_server.list_tools()
    assert set(tool.parameters["properties"]) == {"code", "session_id"}
    assert set(tool.parameters["required"]) == {"code", "session_id"}
    assert all(p["type"] == "string" for p in tool.parameters["properties"].values())
    assert "without blocking" in tool.description
    assert "same-owner retry does not renew deadlines" in tool.description
    assert "No queue fallback or process launch" in tool.description


def test_claim_code_wrapper_returns_core_dict_unchanged(monkeypatch) -> None:
    result = {"code": "HR-synthetic", "status": "claimed"}

    def claim(code: str, *, session_id: str) -> dict:
        assert code == "HR-synthetic"
        assert session_id == "ses_synthetic"
        return result

    monkeypatch.setattr(tools, "claim_help_request", claim)
    assert tools.claim_help_request_tool("HR-synthetic", session_id="ses_synthetic") is result


@pytest.mark.parametrize(
    "args",
    [
        ["help-claim-code", "HR-synthetic"],
        ["help-claim-code", "--session", "ses_synthetic"],
        ["help-claim-code", "--code", "HR-synthetic", "--session", "ses_synthetic"],
    ],
)
def test_claim_code_cli_requires_positional_code_and_session(args) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output

"""Tests for the ``brains_ask_human`` MCP tool (human-in-the-loop primitive).

Covers the ticket+poll lifecycle and idempotency. Does NOT exercise the
messaging bridges (disabled in the test settings, so no outbound is sent).
"""

from __future__ import annotations

from brains.control.decisions import (
    file_decision_request,
    list_open_requests,
    resolve_decision,
)
from brains.mcp.tools import ask_human


def test_ask_human_files_then_returns_operator_answer() -> None:
    r = ask_human("Pick a flag name", options=["x", "y"], timeout_seconds=1)
    assert r["status"] == "pending"
    code = r["ticket"]
    assert code and r["short_id"]

    # operator answers from any channel
    resolve_decision(code, chosen="y", status="resolved")

    r2 = ask_human("Pick a flag name", wait_ticket=code, timeout_seconds=2)
    assert r2["status"] == "resolved"
    assert r2["answer"] == "y"


def test_ask_human_is_idempotent_on_same_open_question() -> None:
    # Pre-file the question, then ask_human with the same prompt must REUSE it
    # (agents often re-call with the prompt instead of the ticket while waiting).
    filed = file_decision_request(workspace_path="/tmp/x", title="Same question?")
    r = ask_human("Same question?", timeout_seconds=1)
    assert r["code"] == filed["code"]
    titles = [o["title"] for o in list_open_requests()]
    assert titles.count("Same question?") == 1


def test_ask_human_rejected_status_passthrough() -> None:
    r = ask_human("Deploy to prod?", timeout_seconds=1)
    resolve_decision(r["ticket"], chosen="deny", status="rejected")
    r2 = ask_human("Deploy to prod?", wait_ticket=r["ticket"], timeout_seconds=2)
    assert r2["status"] == "rejected"
    assert r2["answer"] == "deny"

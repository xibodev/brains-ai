"""Durable asynchronous peer-help lifecycle."""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from brains.cli.app import app
from brains.control.common import utc_now
from brains.control.help import (
    answer_request,
    cancel_help_request,
    file_help_request,
    get_help_request,
    release_help_request,
    wait_for_request,
    wait_help_request,
)
from brains.control.sessions import end_session, start_session
from brains.mcp import server as mcp_server
from brains.storage.db import SessionLocal
from brains.storage.models import Event, HelpRequest


def test_file_returns_immediately_and_wait_timeout_preserves_open_request(tmp_path) -> None:
    asker = start_session(str(tmp_path / "asker"), tool="opencode")
    started = time.monotonic()
    filed = file_help_request(
        "async check",
        "can anyone inspect this?",
        from_session_id=asker["session_id"],
        to_workspace=f"unclaimed-{asker['session_id']}",
        timeout_ms=5000,
    )

    assert filed["status"] == "open"
    assert time.monotonic() - started < 2
    waited = wait_help_request(
        filed["code"],
        session_id=asker["session_id"],
        timeout_ms=100,
    )
    assert waited["status"] == "open"
    assert waited["wait_timed_out"] is True
    assert get_help_request(filed["code"], session_id=asker["session_id"])["status"] == "open"


def test_async_status_persists_expiry(tmp_path) -> None:
    asker = start_session(str(tmp_path / "asker"), tool="opencode")
    filed = file_help_request(
        "expire",
        "no longer actionable",
        from_session_id=asker["session_id"],
        to_workspace="nobody",
        timeout_ms=5000,
    )
    with SessionLocal() as session:
        row = session.query(HelpRequest).filter_by(code=filed["code"]).one()
        row.expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    result = get_help_request(filed["code"], session_id=asker["session_id"])
    assert result is not None
    assert result["status"] == "expired"
    with SessionLocal() as session:
        assert session.query(HelpRequest).filter_by(code=filed["code"]).one().status == "expired"


def test_claimant_can_release_and_another_peer_can_answer(tmp_path) -> None:
    asker = start_session(str(tmp_path / "asker"), tool="opencode")
    first_peer = start_session(str(tmp_path / "answerer"), tool="claude")
    second_peer = start_session(str(tmp_path / "answerer"), tool="codex")
    filed = file_help_request(
        "review",
        "please check",
        from_session_id=asker["session_id"],
        to_workspace=first_peer["workspace"],
        timeout_ms=5000,
    )
    claimed = wait_for_request(session_id=first_peer["session_id"], timeout_ms=200)
    assert claimed is not None
    assert claimed["code"] == filed["code"]

    released = release_help_request(
        filed["code"],
        session_id=first_peer["session_id"],
        retry_timeout_ms=5000,
    )
    assert released["status"] == "open"
    reclaimed = wait_for_request(session_id=second_peer["session_id"], timeout_ms=200)
    assert reclaimed is not None
    assert reclaimed["code"] == filed["code"]
    answer_request(
        filed["code"],
        "looks good",
        "tests/test_async_peer_help.py",
        session_id=second_peer["session_id"],
    )
    result = wait_help_request(filed["code"], session_id=asker["session_id"], timeout_ms=200)
    assert result["status"] == "answered"
    assert result["answer"] == "looks good"
    with SessionLocal() as session:
        assert (
            session.query(Event)
            .filter_by(kind="help_answered", session_id=second_peer["session_id"])
            .count()
            == 1
        )


def test_only_requester_can_cancel_and_retry_is_idempotent(tmp_path) -> None:
    asker = start_session(str(tmp_path / "asker"), tool="opencode")
    stranger = start_session(str(tmp_path / "stranger"), tool="claude")
    filed = file_help_request(
        "cancel me",
        "obsolete",
        from_session_id=asker["session_id"],
        to_session_id=stranger["session_id"],
        timeout_ms=5000,
    )

    with pytest.raises(ValueError, match="another session"):
        cancel_help_request(filed["code"], session_id=stranger["session_id"])
    first = cancel_help_request(filed["code"], session_id=asker["session_id"])
    retry = cancel_help_request(filed["code"], session_id=asker["session_id"])

    assert first["status"] == "cancelled"
    assert first["duplicate"] is False
    assert retry["duplicate"] is True
    with SessionLocal() as session:
        assert (
            session.query(Event)
            .filter_by(kind="help_cancelled", session_id=asker["session_id"])
            .count()
            == 1
        )


def test_release_refuses_non_claimant(tmp_path) -> None:
    asker = start_session(str(tmp_path / "asker"), tool="opencode")
    peer = start_session(str(tmp_path / "answerer"), tool="claude")
    stranger = start_session(str(tmp_path / "stranger"), tool="codex")
    filed = file_help_request(
        "ownership",
        "who owns this?",
        from_session_id=asker["session_id"],
        to_session_id=peer["session_id"],
        timeout_ms=5000,
    )
    assert wait_for_request(session_id=peer["session_id"], timeout_ms=200) is not None

    with pytest.raises(ValueError, match="another session"):
        release_help_request(filed["code"], session_id=stranger["session_id"])


def test_claim_and_answer_refuse_ended_session_handles(tmp_path) -> None:
    asker = start_session(str(tmp_path / "asker"), tool="opencode")
    peer = start_session(str(tmp_path / "peer"), tool="claude")
    filed = file_help_request(
        "liveness",
        "still there?",
        from_session_id=asker["session_id"],
        to_session_id=peer["session_id"],
        timeout_ms=5000,
    )
    claimed = wait_for_request(session_id=peer["session_id"], timeout_ms=200)
    assert claimed is not None
    end_session(peer["session_id"])

    with pytest.raises(ValueError, match="ended"):
        answer_request(
            filed["code"],
            "late",
            "tests/test_async_peer_help.py",
            session_id=peer["session_id"],
        )
    with pytest.raises(ValueError, match="ended"):
        wait_for_request(session_id=peer["session_id"], timeout_ms=100)


def test_async_help_cli_and_mcp_surfaces_are_wired(tmp_path) -> None:
    asker = start_session(str(tmp_path / "asker"), tool="opencode")
    peer = start_session(str(tmp_path / "peer"), tool="claude")
    result = CliRunner().invoke(
        app,
        [
            "help-file",
            "--subject",
            "cli ask",
            "--question",
            "works?",
            "--from-session",
            asker["session_id"],
            "--to-session",
            peer["session_id"],
            "--timeout-ms",
            "5000",
        ],
    )
    assert result.exit_code == 0, result.output
    code = json.loads(result.stdout)["code"]
    got = CliRunner().invoke(app, ["help-get", code, "--session", asker["session_id"]])
    assert got.exit_code == 0, got.output
    assert json.loads(got.stdout)["status"] == "open"

    assert {
        "file_help_request",
        "get_help_request",
        "wait_help_request",
        "cancel_help_request",
        "release_help_request",
    } <= set(mcp_server.TOOL_REGISTRY)


def test_help_cli_and_mcp_expose_ephemeral_execution_mode(tmp_path, monkeypatch) -> None:
    from brains.control import help_execution

    workspace = tmp_path / "review"
    workspace.mkdir()
    register = start_session(str(workspace), tool="opencode")
    monkeypatch.setattr(help_execution, "schedule_help_review", lambda _code: True)
    result = CliRunner().invoke(
        app,
        [
            "help-file",
            "--subject",
            "review",
            "--question",
            "inspect",
            "--from-session",
            register["session_id"],
            "--to-workspace",
            str(workspace),
            "--required-tool",
            "copilot",
            "--execution-mode",
            "ephemeral",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["execution_mode"] == "ephemeral"

    mcp_result = mcp_server.call_tool(
        "file_help_request",
        subject="review through mcp",
        question="inspect",
        from_session_id=register["session_id"],
        to_workspace=str(workspace),
        required_tool="copilot",
        execution_mode="auto",
    )
    assert mcp_result["execution_mode"] == "auto"

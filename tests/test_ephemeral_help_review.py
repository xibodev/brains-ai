from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from brains.control import help_execution
from brains.control.help import file_help_request, get_help_request
from brains.control.sessions import register_workspace, start_session
from brains.storage.db import SessionLocal
from brains.storage.models import (
    AgentSession,
    HelpRequest,
    HelpRequestExecution,
    Runtime,
    Workspace,
)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "review.txt").write_text("keep\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "review.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        check=True,
    )
    return repo


def test_auto_exact_tool_files_durable_review_and_returns_immediately(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    asker = start_session(str(repo), tool="opencode")
    scheduled: list[str] = []
    monkeypatch.setattr(
        help_execution, "schedule_help_review", lambda code: scheduled.append(code) or True
    )

    filed = file_help_request(
        "adversarial review",
        "inspect this repository",
        from_session_id=asker["session_id"],
        to_workspace=str(repo),
        required_tool="copilot",
        execution_mode="auto",
        timeout_ms=60_000,
    )

    assert filed["status"] == "open"
    assert filed["execution_mode"] == "auto"
    assert filed["execution"]["status"] == "queued"
    assert scheduled == [filed["code"]]


def test_ephemeral_mode_rejects_session_targets_and_negative_tools(tmp_path):
    repo = _git_repo(tmp_path)
    peer = start_session(str(repo), tool="copilot")
    with pytest.raises(ValueError, match="Workspace target and exact required_tool"):
        file_help_request(
            "review",
            "inspect",
            to_session_id=peer["session_id"],
            required_tool="copilot",
            execution_mode="ephemeral",
        )
    with pytest.raises(ValueError, match="Workspace target and exact required_tool"):
        file_help_request(
            "review",
            "inspect",
            to_workspace=str(repo),
            required_tool="not:copilot",
            execution_mode="ephemeral",
        )


def test_read_only_reviewer_never_receives_or_changes_source(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    workspace = register_workspace(str(repo))
    request = {
        "code": "HR-safety",
        "subject": "review",
        "question": "find issues",
        "context": "",
        "required_tool": "copilot",
    }
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["cwd"] = str(kwargs["snapshot"])
        seen["workspace_path"] = kwargs.get("workspace_path")
        seen["target_workspace_id"] = kwargs.get("workspace_id")
        snapshot = Path(kwargs["snapshot"])
        with contextlib.suppress(OSError):
            (snapshot / "review.txt").write_text("changed snapshot\n", encoding="utf-8")
        return type(
            "Outcome",
            (),
            {
                "allowed": True,
                "returncode": 0,
                "stdout": "No findings. Residual risk: tests were not run.",
                "stderr": "",
                "reason": "",
            },
        )()

    monkeypatch.setattr(help_execution, "_run_review_process", fake_run)
    result = help_execution.run_read_only_review(
        request,
        source_path=str(repo),
        workspace_id=workspace.id,
        session_id="ses_review",
    )

    assert result.returncode == 0
    assert result.source_unchanged is True
    assert (repo / "review.txt").read_text(encoding="utf-8") == "keep\n"
    serialized = json.dumps({"argv": seen["argv"], "cwd": seen["cwd"]})
    assert str(repo) not in serialized
    assert seen["workspace_path"] is None
    assert seen["target_workspace_id"] is None


def test_source_change_discards_review_output(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    workspace = register_workspace(str(repo))

    def fake_run(_argv, **_kwargs):
        (repo / "review.txt").write_text("mutated externally\n", encoding="utf-8")
        return type(
            "Outcome",
            (),
            {"allowed": True, "returncode": 0, "stdout": "looks good", "stderr": "", "reason": ""},
        )()

    monkeypatch.setattr(help_execution, "_run_review_process", fake_run)
    result = help_execution.run_read_only_review(
        {
            "code": "HR-race",
            "subject": "review",
            "question": "inspect",
            "context": "",
            "required_tool": "copilot",
        },
        source_path=str(repo),
        workspace_id=workspace.id,
        session_id="ses_review",
    )
    assert result.source_unchanged is False
    assert result.error_code == "source_changed_during_review"
    assert "discarded" in result.answer.lower()


def test_runtime_claim_and_complete_answers_original_request(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    workspace = register_workspace(str(repo))
    asker = start_session(str(repo), tool="opencode")
    monkeypatch.setattr(help_execution, "schedule_help_review", lambda _code: True)
    filed = file_help_request(
        "remote review",
        "inspect",
        from_session_id=asker["session_id"],
        to_workspace=str(repo),
        required_tool="copilot",
        execution_mode="ephemeral",
        timeout_ms=60_000,
    )
    with SessionLocal() as session:
        runtime = Runtime(
            slug="review-runtime",
            org_id=workspace.org_id,
            machine_id="review-machine",
            tool="copilot",
            working_root=str(repo),
            status="online",
            health="healthy",
        )
        session.add(runtime)
        session.commit()
        runtime_id = runtime.id

    queued = help_execution.list_reviews_for_runtime(runtime_id)
    assert [row["code"] for row in queued] == [filed["code"]]
    claim = help_execution.claim_review_for_runtime(runtime_id, filed["code"])
    assert claim is not None
    completed = help_execution.complete_review(
        filed["code"],
        session_id=claim["session_id"],
        answer="One finding.",
        evidence="review.txt:1",
        returncode=0,
        source_unchanged=True,
        runtime_id=runtime_id,
    )
    assert completed["status"] == "answered"
    result = get_help_request(filed["code"], session_id=asker["session_id"])
    assert result is not None
    assert result["answer"] == "One finding."
    with SessionLocal() as session:
        review_session = session.get(AgentSession, claim["session_id"])
        assert review_session.ended_at is not None
        assert review_session.state == "completed"


def test_runtime_direct_claim_refuses_wrong_tool(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    workspace = register_workspace(str(repo))
    asker = start_session(str(repo), tool="opencode")
    monkeypatch.setattr(help_execution, "schedule_help_review", lambda _code: True)
    filed = file_help_request(
        "exact tool review",
        "inspect",
        from_session_id=asker["session_id"],
        to_workspace=workspace.slug,
        required_tool="copilot",
        execution_mode="ephemeral",
        timeout_ms=60_000,
    )
    with SessionLocal() as session:
        runtime = Runtime(
            slug="wrong-tool-runtime",
            org_id=workspace.org_id,
            machine_id="wrong-tool-machine",
            tool="claude",
            working_root=str(repo),
            status="online",
            health="healthy",
        )
        session.add(runtime)
        session.commit()
        runtime_id = runtime.id

    assert help_execution.claim_review_for_runtime(runtime_id, filed["code"]) is None
    assert get_help_request(filed["code"], session_id=asker["session_id"])["status"] == "open"


def test_runtime_listing_filters_eligibility_before_limit(tmp_path, monkeypatch):
    eligible_root = tmp_path / "eligible"
    eligible_root.mkdir()
    eligible_repo = _git_repo(eligible_root)
    eligible = register_workspace(str(eligible_repo))
    asker = start_session(str(eligible_repo), tool="opencode")
    monkeypatch.setattr(help_execution, "schedule_help_review", lambda _code: True)
    with SessionLocal() as session:
        runtime = Runtime(
            slug="eligible-runtime",
            org_id=eligible.org_id,
            machine_id="eligible-machine",
            tool="copilot",
            working_root=str(eligible_repo),
            status="online",
            health="healthy",
        )
        session.add(runtime)
        session.commit()
        runtime_id = runtime.id
        for index in range(12):
            foreign = Workspace(
                slug=f"foreign-review-{index}",
                path=str(tmp_path / f"foreign-{index}"),
                status="active",
                org_id=eligible.org_id,
            )
            session.add(foreign)
            session.flush()
            request = HelpRequest(
                code=f"HR-foreign-{index}",
                to_workspace=foreign.slug,
                subject="foreign",
                question="inspect",
                status="open",
                ask_depth=1,
                created_at=help_execution.utc_now() - help_execution.timedelta(minutes=2),
                expires_at=help_execution.utc_now() + help_execution.timedelta(minutes=5),
            )
            session.add(request)
            session.add(
                HelpRequestExecution(
                    request_code=request.code,
                    mode="ephemeral",
                    source_workspace_id=foreign.id,
                    required_tool="copilot",
                    status="queued",
                    launch_after=help_execution.utc_now() - help_execution.timedelta(minutes=1),
                )
            )
        session.commit()
    filed = file_help_request(
        "eligible review",
        "inspect",
        from_session_id=asker["session_id"],
        to_workspace=eligible.slug,
        required_tool="copilot",
        execution_mode="ephemeral",
        timeout_ms=60_000,
    )

    assert [
        row["code"] for row in help_execution.list_reviews_for_runtime(runtime_id, limit=1)
    ] == [filed["code"]]


def test_expired_review_cannot_be_claimed(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    workspace = register_workspace(str(repo))
    asker = start_session(str(repo), tool="opencode")
    monkeypatch.setattr(help_execution, "schedule_help_review", lambda _code: True)
    filed = file_help_request(
        "expired review",
        "inspect",
        from_session_id=asker["session_id"],
        to_workspace=workspace.slug,
        required_tool="copilot",
        execution_mode="ephemeral",
        timeout_ms=60_000,
    )
    with SessionLocal() as session:
        session.query(HelpRequest).filter_by(code=filed["code"]).update(
            {HelpRequest.expires_at: help_execution.utc_now() - help_execution.timedelta(seconds=1)}
        )
        session.commit()

    assert help_execution._claim(filed["code"]) is None
    with SessionLocal() as session:
        assert session.query(HelpRequest).filter_by(code=filed["code"]).one().status == "expired"
        assert session.get(HelpRequestExecution, filed["code"]).status == "cancelled"


def test_stale_review_lease_requeues_with_attempt_bound(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    asker = start_session(str(repo), tool="opencode")
    monkeypatch.setattr(help_execution, "schedule_help_review", lambda _code: True)
    filed = file_help_request(
        "stale review",
        "inspect",
        from_session_id=asker["session_id"],
        to_workspace=str(repo),
        required_tool="copilot",
        execution_mode="ephemeral",
        timeout_ms=60_000,
    )
    claim = help_execution._claim(filed["code"])
    assert claim is not None
    with SessionLocal() as session:
        execution = session.get(HelpRequestExecution, filed["code"])
        execution.lease_expires_at = help_execution.utc_now() - help_execution.timedelta(seconds=1)
        session.commit()
    monkeypatch.setattr(help_execution.shutil, "which", lambda _tool: None)

    assert help_execution.dispatch_due_help_reviews() == []
    with SessionLocal() as session:
        execution = session.get(HelpRequestExecution, filed["code"])
        assert execution.status == "queued"
        assert execution.review_session_id is None


def test_completion_after_lease_expiry_is_refused(tmp_path, monkeypatch):
    """An expired lease belongs to recovery: a late worker may not answer it,
    or it races the requeue that can hand the review to somebody else."""
    repo = _git_repo(tmp_path)
    asker = start_session(str(repo), tool="opencode")
    monkeypatch.setattr(help_execution, "schedule_help_review", lambda _code: True)
    filed = file_help_request(
        "expired lease review",
        "inspect",
        from_session_id=asker["session_id"],
        to_workspace=str(repo),
        required_tool="copilot",
        execution_mode="ephemeral",
        timeout_ms=60_000,
    )
    claim = help_execution._claim(filed["code"])
    assert claim is not None
    with SessionLocal() as session:
        execution = session.get(HelpRequestExecution, filed["code"])
        execution.lease_expires_at = help_execution.utc_now() - help_execution.timedelta(seconds=1)
        session.commit()

    with pytest.raises(ValueError, match="lease has expired"):
        help_execution.complete_review(
            filed["code"],
            session_id=claim["session_id"],
            answer="too late",
            evidence="review.txt:1",
            returncode=0,
            source_unchanged=True,
        )
    with SessionLocal() as session:
        assert session.query(HelpRequest).filter_by(code=filed["code"]).one().answer is None


@pytest.mark.parametrize(
    ("tool", "kept"),
    [
        ("copilot", {"data.db", "config.json", "permissions-config.json"}),
        ("codex", {".credentials.json"}),
        ("claude", {".credentials.json"}),
    ],
)
def test_tool_state_copy_excludes_history_and_worktrees(tmp_path, monkeypatch, tool, kept):
    source = tmp_path / "source"
    source.mkdir()
    for name in kept:
        (source / name).write_text("credential-state", encoding="utf-8")
    (source / "history.jsonl").write_text("history", encoding="utf-8")
    (source / "session-state").mkdir()
    (source / "session-state" / "worktree.txt").write_text("worktree", encoding="utf-8")
    variable = {
        "copilot": "COPILOT_HOME",
        "codex": "CODEX_HOME",
        "claude": "CLAUDE_CONFIG_DIR",
    }[tool]
    monkeypatch.setenv(variable, str(source))
    destination = tmp_path / "destination"

    copied = help_execution._copy_tool_state(tool, destination)

    assert {path.name for path in copied.iterdir()} == kept
    assert not (copied / "session-state").exists()


def test_ephemeral_session_mixed_timezone_duration_serializes(tmp_path):
    from datetime import UTC, datetime

    from brains.control.sessions import _agent_session_to_dict

    row = AgentSession(
        id="ses_timezone",
        workspace_id=1,
        tool="copilot",
        started_at=datetime(2026, 1, 1, 0, 0),
        ended_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        state="completed",
    )
    assert _agent_session_to_dict(row)["duration_seconds"] == 60.0


def test_local_fake_reviewer_answers_and_deletes_sandbox(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    workspace = register_workspace(str(repo))
    asker = start_session(str(repo), tool="opencode")
    fake_bin = tmp_path / ("copilot.cmd" if os.name == "nt" else "copilot")
    if os.name == "nt":
        fake_bin.write_text("@echo off\necho Finding at review.txt:1\n", encoding="utf-8")
    else:
        fake_bin.write_text("#!/bin/sh\nprintf 'Finding at review.txt:1\\n'\n", encoding="utf-8")
        fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    monkeypatch.setattr(help_execution, "_review_command", lambda *_args: ([str(fake_bin)], None))
    sandboxes: list[Path] = []
    original_process = help_execution._run_review_process

    def observe_sandbox(argv, **kwargs):
        sandboxes.append(Path(kwargs["snapshot"]).parent)
        return original_process(argv, **kwargs)

    monkeypatch.setattr(help_execution, "_run_review_process", observe_sandbox)
    monkeypatch.setattr(help_execution, "schedule_help_review", lambda _code: True)
    filed = file_help_request(
        "local fake review",
        "inspect",
        from_session_id=asker["session_id"],
        to_workspace=workspace.slug,
        required_tool="copilot",
        execution_mode="ephemeral",
        timeout_ms=60_000,
    )

    completed = help_execution.run_local_review(filed["code"])

    assert completed is not None
    assert completed["status"] == "answered"
    assert get_help_request(filed["code"], session_id=asker["session_id"])["answer"] == (
        "Finding at review.txt:1"
    )
    assert (repo / "review.txt").read_text(encoding="utf-8") == "keep\n"
    assert sandboxes and all(not path.exists() for path in sandboxes)

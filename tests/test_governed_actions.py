"""The governed-action contract: approval, single use, idempotency, fail-closed.

Every test here works against fakes. Nothing in this file performs a real
outward action - the "effect" is a recorder, a local interpreter no-op, or a
refusal - because a suite that proved the gate by pushing to a remote would be
the exact failure it is meant to prevent.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import brains.govern as govern
from brains.audit import AuditWriteError, list_entries, verify_chain
from brains.control.decisions import get_decision, resolve_decision
from brains.control.sessions import register_workspace
from brains.govern import (
    DECISION_ALLOW,
    DECISION_APPROVED,
    DECISION_EXPIRED,
    DECISION_SCOPE_MISMATCH,
    DECISION_UNSUPPORTED,
    STATUS_AUTHORIZED,
    STATUS_DENIED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUCCEEDED,
    TIER_LOCAL,
    TIER_OUTWARD,
    ActionTarget,
    DuplicateActionError,
    GovernedRequest,
    authorize,
    consume_approval,
    expire_stale_pending,
    get_governed_action,
    list_governed_actions,
    normalize_args,
    run_governed,
)


def _request(tmp_path, **overrides) -> GovernedRequest:
    workspace = tmp_path / overrides.pop("workspace_name", "ws")
    workspace.mkdir(exist_ok=True)
    registered = register_workspace(str(workspace))
    fields = {
        "actor": "tester",
        "action": "exec.command",
        "tool": "git",
        "args": ["push", "origin", "main"],
        "target": ActionTarget(
            workspace_id=registered.id, workspace_path=str(workspace), session_id="sess-1"
        ),
        "tier": TIER_OUTWARD,
        "summary": "git push origin main",
        "cwd": str(workspace),
        "idempotency_key": f"test:{uuid.uuid4().hex}",
    }
    fields.update(overrides)
    return GovernedRequest(**fields)


# ----------------------------------------------------------------------
# Normalisation and recording
# ----------------------------------------------------------------------


def test_normalize_args_redacts_secret_shaped_values():
    assert normalize_args(["--token", "hunter2", "push"]) == ["--token", "<redacted>", "push"]
    assert normalize_args(["--api-key=abc"]) == ["--api-key=<redacted>"]
    assert normalize_args({"authorization": "Bearer x", "repo": "brains"}) == [
        "authorization=<redacted>",
        "repo=brains",
    ]
    # The digest must not depend on the secret, only on the shape.
    left = govern.args_digest("exec.command", "gh", ["--token", "one"])
    right = govern.args_digest("exec.command", "gh", ["--token", "two"])
    assert left == right


def test_local_action_is_allowed_and_recorded(tmp_path):
    request = _request(tmp_path, tier=TIER_LOCAL, tool="pytest", args=["-q"], summary="pytest -q")

    decision = authorize(request, notify=False)

    assert decision.allowed is True
    assert decision.decision == DECISION_ALLOW
    row = get_governed_action(decision.action_id)
    assert row["status"] == STATUS_AUTHORIZED
    assert row["args_hash"] == request.digest()
    assert row["session_id"] == "sess-1"
    entries = [e for e in list_entries(action_prefix="governed.", limit=50)]
    actions = {e["action"] for e in entries if e["payload"].get("action_id") == decision.action_id}
    assert {"governed.requested", "governed.allowed"} <= actions


def test_audit_payload_carries_target_tier_and_args_hash_but_not_arguments(tmp_path):
    request = _request(
        tmp_path,
        tier=TIER_LOCAL,
        tool="gh",
        args=["auth", "--token", "s3cr3t"],
        summary="gh auth",
    )
    decision = authorize(request, notify=False)

    entry = next(
        e
        for e in list_entries(action_prefix="governed.", limit=50)
        if e["payload"].get("action_id") == decision.action_id
    )
    assert entry["payload"]["args_hash"] == request.digest()
    assert entry["payload"]["tier"] == TIER_LOCAL
    assert entry["payload"]["target"]["session_id"] == "sess-1"
    assert "s3cr3t" not in str(entry["payload"])


# ----------------------------------------------------------------------
# Approval: required, scoped, single-use, expiring
# ----------------------------------------------------------------------


def test_outward_action_is_pending_until_a_human_resolves_it(tmp_path):
    request = _request(tmp_path, workspace_name="ws-pending")

    decision = authorize(request, wait=False, notify=False)

    assert decision.allowed is False
    assert decision.status == STATUS_PENDING
    assert get_decision(decision.approval_code)["status"] == "open"


def test_approval_is_consumed_exactly_once(tmp_path):
    """Two governed actions cannot both spend one human decision."""
    first = _request(tmp_path, workspace_name="ws-once")
    pending = authorize(first, wait=False, notify=False)
    resolve_decision(pending.approval_code, chosen="approve", reasoning="ok")

    granted = consume_approval(first, pending.action_id, pending.approval_code)
    assert granted.allowed is True
    assert granted.decision == DECISION_APPROVED

    second = _request(tmp_path, workspace_name="ws-once")
    replay_target = authorize(second, wait=False, notify=False)
    stolen = consume_approval(second, replay_target.action_id, pending.approval_code)

    assert stolen.allowed is False
    assert stolen.decision == DECISION_SCOPE_MISMATCH
    assert get_governed_action(replay_target.action_id)["status"] == STATUS_DENIED


def test_approval_granted_for_other_arguments_is_refused(tmp_path):
    filed = _request(tmp_path, workspace_name="ws-scope")
    pending = authorize(filed, wait=False, notify=False)
    resolve_decision(pending.approval_code, chosen="approve", reasoning="ok")

    # Same tool and target, different argument vector: not what was reviewed.
    substituted = GovernedRequest(
        actor=filed.actor,
        action=filed.action,
        tool=filed.tool,
        args=["push", "--force", "origin", "main"],
        target=filed.target,
        tier=filed.tier,
        summary="git push --force origin main",
        cwd=filed.cwd,
        idempotency_key=filed.idempotency_key,
    )
    outcome = consume_approval(substituted, pending.action_id, pending.approval_code)

    assert outcome.allowed is False
    assert outcome.decision == DECISION_SCOPE_MISMATCH
    assert get_decision(pending.approval_code)["status"] == "resolved", (
        "a refused consumption must not spend the approval"
    )


def test_expired_approval_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(govern.APPROVAL_TTL_ENV, "1")
    request = _request(tmp_path, workspace_name="ws-expiry")
    pending = authorize(request, wait=False, notify=False)
    resolve_decision(pending.approval_code, chosen="approve", reasoning="ok")
    time.sleep(1.2)

    outcome = consume_approval(request, pending.action_id, pending.approval_code)

    assert outcome.allowed is False
    assert outcome.decision == DECISION_EXPIRED
    assert get_governed_action(pending.action_id)["status"] == STATUS_EXPIRED


def test_unscoped_approval_cannot_authorise_anything(tmp_path):
    """An ASK filed outside the governed contract is not a licence."""
    from brains.control.decisions import file_decision_request

    workspace = tmp_path / "ws-unscoped"
    workspace.mkdir()
    filed = file_decision_request(str(workspace), title="please approve something")
    resolve_decision(filed["code"], chosen="approve", reasoning="sure")

    request = _request(tmp_path, workspace_name="ws-unscoped")
    reserved = authorize(request, wait=False, notify=False)
    outcome = consume_approval(request, reserved.action_id, filed["code"])

    assert outcome.allowed is False
    assert outcome.decision == DECISION_SCOPE_MISMATCH


def test_pending_action_past_its_window_is_settled_as_expired(tmp_path, monkeypatch):
    monkeypatch.setenv(govern.APPROVAL_TTL_ENV, "1")
    request = _request(tmp_path, workspace_name="ws-stale")
    pending = authorize(request, wait=False, notify=False)
    time.sleep(1.2)

    assert expire_stale_pending() >= 1
    assert get_governed_action(pending.action_id)["status"] == STATUS_EXPIRED


def test_denied_approval_blocks_the_effect(tmp_path):
    from brains.control.decisions import list_open_decisions

    workspace = tmp_path / "ws-denied"
    workspace.mkdir()
    register_workspace(str(workspace))
    request = _request(tmp_path, workspace_name="ws-denied")
    calls: list[int] = []

    def _deny_when_filed() -> None:
        deadline = time.time() + 15
        while time.time() < deadline:
            pending = list_open_decisions(workspace_path=str(workspace))
            if pending:
                resolve_decision(
                    pending[0]["code"], chosen="deny", reasoning="not now", status="rejected"
                )
                return
            time.sleep(0.05)

    resolver = threading.Thread(target=_deny_when_filed, daemon=True)
    resolver.start()
    outcome = run_governed(
        request,
        lambda: calls.append(1),
        wait=True,
        poll_seconds=0.05,
        timeout_seconds=20,
        notify=False,
    )
    resolver.join(timeout=5)

    assert outcome.allowed is False
    assert outcome.status == STATUS_DENIED
    assert calls == [], "a denied action reached its effect"


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


def test_retry_with_the_same_key_does_not_execute_twice(tmp_path):
    key = f"test:{uuid.uuid4().hex}"
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["x"], idempotency_key=key)
    calls: list[int] = []

    first = run_governed(request, lambda: calls.append(1), notify=False)
    second = run_governed(request, lambda: calls.append(1), notify=False)

    assert first.allowed is True
    assert first.status == STATUS_SUCCEEDED
    assert second.replayed is True
    assert second.allowed is False
    assert calls == [1], "the retry executed the effect a second time"
    assert len(list_governed_actions(limit=200)) >= 1
    replays = [
        e
        for e in list_entries(action_prefix="governed.replayed", limit=50)
        if e["payload"].get("idempotency_key") == key
    ]
    assert len(replays) == 1, "a replay must be observed once, not decided again"


def test_retry_while_in_flight_is_refused(tmp_path):
    key = f"test:{uuid.uuid4().hex}"
    request = _request(tmp_path, workspace_name="ws-inflight", idempotency_key=key)
    authorize(request, wait=False, notify=False)

    with pytest.raises(DuplicateActionError):
        authorize(request, wait=False, notify=False)


def test_abandoned_attempt_is_settled_and_the_key_becomes_retryable(tmp_path, monkeypatch):
    """A crashed attempt must not block its idempotency key forever."""
    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 0)
    key = f"test:{uuid.uuid4().hex}"
    request = _request(tmp_path, workspace_name="ws-abandoned", idempotency_key=key)
    first = authorize(request, wait=False, notify=False)

    second = authorize(request, wait=False, notify=False)

    assert second.action_id == first.action_id
    assert second.status == STATUS_PENDING
    row = get_governed_action(first.action_id)
    assert row["attempt"] == 2
    failures = [
        entry
        for entry in list_entries(action_prefix="governed.failed", limit=50)
        if entry["payload"].get("action_id") == first.action_id
    ]
    assert failures, "the abandoned attempt was reused without being recorded as failed"


def test_attempt_abandoned_mid_effect_is_never_silently_retried(tmp_path, monkeypatch):
    """Whether the effect happened is exactly what is unknown, so refuse."""
    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 0)
    key = f"test:{uuid.uuid4().hex}"
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["x"], idempotency_key=key)
    decision = authorize(request, notify=False)
    govern.mark_executing(request, decision.action_id)

    with pytest.raises(DuplicateActionError):
        authorize(request, notify=False)

    row = get_governed_action(decision.action_id)
    assert row["status"] == "failed"
    assert "abandoned while executing" in (row["error"] or "")


def test_outward_action_without_a_workspace_is_denied_not_crashed(tmp_path):
    """An approval needs a home; an unattributable action is refused, not raised."""
    request = GovernedRequest(
        actor="tester",
        action="exec.command",
        tool="git",
        args=["push"],
        target=ActionTarget(),
        tier=TIER_OUTWARD,
        summary="git push",
        idempotency_key=f"test:{uuid.uuid4().hex}",
    )

    decision = authorize(request, wait=False, notify=False)

    assert decision.allowed is False
    assert decision.status == STATUS_DENIED
    assert "Workspace" in decision.reason


def test_stale_sweep_settles_abandoned_non_terminal_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 0)
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["sweep"])
    decision = authorize(request, notify=False)

    assert govern.expire_stale_pending() >= 1
    assert get_governed_action(decision.action_id)["status"] in {"failed", STATUS_EXPIRED}


# ----------------------------------------------------------------------
# The attempt lease is per attempt, and something actually runs it
# ----------------------------------------------------------------------


def _backdate_attempt(action_id: str, *, seconds: int) -> None:
    """Age the current attempt so its lease has expired, without sleeping."""
    from sqlalchemy import select as sa_select

    from brains.storage.db import SessionLocal
    from brains.storage.models import GovernedAction

    with SessionLocal() as session:
        row = session.execute(
            sa_select(GovernedAction).where(GovernedAction.action_id == action_id)
        ).scalar_one()
        row.attempt_started_at = datetime.now(UTC) - timedelta(seconds=seconds)
        row.created_at = row.attempt_started_at
        session.commit()


def test_a_reset_attempt_starts_a_fresh_lease(tmp_path, monkeypatch):
    """A retry that legitimately reset an attempt must not be born expired.

    The lease used to be measured from ``created_at``, which does not move
    when an attempt is reset: the new attempt was already older than its own
    lease, so the *next* retry could reset it again immediately, and two
    concurrent retries could each open their own attempt under one key.
    """
    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 0)
    key = f"test:{uuid.uuid4().hex}"
    request = _request(tmp_path, workspace_name="ws-lease", idempotency_key=key)
    first = authorize(request, wait=False, notify=False)
    authorize(request, wait=False, notify=False)  # settles attempt 1, opens attempt 2

    row = get_governed_action(first.action_id)
    assert row["attempt"] == 2
    started = row["attempt_started_at"]
    assert started is not None
    assert started > row["created_at"] or row["attempt"] == 2

    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 3600)
    with pytest.raises(DuplicateActionError):
        authorize(request, wait=False, notify=False)
    assert get_governed_action(first.action_id)["attempt"] == 2


def test_concurrent_retries_after_a_reset_yield_one_new_attempt(tmp_path):
    """Many processes retrying one abandoned key must produce one attempt."""
    key = f"test:{uuid.uuid4().hex}"
    request = _request(tmp_path, workspace_name="ws-race", idempotency_key=key)
    first = authorize(request, wait=False, notify=False)
    _backdate_attempt(first.action_id, seconds=govern.attempt_lease_seconds() * 2)

    accepted: list[str] = []
    refused: list[BaseException] = []
    unexpected: list[BaseException] = []
    barrier = threading.Barrier(4)

    def _retry() -> None:
        barrier.wait(timeout=30)
        try:
            outcome = govern.reserve(request)
            accepted.append(outcome[0]["action_id"])
        except DuplicateActionError as exc:
            refused.append(exc)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            unexpected.append(exc)

    threads = [threading.Thread(target=_retry) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert unexpected == []
    assert len(accepted) == 1, "two retries opened an attempt on one idempotency key"
    assert len(refused) == 3
    row = get_governed_action(first.action_id)
    assert row["attempt"] == 2, f"expected exactly one new attempt, got {row['attempt']}"
    assert verify_chain() is None


def test_concurrent_retries_of_a_terminal_key_all_replay(tmp_path):
    """Replay stays idempotent under concurrency: no second execution."""
    key = f"test:{uuid.uuid4().hex}"
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["x"], idempotency_key=key)
    calls: list[int] = []
    run_governed(request, lambda: calls.append(1), notify=False)

    results: list[bool] = []
    errors: list[BaseException] = []

    def _replay() -> None:
        try:
            results.append(run_governed(request, lambda: calls.append(1), notify=False).replayed)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_replay) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert results == [True, True, True, True]
    assert calls == [1], "a replay re-executed the effect"


def test_sweep_leaves_a_live_attempt_alone(tmp_path, monkeypatch):
    """Maintenance must not settle an action that is still inside its lease."""
    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 3600)
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["live"])
    decision = authorize(request, notify=False)
    govern.mark_executing(request, decision.action_id)

    govern.run_maintenance()

    assert get_governed_action(decision.action_id)["status"] == "executing"


def test_executing_refreshes_the_lease_so_a_long_wait_is_not_swept(tmp_path, monkeypatch):
    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 2)
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["slow"])
    decision = authorize(request, notify=False)
    time.sleep(2.2)
    govern.mark_executing(request, decision.action_id)

    govern.run_maintenance()

    assert get_governed_action(decision.action_id)["status"] == "executing", (
        "an action that had just started executing was swept as abandoned"
    )


def test_the_recurring_scheduler_tick_runs_the_sweep(tmp_path, monkeypatch):
    """The expiry rules need a periodic owner that actually runs."""
    from brains.mcp import server as mcp_server

    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 0)
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["tick"])
    decision = authorize(request, notify=False)

    fired = mcp_server._scheduler_tick()

    assert isinstance(fired, list)
    assert get_governed_action(decision.action_id)["status"] in {"failed", STATUS_EXPIRED}


def test_governed_sweep_cli_reports_what_it_settled(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brains.cli.app import app

    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 0)
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["cli-sweep"])
    decision = authorize(request, notify=False)

    result = CliRunner().invoke(app, ["governed-sweep"])

    assert result.exit_code == 0, result.output
    assert '"swept"' in result.output
    assert get_governed_action(decision.action_id)["status"] in {"failed", STATUS_EXPIRED}


# ----------------------------------------------------------------------
# Fail-closed on audit failure
# ----------------------------------------------------------------------


def test_effect_never_runs_when_the_request_cannot_be_recorded(tmp_path, monkeypatch):
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["x"])
    calls: list[int] = []

    def _explode(*_args, **_kwargs):
        raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr(govern, "append_in_session", _explode)

    with pytest.raises(AuditWriteError):
        run_governed(request, lambda: calls.append(1), notify=False)

    assert calls == []
    assert all(
        row["idempotency_key"] != request.idempotency_key
        for row in list_governed_actions(limit=500)
    ), "a request that could not be recorded still left a governed-action row"


def test_approval_is_not_spent_when_the_decision_cannot_be_recorded(tmp_path, monkeypatch):
    request = _request(tmp_path, workspace_name="ws-audit-fail")
    pending = authorize(request, wait=False, notify=False)
    resolve_decision(pending.approval_code, chosen="approve", reasoning="ok")

    def _explode(*_args, **_kwargs):
        raise AuditWriteError("audit store unavailable")

    monkeypatch.setattr(govern, "append_in_session", _explode)
    with pytest.raises(AuditWriteError):
        consume_approval(request, pending.action_id, pending.approval_code)
    monkeypatch.undo()

    assert get_decision(pending.approval_code)["status"] == "resolved"
    assert get_governed_action(pending.action_id)["status"] == STATUS_PENDING
    assert verify_chain() is None


# ----------------------------------------------------------------------
# The in-process execution boundary
# ----------------------------------------------------------------------


def test_guard_refuses_shapes_it_cannot_classify(tmp_path):
    from brains.exec import guard

    workspace = tmp_path / "ws-guard"
    workspace.mkdir()

    as_string = guard.run("echo hi", actor="tester", workspace_path=str(workspace))
    with_shell = guard.run(
        [sys.executable, "-c", "pass"], actor="tester", workspace_path=str(workspace), shell=True
    )

    for outcome in (as_string, with_shell):
        assert outcome.allowed is False
        assert outcome.status == STATUS_DENIED
        row = get_governed_action(outcome.action_id)
        assert row["decision"] == DECISION_UNSUPPORTED


def _settlements(action_id: str) -> list[str]:
    """The outcome entries recorded for one action, in order.

    An action is settled once: a second entry would mean the same effect was
    reported twice, which is what "do not double-complete" has to be proved by.
    """
    return [
        entry["action"]
        for entry in reversed(list_entries(action_prefix="governed.", limit=500))
        if entry["payload"].get("action_id") == action_id
        and entry["action"] in {"governed.succeeded", "governed.failed"}
    ]


def test_guard_runs_and_records_a_local_command(tmp_path):
    from brains.exec import guard

    workspace = tmp_path / "ws-guard-run"
    workspace.mkdir()

    outcome = guard.run(
        [sys.executable, "-c", "print('governed')"],
        actor="tester",
        workspace_path=str(workspace),
        capture_output=True,
    )

    assert outcome.allowed is True
    assert outcome.returncode == 0
    assert "governed" in (outcome.stdout or "")
    row = get_governed_action(outcome.action_id)
    assert row["status"] == STATUS_SUCCEEDED
    assert row["result"] == "exit 0"
    assert row["error"] is None
    assert row["tier"] == TIER_LOCAL
    assert _settlements(outcome.action_id) == ["governed.succeeded"]


def test_guard_records_a_nonzero_exit_as_failed(tmp_path):
    """A command that ran and failed is recorded as failed, not as succeeded.

    ``subprocess.run(check=False)`` returns instead of raising, so "the effect
    did not raise" is not evidence the command worked. The exit status is the
    observable outcome and is what the row must carry.
    """
    from brains.exec import guard

    workspace = tmp_path / "ws-guard-fail"
    workspace.mkdir()

    outcome = guard.run(
        [sys.executable, "-c", "raise SystemExit(3)"],
        actor="tester",
        workspace_path=str(workspace),
        capture_output=True,
    )

    # check=False semantics survive: the failure is returned, never raised.
    assert outcome.returncode == 3
    assert outcome.allowed is True, "the command was authorised; it merely failed"
    assert outcome.status == STATUS_FAILED
    assert outcome.error == "exit 3"
    row = get_governed_action(outcome.action_id)
    assert row["status"] == STATUS_FAILED
    assert row["result"] == "exit 3"
    assert row["error"] == "exit 3"
    assert _settlements(outcome.action_id) == ["governed.failed"]
    assert verify_chain() is None


#: A child that hands its inherited stdout pipe to a grandchild and then blocks.
#: Killing the child does not close that pipe - the grandchild still holds the
#: write end - so a ``communicate()`` after the kill has nothing to return
#: until the grandchild exits, minutes later.
_PIPE_HOLDING_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "time.sleep(120)\n"
)


def test_guard_kills_and_returns_when_a_command_times_out(tmp_path):
    """A timeout is a timeout: the child is killed and the call comes back."""
    from brains.exec import guard

    workspace = tmp_path / "ws-guard-timeout"
    workspace.mkdir()

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        guard.run(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            actor="tester",
            workspace_path=str(workspace),
            capture_output=True,
            timeout=1,
        )
    assert time.monotonic() - started < 60, "the timeout did not return promptly"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the POSIX timeout path is what must not block on an inherited pipe",
)
def test_a_timeout_does_not_wait_for_a_grandchild_holding_the_pipe(tmp_path):
    """The kill-on-timeout path must not block on a process it cannot kill.

    ``subprocess.run`` reaps a timed-out child with ``wait()`` on POSIX
    precisely because the output read so far is already on the exception and a
    second ``communicate()`` would wait for *every* writer to close the pipe -
    including a grandchild the child spawned, which the kill never reached.
    Copying the Windows half of that path onto POSIX turns a one-second timeout
    into a two-minute hang, and a governed run that cannot time out cannot be
    stopped.
    """
    from brains.exec import guard

    workspace = tmp_path / "ws-guard-grandchild"
    workspace.mkdir()

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        guard.run(
            [sys.executable, "-c", _PIPE_HOLDING_GRANDCHILD],
            actor="tester",
            workspace_path=str(workspace),
            capture_output=True,
            timeout=1,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 30, f"the timeout waited on the grandchild's pipe ({elapsed:.1f}s)"


@pytest.mark.parametrize("windows", [True, False])
def test_the_timeout_path_mirrors_subprocess_run_on_each_platform(tmp_path, monkeypatch, windows):
    """Both halves of ``subprocess.run``'s kill-on-timeout are reproduced.

    Windows collects the output from the reader threads with a
    ``communicate()`` after the kill and puts it on the exception; POSIX
    already has it there and only reaps the child. Getting either half wrong
    silently changes what a governed timeout means - lost partial output on one
    platform, a call that never returns on the other - so both are asserted
    here whatever platform the suite runs on.
    """
    from brains.exec import guard

    workspace = tmp_path / f"ws-guard-platform-{windows}"
    workspace.mkdir()
    calls: list[str] = []

    class _FakeProcess:
        returncode = -9
        pid = 4242

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def communicate(self, _input=None, timeout=None):
            calls.append("communicate" if timeout is None else "communicate-timeout")
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd=["fake"], timeout=timeout, output="partial")
            return ("collected-after-kill", "")

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append("wait")
            return -9

        def poll(self):
            return -9

    monkeypatch.setattr(guard, "_WINDOWS", windows)
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *_a, **_k: _FakeProcess())

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        guard.run(
            [sys.executable, "-c", "pass"],
            actor="tester",
            workspace_path=str(workspace),
            capture_output=True,
            timeout=0.25,
        )

    assert calls[0] == "communicate-timeout"
    assert calls[1] == "kill", "the child must be killed before anything else"
    if windows:
        assert calls[2:] == ["communicate"]
        assert raised.value.stdout == "collected-after-kill"
    else:
        assert calls[2:] == ["wait"], "a second communicate() would block on an inherited pipe"
        assert raised.value.stdout == "partial", "the partial output already read was lost"


def test_guard_records_a_command_that_could_not_run_as_failed(tmp_path):
    """An effect that raises is settled failed once, and the error propagates."""
    from brains.exec import guard

    workspace = tmp_path / "ws-guard-error"
    workspace.mkdir()
    actor = f"tester-{uuid.uuid4().hex}"

    with pytest.raises(FileNotFoundError):
        guard.run(
            [str(workspace / "no-such-binary-here"), "--version"],
            actor=actor,
            workspace_path=str(workspace),
        )

    rows = list_governed_actions(actor=actor, limit=10)
    assert len(rows) == 1
    assert rows[0]["status"] == STATUS_FAILED
    assert "FileNotFoundError" in (rows[0]["error"] or "")
    assert _settlements(rows[0]["action_id"]) == ["governed.failed"]


def test_guard_spawn_still_records_only_the_launch(tmp_path):
    """A spawned child outlives the call, so its exit status is not claimed."""
    from brains.exec import guard

    workspace = tmp_path / "ws-guard-spawn-fail"
    workspace.mkdir()

    outcome = guard.spawn(
        [sys.executable, "-c", "raise SystemExit(4)"],
        actor="tester",
        workspace_path=str(workspace),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assert outcome.allowed is True
    if outcome.process is not None:
        assert outcome.process.wait(timeout=60) == 4
    row = get_governed_action(outcome.action_id)
    # The launch succeeded; the child's non-zero exit is not this row's claim.
    assert row["status"] == STATUS_SUCCEEDED
    assert row["error"] is None
    assert _settlements(outcome.action_id) == ["governed.succeeded"]


def test_guard_blocks_an_outward_command_without_approval(tmp_path):
    from brains.exec import guard

    workspace = tmp_path / "ws-guard-block"
    workspace.mkdir()

    outcome = guard.run(
        ["git", "push", "origin", "main"],
        actor="tester",
        workspace_path=str(workspace),
        wait_for_approval=False,
    )

    assert outcome.allowed is False
    assert outcome.returncode is None, "an unapproved outward command must not run"
    assert outcome.tier == TIER_OUTWARD


def test_guard_spawn_records_the_launch(tmp_path):
    from brains.exec import guard

    workspace = tmp_path / "ws-guard-spawn"
    workspace.mkdir()

    outcome = guard.spawn(
        [sys.executable, "-c", "pass"],
        actor="tester",
        workspace_path=str(workspace),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assert outcome.allowed is True
    assert outcome.pid
    if outcome.process is not None:
        outcome.process.wait(timeout=60)
    assert get_governed_action(outcome.action_id)["status"] == STATUS_SUCCEEDED


# ----------------------------------------------------------------------
# Approval-code minting
# ----------------------------------------------------------------------


def _ask_number(code: str) -> int:
    return int(code.split("-")[1])


def _prune_workspaces(workspace_ids: list[int]) -> dict[str, int]:
    """Run the real schema-derived Workspace cascade over ``workspace_ids``.

    This is what an operator's ``brains-ai workspaces prune --apply`` does: the
    ``approval_requests`` rows go with the Workspace that owned them, while
    ``governed_actions`` - whose Workspace reference is optional - keeps its
    row, its history, and the approval code it spent.
    """
    from brains.cli.app import _apply_workspace_cascade
    from brains.storage.db import SessionLocal

    with SessionLocal() as session:
        return _apply_workspace_cascade(session, workspace_ids)


def _live_ask_codes() -> list[str]:
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest

    with SessionLocal() as session:
        return [code for (code,) in session.query(ApprovalRequest.code).all()]


def _spent_ask_codes() -> list[str]:
    from brains.storage.db import SessionLocal
    from brains.storage.models import GovernedAction

    with SessionLocal() as session:
        return [
            code
            for (code,) in session.query(GovernedAction.approval_code).all()
            if code is not None
        ]


def _highest_ask_number() -> int:
    return max((_ask_number(code) for code in _live_ask_codes() + _spent_ask_codes()), default=0)


def test_ask_codes_are_minted_above_codes_only_a_governed_action_still_holds(tmp_path):
    """The live ASK table is not the whole record of which codes are taken.

    ``governed_actions.approval_code`` is unique and permanent; the ASK row it
    came from is not. Minting from ``approval_requests`` alone would hand out a
    code that is still bound to a governed action, and the collision would only
    be discovered when the *next* approval is spent.
    """
    from brains.control.decisions import file_decision_request
    from brains.storage.db import SessionLocal
    from brains.storage.models import GovernedAction

    workspace = tmp_path / "ws-mint-floor"
    workspace.mkdir()
    register_workspace(str(workspace))
    reserved = f"ASK-{_highest_ask_number() + 41:04d}"
    with SessionLocal() as session:
        session.add(
            GovernedAction(
                action_id=f"ga_{uuid.uuid4().hex[:16]}",
                idempotency_key=f"test:{uuid.uuid4().hex}",
                actor="tester",
                action="exec.command",
                tool="git",
                args_hash="0" * 64,
                tier=TIER_OUTWARD,
                status=STATUS_DENIED,
                approval_code=reserved,
            )
        )
        session.commit()

    filed = file_decision_request(str(workspace), title="[gate] approve outward action: git")

    assert _ask_number(filed["code"]) > _ask_number(reserved)


def test_a_workspace_prune_cannot_recycle_a_spent_ask_code(tmp_path):
    """approve -> spend -> prune -> file again: the code must not come back.

    The prune is the real cascade, so every ASK row the Workspace owned is
    gone and the live table's highest suffix is back to zero (or to whatever
    another Workspace still holds). The next ASK must still be minted above the
    code the surviving governed action holds, and spending it must not trip the
    unique index.
    """
    from brains.control.decisions import CONSUMED_STATUS, get_decision

    first = _request(tmp_path, workspace_name="ws-recycle-a")
    filed = authorize(first, wait=False, notify=False)
    spent_code = filed.approval_code
    assert filed.status == STATUS_PENDING
    resolve_decision(spent_code, chosen="approve", reasoning="ok")
    authorization = consume_approval(first, filed.action_id, spent_code)
    assert authorization.allowed is True
    assert get_decision(spent_code)["status"] == CONSUMED_STATUS

    workspace_id = first.target.workspace_id
    assert workspace_id is not None
    deleted = _prune_workspaces([workspace_id])

    assert deleted["approval_requests"] >= 1
    assert spent_code not in _live_ask_codes(), "the prune took the ASK row with it"
    assert spent_code in _spent_ask_codes(), "the governed action keeps what it spent"

    second = _request(tmp_path, workspace_name="ws-recycle-b")
    refiled = authorize(second, wait=False, notify=False)

    assert refiled.approval_code != spent_code
    assert _ask_number(refiled.approval_code) > _ask_number(spent_code)
    resolve_decision(refiled.approval_code, chosen="approve", reasoning="ok")
    assert consume_approval(second, refiled.action_id, refiled.approval_code).allowed is True
    assert verify_chain() is None


@pytest.mark.parametrize("outcome", ["approve", "deny", "timeout"])
def test_every_settled_outcome_reserves_its_code_across_a_prune(tmp_path, outcome):
    """Approve, deny and time-out all bind the code to a permanent row.

    A denial and a timeout record which approval they refused, so those codes
    are as spent as an approved one. If minting ignored them, the next ASK
    after a prune would re-use a code that already names a recorded refusal -
    and an operator reading the chain would see one code with two meanings.
    """
    from brains.control.decisions import list_open_decisions

    workspace_name = f"ws-outcome-{outcome}"
    request = _request(tmp_path, workspace_name=workspace_name)
    workspace_path = str(tmp_path / workspace_name)

    if outcome == "timeout":
        result = authorize(request, poll_seconds=0.01, timeout_seconds=0.05, notify=False)
        assert result.status == STATUS_EXPIRED
        assert result.decision == DECISION_EXPIRED
    else:
        holder: dict[str, object] = {}

        def _decide() -> None:
            holder["result"] = authorize(
                request, poll_seconds=0.01, timeout_seconds=20, notify=False
            )

        worker = threading.Thread(target=_decide, daemon=True)
        worker.start()
        deadline = time.time() + 10
        code = None
        while code is None and time.time() < deadline:
            open_asks = list_open_decisions(workspace_path=workspace_path)
            code = open_asks[0]["code"] if open_asks else None
            if code is None:
                time.sleep(0.01)
        assert code, "the outward action must file an ASK before it waits"
        resolve_decision(
            code,
            chosen=outcome,
            reasoning="ok" if outcome == "approve" else "no",
            status="resolved" if outcome == "approve" else "rejected",
        )
        worker.join(timeout=30)
        result = holder["result"]
        assert result.allowed is (outcome == "approve")
        assert result.status == (STATUS_AUTHORIZED if outcome == "approve" else STATUS_DENIED)

    row = get_governed_action(result.action_id)
    spent = row["approval_code"]
    assert spent, "a settled outward action records which approval it settled"

    _prune_workspaces([request.target.workspace_id])
    assert spent not in _live_ask_codes()

    follow_up = _request(tmp_path, workspace_name=f"{workspace_name}-next")
    refiled = authorize(follow_up, wait=False, notify=False)

    assert _ask_number(refiled.approval_code) > _ask_number(spent)
    assert verify_chain() is None


def test_a_duplicate_approval_code_is_diagnosed_as_a_collision_not_an_audit_failure(tmp_path):
    """If a duplicate ever escapes, it must not be blamed on the audit chain.

    ``append_in_session`` normalises everything it touches into
    ``AuditWriteError``, so a unique-constraint violation on
    ``governed_actions.approval_code`` used to reach the operator as "the audit
    append failed" - sending them to verify a chain that is perfectly intact.
    The binding is flushed on its own so the collision names itself, the
    transaction rolls back, and the approval stays unspent.
    """
    from sqlalchemy import update

    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest

    first = _request(tmp_path, workspace_name="ws-collide")
    filed = authorize(first, wait=False, notify=False)
    code = filed.approval_code
    resolve_decision(code, chosen="approve", reasoning="ok")
    assert consume_approval(first, filed.action_id, code).allowed is True

    # Simulate the escape: the spent ASK is back to ``resolved`` (a restored
    # backup, a manual edit) while the governed action still holds its code.
    with SessionLocal() as session:
        session.execute(
            update(ApprovalRequest).where(ApprovalRequest.code == code).values(status="resolved")
        )
        session.commit()

    second = _request(tmp_path, workspace_name="ws-collide")
    second_filed = authorize(second, wait=False, notify=False)

    with pytest.raises(govern.ApprovalCodeCollisionError) as excinfo:
        consume_approval(second, second_filed.action_id, code)

    assert not isinstance(excinfo.value, AuditWriteError)
    assert code in str(excinfo.value)
    assert verify_chain() is None, "the chain was never the problem"
    row = get_governed_action(second_filed.action_id)
    assert row["status"] == STATUS_PENDING, "the refusal left the action unauthorised"
    assert row["approval_code"] is None
    assert get_decision(code)["status"] == "resolved", "the approval was not spent twice"


# ----------------------------------------------------------------------
# The execution lease: a running action is not an abandoned one
# ----------------------------------------------------------------------


def _short_lease(monkeypatch, *, lease: str = "1", beat: str = "0.05") -> None:
    """A lease short enough to expire inside a test, beaten fast enough to hold."""
    monkeypatch.setenv(govern.EXECUTION_LEASE_ENV, lease)
    monkeypatch.setenv(govern.EXECUTION_HEARTBEAT_ENV, beat)


def _backdate_heartbeat(action_id: str, *, seconds: float) -> None:
    """Age every liveness anchor, so the row looks like a crashed owner."""
    from sqlalchemy import select as sa_select

    from brains.storage.db import SessionLocal
    from brains.storage.models import GovernedAction

    stale = datetime.now(UTC) - timedelta(seconds=seconds)
    with SessionLocal() as session:
        row = session.execute(
            sa_select(GovernedAction).where(GovernedAction.action_id == action_id)
        ).scalar_one()
        row.heartbeat_at = stale
        row.executed_at = stale
        row.attempt_started_at = stale
        row.created_at = stale
        session.commit()


def _failures_for(action_id: str) -> list[dict]:
    return [
        entry
        for entry in list_entries(action_prefix="governed.failed", limit=200)
        if entry["payload"].get("action_id") == action_id
    ]


def test_a_live_execution_survives_repeated_sweeps(tmp_path, monkeypatch):
    """The bug: a command that runs longer than the lease was recorded as failed.

    The effect here outlives its own lease several times over while the sweep
    runs underneath it. Nothing about elapsed runtime may settle it - only
    silence - so the row must still be executing throughout and succeed at the
    end, with no fabricated failure in the log.
    """
    _short_lease(monkeypatch)
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["long"])
    seen: list[str] = []

    def _effect() -> str:
        deadline = time.monotonic() + 1.5  # comfortably past the 1s lease
        while time.monotonic() < deadline:
            govern.run_maintenance()
            seen.append(get_governed_action(action["id"])["status"])
            time.sleep(0.05)
        return "done"

    action: dict = {}
    original_mark = govern.mark_executing

    def _capture(req, action_id):
        action["id"] = action_id
        return original_mark(req, action_id)

    monkeypatch.setattr(govern, "mark_executing", _capture)

    result = run_governed(request, _effect, notify=False)

    assert result.allowed is True
    assert seen and set(seen) == {"executing"}, (
        f"a live execution was settled by the sweep while it was still running: {set(seen)}"
    )
    row = get_governed_action(action["id"])
    assert row["status"] == STATUS_SUCCEEDED
    assert row["heartbeat_at"] is None, "a terminal row still advertises a lease"
    assert _failures_for(action["id"]) == []
    assert verify_chain() is None


def test_a_crashed_execution_settles_once_its_heartbeat_lease_expires(tmp_path, monkeypatch):
    """Liveness is a claim that has to be renewed; silence still settles."""
    _short_lease(monkeypatch)
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["crash"])
    decision = authorize(request, notify=False)
    govern.mark_executing(request, decision.action_id)

    assert govern.run_maintenance()["swept"] == 0, "a fresh execution was swept"

    _backdate_heartbeat(decision.action_id, seconds=5)
    assert govern.run_maintenance()["swept"] >= 1

    row = get_governed_action(decision.action_id)
    assert row["status"] == STATUS_FAILED
    assert "abandoned while executing" in (row["error"] or "")
    assert "heartbeat" in (row["error"] or ""), "the reason does not say what was missing"
    assert _failures_for(decision.action_id), "the sweep settled a row without recording it"
    assert verify_chain() is None


def test_an_old_attempt_cannot_renew_a_newer_one(tmp_path, monkeypatch):
    """A hung owner must not hold a lease open for the retry that replaced it."""
    monkeypatch.setenv(govern.EXECUTION_LEASE_ENV, "3600")
    key = f"test:{uuid.uuid4().hex}"
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["stale"], idempotency_key=key)
    first = authorize(request, notify=False)

    # Attempt 1 is abandoned before any effect, so a retry legitimately opens
    # attempt 2 on the same row - and attempt 1's owner is still out there.
    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 0)
    again = authorize(request, notify=False)
    assert again.action_id == first.action_id
    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 3600)
    live_attempt = govern.mark_executing(request, first.action_id)
    assert live_attempt == 2

    anchor = get_governed_action(first.action_id)["heartbeat_at"]
    stale_beat = govern.heartbeat(first.action_id, attempt=1)

    assert stale_beat.renewed is False
    assert "not renewable" in stale_beat.reason
    assert get_governed_action(first.action_id)["heartbeat_at"] == anchor, (
        "an attempt that no longer exists renewed the lease of the one that replaced it"
    )

    time.sleep(0.01)
    assert govern.heartbeat(first.action_id, attempt=live_attempt).renewed is True
    assert get_governed_action(first.action_id)["heartbeat_at"] != anchor
    govern.complete(request, first.action_id, ok=True)


def test_a_late_heartbeat_cannot_overwrite_a_terminal_outcome(tmp_path, monkeypatch):
    """Completion wins every race with the lease that was renewing the row."""
    _short_lease(monkeypatch)
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["race"])
    action: dict = {}
    original_mark = govern.mark_executing

    def _capture(req, action_id):
        action["id"] = action_id
        action["attempt"] = original_mark(req, action_id)
        return action["attempt"]

    monkeypatch.setattr(govern, "mark_executing", _capture)
    run_governed(request, lambda: "ok", notify=False)

    settled = get_governed_action(action["id"])
    assert settled["status"] == STATUS_SUCCEEDED

    late = govern.heartbeat(action["id"], attempt=action["attempt"])

    assert late.renewed is False
    after = get_governed_action(action["id"])
    assert after["status"] == STATUS_SUCCEEDED
    assert after["completed_at"] == settled["completed_at"]
    assert after["heartbeat_at"] is None

    # A lease that is still beating stops itself rather than writing on.
    lease = govern.ExecutionLease(action["id"], action["attempt"], interval=0.01)
    assert lease.beat_once() is False
    assert lease.lost is True
    assert lease.healthy is False
    assert lease.stopped_reason and "no longer executing" in lease.stopped_reason


def test_a_concurrent_reserve_cannot_replay_or_retry_a_live_execution(tmp_path, monkeypatch):
    """The other half of the guarantee: alive means the key stays taken."""
    _short_lease(monkeypatch)
    key = f"test:{uuid.uuid4().hex}"
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["held"], idempotency_key=key)
    decision = authorize(request, notify=False)
    attempt = govern.mark_executing(request, decision.action_id)

    with govern.execution_lease(decision.action_id, attempt) as lease:
        time.sleep(1.3)  # past the lease, held open by heartbeats alone
        assert lease.beats >= 1
        assert lease.healthy is True

        with pytest.raises(DuplicateActionError):
            govern.reserve(request)
        assert govern.run_maintenance()["swept"] == 0
        assert get_governed_action(decision.action_id)["status"] == "executing"

    assert lease.healthy is True, "a clean stop must not look like a lost lease"
    govern.complete(request, decision.action_id, ok=True, result="exit 0")
    assert get_governed_action(decision.action_id)["status"] == STATUS_SUCCEEDED
    assert verify_chain() is None


def test_heartbeats_are_not_audit_events(tmp_path, monkeypatch):
    """Proof of life every few seconds must not bury the transitions."""
    _short_lease(monkeypatch)
    request = _request(tmp_path, tier=TIER_LOCAL, tool="echo", args=["quiet"])
    decision = authorize(request, notify=False)
    attempt = govern.mark_executing(request, decision.action_id)
    before = len(list_entries(action_prefix="governed.", limit=500))

    for _ in range(5):
        assert govern.heartbeat(decision.action_id, attempt=attempt).renewed is True

    assert len(list_entries(action_prefix="governed.", limit=500)) == before
    govern.complete(request, decision.action_id, ok=True)
    assert verify_chain() is None


def test_the_lease_thread_is_a_daemon_that_stops_promptly():
    """A lease must never be the reason a process cannot exit."""
    lease = govern.ExecutionLease("ga_never", 1, interval=3600).start()
    live = [
        thread for thread in threading.enumerate() if thread.name == "brains-govern-lease-ga_never"
    ]

    assert live and live[0].daemon is True

    started = time.monotonic()
    lease.stop()

    assert time.monotonic() - started < 2.0, "stop() waited out the heartbeat interval"
    assert not [
        thread for thread in threading.enumerate() if thread.name == "brains-govern-lease-ga_never"
    ]


def test_a_storage_failure_is_recorded_rather_than_reported_as_health():
    """A lease that cannot write must not claim the execution is covered."""
    calls: list[int] = []

    def _explode(action_id: str, *, attempt: int):
        calls.append(attempt)
        raise RuntimeError("database is locked")

    lease = govern.ExecutionLease("ga_broken", 3, interval=0.01, beat=_explode)

    assert lease.beat_once() is True, (
        "a transient store failure ends the effect's cover, not the try"
    )
    assert lease.healthy is False
    assert lease.failures == 1
    assert "database is locked" in (lease.last_error or "")
    assert calls == [3]

    lease.stop()


def test_the_heartbeat_interval_stays_under_the_lease_it_renews(monkeypatch):
    """A beat slower than the lease would guarantee the sweep it prevents."""
    monkeypatch.setenv(govern.EXECUTION_LEASE_ENV, "60")
    monkeypatch.delenv(govern.EXECUTION_HEARTBEAT_ENV, raising=False)

    assert govern.execution_lease_seconds() == 60
    assert govern.heartbeat_seconds() == pytest.approx(20.0)

    monkeypatch.setenv(govern.EXECUTION_HEARTBEAT_ENV, "600")
    assert govern.heartbeat_seconds() <= 30.0

    monkeypatch.delenv(govern.EXECUTION_LEASE_ENV, raising=False)
    monkeypatch.delenv(govern.EXECUTION_HEARTBEAT_ENV, raising=False)
    monkeypatch.setattr(govern, "attempt_lease_seconds", lambda: 4242)
    assert govern.execution_lease_seconds() == 4242, (
        "a caller that configures nothing must keep the attempt lease it had"
    )

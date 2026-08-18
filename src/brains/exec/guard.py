"""The in-process execution boundary: every subprocess Brains itself launches.

:mod:`brains.exec.gate` governs what an *agent* runs through the PATH shims.
This module governs what *Brains* runs: recurring/autopilot spawns, tool
launches, anything that would otherwise reach :mod:`subprocess` directly.

Why it exists
-------------

A gate that only covers shimmed agent commands leaves Brains' own
``subprocess.Popen`` calls outside the boundary - which is exactly how the
recurring auto-spawn used to fork an agent CLI with no classification, no
approval and no record. Routing every internal launch through one function
makes "did this effect pass the gate?" answerable by construction rather than
by reading each call site.

What it refuses
---------------

Execution shapes that cannot be classified are denied rather than allowed with
a caveat:

* a string command line instead of an argument vector,
* ``shell=True`` (the shell, not us, would decide what runs),
* an empty argument vector.

Each refusal is recorded as a governed action with tier
:data:`~brains.govern.TIER_UNSUPPORTED`, so a denial is as visible as an
approval.

What it does not claim
----------------------

This is in-process enforcement, and its scope is *agent execution*: the PATH
shims, the agent-session launcher, the recurring/autopilot spawn, and anything
else that routes through here. It is not a claim that every line of Brains
never touches :mod:`subprocess`. Several operator-invoked paths still exec
directly and are named in BL-P0-03 rather than hidden: ``control/supervisor``
(child service processes), ``cli/run`` (interactive tool hand-off),
``cli/app`` self-update (``git pull``, ``pip install``), ``auth/copilot``
(``gh`` device login), ``backup`` (``pg_dump``/``psql``), ``install`` and
``service/common``.

It also cannot stop a *child* process from spawning grandchildren of its own -
that needs the process/network sandbox tracked as BL-P0-03. What it does
guarantee is that the agent-execution paths listed above reach an
operating-system exec only with a committed decision and a committed record.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from brains.exec.gate import classify, resolve_executable
from brains.govern import (
    TIER_UNSUPPORTED,
    ActionTarget,
    EffectOutcome,
    GovernedRequest,
    GovernedResult,
    run_governed,
)


@dataclass
class GovernedRun:
    """The outcome of a governed subprocess launch."""

    allowed: bool
    action_id: str
    status: str
    tier: str
    reason: str = ""
    approval_code: str | None = None
    returncode: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    pid: int | None = None
    process: subprocess.Popen | None = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action_id": self.action_id,
            "status": self.status,
            "tier": self.tier,
            "reason": self.reason,
            "approval_code": self.approval_code,
            "returncode": self.returncode,
            "pid": self.pid,
            "error": self.error,
        }


class UnsupportedExecution(ValueError):
    """A shape this boundary refuses to launch at all."""


#: ``subprocess.run``'s own platform switch, by the same name it uses. The
#: kill-on-timeout path differs between the platforms and copying only one half
#: of it would either lose the partial output (Windows) or block forever on a
#: grandchild that inherited the pipe (POSIX).
_WINDOWS = sys.platform == "win32"


def _validate(argv: Sequence[str] | str, shell: bool) -> str | None:
    """Return the refusal reason for an ungovernable shape, or ``None``."""
    if shell:
        return "shell=True hands command selection to the shell, which cannot be classified"
    if isinstance(argv, str):
        return "a string command line cannot be classified; pass an argument vector"
    if not argv:
        return "empty argument vector"
    return None


def _request(
    argv: Sequence[str],
    *,
    actor: str,
    action: str,
    tier: str,
    summary: str,
    target: ActionTarget,
    cwd: str | None,
    idempotency_key: str | None,
) -> GovernedRequest:
    tool = str(argv[0]) if argv else ""
    return GovernedRequest(
        actor=actor,
        action=action,
        tool=tool,
        args=[str(a) for a in argv[1:]],
        target=target,
        tier=tier,
        summary=summary[:500],
        cwd=cwd,
        idempotency_key=idempotency_key,
    )


def _refuse(
    reason: str,
    *,
    actor: str,
    action: str,
    target: ActionTarget,
    cwd: str | None,
    idempotency_key: str | None,
    tool: str,
) -> GovernedRun:
    from brains.govern import authorize

    request = GovernedRequest(
        actor=actor,
        action=action,
        tool=tool,
        args=(),
        target=target,
        tier=TIER_UNSUPPORTED,
        summary=reason,
        cwd=cwd,
        idempotency_key=idempotency_key,
    )
    decision = authorize(request, wait=False, notify=False)
    return GovernedRun(
        allowed=False,
        action_id=decision.action_id,
        status=decision.status,
        tier=TIER_UNSUPPORTED,
        reason=reason,
    )


def _result_to_run(result: GovernedResult, **extra: Any) -> GovernedRun:
    return GovernedRun(
        allowed=result.allowed,
        action_id=result.action_id,
        status=result.status,
        tier=result.tier,
        reason=result.reason,
        approval_code=result.approval_code,
        error=result.error,
        **extra,
    )


def run(
    argv: Sequence[str] | str,
    *,
    actor: str,
    action: str = "exec.command",
    workspace_path: str | None = None,
    session_id: str | None = None,
    org_id: int | None = None,
    workspace_id: int | None = None,
    issue_code: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: float | None = None,
    capture_output: bool = False,
    idempotency_key: str | None = None,
    wait_for_approval: bool = True,
    approval_timeout: float | None = None,
    shell: bool = False,
    process_observer: Callable[[subprocess.Popen], None] | None = None,
) -> GovernedRun:
    """Run one command to completion, only if it is authorised and recorded.

    The recorded outcome is the child's exit status: ``0`` settles the action
    ``succeeded``, anything else settles it ``failed`` with ``exit N``. The
    call itself keeps ``check=False`` semantics - a non-zero exit is returned
    on :class:`GovernedRun` (with the ``CompletedProcess`` fields) rather than
    raised - so the record disagreeing with reality is the only thing that
    changes.

    ``process_observer`` is handed the live child as soon as it exists. A
    blocking run still has to be *stoppable*: BL-P0-05 requires an operator
    stop to reach the exact process this call launched, and the only identity
    that survives that requirement is the handle itself - matching by name
    would signal the operator's own tools, and matching by pid alone is unsafe
    once pids are reused. The observer never changes what runs; it only lets
    the caller record what it owns.
    """
    target = ActionTarget(
        org_id=org_id,
        workspace_id=workspace_id,
        issue_code=issue_code,
        session_id=session_id,
        workspace_path=workspace_path,
    )
    refusal = _validate(argv, shell)
    if refusal is not None:
        return _refuse(
            refusal,
            actor=actor,
            action=action,
            target=target,
            cwd=cwd,
            idempotency_key=idempotency_key,
            tool=str(argv[0]) if argv and not isinstance(argv, str) else str(argv)[:120],
        )

    vector = [str(a) for a in argv]
    decision = classify(vector[0], vector[1:])
    request = _request(
        vector,
        actor=actor,
        action=action,
        tier=decision.tier,
        summary=decision.summary,
        target=target,
        cwd=cwd,
        idempotency_key=idempotency_key,
    )

    captured: dict[str, Any] = {}

    def _effect() -> subprocess.CompletedProcess:
        # ``subprocess.run`` in all but name: the child is created explicitly
        # so the live handle can be published to ``process_observer`` while it
        # runs, which is what makes a blocking governed run stoppable by its
        # exact identity rather than by a name match. The context manager and
        # the two except clauses reproduce ``run``'s cleanup exactly -
        # including its *platform-specific* kill-on-timeout - so a command that
        # raises still leaves no orphaned child or open pipe, and a timeout
        # still returns.
        with subprocess.Popen(  # noqa: S603 - governed argument vector, never a shell
            vector,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=True,
        ) as process:
            if process_observer is not None:
                with contextlib.suppress(Exception):
                    process_observer(process)
            try:
                stdout_data, stderr_data = process.communicate(input_text, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                if _WINDOWS:
                    # Windows reads the child's output on helper threads that
                    # the timeout only joins, so what was read is still held
                    # there: a communicate() after kill() is the only way to
                    # collect it, and it is what carries the partial output
                    # onto the exception.
                    exc.stdout, exc.stderr = cast("tuple[bytes, bytes]", process.communicate())
                else:
                    # POSIX already moved the output read so far onto the
                    # exception. A second communicate() here would block until
                    # every writer closed the pipe - and a killed child's
                    # grandchild still holds it - so a timeout would not
                    # actually time out. Reap the child instead.
                    process.wait()
                raise
            except BaseException:
                process.kill()
                raise
            completed = subprocess.CompletedProcess(
                vector, process.returncode, stdout_data, stderr_data
            )
        captured["returncode"] = completed.returncode
        captured["stdout"] = completed.stdout
        captured["stderr"] = completed.stderr
        return completed

    def _settle(completed: subprocess.CompletedProcess) -> EffectOutcome:
        # ``check=False`` means a failed command returns instead of raising, so
        # without this the governed row would read ``succeeded`` for a command
        # that did not succeed.
        returncode = completed.returncode
        ok = returncode == 0
        return EffectOutcome(
            ok=ok,
            result=f"exit {returncode}",
            error=None if ok else f"exit {returncode}",
        )

    result = run_governed(
        request,
        _effect,
        wait=wait_for_approval,
        timeout_seconds=approval_timeout,
        settle=_settle,
    )
    return _result_to_run(
        result,
        returncode=captured.get("returncode"),
        stdout=captured.get("stdout"),
        stderr=captured.get("stderr"),
    )


def spawn(
    argv: Sequence[str] | str,
    *,
    actor: str,
    action: str = "exec.spawn",
    workspace_path: str | None = None,
    session_id: str | None = None,
    org_id: int | None = None,
    workspace_id: int | None = None,
    issue_code: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
    stdin: Any = None,
    text: bool = False,
    bufsize: int = -1,
    creationflags: int = 0,
    idempotency_key: str | None = None,
    wait_for_approval: bool = True,
    approval_timeout: float | None = None,
    shell: bool = False,
) -> GovernedRun:
    """Launch a long-running command, only if it is authorised and recorded.

    The recorded outcome is *the launch*, not the child's exit status: the
    child outlives this call, so claiming to know how it ended would be a
    fabrication. Callers that need the exit status wait on
    :attr:`GovernedRun.process` themselves.
    """
    target = ActionTarget(
        org_id=org_id,
        workspace_id=workspace_id,
        issue_code=issue_code,
        session_id=session_id,
        workspace_path=workspace_path,
    )
    refusal = _validate(argv, shell)
    if refusal is not None:
        return _refuse(
            refusal,
            actor=actor,
            action=action,
            target=target,
            cwd=cwd,
            idempotency_key=idempotency_key,
            tool=str(argv[0]) if argv and not isinstance(argv, str) else str(argv)[:120],
        )

    vector = [str(a) for a in argv]
    decision = classify(vector[0], vector[1:])
    request = _request(
        vector,
        actor=actor,
        action=action,
        tier=decision.tier,
        summary=decision.summary,
        target=target,
        cwd=cwd,
        idempotency_key=idempotency_key,
    )

    launched: dict[str, Any] = {}

    def _effect() -> int:
        process = subprocess.Popen(  # noqa: S603 - governed argument vector, never a shell
            vector,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
            text=text,
            bufsize=bufsize,
            creationflags=creationflags,
        )
        launched["process"] = process
        launched["pid"] = process.pid
        return process.pid

    result = run_governed(
        request,
        _effect,
        wait=wait_for_approval,
        timeout_seconds=approval_timeout,
    )
    return _result_to_run(
        result,
        pid=launched.get("pid"),
        process=launched.get("process"),
    )


__all__ = [
    "GovernedRun",
    "UnsupportedExecution",
    "resolve_executable",
    "run",
    "spawn",
]

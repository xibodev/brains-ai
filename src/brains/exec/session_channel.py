"""The Runtime's end of a Session: the processes it owns, and what it can do to them.

BL-P0-05 splits one question into two that must be answered separately:

*Can a message reach the agent at all?*
    Not every agent CLI has an input channel once it is running. The three
    tools Brains launches (``copilot``, ``claude``, ``codex``) are invoked in
    their non-interactive shapes: the prompt is either an argv element or a
    single stdin feed terminated by EOF, and after that the process has no
    stdin to write to. Telling an operator "sent" in that situation would be a
    fabrication, so this module *declares* the capability
    (:func:`message_capability`) and the queue settles a message for an
    incapable Session as ``failed``/``unsupported`` with the reason - a
    durable, honest blocked state rather than an echo.

*Which process may this Runtime signal?*
    A stop must reach the process this Runtime launched for this Session and
    nothing else. Matching by name would be a footgun (``pkill copilot`` on a
    developer box kills the operator's own editor session), and matching by
    pid alone is unsafe across a restart because pids are reused. So the only
    thing that can be stopped is a handle in :data:`_OWNED` - a
    :class:`subprocess.Popen` this process created and still holds - and the
    stop verifies the handle's pid still matches the one recorded at launch.
    A Runtime that restarted owns nothing, cannot signal anything, and says so
    (``not_owned``); the hub then reconciles that Session from the terminal
    side instead of pretending a signal was delivered.

The registry is per-process and deliberately not persisted: ownership is
exactly "this process holds this handle", which is the only claim that stays
true after a crash.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: Tools whose launch shape leaves a writable stdin for the life of the
#: Session. Empty for the shipped CLIs: ``copilot`` takes its prompt in argv,
#: and ``claude``/``codex`` read one prompt from stdin and then see EOF. A
#: tool is added here only together with a launch path that keeps stdin open
#: (:func:`brains.exec.runner.run_session` honours this set), which is what
#: keeps the declaration and the behaviour from drifting apart.
_INTERACTIVE_TOOLS: set[str] = set()

#: Why a tool cannot be messaged. Shown to the operator verbatim, so it names
#: the launch shape rather than blaming the agent.
_UNSUPPORTED_REASON = (
    "{tool} is launched in its non-interactive shape: the prompt is delivered "
    "once at start and the process has no open input channel afterwards, so a "
    "message cannot reach the running agent"
)
_UNKNOWN_TOOL_REASON = (
    "no interactive input channel is declared for {tool}, so a message cannot "
    "reach the running agent"
)

#: Outcomes a delivery or a stop can report. The queue stores these verbatim
#: as the command's ``result``.
RESULT_DELIVERED = "delivered"
RESULT_STOPPED = "stopped"
RESULT_ALREADY_EXITED = "already_exited"
RESULT_UNSUPPORTED = "unsupported"
RESULT_NOT_OWNED = "not_owned"
RESULT_WRITE_FAILED = "write_failed"

#: How long a stop waits for a terminated process before escalating to a kill.
TERMINATE_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class ChannelOutcome:
    """What actually happened, with no optimistic middle ground."""

    ok: bool
    result: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "result": self.result, "error": self.error}


@dataclass
class OwnedProcess:
    """One agent process this Runtime launched for one Session."""

    session_id: str
    process: subprocess.Popen
    tool: str
    pid: int
    started_at: datetime
    #: True only while the launch path is keeping stdin open on purpose.
    stdin_open: bool = False
    #: The Runtime this process was launched for, where one launched it. A
    #: handle with no Runtime belongs to the local process (a CLI session, the
    #: hub's own console session) and is nobody's to reconcile but its own.
    runtime_id: int | None = None

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def identity_matches(self) -> bool:
        """The handle still names the process we launched.

        ``Popen.pid`` never changes, so this is a guard against a caller
        handing us a fabricated handle rather than against pid reuse - pid
        reuse is excluded structurally, because a reused pid belongs to a
        process we never launched and therefore hold no handle for.
        """
        return self.process.pid == self.pid


_OWNED: dict[str, OwnedProcess] = {}
_LOCK = threading.Lock()


def declare_interactive_tool(tool: str) -> None:
    """Declare that ``tool`` is launched with a durable stdin channel.

    Used by a launch path that genuinely keeps stdin open (and by tests that
    exercise that path with a fake agent). Declaring a tool here without such
    a launch path would make :func:`message_capability` lie, which is the one
    thing this module exists to prevent.
    """
    with _LOCK:
        _INTERACTIVE_TOOLS.add(tool)


def undeclare_interactive_tool(tool: str) -> None:
    with _LOCK:
        _INTERACTIVE_TOOLS.discard(tool)


def interactive_tools() -> frozenset[str]:
    with _LOCK:
        return frozenset(_INTERACTIVE_TOOLS)


def supports_message(tool: str | None) -> bool:
    return bool(tool) and tool in interactive_tools()


def message_capability(tool: str | None) -> dict[str, Any]:
    """``{supported, reason}`` for messaging a Session running ``tool``.

    The console reads this to decide whether its composer is usable, so the
    reason is written for an operator rather than for a log.
    """
    name = tool or "this agent"
    if supports_message(tool):
        return {"supported": True, "reason": None}
    known = {"copilot", "claude", "codex"}
    template = _UNSUPPORTED_REASON if tool in known else _UNKNOWN_TOOL_REASON
    return {"supported": False, "reason": template.format(tool=name)}


def register(
    session_id: str,
    process: subprocess.Popen,
    *,
    tool: str,
    stdin_open: bool = False,
    runtime_id: int | None = None,
) -> OwnedProcess:
    """Record that this process owns ``process`` for ``session_id``.

    ``runtime_id`` names the Runtime the launch was made for, so a daemon
    hosting several Runtimes on one box can report each one exactly what it
    holds. Without it every Runtime on the machine would be told about every
    other Runtime's Sessions, which the hub refuses as a foreign claim - and
    the sibling that had nothing to report would never reconcile its own
    stale rows.
    """
    owned = OwnedProcess(
        session_id=session_id,
        process=process,
        tool=tool,
        pid=process.pid,
        started_at=datetime.now(UTC),
        stdin_open=stdin_open,
        runtime_id=int(runtime_id) if runtime_id is not None else None,
    )
    with _LOCK:
        _OWNED[session_id] = owned
    return owned


def unregister(session_id: str) -> None:
    with _LOCK:
        _OWNED.pop(session_id, None)


def clear() -> None:
    """Drop every registration (process shutdown, and test isolation)."""
    with _LOCK:
        _OWNED.clear()


def owned(session_id: str | None) -> OwnedProcess | None:
    if not session_id:
        return None
    with _LOCK:
        return _OWNED.get(session_id)


def owned_session_ids() -> list[str]:
    """The Sessions this process can still act on, live ones only.

    This is what a daemon reports to the hub on startup so the hub can
    reconcile everything it believes is running here and this process cannot
    prove is.
    """
    with _LOCK:
        items = list(_OWNED.items())
    return sorted(sid for sid, owned_process in items if owned_process.alive)


def owned_session_ids_by_runtime() -> dict[int | None, list[str]]:
    """Live owned Sessions grouped by the Runtime each was launched for.

    Reconciliation is per Runtime, not per machine: the hub refuses a Runtime
    that claims to own another Runtime's Session, and one refused call would
    take the whole reconciliation of that Runtime with it. Grouping here means
    each Runtime is told exactly its own handles, and a sibling holding
    nothing is told so explicitly - which is what lets it end the rows the hub
    still shows running for it.

    Handles launched outside a Runtime are grouped under ``None``; they belong
    to this process, and no Runtime should ever claim them.
    """
    with _LOCK:
        items = list(_OWNED.values())
    grouped: dict[int | None, list[str]] = {}
    for owned_process in items:
        if not owned_process.alive:
            continue
        grouped.setdefault(owned_process.runtime_id, []).append(owned_process.session_id)
    return {runtime_id: sorted(ids) for runtime_id, ids in grouped.items()}


def describe(session_id: str) -> dict[str, Any] | None:
    owned_process = owned(session_id)
    if owned_process is None:
        return None
    return {
        "session_id": owned_process.session_id,
        "tool": owned_process.tool,
        "pid": owned_process.pid,
        "alive": owned_process.alive,
        "stdin_open": owned_process.stdin_open,
        "runtime_id": owned_process.runtime_id,
        "started_at": owned_process.started_at.isoformat(),
    }


def deliver_message(session_id: str, text: str) -> ChannelOutcome:
    """Write ``text`` to the owned agent's stdin, or explain why it cannot be.

    Every refusal is a distinct, durable result: an operator who is told
    ``unsupported`` knows the tool has no input channel, one told
    ``not_owned`` knows the Runtime restarted, and one told ``already_exited``
    knows the agent finished first. None of them is told "sent".
    """
    owned_process = owned(session_id)
    if owned_process is None:
        return ChannelOutcome(
            False,
            RESULT_NOT_OWNED,
            "this Runtime does not own a process for the Session; it was started by "
            "another Runtime, or this Runtime restarted after launching it",
        )
    if not supports_message(owned_process.tool) or not owned_process.stdin_open:
        return ChannelOutcome(
            False,
            RESULT_UNSUPPORTED,
            message_capability(owned_process.tool)["reason"],
        )
    if not owned_process.alive:
        return ChannelOutcome(False, RESULT_ALREADY_EXITED, "the agent process has already exited")
    stdin = owned_process.process.stdin
    if stdin is None or stdin.closed:
        return ChannelOutcome(
            False, RESULT_UNSUPPORTED, "the agent process has no open input channel"
        )
    try:
        stdin.write(text if text.endswith("\n") else f"{text}\n")
        stdin.flush()
    except (OSError, ValueError) as exc:
        return ChannelOutcome(False, RESULT_WRITE_FAILED, f"writing to the agent failed: {exc}")
    return ChannelOutcome(True, RESULT_DELIVERED)


def stop_session(session_id: str) -> ChannelOutcome:
    """Terminate the owned agent process for ``session_id``.

    Only a handle this process holds is signalled, and only after its pid is
    re-checked against the one recorded at launch. Nothing is ever matched by
    executable name: a Runtime shares a box with the operator's own tools, and
    a name match would stop theirs too.
    """
    owned_process = owned(session_id)
    if owned_process is None:
        return ChannelOutcome(
            False,
            RESULT_NOT_OWNED,
            "this Runtime does not own a process for the Session; it was started by "
            "another Runtime, or this Runtime restarted after launching it",
        )
    if not owned_process.identity_matches():  # pragma: no cover - defensive
        return ChannelOutcome(
            False, RESULT_NOT_OWNED, "the recorded process identity no longer matches the handle"
        )
    process = owned_process.process
    if process.poll() is not None:
        unregister(session_id)
        return ChannelOutcome(True, RESULT_ALREADY_EXITED)
    try:
        process.terminate()
        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except OSError as exc:  # pragma: no cover - platform dependent
        return ChannelOutcome(False, RESULT_WRITE_FAILED, f"stopping the agent failed: {exc}")
    unregister(session_id)
    return ChannelOutcome(True, RESULT_STOPPED)


__all__ = [
    "RESULT_ALREADY_EXITED",
    "RESULT_DELIVERED",
    "RESULT_NOT_OWNED",
    "RESULT_STOPPED",
    "RESULT_UNSUPPORTED",
    "RESULT_WRITE_FAILED",
    "ChannelOutcome",
    "OwnedProcess",
    "clear",
    "declare_interactive_tool",
    "deliver_message",
    "describe",
    "interactive_tools",
    "message_capability",
    "owned",
    "owned_session_ids",
    "owned_session_ids_by_runtime",
    "register",
    "stop_session",
    "supports_message",
    "undeclare_interactive_tool",
    "unregister",
]

"""Turning a claimed Session command into an actual effect on an agent process.

The queue (:mod:`brains.control.session_commands`) records *what was asked
for*; :mod:`brains.exec.session_channel` knows *what this process can do*.
This module is the one place the two meet, so the rule "claim, execute,
acknowledge exactly what happened" is written once and used by both consumers:

* the Runtime daemon, which claims over HTTP, executes here, and acknowledges
  over HTTP;
* the hub process itself, for a Session whose agent process it launched (the
  streamed console session), which claims, executes and settles in-process.

Nothing here decides authorization or invents an outcome: an effect this
process cannot perform is reported as the refusal the channel returned, and
the queue settles the command with that refusal.
"""

from __future__ import annotations

import os
from typing import Any

from brains.exec import session_channel
from brains.exec.session_channel import ChannelOutcome


def local_consumer_id() -> str:
    """A stable identity for this process as a command consumer.

    The lease holder must be identifiable, because only the holder may settle
    a command. Process identity is the honest granularity: ownership of an
    agent process does not survive a restart, and neither should a claim.
    """
    try:
        from brains.control.sessions import current_machine_id

        machine = current_machine_id()
    except Exception:  # pragma: no cover - machine id is best effort
        machine = "local"
    return f"local:{machine}:{os.getpid()}"[:64]


def local_machine_id() -> str | None:
    """The machine this process runs on, where it can be determined."""
    try:
        from brains.control.sessions import current_machine_id

        return current_machine_id()
    except Exception:  # pragma: no cover - machine id is best effort
        return None


def owns(
    command: dict[str, Any],
    *,
    runtime_id: int | None = None,
    machine_id: str | None = None,
) -> bool:
    """Whether this consumer is the one allowed to deliver ``command``."""
    from brains.control import session_commands as commands_ctl

    return commands_ctl.owned_by(command, runtime_id=runtime_id, machine_id=machine_id)


def release_foreign(command: dict[str, Any], *, consumer: str) -> dict[str, Any] | None:
    """Requeue a claimed command this consumer turns out not to own.

    Settling it would be a lie twice over: the outcome would describe a
    delivery that was never attempted, and it would consume the command its
    real owner is about to claim. Handing it back costs one poll interval and
    keeps the record true.
    """
    from brains.control import session_commands as commands_ctl

    return commands_ctl.release(
        command["command_id"],
        consumer=consumer,
        reason="claimed by a consumer that does not own this Session; requeued for its owner",
    )


def execute(command: dict[str, Any]) -> ChannelOutcome:
    """Perform one claimed command against the process this Runtime owns."""
    from brains.control import session_commands as commands_ctl

    session_id = str(command.get("session_id") or "")
    kind = command.get("kind")
    if kind == commands_ctl.KIND_STOP:
        return session_channel.stop_session(session_id)
    if kind == commands_ctl.KIND_MESSAGE:
        text = command.get("text") or (command.get("payload") or {}).get("text") or ""
        return session_channel.deliver_message(session_id, text)
    return ChannelOutcome(False, "unsupported", f"no delivery is defined for a {kind!r} command")


def settle_outcome(command_id: str, outcome: ChannelOutcome, *, consumer: str) -> dict[str, Any]:
    """Acknowledge a command with the outcome the channel actually produced."""
    from brains.control import session_commands as commands_ctl

    return commands_ctl.acknowledge(
        command_id,
        consumer=consumer,
        result=outcome.result,
        error=outcome.error,
        ok=outcome.ok,
    )


def dispatch_owned(*, session_id: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
    """Claim and run the queued commands for Sessions this process owns.

    Used by the hub process for Sessions it launched itself. Ownership is
    checked twice, and both checks matter: the process must hold the agent's
    handle, *and* the command must belong to the local consumer - a Session
    bound to a Runtime is that Runtime's to deliver, and claiming it here
    would take a lease on somebody else's work. A command claimed a moment
    before its Session was re-bound is handed back rather than settled.
    """
    from brains.control import session_commands as commands_ctl

    owned = set(session_channel.owned_session_ids())
    if session_id is not None:
        owned &= {session_id}
    if not owned:
        return []
    consumer = local_consumer_id()
    machine = local_machine_id()
    settled: list[dict[str, Any]] = []
    for candidate in sorted(owned):
        for command in commands_ctl.list_for_session(candidate, limit=limit):
            if command["status"] != commands_ctl.STATUS_REQUESTED:
                continue
            if not owns(command, machine_id=machine):
                continue
            try:
                claimed = commands_ctl.claim(
                    command["command_id"], consumer=consumer, machine_id=machine
                )
            except commands_ctl.NotOwnedError:  # pragma: no cover - re-bound mid-claim
                continue
            if claimed is None:
                continue
            if not owns(claimed, machine_id=machine):  # pragma: no cover - re-bound mid-claim
                release_foreign(claimed, consumer=consumer)
                continue
            settled.append(
                settle_outcome(claimed["command_id"], execute(claimed), consumer=consumer)
            )
    return settled


__all__ = [
    "dispatch_owned",
    "execute",
    "local_consumer_id",
    "local_machine_id",
    "owns",
    "release_foreign",
    "settle_outcome",
]

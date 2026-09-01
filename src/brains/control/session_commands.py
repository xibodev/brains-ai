"""The durable Session command queue: message and stop, recorded before delivered.

BL-P0-05. A message typed into the console and a stop pressed on a Session are
*requests*. The browser cannot deliver them, and the Runtime that can may be
busy, restarting, or gone. This module is the record of what was asked for and
what became of it.

The contract it implements
--------------------------

**Persist before notify.** :func:`enqueue` commits the row and only then
publishes. Nothing is announced, echoed or optimistically rendered before it
is durable, so a reload after a crash shows the same queue the server holds.

**One logical operation per retry.** ``operation_key`` is unique. A retried
POST - the same browser submit replayed, a stop pressed twice, a request
re-sent after a network timeout - reuses the existing row and returns it with
``duplicate: true``. Two concurrent retries race one INSERT and exactly one
wins, so a retry can never queue a second prompt or a second signal. A stop
that *failed* without ending the Session is the one case where the next press
is a new command rather than the old one: dedupe there would leave an operator
holding a dead handle, pressing a button that can only ever return the failure
it already saw.

**One owner per command.** A command carries the Session's binding. A command
whose Session is bound to a Runtime belongs to *that* Runtime and to no other
consumer, however many workers share the machine; a command whose Session has
no Runtime binding belongs to the local process that launched the agent. A
consumer that finds itself holding a command it does not own hands it back so
its owner can run it, rather than settling it failed on the owner's behalf.

**Ordered.** ``(session_id, sequence)`` is unique and dense per Session, so a
consumer delivers in the order the operator typed and the console renders the
same order after a reload.

**At most one active consumer.** A claim is a single conditional UPDATE from
``requested`` to ``delivered``; two Runtimes racing one command resolve to one
winner. The winner holds a lease, and only the lease holder can settle the
command - a late acknowledgement from a previous, expired holder is refused
rather than allowed to overwrite the outcome.

**Recoverable.** A consumer that dies mid-flight strands nothing: the lease
expires, :func:`expire_leases` returns the command to ``requested`` with
``attempt`` incremented, and it is claimed again. A command whose Session
reached a terminal state is ``cancelled`` rather than left pending forever,
and a command that has exhausted its attempts is ``failed`` with a reason
instead of retried into a loop.

**Truthful.** A message to a Session whose agent has no input channel is
settled ``failed``/``unsupported`` with the reason the operator needs, and a
stop the Runtime cannot prove it delivered is settled ``failed``/``not_owned``
rather than reported as a stop. Nothing here reports success it cannot
demonstrate.

Pure control logic - no FastAPI. Authorization is the caller's job
(:mod:`brains.api.coordination` for operators, :mod:`brains.api.runtimes` for
Runtimes).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, Runtime, SessionCommand

KIND_MESSAGE = "message"
KIND_STOP = "stop"
KINDS = frozenset({KIND_MESSAGE, KIND_STOP})

STATUS_REQUESTED = "requested"
STATUS_DELIVERED = "delivered"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
OPEN_STATUSES = frozenset({STATUS_REQUESTED, STATUS_DELIVERED})
TERMINAL_STATUSES = frozenset({STATUS_ACKNOWLEDGED, STATUS_FAILED, STATUS_CANCELLED})
STATUSES = OPEN_STATUSES | TERMINAL_STATUSES

#: Results the queue itself settles, alongside the ones
#: :mod:`brains.exec.session_channel` reports from an actual delivery.
RESULT_UNSUPPORTED = "unsupported"
RESULT_SESSION_ENDED = "session_ended"
RESULT_SESSION_DORMANT = "session_dormant"
RESULT_ALREADY_TERMINAL = "already_terminal"
RESULT_ABANDONED = "abandoned"
RESULT_SUPERSEDED = "superseded"
RESULT_STOPPED = "stopped"

#: The two consumers a command can belong to. A Runtime consumer speaks for
#: one ``runtime_id``; the local consumer is the process that launched the
#: agent itself and owns the commands of Sessions with no Runtime binding.
CONSUMER_RUNTIME = "runtime"
CONSUMER_LOCAL = "local"

#: How long a consumer holds a claimed command before the lease expires and
#: the command becomes claimable again. Long enough to cover a slow
#: delivery, short enough that a crashed consumer does not strand an operator.
LEASE_SECONDS_ENV = "BRAINS_SESSION_COMMAND_LEASE_SECONDS"
DEFAULT_LEASE_SECONDS = 60

#: How many times a command may be claimed before it is settled ``failed``.
#: Without a bound, a consumer that crashes deterministically on one command
#: retries it forever and never drains the queue behind it.
MAX_ATTEMPTS_ENV = "BRAINS_SESSION_COMMAND_MAX_ATTEMPTS"
DEFAULT_MAX_ATTEMPTS = 5

#: A message body longer than this is refused rather than truncated: a queue
#: is not a blob store, and silently dropping the tail of a prompt is worse
#: than saying no.
MAX_MESSAGE_CHARS = 8000

#: An ``operation_id`` longer than this is refused for the same reason, and
#: for a sharper one: it is the *uniqueness* key. Truncating it to fit the
#: column would make two genuinely different operations collide on their
#: shared prefix, and the second one would be silently answered with the
#: first one's command.
MAX_OPERATION_ID_CHARS = 100

#: How many times :func:`enqueue` retries a lost ``sequence`` race before
#: giving up. Each retry is another concurrent enqueue winning the position.
_SEQUENCE_ATTEMPTS = 8

#: Separates a default-keyed stop from its retry epoch. A stop pressed with no
#: ``operation_id`` keys on the Session, so pressing it twice is one command -
#: but once an attempt has *failed* without ending the Session, the next press
#: is a new attempt and needs a key of its own. The epoch is derived from what
#: is already recorded rather than minted at random, so two operators pressing
#: stop at the same moment still compute the same key and still collide on one
#: INSERT.
_STOP_EPOCH_SEPARATOR = "#"


class SessionCommandError(ValueError):
    """A command that cannot be created or settled as asked."""


class UnknownSessionError(SessionCommandError):
    """The Session the command names does not exist."""


class NotOwnedError(SessionCommandError):
    """A consumer tried to take a command that belongs to a different one."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def lease_seconds() -> int:
    return _env_int(LEASE_SECONDS_ENV, DEFAULT_LEASE_SECONDS)


def max_attempts() -> int:
    return _env_int(MAX_ATTEMPTS_ENV, DEFAULT_MAX_ATTEMPTS)


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _to_dict(row: SessionCommand) -> dict[str, Any]:
    payload = _loads(row.payload_json)
    return {
        "command_id": row.command_id,
        "operation_key": row.operation_key,
        "session_id": row.session_id,
        "sequence": row.sequence,
        "kind": row.kind,
        "status": row.status,
        "result": row.result,
        "error": row.error,
        "text": payload.get("text"),
        "reason": payload.get("reason"),
        "payload": payload,
        "org_id": row.org_id,
        "workspace_id": row.workspace_id,
        "runtime_id": row.runtime_id,
        "machine_id": row.machine_id,
        "requested_by": row.requested_by,
        "attempt": row.attempt,
        "claimed_by": row.claimed_by,
        "claimed_at": _iso(row.claimed_at),
        "lease_expires_at": _iso(row.lease_expires_at),
        "delivered_at": _iso(row.delivered_at),
        "completed_at": _iso(row.completed_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def operation_key_for(session_id: str, kind: str, operation_id: str | None) -> str:
    """The idempotency key for one logical command.

    A stop with no explicit operation id keys on the Session alone, because
    "stop this Session" *is* one logical operation however many times it is
    pressed. A message keys on the caller's operation id; a caller that
    supplies none is asking for a distinct message each time, which is what a
    chat composer means by pressing send twice.

    The key is never truncated to fit: :func:`enqueue` refuses an over-long
    ``operation_id`` first, because a truncated key would make two different
    operations collide and answer the second with the first one's command.
    """
    if operation_id:
        if len(operation_id) > MAX_OPERATION_ID_CHARS:
            raise SessionCommandError(
                f"an operation_id is limited to {MAX_OPERATION_ID_CHARS} characters; "
                f"got {len(operation_id)}"
            )
        return f"{kind}:{session_id}:{operation_id}"
    if kind == KIND_STOP:
        return default_stop_key(session_id)
    return f"{kind}:{session_id}:{uuid.uuid4().hex}"


def default_stop_key(session_id: str, *, epoch: int = 1) -> str:
    """The key for the ``epoch``-th default (no ``operation_id``) stop.

    The first attempt keeps the bare ``stop:{session_id}`` key it has always
    had, so pressing stop twice is one command. A later attempt exists only
    because an earlier one reached a terminal *failure* while the Session kept
    running, and it is keyed by its epoch so that it is a distinct, durable
    command rather than a second delivery of the dead one.
    """
    base = f"{KIND_STOP}:{session_id}"
    return base if epoch <= 1 else f"{base}{_STOP_EPOCH_SEPARATOR}{epoch}"


def _is_default_stop_key(key: str | None, session_id: str) -> bool:
    """Whether ``key`` is a default stop key for this Session (any epoch)."""
    if not key:
        return False
    base = f"{KIND_STOP}:{session_id}"
    if key == base:
        return True
    suffix = key[len(base) :] if key.startswith(base) else ""
    return suffix.startswith(_STOP_EPOCH_SEPARATOR) and suffix[1:].isdigit()


def _stop_attempt_is_spent(row: SessionCommand, agent_session: AgentSession) -> bool:
    """Whether the last default stop leaves the operator with nothing to press.

    An attempt is *not* spent - and the press is therefore the same command -
    while it is still open, once the Session is terminal whatever happened to
    it, and when it was acknowledged with an outcome that proves the process is
    gone. Anything else is a stop that did not stop anything: a Runtime that
    answered ``not_owned``, a delivery abandoned after its attempts ran out, a
    command cancelled by a Session that then kept running. Returning that
    record to the next press would make the button permanently inert, which is
    worse than the double-signal dedupe exists to prevent - a signal to a
    process that is provably still running is exactly what was asked for.
    """
    if row.status in OPEN_STATUSES:
        return False
    if agent_session.ended_at is not None:
        return False
    return not (row.status == STATUS_ACKNOWLEDGED and row.result in _STOP_PROVES_TERMINAL)


def _default_stop_slot(
    session, session_id: str, agent_session: AgentSession
) -> tuple[str, SessionCommand | None]:
    """Return ``(key, existing)`` for a stop pressed with no ``operation_id``.

    ``existing`` is the command this press *is* - an open attempt or one that
    already did its job - and ``key`` is a fresh epoch when the last attempt
    was spent.
    """
    rows = [
        row
        for row in session.query(SessionCommand)
        .filter(SessionCommand.session_id == session_id, SessionCommand.kind == KIND_STOP)
        .order_by(SessionCommand.sequence.asc())
        .all()
        if _is_default_stop_key(row.operation_key, session_id)
    ]
    if not rows:
        return default_stop_key(session_id), None
    latest = rows[-1]
    if not _stop_attempt_is_spent(latest, agent_session):
        return latest.operation_key, latest
    return default_stop_key(session_id, epoch=len(rows) + 1), None


def owner_of(command: dict[str, Any]) -> tuple[str, int | None]:
    """Which consumer may run this command: ``(kind, runtime_id)``.

    Ownership is the Session's binding, not the machine. A Session bound to a
    Runtime is that Runtime's to deliver even where several workers, a CLI and
    the hub all share one box; a Session with no binding was launched by the
    local process and only that process holds a handle for it. "Whoever is on
    this machine" is not an owner: it hands one worker's stop to another
    worker, which can only answer ``not_owned`` and burn the command.
    """
    runtime_id = command.get("runtime_id")
    if runtime_id is None:
        return CONSUMER_LOCAL, None
    return CONSUMER_RUNTIME, int(runtime_id)


def owned_by(
    command: dict[str, Any],
    *,
    runtime_id: int | None = None,
    machine_id: str | None = None,
) -> bool:
    """Whether the described consumer owns ``command``.

    A Runtime consumer passes its own ``runtime_id``; the local/hub consumer
    passes none and identifies itself by machine.

    ``machine_id`` is deliberately *not* consulted for a Runtime-owned
    command. The machine stamp is a co-location hint written when the row was
    created, and in the production shape it is frequently the hub's own
    machine rather than the Runtime's - a spawn Session is opened by the hub
    process before the daemon ever touches it. Comparing it would make the
    remote Runtime that actually holds the agent process a stranger to its own
    Session's commands, which is a stop the operator presses and nobody
    delivers. The binding is the owner; the stamp is a hint.
    """
    kind, owner_runtime = owner_of(command)
    if kind == CONSUMER_RUNTIME:
        return runtime_id is not None and int(runtime_id) == owner_runtime
    if runtime_id is not None:
        return False
    if command.get("machine_id") and machine_id:
        return str(command["machine_id"]) == str(machine_id)
    return True


def assert_owned(
    command: dict[str, Any],
    *,
    runtime_id: int | None = None,
    machine_id: str | None = None,
) -> None:
    """Raise :class:`NotOwnedError` unless the described consumer owns it."""
    if not owned_by(command, runtime_id=runtime_id, machine_id=machine_id):
        kind, owner_runtime = owner_of(command)
        owner = f"runtime {owner_runtime}" if kind == CONSUMER_RUNTIME else "the local process"
        raise NotOwnedError(
            f"session command {command.get('command_id')} belongs to {owner}; "
            "another consumer must not deliver it"
        )


def _session_binding(session, session_id: str) -> tuple[AgentSession, int | None]:
    row = session.get(AgentSession, session_id)
    if row is None:
        raise UnknownSessionError(f"unknown session: {session_id}")
    org_id = None
    try:
        from brains.authz import policy

        org_id = policy.workspace_org_id(row.workspace_id)
    except Exception:  # pragma: no cover - scope resolution is best effort
        org_id = None
    return row, org_id


def _binding_machine_id(session, agent_session: AgentSession) -> str | None:
    """The machine a command for ``agent_session`` will actually be run on.

    A Session bound to a Runtime runs on *that Runtime's* machine, whatever
    machine stamp the Session row happens to carry: a spawn row is created by
    the hub process, so it is stamped with the hub's machine until the daemon
    opens it. Copying that stamp onto the command would record a box the agent
    was never on. The stamp is a diagnostic, never the ownership test (see
    :func:`owned_by`), so a Runtime whose machine cannot be resolved simply
    keeps the Session's.
    """
    if agent_session.runtime_id is None:
        return agent_session.machine_id
    row = session.get(Runtime, agent_session.runtime_id)
    if row is None or not row.machine_id:
        return agent_session.machine_id
    return row.machine_id


def enqueue(
    session_id: str,
    kind: str,
    *,
    text: str | None = None,
    reason: str | None = None,
    operation_id: str | None = None,
    requested_by: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Record one command. Returns ``(command, created)``.

    ``created`` is ``False`` when ``operation_key`` already existed: the
    caller then holds the *original* command, which is what makes a retry a
    no-op rather than a second delivery. A stop pressed with no explicit
    ``operation_id`` is the one exception, and only in one direction: while an
    attempt is open, or once it has stopped the Session, the press is that
    same command; once an attempt has failed terminally and the Session is
    still running, the press mints a new durable attempt (see
    :func:`_default_stop_slot`). An explicit ``operation_id`` always means one
    command, whatever became of it - that is what the caller asked for by
    naming it.

    Two situations are settled here rather than queued, because queueing them
    would be a promise nobody can keep:

    * a Session that has already ended - a message is ``failed``
      (``session_ended``) and a stop is ``acknowledged``
      (``already_terminal``), which is what makes stop idempotent against a
      natural finish;
    * a message to an agent with no input channel - ``failed``
      (``unsupported``) with the reason, so the console can show a blocked
      state instead of a sent bubble.
    """
    if kind not in KINDS:
        raise SessionCommandError(f"kind must be one of {sorted(KINDS)}")
    if kind == KIND_MESSAGE:
        text = (text or "").strip()
        if not text:
            raise SessionCommandError("a message needs text")
        if len(text) > MAX_MESSAGE_CHARS:
            raise SessionCommandError(
                f"a message is limited to {MAX_MESSAGE_CHARS} characters; got {len(text)}"
            )
    key = operation_key_for(session_id, kind, operation_id)
    payload: dict[str, Any] = {}
    if text:
        payload["text"] = text
    if reason:
        payload["reason"] = reason
    if operation_id:
        payload["operation_id"] = operation_id

    init_db()
    created = False
    default_stop = kind == KIND_STOP and not operation_id
    for _attempt in range(_SEQUENCE_ATTEMPTS):
        with SessionLocal() as session:
            agent_session, org_id = _session_binding(session, session_id)
            if default_stop:
                # Resolved inside the loop and against the current record: a
                # concurrent press that won the key is found on the next pass
                # and answered with *its* command rather than a second signal.
                key, existing = _default_stop_slot(session, session_id, agent_session)
            else:
                existing = (
                    session.query(SessionCommand)
                    .filter(SessionCommand.operation_key == key)
                    .one_or_none()
                )
            if existing is not None:
                return _to_dict(existing), False
            status, result, error = _initial_state(agent_session, kind)
            now = utc_now()
            highest = (
                session.query(SessionCommand.sequence)
                .filter(SessionCommand.session_id == session_id)
                .order_by(SessionCommand.sequence.desc())
                .first()
            )
            row = SessionCommand(
                command_id=f"sc_{uuid.uuid4().hex[:16]}",
                operation_key=key,
                session_id=session_id,
                sequence=(highest[0] if highest else 0) + 1,
                kind=kind,
                status=status,
                payload_json=json.dumps(payload, default=str),
                org_id=org_id,
                workspace_id=agent_session.workspace_id,
                runtime_id=agent_session.runtime_id,
                machine_id=_binding_machine_id(session, agent_session),
                requested_by=(requested_by or None),
                attempt=0,
                result=result,
                error=error,
                created_at=now,
                updated_at=now,
                completed_at=now if status in TERMINAL_STATUSES else None,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                # Either a concurrent enqueue took this sequence, or a
                # concurrent retry of the same operation won the key. Both are
                # resolved by looking again.
                continue
            session.refresh(row)
            command = _to_dict(row)
            created = True
        break
    else:  # pragma: no cover - eight consecutive lost races
        raise SessionCommandError("could not allocate a command sequence for the session")

    append_event(
        "session_command_requested",
        f"{kind} queued for session {session_id}",
        session_id=session_id,
        metadata={
            "command_id": command["command_id"],
            "kind": kind,
            "status": command["status"],
            "result": command["result"],
            "operation_key": key,
        },
    )
    _publish(command, "session.command")
    return command, created


def _initial_state(agent_session: AgentSession, kind: str) -> tuple[str, str | None, str | None]:
    """The state a freshly recorded command starts in.

    A command that cannot possibly be delivered is settled at creation rather
    than queued: leaving it ``requested`` would show an operator a pending
    action that no consumer will ever pick up.
    """
    from brains.exec import session_channel

    ended = agent_session.ended_at is not None
    if kind == KIND_STOP:
        if ended:
            return STATUS_ACKNOWLEDGED, RESULT_ALREADY_TERMINAL, None
        return STATUS_REQUESTED, None, None
    if ended:
        return (
            STATUS_FAILED,
            RESULT_SESSION_ENDED,
            "the Session had already ended when the message was submitted",
        )
    capability = session_channel.message_capability(agent_session.tool)
    if not capability["supported"]:
        return STATUS_FAILED, RESULT_UNSUPPORTED, capability["reason"]
    return STATUS_REQUESTED, None, None


def get(command_id: str) -> dict[str, Any] | None:
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(SessionCommand)
            .filter(SessionCommand.command_id == command_id)
            .one_or_none()
        )
        return _to_dict(row) if row is not None else None


def list_for_session(session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """The durable command history for one Session, oldest first.

    This is what a reloaded console renders: every message and stop that was
    accepted, in order, with the outcome each one reached.
    """
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(SessionCommand)
            .filter(SessionCommand.session_id == session_id)
            .order_by(SessionCommand.sequence.asc())
            .limit(limit)
            .all()
        )
        return [_to_dict(row) for row in rows]


def list_open_for_consumer(
    *,
    runtime_id: int | None = None,
    machine_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Claimable commands for one consumer.

    Ownership, not co-location, decides what is listed, and the two consumers
    identify themselves differently because they *are* different:

    * a Runtime consumer (``runtime_id`` given) sees exactly the commands of
      the Sessions bound to it - no more, because a second worker on the same
      box, a Session the operator started from the CLI and the hub's own
      console Session all hold their own process handles; and no fewer,
      because the Runtime binding is the ownership fact. The machine stamp is
      not consulted at all: it is written when the row is created, and for a
      spawn that is the hub's machine rather than the Runtime's, so filtering
      on it would hide an operator's stop from the only process that can
      deliver it (see :func:`owned_by`).
    * the local consumer (no ``runtime_id``) sees the mirror image - commands
      of Sessions with *no* Runtime binding, on its own machine, because
      "this process launched it" is all the ownership there is to check.

    Expired leases are reclaimed first, so a consumer that crashed mid-flight
    does not hide its command from the consumer that replaces it.
    """
    expire_leases()
    init_db()
    with SessionLocal() as session:
        query = session.query(SessionCommand).filter(SessionCommand.status == STATUS_REQUESTED)
        if runtime_id is not None:
            query = query.filter(SessionCommand.runtime_id == runtime_id)
        else:
            query = query.filter(SessionCommand.runtime_id.is_(None))
            if machine_id is not None:
                query = query.filter(SessionCommand.machine_id == machine_id)
        rows = query.order_by(SessionCommand.sequence.asc(), SessionCommand.id.asc()).limit(limit)
        return [_to_dict(row) for row in rows]


def claim(
    command_id: str,
    *,
    consumer: str,
    lease: int | None = None,
    runtime_id: int | None = None,
    machine_id: str | None = None,
) -> dict[str, Any] | None:
    """Claim one command for ``consumer``. ``None`` when somebody else won.

    The claim is a single conditional UPDATE off ``requested``, which is what
    makes "at most one active consumer" a property of the store rather than of
    a read-then-write window two daemons can both pass.

    ``runtime_id``/``machine_id`` describe *which* consumer is asking, and a
    consumer that does not own the command is refused with
    :class:`NotOwnedError` before the UPDATE rather than allowed to take a
    lease it can only fail.
    """
    init_db()
    now = utc_now()
    expires = now + timedelta(seconds=lease or lease_seconds())
    with SessionLocal() as session:
        row = (
            session.query(SessionCommand)
            .filter(SessionCommand.command_id == command_id)
            .one_or_none()
        )
        if row is None:
            raise SessionCommandError(f"unknown session command: {command_id}")
        assert_owned(_to_dict(row), runtime_id=runtime_id, machine_id=machine_id)
        attempt = row.attempt + 1
        updated = (
            session.query(SessionCommand)
            .filter(
                SessionCommand.command_id == command_id,
                SessionCommand.status == STATUS_REQUESTED,
            )
            .update(
                {
                    "status": STATUS_DELIVERED,
                    "claimed_by": consumer[:64],
                    "claimed_at": now,
                    "lease_expires_at": expires,
                    "delivered_at": now,
                    "attempt": attempt,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        session.commit()
        if not updated:
            return None
        session.expire_all()
        claimed = _to_dict(
            session.query(SessionCommand).filter(SessionCommand.command_id == command_id).one()
        )
    _publish(claimed, "session.command")
    return claimed


def settle(
    command_id: str,
    *,
    consumer: str | None,
    status: str,
    result: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Move a claimed command to a terminal state.

    Only the current lease holder may settle: after a lease expires and the
    command is claimed again, the previous holder's acknowledgement is stale
    and applying it would report an outcome for an attempt that was replaced.
    ``consumer=None`` is the hub settling its own command (cancellation,
    abandonment), which no lease governs.
    """
    if status not in TERMINAL_STATUSES:
        raise SessionCommandError(f"status must be one of {sorted(TERMINAL_STATUSES)}")
    init_db()
    now = utc_now()
    with SessionLocal() as session:
        row = (
            session.query(SessionCommand)
            .filter(SessionCommand.command_id == command_id)
            .one_or_none()
        )
        if row is None:
            raise SessionCommandError(f"unknown session command: {command_id}")
        if row.status in TERMINAL_STATUSES:
            # Already settled. A repeated acknowledgement of the same outcome
            # is the retry case and is answered with the recorded outcome.
            return _to_dict(row)
        if consumer is not None and row.claimed_by != consumer[:64]:
            raise SessionCommandError(
                f"session command {command_id} is not held by {consumer}; its lease was reassigned"
            )
        query = session.query(SessionCommand).filter(SessionCommand.command_id == command_id)
        if consumer is not None:
            query = query.filter(
                SessionCommand.status == STATUS_DELIVERED,
                SessionCommand.claimed_by == consumer[:64],
            )
        else:
            query = query.filter(SessionCommand.status.in_(sorted(OPEN_STATUSES)))
        updated = query.update(
            {
                "status": status,
                "result": result,
                "error": error,
                "completed_at": now,
                "updated_at": now,
                "lease_expires_at": None,
            },
            synchronize_session=False,
        )
        session.commit()
        if not updated:  # pragma: no cover - lost to a concurrent settle
            session.expire_all()
            return _to_dict(
                session.query(SessionCommand).filter(SessionCommand.command_id == command_id).one()
            )
        session.expire_all()
        settled = _to_dict(
            session.query(SessionCommand).filter(SessionCommand.command_id == command_id).one()
        )
    append_event(
        "session_command_settled",
        f"{settled['kind']} {status} for session {settled['session_id']}"
        + (f" ({result})" if result else ""),
        session_id=settled["session_id"],
        metadata={
            "command_id": command_id,
            "kind": settled["kind"],
            "status": status,
            "result": result,
        },
        renew_session=result
        not in {RESULT_SESSION_DORMANT, RESULT_SESSION_ENDED, RESULT_SUPERSEDED},
    )
    _synchronize_terminal_state(settled)
    _publish(settled, "session.command")
    return settled


#: Stop outcomes that prove the agent process is gone. Anything else - a
#: Runtime that no longer owns the process, a write that failed - is *not*
#: evidence the Session ended, and must not stamp a terminal state.
_STOP_PROVES_TERMINAL = frozenset({"stopped", "already_exited"})


def _synchronize_terminal_state(command: dict[str, Any]) -> None:
    """Bring the Session terminal after a stop that actually stopped something.

    Only an acknowledged stop whose result proves the process is gone counts.
    A ``not_owned`` stop means the Runtime could not reach the process, and
    recording the Session as ended on that basis would be exactly the
    hub/local divergence BL-P0-05 exists to remove.
    """
    if command.get("kind") != KIND_STOP:
        return
    if command.get("status") != STATUS_ACKNOWLEDGED:
        return
    if command.get("result") not in _STOP_PROVES_TERMINAL:
        return
    try:
        from brains.control import sessions as sessions_ctl

        sessions_ctl.finalize_session(
            command["session_id"],
            state="failed",
            summary=(
                f"stopped by operator request ({command.get('command_id')}); "
                f"the agent process was {command.get('result')}"
            ),
        )
    except Exception:  # pragma: no cover - terminal sync must not fail an ack
        pass


def acknowledge(
    command_id: str,
    *,
    consumer: str | None,
    result: str,
    error: str | None = None,
    ok: bool = True,
) -> dict[str, Any]:
    """Settle a command with the outcome its consumer actually observed."""
    return settle(
        command_id,
        consumer=consumer,
        status=STATUS_ACKNOWLEDGED if ok else STATUS_FAILED,
        result=result,
        error=error,
    )


def release(command_id: str, *, consumer: str, reason: str | None = None) -> dict[str, Any] | None:
    """Hand a claimed command back without settling it.

    A consumer that is shutting down releases what it holds so the command is
    re-claimable immediately rather than after its lease runs out. It is also
    the honest answer to "I am not the owner of this": a worker that finds
    itself holding another consumer's command must requeue it, because
    settling it ``failed``/``not_owned`` would report an outcome for a
    delivery its owner never got to attempt.

    Only the current holder can release, so a stale holder cannot reopen an
    attempt that has already been reassigned.
    """
    init_db()
    now = utc_now()
    with SessionLocal() as session:
        updated = (
            session.query(SessionCommand)
            .filter(
                SessionCommand.command_id == command_id,
                SessionCommand.status == STATUS_DELIVERED,
                SessionCommand.claimed_by == consumer[:64],
            )
            .update(
                {
                    "status": STATUS_REQUESTED,
                    "claimed_by": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                    "delivered_at": None,
                    "error": reason,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        session.commit()
        if not updated:
            return None
        session.expire_all()
        return _to_dict(
            session.query(SessionCommand).filter(SessionCommand.command_id == command_id).one()
        )


def expire_leases(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return abandoned in-flight commands to the queue.

    A consumer that crashed between claiming and acknowledging holds a lease
    nobody will release. When it expires the command becomes claimable again -
    unless it has already used its attempts, in which case it is settled
    ``failed`` rather than retried forever.

    The requeue is a conditional UPDATE on the exact row *and* the exact lease
    it read, for the same reason the claim is: the lease holder may be
    acknowledging right now, and a read-modify-write would clobber that
    settled outcome and hand the command to a second consumer - a second
    ``terminate()``, or the operator's prompt written to the agent twice.
    """
    init_db()
    moment = now or utc_now()
    limit = max_attempts()
    requeued: list[str] = []
    abandoned: list[str] = []
    with SessionLocal() as session:
        candidates = [
            (row.command_id, row.claimed_by, row.lease_expires_at, row.attempt)
            for row in session.query(SessionCommand)
            .filter(SessionCommand.status == STATUS_DELIVERED)
            .filter(SessionCommand.lease_expires_at.isnot(None))
            .all()
        ]
        for command_id, claimed_by, lease_expires_at, attempt in candidates:
            expires = _aware(lease_expires_at)
            if expires is None or expires > moment:
                continue
            if attempt >= limit:
                abandoned.append(command_id)
                continue
            updated = (
                session.query(SessionCommand)
                .filter(
                    SessionCommand.command_id == command_id,
                    SessionCommand.status == STATUS_DELIVERED,
                    SessionCommand.claimed_by == claimed_by,
                    SessionCommand.lease_expires_at == lease_expires_at,
                )
                .update(
                    {
                        "status": STATUS_REQUESTED,
                        "claimed_by": None,
                        "claimed_at": None,
                        "lease_expires_at": None,
                        "delivered_at": None,
                        "result": None,
                        "completed_at": None,
                        "error": f"lease expired after attempt {attempt}; requeued",
                        "updated_at": moment,
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            if updated:
                requeued.append(command_id)
    out: list[dict[str, Any]] = []
    for command_id in requeued:
        command = get(command_id)
        if command is not None:
            out.append(command)
            _publish(command, "session.command")
    for command_id in abandoned:
        out.append(
            settle(
                command_id,
                consumer=None,
                status=STATUS_FAILED,
                result=RESULT_ABANDONED,
                error=(
                    f"no consumer completed this command in {limit} attempts; "
                    "the Runtime is not delivering it"
                ),
            )
        )
    return out


def cancel_open_for_session(
    session_id: str,
    *,
    reason: str,
    result: str = RESULT_SESSION_ENDED,
) -> list[dict[str, Any]]:
    """Cancel every still-open command for a Session that has ended.

    A Session that finished naturally cannot receive anything, so leaving its
    queue pending would show an operator work that will never happen.
    """
    init_db()
    with SessionLocal() as session:
        ids = [
            row.command_id
            for row in session.query(SessionCommand)
            .filter(SessionCommand.session_id == session_id)
            .filter(SessionCommand.status.in_(sorted(OPEN_STATUSES)))
            .all()
        ]
    return [
        settle(
            command_id,
            consumer=None,
            status=STATUS_CANCELLED,
            result=result,
            error=reason,
        )
        for command_id in ids
    ]


def _publish(command: dict[str, Any], event_type: str) -> None:
    """Announce a command on the Session's chat stream, exactly once per state.

    The dedupe key is the command id and the state it reached, so a mutation
    retried by a client - or reacted to by two processes - is one durable
    event with one ``event_id`` and one delivery, which is the publisher-level
    idempotency the realtime log's unique key can only enforce when a
    publisher supplies a stable key.
    """
    try:
        from brains.api.realtime_publish import publish_session_command

        publish_session_command(command, event_type=event_type)
    except Exception:  # pragma: no cover - realtime is best effort
        pass


__all__ = [
    "CONSUMER_LOCAL",
    "CONSUMER_RUNTIME",
    "KINDS",
    "KIND_MESSAGE",
    "KIND_STOP",
    "MAX_MESSAGE_CHARS",
    "OPEN_STATUSES",
    "RESULT_ABANDONED",
    "RESULT_ALREADY_TERMINAL",
    "RESULT_SESSION_DORMANT",
    "RESULT_SESSION_ENDED",
    "RESULT_STOPPED",
    "RESULT_SUPERSEDED",
    "RESULT_UNSUPPORTED",
    "STATUSES",
    "STATUS_ACKNOWLEDGED",
    "STATUS_CANCELLED",
    "STATUS_DELIVERED",
    "STATUS_FAILED",
    "STATUS_REQUESTED",
    "TERMINAL_STATUSES",
    "NotOwnedError",
    "SessionCommandError",
    "UnknownSessionError",
    "acknowledge",
    "assert_owned",
    "cancel_open_for_session",
    "claim",
    "default_stop_key",
    "enqueue",
    "expire_leases",
    "get",
    "lease_seconds",
    "list_for_session",
    "list_open_for_consumer",
    "max_attempts",
    "operation_key_for",
    "owned_by",
    "owner_of",
    "release",
    "settle",
]

"""The canonical governed-action contract (B4, F10, BL-P0-04).

Every Brains path that can produce an outward effect - the PATH-shim
action gate, an in-process subprocess spawn, a recurring/autopilot fire,
a CLI/MCP outward tool - goes through this module, and nothing else is
allowed to decide on its own that an action is safe.

What one governed action guarantees
-----------------------------------

1. **Nothing runs before it is recorded.** The request row and its audit
   entry are written in one transaction. If the audit append fails the
   transaction rolls back and the action is refused - there is no
   "carried on without a record" path.
2. **Nothing outward runs without a recorded decision.** An outward-tier
   action reaches :data:`STATUS_AUTHORIZED` only by consuming an approval
   that is *resolved*, *unexpired*, and *scope-matched* to the exact
   normalised arguments that were reviewed. The approval queue is the
   contract; binding the *resolver's* identity - so the same Session that
   tripped the gate cannot resolve its own ASK - depends on HTTP/MCP identity
   and is BL-P0-01, not something this module can claim today.
3. **An approval is spent once.** Consumption is a conditional update
   guarded by ``UNIQUE(governed_actions.approval_code)``, so two
   processes racing the same approval cannot both win, and a replayed
   approval cannot authorise a second action.
4. **A retry does not duplicate the effect or the decision.** Reusing an
   ``idempotency_key`` returns the recorded outcome of the first attempt
   and appends an observation, never a second decision.
5. **The record says what actually happened.** Actor, Org/Workspace/
   Issue/Session target, action and tool, the normalised-argument digest
   (never the arguments), tier, decision, approval code, attempt,
   result, error, and timestamps all land in the hash-chained log. Where
   an outcome is genuinely unobservable - ``os.execv`` replaces the
   process that would have reported it - the record says *that*
   (:data:`STATUS_RELEASED` via :func:`release_for_handoff`) instead of
   inventing a success or letting the stale sweep invent a failure.
6. **A running action is not a stale one.** An execution proves it is alive
   by renewing its lease (:func:`heartbeat`, :class:`ExecutionLease`), so a
   governed agent session or deploy that runs for hours is never settled as
   abandoned while it is still running, and an owner that died stops renewing
   and is settled once the silence budget is spent.

What this module is not
-----------------------

This is an **in-process** boundary. It governs every effect Brains itself
launches. It cannot intercept a subprocess that a third-party agent CLI
spawns behind Brains' back - that is a process/network sandbox problem
tracked as BL-P0-03, and :mod:`brains.exec.gate` documents exactly how
far the cooperative PATH-shim boundary reaches. Where an execution shape
cannot be governed in-process (``shell=True``, a string command line,
``os.system``), the boundary refuses it instead of pretending to cover
it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from brains.audit import AuditWriteError, append_in_session
from brains.govern.redaction import (
    SECRET_NAME_PARTS,
    is_secret_name,
    redact_argv,
    redact_command_text,
    redact_mapping,
)
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import GovernedAction

# --- vocabulary -----------------------------------------------------------

TIER_LOCAL = "local"
"""Read/propose work that stays inside the workspace: allowed, still recorded."""

TIER_OUTWARD = "outward"
"""Crosses out of the workspace (push, deploy, remote host, money, network)."""

TIER_UNSUPPORTED = "unsupported"
"""An execution shape this boundary cannot govern; always denied."""

STATUS_REQUESTED = "requested"
STATUS_PENDING = "pending"
STATUS_AUTHORIZED = "authorized"
STATUS_EXECUTING = "executing"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_RELEASED = "released"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"

#: A status nothing will ever transition out of. ``released`` belongs here for
#: the same reason as the rest: the process that owned the action handed the
#: authorisation to a replacement image and no longer exists to report an
#: outcome, so no later transition is coming and the stale sweep must leave the
#: row alone rather than rewrite a successful handoff as a failure.
TERMINAL_STATUSES = frozenset(
    {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_RELEASED, STATUS_DENIED, STATUS_EXPIRED}
)

DECISION_ALLOW = "allow"
DECISION_REQUIRE_APPROVAL = "require_approval"
DECISION_APPROVED = "approved"
DECISION_DENIED = "denied"
DECISION_EXPIRED = "expired"
DECISION_SCOPE_MISMATCH = "scope_mismatch"
DECISION_UNSUPPORTED = "unsupported"

#: How long a human decision stays spendable. An approval reviewed hours ago
#: is not evidence that the action is still wanted, so consumption refuses it.
APPROVAL_TTL_ENV = "BRAINS_APPROVAL_TTL_SECONDS"
DEFAULT_APPROVAL_TTL_SECONDS = 900

#: How long an *executing* attempt may go without proof of life before the
#: sweep may settle it. This is a silence budget, not a runtime budget: an
#: agent session or a deploy that runs for hours stays alive by renewing its
#: lease, while a process that died stops renewing and is settled once the
#: budget is spent.
EXECUTION_LEASE_ENV = "BRAINS_EXECUTION_LEASE_SECONDS"

#: How often the owner of an executing attempt renews its lease. Defaults to a
#: third of the lease, so a renewal can be missed twice before anything is
#: settled, and is clamped below half the lease: a heartbeat slower than the
#: lease it renews would guarantee the sweep it exists to prevent.
EXECUTION_HEARTBEAT_ENV = "BRAINS_EXECUTION_HEARTBEAT_SECONDS"
_HEARTBEAT_LEASE_FRACTION = 3
_MIN_HEARTBEAT_SECONDS = 0.05

#: How long :meth:`ExecutionLease.stop` waits for its beater to notice. The
#: thread only ever sleeps on an event, so this is a bound on a lost wakeup,
#: not on the interval.
_LEASE_JOIN_TIMEOUT_SECONDS = 5.0

_LOG = logging.getLogger("brains.govern")

_TRANSACT_ATTEMPTS = 6
_TRANSACT_BACKOFF_SECONDS = 0.05

#: Argument names whose *value* is redacted before hashing, so the digest
#: identifies the reviewed command without ever depending on a secret. The
#: canonical rules (URL credentials, header values, ``curl -u``, request
#: bodies, known token shapes) live in :mod:`brains.govern.redaction`.
_SECRET_MARKERS = SECRET_NAME_PARTS


class GovernanceError(RuntimeError):
    """Base class for every refusal this boundary raises."""


class ApprovalRequiredError(GovernanceError):
    """An outward action has no spendable approval (yet)."""


class ApprovalDeniedError(GovernanceError):
    """The operator resolved the approval as a refusal."""


class ApprovalExpiredError(GovernanceError):
    """The approval exists but is older than the configured TTL."""


class ApprovalScopeMismatchError(GovernanceError):
    """The approval was granted for a different action, tool, target or args."""


class DuplicateActionError(GovernanceError):
    """The idempotency key names an action that is still in flight."""


class UnsupportedExecutionPathError(GovernanceError):
    """The requested execution shape cannot be governed, so it is refused."""


class ApprovalCodeCollisionError(GovernanceError):
    """An approval code is already bound to a different governed action.

    ``UNIQUE(governed_actions.approval_code)`` is what makes an approval
    single-use, so a violation is a real refusal - but it is a *minting*
    failure, not an audit failure. Naming it here keeps the collision from
    reaching an operator disguised as :class:`~brains.audit.AuditWriteError`,
    which would send them to verify a chain that is perfectly intact.
    """


# --- request/result shapes -------------------------------------------------


@dataclass(frozen=True)
class ActionTarget:
    """What the action acts on. Every field is optional but recorded."""

    org_id: int | None = None
    workspace_id: int | None = None
    issue_code: str | None = None
    session_id: str | None = None
    workspace_path: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "workspace_id": self.workspace_id,
            "issue_code": self.issue_code,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class GovernedRequest:
    """One action a caller wants to take, before any decision is made.

    The summary is redacted on construction rather than at each sink: it is
    the field that reaches the stored row, the audit payload, the ASK body and
    the bridge notification, and a secret that survived into any one of them
    would be persisted in a log that is deliberately hard to rewrite. It is
    redacted with the *tool's* flag grammar, so a caller that hands over a raw
    command line (``docker login -p hunter2``) loses the password too, not only
    the shapes that look secret on their own.
    """

    actor: str
    action: str
    tool: str
    args: Sequence[str] | Mapping[str, Any] = field(default_factory=tuple)
    target: ActionTarget = field(default_factory=ActionTarget)
    tier: str = TIER_LOCAL
    summary: str = ""
    cwd: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", redact_command_text(self.summary or "", tool=self.tool))

    def digest(self) -> str:
        return args_digest(self.action, self.tool, self.args)

    def scope(self) -> dict[str, Any]:
        """The identity an approval is bound to."""
        return {
            "action": self.action,
            "tool": self.tool,
            "tier": self.tier,
            "args_hash": self.digest(),
            "target": self.target.to_payload(),
        }


@dataclass
class Authorization:
    """The outcome of the decision phase; ``allowed`` gates the effect."""

    action_id: str
    allowed: bool
    status: str
    decision: str
    tier: str
    approval_code: str | None = None
    reason: str = ""
    replayed: bool = False
    prior_result: str | None = None
    prior_error: str | None = None


@dataclass(frozen=True)
class EffectOutcome:
    """How an executed effect actually ended, as observed by its caller.

    ``run_governed`` cannot inspect an arbitrary effect's return value, so a
    caller that *can* observe the outcome (a subprocess exit status, say)
    supplies this instead of letting "it returned without raising" stand in
    for "it succeeded".
    """

    ok: bool
    result: str | None = None
    error: str | None = None


@dataclass
class GovernedResult:
    """The outcome of a full :func:`run_governed` call."""

    action_id: str
    allowed: bool
    status: str
    decision: str
    tier: str
    approval_code: str | None = None
    reason: str = ""
    replayed: bool = False
    value: Any = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "allowed": self.allowed,
            "status": self.status,
            "decision": self.decision,
            "tier": self.tier,
            "approval_code": self.approval_code,
            "reason": self.reason,
            "replayed": self.replayed,
            "error": self.error,
        }


# --- normalisation ---------------------------------------------------------


def _looks_secret(token: str) -> bool:
    return is_secret_name(token)


def normalize_args(
    args: Sequence[str] | Mapping[str, Any] | None, tool: str | None = None
) -> list[str]:
    """Return a stable argument vector with every credential shape redacted.

    ``--token=abc``, ``--token abc``, ``GITHUB_TOKEN=abc``,
    ``https://user:abc@host``, ``curl -u me:abc`` and
    ``-H "Authorization: Bearer abc"`` all normalise to a redacted form, so the
    digest identifies *the command that was reviewed* without the audit log,
    the approval record or the ASK body ever carrying the secret. ``tool``
    scopes the flags whose meaning depends on the binary (``curl -u`` is a
    credential, ``python -u`` is not).
    """
    if args is None:
        return []
    if isinstance(args, Mapping):
        return redact_mapping(dict(args))
    return redact_argv(args, tool=tool)


def args_digest(action: str, tool: str, args: Sequence[str] | Mapping[str, Any] | None) -> str:
    """SHA-256 over the canonical ``(action, tool, normalised args)`` triple."""
    canonical = json.dumps(
        {"action": action, "tool": tool, "args": normalize_args(args, tool)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approval_ttl_seconds() -> int:
    raw = os.environ.get(APPROVAL_TTL_ENV)
    if not raw:
        return DEFAULT_APPROVAL_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_APPROVAL_TTL_SECONDS
    return value if value > 0 else DEFAULT_APPROVAL_TTL_SECONDS


# --- transaction helpers ---------------------------------------------------


def _retryable_cause(exc: BaseException) -> BaseException | None:
    """The lock-contention error behind ``exc``, if there is one.

    ``append_in_session`` normalises every failure into
    :class:`~brains.audit.AuditWriteError`, so contention raised *inside* the
    append would otherwise look permanent while the identical error raised by
    ``commit()`` was retried. A corrupt chain is never retryable: retrying it
    would only re-refuse.
    """
    from brains.audit import AuditChainCorruptError

    if isinstance(exc, AuditChainCorruptError):
        return None
    cause = exc.__cause__ if isinstance(exc, AuditWriteError) else exc
    if isinstance(cause, OperationalError | IntegrityError):
        return cause
    return None


def _transact(work: Callable[[Any], Any]) -> Any:
    """Run ``work(session)`` in one transaction, retrying lock contention.

    Only lock contention is retried, and only before anything was committed, so
    a retry can never re-apply an effect or a decision: the effect runs outside
    this helper and every transaction here is all-or-nothing. A genuine
    :class:`~brains.audit.AuditWriteError` propagates so the caller fails closed
    with the transaction rolled back.
    """
    last: BaseException | None = None
    for attempt in range(_TRANSACT_ATTEMPTS):
        session = SessionLocal()
        try:
            result = work(session)
            session.commit()
            return result
        except BaseException as exc:
            session.rollback()
            if _retryable_cause(exc) is None or attempt == _TRANSACT_ATTEMPTS - 1:
                raise
            last = exc
            time.sleep(_TRANSACT_BACKOFF_SECONDS * (2**attempt) * (0.5 + random.random()))  # noqa: S311
        finally:
            session.close()
    raise GovernanceError(f"governed transaction failed after retries: {last}")


def _snapshot(row: GovernedAction) -> dict[str, Any]:
    return {
        "id": row.id,
        "action_id": row.action_id,
        "idempotency_key": row.idempotency_key,
        "actor": row.actor,
        "action": row.action,
        "tool": row.tool,
        "args_hash": row.args_hash,
        "tier": row.tier,
        "status": row.status,
        "decision": row.decision,
        "approval_code": row.approval_code,
        "approval_expires_at": (
            row.approval_expires_at.isoformat() if row.approval_expires_at else None
        ),
        "org_id": row.org_id,
        "workspace_id": row.workspace_id,
        "issue_code": row.issue_code,
        "session_id": row.session_id,
        "attempt": row.attempt,
        "attempt_started_at": (
            row.attempt_started_at.isoformat() if row.attempt_started_at else None
        ),
        "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        "result": row.result,
        "error": row.error,
        "summary": row.summary,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "authorized_at": row.authorized_at.isoformat() if row.authorized_at else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def _audit_payload(request: GovernedRequest, action_id: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action_id": action_id,
        "governed_action": request.action,
        "tool": request.tool,
        "tier": request.tier,
        "args_hash": request.digest(),
        "target": request.target.to_payload(),
        "summary": (request.summary or "")[:500],
        "cwd": request.cwd,
    }
    payload.update(extra)
    # Reasons, errors and operator text reach the chain through here too, and a
    # subprocess error that echoes its own command line is exactly the shape
    # that carries a token - including one that only a flag identifies
    # (``docker login -p``). Redaction is canonical, so the tool-aware command
    # rules are applied to every string that lands in the payload, not only to
    # the summary.
    return {
        key: redact_command_text(value, tool=request.tool) if isinstance(value, str) else value
        for key, value in payload.items()
    }


def _row(session: Any, action_id: str) -> GovernedAction:
    return session.execute(
        select(GovernedAction).where(GovernedAction.action_id == action_id)
    ).scalar_one()


def _bind_approval_code(session: Any, row: GovernedAction, code: str) -> None:
    """Bind ``code`` to ``row``, flushing so a collision names its own cause.

    Assigning the attribute and letting the *next* statement flush it means the
    unique-constraint violation surfaces from inside
    :func:`~brains.audit.append_in_session`, which normalises every failure into
    :class:`~brains.audit.AuditWriteError` - so a duplicated approval code would
    be reported as a broken audit chain. Flushing the binding on its own turns
    that into :class:`ApprovalCodeCollisionError`, and because it is not an
    ``IntegrityError`` the transaction helper fails closed immediately instead
    of retrying a collision that cannot resolve itself.
    """
    if row.approval_code == code:
        return
    row.approval_code = code
    try:
        session.flush()
    except IntegrityError as exc:
        raise ApprovalCodeCollisionError(
            f"approval code {code!r} is already bound to another governed action; "
            "the approval was not spent"
        ) from exc


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- phase 1: reserve ------------------------------------------------------


def reserve(request: GovernedRequest) -> tuple[dict[str, Any], bool]:
    """Record the request (and its audit entry) before anything happens.

    Returns ``(row_snapshot, replayed)``. ``replayed`` is true when the
    idempotency key already named a *terminal* action: the caller must
    return that outcome instead of executing again.

    An in-flight row is refused. An *abandoned* one - older than the attempt
    lease, because the process that owned it died - is settled first: a row
    that never reached the effect starts a fresh attempt, while one abandoned
    while executing is settled ``failed`` and still refused, because whether
    its effect happened is exactly what nobody knows.
    """
    init_db()
    key = request.idempotency_key or f"auto:{uuid.uuid4().hex}"
    action_id = f"ga_{uuid.uuid4().hex[:16]}"

    def _work(session: Any) -> tuple[dict[str, Any], bool, str | None]:
        existing = _locked_by_key(session, key)
        if existing is not None:
            if existing.status in TERMINAL_STATUSES:
                append_in_session(
                    session,
                    actor=request.actor,
                    action="governed.replayed",
                    payload=_audit_payload(
                        request,
                        existing.action_id,
                        idempotency_key=key,
                        replayed_status=existing.status,
                        attempt=existing.attempt,
                    ),
                    workspace_id=existing.workspace_id,
                )
                return _snapshot(existing), True, None
            return _resume_or_refuse(session, request, existing, key)
        now = datetime.now(UTC)
        row = GovernedAction(
            action_id=action_id,
            idempotency_key=key,
            actor=request.actor or "anonymous",
            action=request.action,
            tool=request.tool,
            args_hash=request.digest(),
            tier=request.tier,
            status=STATUS_REQUESTED,
            decision=None,
            org_id=request.target.org_id,
            workspace_id=request.target.workspace_id,
            issue_code=request.target.issue_code,
            session_id=request.target.session_id,
            attempt=1,
            attempt_started_at=now,
            summary=(request.summary or "")[:1000],
            created_at=now,
        )
        session.add(row)
        session.flush()
        entry = append_in_session(
            session,
            actor=request.actor,
            action="governed.requested",
            payload=_audit_payload(request, action_id, idempotency_key=key, attempt=1),
            workspace_id=request.target.workspace_id,
        )
        row.audit_request_id = entry.id
        session.flush()
        return _snapshot(row), False, None

    snapshot, replayed, refusal = _transact(_work)
    if refusal is not None:
        # Raised only after the settlement it describes is committed; raising
        # inside the transaction would roll the settlement back and leave the
        # row in-flight forever.
        raise DuplicateActionError(refusal)
    return snapshot, replayed


def attempt_lease_seconds() -> int:
    """How long an in-flight governed action may go unfinished before it is abandoned."""
    return max(approval_ttl_seconds(), 3600)


def execution_lease_seconds() -> int:
    """How long an *executing* attempt may go unheard from before it is abandoned.

    Distinct from :func:`attempt_lease_seconds` because the two answer
    different questions. An action waiting for a decision is judged by how long
    it has been waiting; an action that is *running* cannot be judged that way
    at all, because a legitimate execution has no upper bound - an agent
    session, a deploy, a Windows child the gate waits on. It is judged instead
    by silence: how long since its owner last renewed the lease
    (:func:`heartbeat`). Defaults to the attempt lease so a caller that renews
    nothing behaves exactly as before.
    """
    raw = os.environ.get(EXECUTION_LEASE_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return attempt_lease_seconds()


def heartbeat_seconds() -> float:
    """The interval between renewals for a live execution.

    Clamped below half the lease it renews: a heartbeat that is slower than
    the silence budget would let the sweep settle a live execution between two
    beats, which is the failure this contract exists to remove.
    """
    lease = float(execution_lease_seconds())
    ceiling = max(_MIN_HEARTBEAT_SECONDS, lease / 2)
    interval = lease / _HEARTBEAT_LEASE_FRACTION
    raw = os.environ.get(EXECUTION_HEARTBEAT_ENV)
    if raw:
        try:
            configured = float(raw)
        except ValueError:
            configured = 0.0
        if configured > 0:
            interval = configured
    return max(_MIN_HEARTBEAT_SECONDS, min(interval, ceiling))


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; compare in UTC either way."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _attempt_started(row: GovernedAction, default: datetime) -> datetime:
    """When the *current* attempt started.

    ``created_at`` is the fallback for rows written before the per-attempt
    timestamp existed; using it as the lease anchor is what let a reset attempt
    look abandoned the instant it was created.
    """
    return _aware(row.attempt_started_at) or _aware(row.created_at) or default


def _liveness_anchor(row: GovernedAction, default: datetime) -> datetime:
    """The most recent moment this row was demonstrably alive.

    For an executing row that is its last heartbeat (falling back to the moment
    the effect started, for a row written before heartbeats or by a caller that
    renews nothing). For everything else it is the start of the attempt, which
    is the only liveness evidence a row waiting for a decision has.
    """
    started = _attempt_started(row, default)
    if row.status != STATUS_EXECUTING:
        return started
    beat = _aware(row.heartbeat_at) or _aware(row.executed_at)
    return max(started, beat) if beat is not None else started


def _lease_expired(row: GovernedAction, moment: datetime) -> bool:
    """Whether ``row`` has gone unheard from for longer than its own lease."""
    budget = (
        execution_lease_seconds() if row.status == STATUS_EXECUTING else attempt_lease_seconds()
    )
    return (moment - _liveness_anchor(row, moment)).total_seconds() >= budget


def _locked_by_key(session: Any, key: str) -> GovernedAction | None:
    """Read the row for ``key``, holding it against a concurrent retry.

    Postgres takes the row lock; SQLite has none, so the settle/reset below is
    a conditional update whose row count decides the winner instead.
    """
    query = select(GovernedAction).where(GovernedAction.idempotency_key == key)
    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    return session.execute(query).scalar_one_or_none()


def _resume_or_refuse(
    session: Any,
    request: GovernedRequest,
    existing: GovernedAction,
    key: str,
) -> tuple[dict[str, Any], bool, str | None]:
    now = datetime.now(UTC)
    if not _lease_expired(existing, now):
        raise DuplicateActionError(
            f"governed action {existing.action_id} for idempotency key {key!r} is "
            f"still {existing.status}; a retry must not start a second attempt"
        )
    abandoned_mid_effect = existing.status == STATUS_EXECUTING
    reason = (
        "abandoned while executing; its execution lease expired without a "
        "heartbeat, so whether the effect happened is unknown"
        if abandoned_mid_effect
        else f"abandoned in {existing.status} before any effect"
    )
    observed_attempt = int(existing.attempt or 1)
    observed_status = existing.status
    # Claim the abandoned attempt conditionally: two retries that both saw the
    # same expired attempt cannot both settle it, so only one of them goes on
    # to open a new attempt. The loser re-reads a row whose attempt timestamp
    # is fresh and is refused as in-flight.
    claimed = session.execute(
        update(GovernedAction)
        .where(
            GovernedAction.id == existing.id,
            GovernedAction.attempt == observed_attempt,
            GovernedAction.status == observed_status,
        )
        .values(
            status=STATUS_FAILED,
            result=STATUS_FAILED,
            error=reason,
            completed_at=now,
            heartbeat_at=None,
        )
    ).rowcount
    if claimed != 1:
        raise DuplicateActionError(
            f"governed action {existing.action_id} for idempotency key {key!r} was "
            "claimed by a concurrent retry; this attempt must not start a second one"
        )
    session.expire(existing)
    append_in_session(
        session,
        actor=request.actor,
        action="governed.failed",
        payload=_audit_payload(
            request,
            existing.action_id,
            idempotency_key=key,
            attempt=observed_attempt,
            reason=reason,
        ),
        workspace_id=existing.workspace_id,
    )
    session.flush()
    if abandoned_mid_effect:
        return (
            _snapshot(existing),
            False,
            f"governed action {existing.action_id} for idempotency key {key!r} was "
            "abandoned while executing; retry under a new key once the effect is known",
        )
    # Nothing ran, so the same key may carry a fresh attempt. Any approval it
    # held is released rather than inherited: a new attempt needs a new
    # decision, and the attempt clock restarts with it - otherwise the next
    # retry would see an attempt that is born expired.
    existing.status = STATUS_REQUESTED
    existing.decision = None
    existing.approval_code = None
    existing.approval_expires_at = None
    existing.result = None
    existing.error = None
    existing.completed_at = None
    existing.attempt = observed_attempt + 1
    existing.attempt_started_at = datetime.now(UTC)
    existing.heartbeat_at = None
    entry = append_in_session(
        session,
        actor=request.actor,
        action="governed.requested",
        payload=_audit_payload(
            request,
            existing.action_id,
            idempotency_key=key,
            attempt=existing.attempt,
            retry_of="abandoned attempt",
        ),
        workspace_id=existing.workspace_id,
    )
    existing.audit_request_id = entry.id
    session.flush()
    return _snapshot(existing), False, None


# --- phase 2: decide -------------------------------------------------------


def _settle(
    request: GovernedRequest,
    action_id: str,
    *,
    status: str,
    decision: str,
    audit_action: str,
    reason: str = "",
    approval_code: str | None = None,
) -> None:
    """Record one decision transition and its audit entry atomically."""

    def _work(session: Any) -> None:
        row = _row(session, action_id)
        now = datetime.now(UTC)
        row.status = status
        row.decision = decision
        if approval_code is not None:
            _bind_approval_code(session, row, approval_code)
        if status == STATUS_AUTHORIZED:
            row.authorized_at = now
        if status in TERMINAL_STATUSES:
            row.completed_at = now
            row.result = row.result or STATUS_DENIED
            row.error = row.error or (redact_command_text(reason, tool=request.tool) or None)
        entry = append_in_session(
            session,
            actor=request.actor,
            action=audit_action,
            payload=_audit_payload(
                request,
                action_id,
                status=status,
                decision=decision,
                approval_code=approval_code or row.approval_code,
                reason=reason,
            ),
            workspace_id=row.workspace_id,
        )
        row.audit_decision_id = entry.id
        session.flush()

    _transact(_work)


def _register_workspace_id(workspace_path: str) -> int | None:
    from brains.control.sessions import register_workspace

    try:
        return register_workspace(workspace_path).id
    except Exception:  # noqa: BLE001 - an unattributable action is denied, not crashed
        return None


def _file_approval(
    request: GovernedRequest,
    action_id: str,
    expires_at: datetime,
    workspace_id: int,
) -> str:
    """File the ASK and its ``governed.approval_requested`` entry atomically."""
    from brains.control.decisions import create_request_in_session

    scope = request.scope()
    scope["expires_at"] = expires_at.isoformat()
    scope["action_id"] = action_id
    scope["kind"] = "governed_action"

    def _work(session: Any) -> str:
        code = create_request_in_session(
            session,
            workspace_id=workspace_id,
            title=f"[gate] approve outward action: {request.tool}",
            body=f"Command:\n  {request.summary}\n\nworking dir: {request.cwd or ''}",
            proposed_answer="approve",
            session_id=request.target.session_id,
            metadata=scope,
        )
        row = _row(session, action_id)
        row.status = STATUS_PENDING
        row.decision = DECISION_REQUIRE_APPROVAL
        row.approval_expires_at = expires_at
        if row.workspace_id is None:
            row.workspace_id = workspace_id
        entry = append_in_session(
            session,
            actor=request.actor,
            action="governed.approval_requested",
            payload=_audit_payload(
                request,
                action_id,
                approval_code=code,
                expires_at=expires_at.isoformat(),
            ),
            workspace_id=workspace_id,
        )
        row.audit_decision_id = entry.id
        session.flush()
        return code

    return _transact(_work)


def consume_approval(
    request: GovernedRequest,
    action_id: str,
    code: str,
    *,
    chosen: str = "approve",
    reasoning: str = "",
) -> Authorization:
    """Spend one approval atomically, or refuse and record why.

    Scope, expiry and single-use are all decided inside the same
    transaction as the audit append, so a refusal leaves the approval
    unspent and an authorisation can never exist without its record.
    """
    from brains.control.decisions import consume_resolved_decision, request_scope

    def _work(session: Any) -> Authorization:
        row = _row(session, action_id)
        now = datetime.now(UTC)
        scope = request_scope(session, code)
        failure: str | None = None
        decision = DECISION_APPROVED
        if scope is None:
            failure = "approval carries no governed-action scope"
            decision = DECISION_SCOPE_MISMATCH
        else:
            expected = request.scope()
            if {key: scope.get(key) for key in expected} != expected:
                failure = "approval was granted for a different action scope"
                decision = DECISION_SCOPE_MISMATCH
            else:
                expires_at = _parse_ts(scope.get("expires_at"))
                if expires_at is None or now > expires_at:
                    failure = f"approval expired at {scope.get('expires_at')}"
                    decision = DECISION_EXPIRED
        if failure is None and not consume_resolved_decision(session, code):
            failure = "approval was already consumed or is no longer resolved"
            decision = DECISION_SCOPE_MISMATCH
        if failure is None:
            row.status = STATUS_AUTHORIZED
            row.decision = DECISION_APPROVED
            _bind_approval_code(session, row, code)
            row.authorized_at = now
        else:
            row.status = STATUS_EXPIRED if decision == DECISION_EXPIRED else STATUS_DENIED
            row.decision = decision
            row.result = STATUS_DENIED
            row.error = failure
            row.completed_at = now
        entry = append_in_session(
            session,
            actor=request.actor,
            action="governed.authorized" if failure is None else "governed.denied",
            payload=_audit_payload(
                request,
                action_id,
                approval_code=code,
                decision=row.decision,
                chosen=chosen[:120],
                reason=(failure or reasoning or "")[:500],
                attempt=row.attempt,
            ),
            workspace_id=row.workspace_id,
        )
        row.audit_decision_id = entry.id
        session.flush()
        return Authorization(
            action_id=action_id,
            allowed=failure is None,
            status=row.status,
            decision=row.decision,
            tier=request.tier,
            approval_code=code,
            reason=failure or reasoning,
        )

    return _transact(_work)


def authorize(
    request: GovernedRequest,
    *,
    wait: bool = True,
    poll_seconds: float = 2.0,
    timeout_seconds: float | None = None,
    notify: bool = True,
) -> Authorization:
    """Decide one action: reserve it, then allow, gate, or refuse it.

    Local-tier work is allowed immediately (and still recorded). Outward
    work files an approval and, when ``wait`` is set, blocks until the
    operator resolves it or the approval TTL runs out. Nothing here
    executes anything: the caller runs the effect only when
    ``Authorization.allowed`` is true.
    """
    snapshot, replayed = reserve(request)
    if replayed:
        return _replay_authorization(snapshot)
    action_id = snapshot["action_id"]

    if request.tier == TIER_UNSUPPORTED:
        reason = request.summary or "unsupported execution path"
        _settle(
            request,
            action_id,
            status=STATUS_DENIED,
            decision=DECISION_UNSUPPORTED,
            audit_action="governed.denied",
            reason=reason,
        )
        return Authorization(
            action_id=action_id,
            allowed=False,
            status=STATUS_DENIED,
            decision=DECISION_UNSUPPORTED,
            tier=request.tier,
            reason="this execution shape cannot be governed in-process",
        )

    if request.tier != TIER_OUTWARD:
        _settle(
            request,
            action_id,
            status=STATUS_AUTHORIZED,
            decision=DECISION_ALLOW,
            audit_action="governed.allowed",
        )
        return Authorization(
            action_id=action_id,
            allowed=True,
            status=STATUS_AUTHORIZED,
            decision=DECISION_ALLOW,
            tier=request.tier,
        )

    ttl = approval_ttl_seconds()
    workspace_id = request.target.workspace_id
    if workspace_id is None and request.target.workspace_path:
        workspace_id = _register_workspace_id(request.target.workspace_path)
    if workspace_id is None:
        reason = "no Workspace could be attributed, so the approval has no home"
        _settle(
            request,
            action_id,
            status=STATUS_DENIED,
            decision=DECISION_UNSUPPORTED,
            audit_action="governed.denied",
            reason=reason,
        )
        return Authorization(
            action_id=action_id,
            allowed=False,
            status=STATUS_DENIED,
            decision=DECISION_UNSUPPORTED,
            tier=request.tier,
            reason=reason,
        )
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
    code = _file_approval(request, action_id, expires_at, workspace_id)
    if notify:
        _notify(code, request)
    if not wait:
        return Authorization(
            action_id=action_id,
            allowed=False,
            status=STATUS_PENDING,
            decision=DECISION_REQUIRE_APPROVAL,
            tier=request.tier,
            approval_code=code,
            reason="awaiting operator approval",
        )

    from brains.control.decisions import get_decision

    budget = timeout_seconds if timeout_seconds is not None else float(ttl)
    deadline = time.time() + budget
    chosen = ""
    reasoning = ""
    resolved: bool | None = None
    while time.time() < deadline:
        state = get_decision(code)
        if state and state["status"] != "open":
            chosen = state.get("chosen") or ""
            reasoning = state.get("reasoning") or ""
            resolved = state["status"] == "resolved" and chosen.strip().lower() not in {
                "deny",
                "reject",
                "no",
            }
            break
        time.sleep(poll_seconds)

    if resolved is None:
        _settle(
            request,
            action_id,
            status=STATUS_EXPIRED,
            decision=DECISION_EXPIRED,
            audit_action="governed.denied",
            reason="approval timed out",
            approval_code=code,
        )
        return Authorization(
            action_id=action_id,
            allowed=False,
            status=STATUS_EXPIRED,
            decision=DECISION_EXPIRED,
            tier=request.tier,
            approval_code=code,
            reason="approval timed out",
        )
    if not resolved:
        _settle(
            request,
            action_id,
            status=STATUS_DENIED,
            decision=DECISION_DENIED,
            audit_action="governed.denied",
            reason=reasoning or "rejected",
            approval_code=code,
        )
        return Authorization(
            action_id=action_id,
            allowed=False,
            status=STATUS_DENIED,
            decision=DECISION_DENIED,
            tier=request.tier,
            approval_code=code,
            reason=reasoning or "rejected",
        )
    return consume_approval(request, action_id, code, chosen=chosen, reasoning=reasoning)


def _replay_authorization(snapshot: dict[str, Any]) -> Authorization:
    return Authorization(
        action_id=snapshot["action_id"],
        allowed=False,
        status=snapshot["status"],
        decision=snapshot["decision"] or "",
        tier=snapshot["tier"],
        approval_code=snapshot["approval_code"],
        reason="replayed: this idempotency key already completed",
        replayed=True,
        prior_result=snapshot["result"],
        prior_error=snapshot["error"],
    )


def _notify(code: str, request: GovernedRequest) -> None:
    """Relay the pending approval to configured bridges.

    Best-effort by design and *only* for the approval-relay control path:
    failing to reach a phone must not authorise anything, and it does not -
    the action stays blocked either way.
    """
    try:
        from brains.exec.relay import notify_pending_approval

        notify_pending_approval(code, request.action, request.summary, request.cwd or "")
    except Exception:  # noqa: BLE001 - notification is not part of the decision
        return


# --- phase 3: execute ------------------------------------------------------


def mark_executing(request: GovernedRequest, action_id: str) -> int:
    """Record that the effect is about to run, before it runs.

    Used by callers that cannot report afterwards - the PATH-shim gate
    replaces its own process with ``execv`` - so the log still shows the
    effect was released.

    Returns the attempt number the row is now executing, which is half of what
    :func:`heartbeat` renews: a lease is only ever renewed for the attempt it
    was taken out on, so a stale owner cannot keep a *later* attempt alive.
    """

    def _work(session: Any) -> int:
        row = _row(session, action_id)
        row.status = STATUS_EXECUTING
        now = datetime.now(UTC)
        row.executed_at = now
        # The attempt is demonstrably alive at this instant, so its lease
        # restarts here: an action that waited most of the approval window
        # must not begin executing with an almost-expired lease and be swept
        # out from under itself.
        row.attempt_started_at = now
        # The first heartbeat is the transition itself. Without it a row that
        # is executing but never renewed would have no liveness anchor of its
        # own beyond ``executed_at``.
        row.heartbeat_at = now
        append_in_session(
            session,
            actor=request.actor,
            action="governed.executing",
            payload=_audit_payload(request, action_id, attempt=row.attempt),
            workspace_id=row.workspace_id,
        )
        session.flush()
        return int(row.attempt or 1)

    return _transact(_work)


@dataclass(frozen=True)
class HeartbeatResult:
    """One renewal attempt, and why it did or did not renew.

    ``renewed`` false is not an error: it is the row saying the caller no
    longer owns it - it completed, it failed, it was swept, or a later attempt
    took over - and the honest response is to stop beating, not to retry.
    """

    renewed: bool
    reason: str
    at: datetime | None = None


def heartbeat(action_id: str, *, attempt: int) -> HeartbeatResult:
    """Renew the execution lease of one attempt. Returns whether it renewed.

    The renewal is a single conditional update - ``action_id`` **and**
    ``status = executing`` **and** ``attempt`` - which is what makes it safe to
    call from any process, at any time, without reading first:

    * it cannot resurrect a terminal row, because a completed, failed, denied,
      expired or released row is no longer ``executing`` and the update matches
      nothing (a heartbeat racing :func:`complete` therefore loses, always);
    * it cannot keep a *newer* attempt alive on behalf of an older one, because
      the attempt is part of the predicate - a process that hung through a
      retry renews nothing;
    * two processes that both believe they own the attempt write the same
      column with the same meaning, so the later write simply wins.

    Nothing is appended to the audit log. A heartbeat is not a transition and
    carries no decision; recording one every few seconds would bury the entries
    that do mean something under proof-of-life noise. What the log keeps is the
    transitions - ``governed.executing`` and the outcome - and what the sweep
    reads is the column.

    Storage failures are *not* swallowed into a false renewal: they propagate,
    and :class:`ExecutionLease` records them rather than reporting health it
    cannot demonstrate.
    """
    expected = int(attempt)

    def _work(session: Any) -> HeartbeatResult:
        now = datetime.now(UTC)
        renewed = session.execute(
            update(GovernedAction)
            .where(
                GovernedAction.action_id == action_id,
                GovernedAction.status == STATUS_EXECUTING,
                GovernedAction.attempt == expected,
            )
            .values(heartbeat_at=now)
        ).rowcount
        if renewed == 1:
            return HeartbeatResult(renewed=True, reason="renewed", at=now)
        return HeartbeatResult(
            renewed=False,
            reason=(
                f"governed action {action_id} is no longer executing attempt {expected}; "
                "the lease is not renewable"
            ),
        )

    return _transact(_work)


class ExecutionLease:
    """Keeps one executing attempt's lease renewed while its effect runs.

    The problem it solves is that a *live* execution and an abandoned one look
    identical to the sweep: both are rows that have been ``executing`` for a
    long time. Rather than raise the deadline until it is meaningless, the
    owner proves it is alive on a timer, and the sweep judges silence.

    Properties this deliberately has:

    * **It stops promptly.** The beater sleeps on an
      :class:`threading.Event`, so :meth:`stop` wakes it immediately rather
      than waiting out an interval.
    * **It cannot keep a process alive.** The thread is a daemon and holds no
      resources, so an interpreter shutdown is never delayed by a lease that
      was not stopped.
    * **It never claims health it cannot demonstrate.** A storage failure
      increments :attr:`failures` and is logged; it is retried (contention is
      routine) but it is never reported as a renewal, and :attr:`healthy` is
      false until a real renewal lands.
    * **It gives up when the row does.** A renewal that matches no row means
      the attempt ended or moved on, so the beater stops instead of writing to
      a row it no longer owns.
    """

    def __init__(
        self,
        action_id: str,
        attempt: int,
        *,
        interval: float | None = None,
        beat: Callable[..., HeartbeatResult] | None = None,
    ) -> None:
        self.action_id = action_id
        self.attempt = int(attempt)
        self.beats = 0
        self.failures = 0
        self.lost = False
        self.last_error: str | None = None
        self.stopped_reason: str | None = None
        self._interval = interval
        self._beat = beat
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def interval(self) -> float:
        """Seconds between renewals; read live so a test can shorten the lease."""
        return float(self._interval) if self._interval is not None else heartbeat_seconds()

    @property
    def healthy(self) -> bool:
        """Whether this lease is still demonstrably covering its attempt.

        False the moment a renewal fails to reach the store or the row tells us
        it moved on - never "probably fine". A clean stop does not make it
        false: the execution it covered ended, and it covered it.
        """
        return self.failures == 0 and not self.lost

    def start(self) -> ExecutionLease:
        if self._thread is not None:
            return self
        thread = threading.Thread(
            target=self._loop,
            name=f"brains-govern-lease-{self.action_id}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return self

    def beat_once(self) -> bool:
        """Renew once. ``False`` means stop beating."""
        beat = self._beat or heartbeat
        try:
            outcome = beat(self.action_id, attempt=self.attempt)
        except Exception as exc:  # noqa: BLE001 - recorded, never reported as health
            with self._lock:
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                first = self.failures == 1
            if first:
                # Contention resolves itself and a lost beat costs nothing
                # while the lease still has budget, so keep trying - but say
                # so once, because a store that cannot be written is also a
                # store whose sweep may settle this action underneath us.
                _LOG.warning(
                    "governed execution heartbeat failed for %s attempt %s: %s",
                    self.action_id,
                    self.attempt,
                    self.last_error,
                )
            return not self._stop.is_set()
        if outcome.renewed:
            with self._lock:
                self.beats += 1
                self.failures = 0
                self.last_error = None
            return True
        self.stopped_reason = outcome.reason
        self.lost = True
        return False

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if not self.beat_once():
                return

    def stop(self, reason: str | None = None) -> None:
        """Stop beating and wait for the beater to notice. Idempotent."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_LEASE_JOIN_TIMEOUT_SECONDS)
        if reason and self.stopped_reason is None:
            self.stopped_reason = reason

    def __enter__(self) -> ExecutionLease:
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()


@contextmanager
def execution_lease(
    action_id: str,
    attempt: int,
    *,
    interval: float | None = None,
) -> Iterator[ExecutionLease]:
    """Hold an execution lease for the duration of the block.

    The lease is stopped on the way out - normal return, exception or
    generator close - *before* the caller records the outcome, so the last
    write to the row is the terminal one.
    """
    lease = ExecutionLease(action_id, attempt, interval=interval).start()
    try:
        yield lease
    finally:
        lease.stop("execution finished")


def release_for_handoff(request: GovernedRequest, action_id: str, *, handoff: str) -> None:
    """Record that the authorisation was released into a process replacement.

    ``os.execv`` does not return: the caller's process image *is* the real
    binary from the next instruction on, so this process can never write a
    later transition. The two dishonest options are to claim a completion
    nobody observed, or to leave the row ``executing`` and let the stale sweep
    call a released action "abandoned while executing" once its lease runs out.

    ``released`` says exactly what is known and no more: the decision was made,
    the authorisation was spent, and the real binary took over here. What it
    then did is outside this boundary's knowledge, and the record does not
    pretend otherwise - so the row is terminal, is never swept, and a retry of
    the same idempotency key replays it instead of executing again.

    Written *before* the handoff, because after it there is no "after". If the
    handoff itself fails the caller records the failure over it
    (:func:`complete`), which is the one transition that can still be observed.
    """

    def _work(session: Any) -> None:
        row = _row(session, action_id)
        now = datetime.now(UTC)
        row.status = STATUS_RELEASED
        row.result = STATUS_RELEASED
        row.executed_at = row.executed_at or now
        row.completed_at = now
        # Terminal: nothing owns this row any more, and a stale beat cannot
        # renew it because the status no longer matches.
        row.heartbeat_at = None
        entry = append_in_session(
            session,
            actor=request.actor,
            action="governed.released",
            payload=_audit_payload(
                request,
                action_id,
                attempt=row.attempt,
                handoff=handoff,
                result=STATUS_RELEASED,
                note=(
                    "authorisation released into a process replacement; the outcome of the "
                    "replacing process is not observable from here"
                ),
            ),
            workspace_id=row.workspace_id,
        )
        row.audit_result_id = entry.id
        session.flush()

    _transact(_work)


def complete(
    request: GovernedRequest,
    action_id: str,
    *,
    ok: bool,
    result: str | None = None,
    error: str | None = None,
) -> None:
    """Record the outcome of an executed effect. Raises if it cannot."""

    def _work(session: Any) -> None:
        row = _row(session, action_id)
        now = datetime.now(UTC)
        row.status = STATUS_SUCCEEDED if ok else STATUS_FAILED
        row.result = result or (STATUS_SUCCEEDED if ok else STATUS_FAILED)
        row.error = redact_command_text(error or "", tool=request.tool)[:2000] or None
        row.completed_at = now
        row.heartbeat_at = None
        entry = append_in_session(
            session,
            actor=request.actor,
            action="governed.succeeded" if ok else "governed.failed",
            payload=_audit_payload(
                request,
                action_id,
                attempt=row.attempt,
                result=row.result,
                error=row.error,
            ),
            workspace_id=row.workspace_id,
        )
        row.audit_result_id = entry.id
        session.flush()

    _transact(_work)


def run_governed(
    request: GovernedRequest,
    effect: Callable[[], Any],
    *,
    wait: bool = True,
    poll_seconds: float = 2.0,
    timeout_seconds: float | None = None,
    notify: bool = True,
    settle: Callable[[Any], EffectOutcome] | None = None,
) -> GovernedResult:
    """Authorise, execute, and record one action - or refuse it.

    ``effect`` runs only after an authorisation is committed. A failure to
    record the *result* raises :class:`~brains.audit.AuditWriteError` with
    the effect already taken: that is reported honestly rather than
    returned as success.

    ``settle`` maps the effect's return value to an :class:`EffectOutcome`
    for callers whose effect reports failure without raising - a subprocess
    run with ``check=False`` is the case that matters, since without it a
    command that exited non-zero would be recorded as ``succeeded``. The
    action is completed exactly once either way; when ``settle`` is omitted
    the recorded outcome remains "the effect returned".

    The effect runs under an :class:`ExecutionLease`, so an effect that takes
    longer than the sweep's silence budget - an agent session, a deploy - is
    not settled as abandoned while it is still running. The lease stops before
    the outcome is recorded, and could not overwrite it anyway.
    """
    decision = authorize(
        request,
        wait=wait,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        notify=notify,
    )
    if not decision.allowed:
        return GovernedResult(
            action_id=decision.action_id,
            allowed=False,
            status=decision.status,
            decision=decision.decision,
            tier=decision.tier,
            approval_code=decision.approval_code,
            reason=decision.reason,
            replayed=decision.replayed,
            error=decision.prior_error if decision.replayed else decision.reason,
        )

    attempt = mark_executing(request, decision.action_id)
    try:
        with execution_lease(decision.action_id, attempt):
            value = effect()
    except Exception as exc:
        complete(request, decision.action_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    outcome = EffectOutcome(ok=True) if settle is None else settle(value)
    complete(
        request,
        decision.action_id,
        ok=outcome.ok,
        result=outcome.result,
        error=outcome.error,
    )
    return GovernedResult(
        action_id=decision.action_id,
        allowed=True,
        status=STATUS_SUCCEEDED if outcome.ok else STATUS_FAILED,
        decision=decision.decision,
        tier=decision.tier,
        approval_code=decision.approval_code,
        value=value,
        error=outcome.error,
    )


# --- reporting -------------------------------------------------------------


def get_governed_action(action_id: str) -> dict[str, Any] | None:
    init_db()
    with SessionLocal() as session:
        row = session.execute(
            select(GovernedAction).where(GovernedAction.action_id == action_id)
        ).scalar_one_or_none()
        return _snapshot(row) if row is not None else None


def list_governed_actions(
    *,
    limit: int = 50,
    status: str | None = None,
    actor: str | None = None,
    action_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first governed actions, for ``brains-ai governed-list`` and MCP."""
    limit = max(1, min(int(limit), 500))
    init_db()
    with SessionLocal() as session:
        query = select(GovernedAction)
        if status:
            query = query.where(GovernedAction.status == status)
        if actor:
            query = query.where(GovernedAction.actor == actor)
        if action_prefix:
            query = query.where(GovernedAction.action.like(f"{action_prefix}%"))
        rows = (
            session.execute(query.order_by(GovernedAction.id.desc()).limit(limit)).scalars().all()
        )
        return [_snapshot(row) for row in rows]


def expire_stale_pending(now: datetime | None = None) -> int:
    """Settle governed actions that can no longer reach a decision or a result.

    Two shapes are swept, both fail-closed:

    * ``pending`` past its approval window - it can never be authorised, so
      leaving it looking approvable would be a lie;
    * any other non-terminal row that has gone unheard from for longer than its
      own lease - the process that owned it is gone, and an unbounded in-flight
      row would block its idempotency key forever.

    "Unheard from" means two different things, because the two states carry
    different evidence. A row waiting for a decision is measured from the start
    of its *current attempt* (``attempt_started_at``), so a retry that
    legitimately reset an attempt is not swept the moment it starts. A row that
    is **executing** is measured from its last heartbeat
    (``heartbeat_at``, :func:`heartbeat`) against
    :func:`execution_lease_seconds`, because how long an execution has been
    running says nothing about whether it is alive: a governed agent session or
    deploy may legitimately run for hours, and settling it on a fixed budget
    would fabricate a failure for work that is still happening - and burn its
    idempotency key. An owner that crashed stops renewing, so silence still
    settles it once the budget is spent, recorded as abandoned mid-effect
    rather than as a failure that definitely did nothing: whether its effect
    happened is exactly what is unknown.

    Each settlement is a conditional update guarded on the status and attempt
    that were read, so a row that moved on between the read and the write - a
    heartbeat is not a status change, but a completion is - is left alone
    instead of being overwritten under a live execution.

    A row that reached :data:`STATUS_RELEASED` is terminal and is never
    considered here. It is not an unfinished action: the process that owned it
    handed the authorisation to a replacement image and cannot report again, so
    sweeping it would turn a successful, correctly recorded handoff into a
    fabricated failure.
    """
    moment = now or datetime.now(UTC)
    init_db()

    def _work(session: Any) -> int:
        rows = (
            session.execute(
                select(GovernedAction).where(
                    GovernedAction.status.in_(
                        [
                            STATUS_REQUESTED,
                            STATUS_PENDING,
                            STATUS_AUTHORIZED,
                            STATUS_EXECUTING,
                        ]
                    )
                )
            )
            .scalars()
            .all()
        )
        swept = 0
        for row in rows:
            expires_at = _aware(row.approval_expires_at)
            window_closed = (
                row.status == STATUS_PENDING and expires_at is not None and expires_at < moment
            )
            lease_expired = _lease_expired(row, moment)
            if not (window_closed or lease_expired):
                continue
            if window_closed:
                status, decision = STATUS_EXPIRED, DECISION_EXPIRED
                reason = "approval window closed before a decision was made"
            elif row.status == STATUS_EXECUTING:
                status, decision = STATUS_FAILED, DECISION_APPROVED
                reason = (
                    "abandoned while executing; its execution lease expired without a "
                    "heartbeat, so whether the effect happened is unknown"
                )
            else:
                status, decision = STATUS_FAILED, row.decision or DECISION_REQUIRE_APPROVAL
                reason = f"abandoned in {row.status} before any effect"
            observed_status, observed_attempt = row.status, int(row.attempt or 1)
            settled = session.execute(
                update(GovernedAction)
                .where(
                    GovernedAction.id == row.id,
                    GovernedAction.status == observed_status,
                    GovernedAction.attempt == observed_attempt,
                )
                .values(
                    status=status,
                    decision=decision,
                    result=STATUS_DENIED if status == STATUS_EXPIRED else STATUS_FAILED,
                    error=reason,
                    completed_at=moment,
                    heartbeat_at=None,
                )
            ).rowcount
            if settled != 1:
                # The row advanced between the read and the write: it is alive
                # after all, and the process that owns it reports its outcome.
                continue
            session.expire(row)
            append_in_session(
                session,
                actor=row.actor,
                action="governed.denied" if status == STATUS_EXPIRED else "governed.failed",
                payload={
                    "action_id": row.action_id,
                    "governed_action": row.action,
                    "tool": row.tool,
                    "tier": row.tier,
                    "args_hash": row.args_hash,
                    "decision": decision,
                    "attempt": observed_attempt,
                    "reason": reason,
                    "swept_by": "maintenance",
                },
                workspace_id=row.workspace_id,
            )
            swept += 1
        return swept

    return _transact(_work)


def run_maintenance(now: datetime | None = None) -> dict[str, Any]:
    """The periodic owner of governed-action hygiene.

    Called by the recurring scheduler tick and by ``brains-ai governed-sweep``
    so the expiry rules are enforced by something that actually runs, rather
    than only by whichever caller happens to retry a key next. It never
    touches a live attempt: :func:`expire_stale_pending` settles only rows
    whose lease has expired - for an executing row, that means silence since
    its last heartbeat, not elapsed runtime - and only when their status and
    attempt are still the ones it read.
    """
    return {"swept": expire_stale_pending(now=now)}


__all__ = [
    "APPROVAL_TTL_ENV",
    "DECISION_ALLOW",
    "DECISION_APPROVED",
    "DECISION_DENIED",
    "DECISION_EXPIRED",
    "DECISION_REQUIRE_APPROVAL",
    "DECISION_SCOPE_MISMATCH",
    "DECISION_UNSUPPORTED",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "EXECUTION_HEARTBEAT_ENV",
    "EXECUTION_LEASE_ENV",
    "STATUS_AUTHORIZED",
    "STATUS_DENIED",
    "STATUS_EXECUTING",
    "STATUS_EXPIRED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RELEASED",
    "STATUS_REQUESTED",
    "STATUS_SUCCEEDED",
    "TIER_LOCAL",
    "TIER_OUTWARD",
    "TIER_UNSUPPORTED",
    "ActionTarget",
    "ApprovalDeniedError",
    "ApprovalExpiredError",
    "ApprovalRequiredError",
    "ApprovalScopeMismatchError",
    "AuditWriteError",
    "Authorization",
    "DuplicateActionError",
    "EffectOutcome",
    "ExecutionLease",
    "GovernanceError",
    "GovernedRequest",
    "GovernedResult",
    "HeartbeatResult",
    "UnsupportedExecutionPathError",
    "approval_ttl_seconds",
    "args_digest",
    "attempt_lease_seconds",
    "authorize",
    "complete",
    "consume_approval",
    "execution_lease",
    "execution_lease_seconds",
    "expire_stale_pending",
    "get_governed_action",
    "heartbeat",
    "heartbeat_seconds",
    "list_governed_actions",
    "mark_executing",
    "normalize_args",
    "release_for_handoff",
    "reserve",
    "run_governed",
    "run_maintenance",
]

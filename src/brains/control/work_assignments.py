"""Durable cooperative work state, without execution or checkout management.

All transitions require the current revision; stale revisions always fail, including
lost-response replays. Read back before retrying. At the current revision, accepting
an already accepted attempt by its original Session is a no-op; identical terminal
settlement is also a no-op. Creation keys survive creator Session replacement.

Expiry is an observation, not a settlement. Uncertain attempts cannot be retried or
reconciled through this API. A later human reconciliation protocol is separate work.
Workspace paths must be registered spellings (or recorded aliases); checkout_ref,
links and tool in the version-1 specification are inert, advisory data.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from brains.authz.policy import require_workspace_capability
from brains.authz.principal import CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.authz.resolver import resolve_local_principal
from brains.control.common import utc_now
from brains.control.events import TAXONOMY_VERSION, classify_event_kind
from brains.control.sessions import _lock_session_lifecycle, require_live_session
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    Event,
    EventContext,
    Operator,
    Org,
    SessionLease,
    WorkAssignment,
    WorkAssignmentAttempt,
    Workspace,
    WorkspaceAlias,
    WorkspaceMembership,
)

SPEC_LIMIT = 32 * 1024
TEXT_LIMIT = 64 * 1024
DEFAULT_MAX_RUNTIME_SECONDS = 3600
MAX_RUNTIME_SECONDS = 7 * 24 * 3600
UNRESOLVED = ("accepted", "cancel_requested", "uncertain")
CONCLUSIVE = ("completed", "failed", "cancelled")
_UNAVAILABLE = "unknown or unavailable work assignment"


def _text(value, name: str, maximum: int, *, required: bool = False) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if size > maximum or "\x00" in value or (required and not value.strip()):
        raise ValueError(f"{name} must be bounded non-NUL text (maximum {maximum} UTF-8 bytes)")
    return value


def _integer(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _json(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _deadline(value: str) -> datetime:
    _text(value, "deadline", 64, required=True)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("naive deadline")
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ValueError("deadline must be an aware ISO datetime") from exc


def _specification(specification: dict) -> str:
    allowed = {
        "version",
        "objective",
        "context",
        "deadline",
        "max_runtime_seconds",
        "checkout_ref",
        "links",
        "tool",
    }
    if type(specification) is not dict or any(
        type(key) is not str or key not in allowed for key in specification
    ):
        raise ValueError("specification must be a version-1 object with only allowed fields")
    spec = dict(specification)
    spec["version"] = _integer(spec.get("version", 1), "version", 1, 1)
    _text(spec.get("objective"), "objective", SPEC_LIMIT, required=True)
    for field, bound in (("context", SPEC_LIMIT), ("checkout_ref", 2048), ("tool", 64)):
        if field in spec:
            _text(spec[field], field, bound)
    if "deadline" in spec:
        spec["deadline"] = _deadline(spec["deadline"]).isoformat()
    if "max_runtime_seconds" in spec:
        _integer(spec["max_runtime_seconds"], "max_runtime_seconds", 1, MAX_RUNTIME_SECONDS)
    if "links" in spec:
        if type(spec["links"]) is not list or len(spec["links"]) > 32:
            raise ValueError("links must be a list of at most 32 strings")
        for link in spec["links"]:
            _text(link, "link", 2048, required=True)
    encoded = _json(spec)
    _text(encoded, "specification", SPEC_LIMIT)
    return encoded


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _visible(session, principal, workspace: Workspace) -> bool:
    if principal.is_bootstrap_admin:
        return True
    org_id = workspace.org_id
    if org_id is None:
        org_id = session.query(Org.id).filter(Org.slug == "default").scalar()
    if org_id not in (principal.visible_org_ids() or set()):
        return False
    return (
        workspace.visibility != "private"
        or session.query(WorkspaceMembership.id)
        .filter(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.operator_id == principal.operator_id,
        )
        .first()
        is not None
    )


@contextmanager
def _with_session_lifecycle(session_id: str, *, write: bool):
    # Resolution may initialize storage: never call it while holding a writer lock.
    principal = resolve_local_principal()
    if not principal.is_operator or principal.operator_id is None:
        raise ValueError(_UNAVAILABLE)
    _text(session_id, "session_id", 32, required=True)
    init_db()
    with SessionLocal() as session:
        if write:
            _lock_session_lifecycle(session, session_id)
            session.expire_all()
        elif session.get_bind().dialect.name == "sqlite":
            # sqlite3's legacy transaction mode does not BEGIN for SELECT.
            # Keep assignment revision and attempt history in one read snapshot.
            session.connection().exec_driver_sql("BEGIN")
        actor = session.get(AgentSession, session_id)
        workspace = session.get(Workspace, actor.workspace_id) if actor else None
        legacy_admin = actor is not None and (
            actor.created_by_operator_id is None and principal.is_bootstrap_admin
        )
        if (
            actor is None
            or workspace is None
            or session.get(Operator, principal.operator_id) is None
            or (not legacy_admin and actor.created_by_operator_id != principal.operator_id)
            or not _visible(session, principal, workspace)
        ):
            raise ValueError(_UNAVAILABLE)
        # Ownership and visibility precede all lease side effects and detailed errors.
        require_live_session(session, session_id, action="work assignment", renew_lease=False)
        yield session, actor, workspace, principal


def _workspace_path(session, workspace: Workspace, path: str) -> None:
    _text(path, "workspace_path", 1024, required=True)
    if (
        path != workspace.path
        and session.query(WorkspaceAlias.id)
        .filter(
            WorkspaceAlias.workspace_id == workspace.id,
            WorkspaceAlias.path == path,
        )
        .first()
        is None
    ):
        raise ValueError(_UNAVAILABLE)


@contextmanager
def _with_operator_workspace(workspace_id: int, principal: Principal, *, write: bool):
    """Use the adapter's authenticated identity, never a local or declared actor.

    Policy resolution opens its own connections, so it precedes the writer lock.
    Recheck its capability and visibility policy in the transaction's snapshot.
    No Session lifecycle or lease operation belongs on this path.
    """
    if (
        not isinstance(principal, Principal)
        or not principal.is_operator
        or principal.operator_id is None
    ):
        raise ValueError(_UNAVAILABLE)
    _integer(workspace_id, "workspace_id", 1, 2**63 - 1)
    capability = CAP_ORG_WRITE if write else CAP_ORG_READ
    require_workspace_capability(
        principal, capability, workspace_id, entity="work assignment", ref=workspace_id
    )
    if write and not principal.is_human_channel:
        raise ValueError("operator work assignment mutations require a human channel")
    init_db()
    with SessionLocal() as session:
        if write:
            # Take SQLite's writer reservation before any state/idempotency reads.
            # This also orders operator writes against Session lifecycle writers.
            session.query(Workspace).filter(Workspace.id == workspace_id).update(
                {Workspace.id: Workspace.id}, synchronize_session=False
            )
        elif session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN")
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or session.get(Operator, principal.operator_id) is None:
            raise ValueError(_UNAVAILABLE)
        org_id = workspace.org_id
        if org_id is None:
            org_id = session.query(Org.id).filter(Org.slug == "default").scalar()
        if not principal.has_capability(capability, org_id) or not _visible(
            session, principal, workspace
        ):
            raise ValueError(_UNAVAILABLE)
        yield session, workspace


def _assignment(session, code: str, workspace: Workspace, principal) -> WorkAssignment:
    _text(code, "code", 39, required=True)
    row = session.get(WorkAssignment, code)
    if row is None or (
        row.workspace_id != workspace.id or row.creator_operator_id != principal.operator_id
    ):
        raise ValueError(_UNAVAILABLE)
    return row


def _attempts(session, row: WorkAssignment) -> list[WorkAssignmentAttempt]:
    return (
        session.query(WorkAssignmentAttempt)
        .filter(
            WorkAssignmentAttempt.assignment_code == row.code,
        )
        .order_by(WorkAssignmentAttempt.generation)
        .all()
    )


def _snapshot(session, row: WorkAssignment) -> dict:
    now = _aware(utc_now())
    attempts = []
    for attempt in _attempts(session, row):
        source = session.get(AgentSession, attempt.source_session_id)
        lease = session.get(SessionLease, attempt.source_session_id)
        unresolved = attempt.status in UNRESOLVED
        expired = unresolved and now >= _aware(attempt.deadline_at)
        unavailable = unresolved and (
            source is None
            or source.ended_at is not None
            or source.state in ("completed", "failed", "cancelled", "dormant")
            or (source.pid is None and lease is not None and _aware(lease.lease_expires_at) < now)
        )
        attempts.append(
            {
                "attempt_id": attempt.attempt_id,
                "generation": attempt.generation,
                "source_session_id": attempt.source_session_id,
                "tool": attempt.tool,
                "status": attempt.status,
                "observed_status": "uncertain" if expired or unavailable else attempt.status,
                "deadline_exceeded": expired,
                "source_session_unavailable": unavailable,
                "accepted_at": _iso(attempt.accepted_at),
                "deadline_at": _iso(attempt.deadline_at),
                "max_runtime_seconds": attempt.max_runtime_seconds,
                "cancel_requested_at": _iso(attempt.cancel_requested_at),
                "reported_at": _iso(attempt.reported_at),
                "settled_at": _iso(attempt.settled_at),
                "evidence": attempt.evidence,
                "result": attempt.result,
                "usage": json.loads(attempt.usage_json) if attempt.usage_json is not None else None,
            }
        )
    current = attempts[-1] if attempts else None
    return {
        "code": row.code,
        "workspace_id": row.workspace_id,
        "title": row.title,
        "creator_operator_id": row.creator_operator_id,
        "creator_kind": row.creator_kind,
        "creator_session_id": row.creator_session_id,
        "idempotency_key": row.idempotency_key,
        "request_hash": row.request_hash,
        "spec_version": row.spec_version,
        "specification": json.loads(row.specification_json),
        "specification_hash": row.specification_hash,
        "status": row.status,
        "revision": row.revision,
        "generation": row.generation,
        "observed_status": current["observed_status"]
        if row.status in UNRESOLVED and current
        else row.status,
        "deadline_exceeded": bool(current and current["deadline_exceeded"]),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "cancel_requested_at": _iso(row.cancel_requested_at),
        "cancel_requested_by_session_id": row.cancel_requested_by_session_id,
        "current_attempt_id": current["attempt_id"] if current else None,
        "attempts": attempts,
    }


def _revision(row: WorkAssignment, expected: int) -> None:
    _integer(expected, "expected_revision", 1, 2**63 - 2)
    if row.revision != expected:
        raise ValueError("stale work assignment revision; read the current assignment")


def _cas(session, row: WorkAssignment, expected: int, **values) -> None:
    changed = (
        session.query(WorkAssignment)
        .filter(
            WorkAssignment.code == row.code,
            WorkAssignment.revision == expected,
        )
        .update(
            {**values, "revision": expected + 1, "updated_at": utc_now()},
            synchronize_session=False,
        )
    )
    if changed != 1:
        raise ValueError("stale work assignment revision; read the current assignment")
    session.refresh(row)


def _record_event(
    session,
    row: WorkAssignment,
    action: str,
    *,
    session_id: str | None,
    attribution: dict,
) -> None:
    kind = f"work_assignment_{action}"
    event = Event(
        workspace_id=row.workspace_id,
        session_id=session_id,
        kind=kind,
        message=f"{row.code}: {action}",
        metadata_json=_json(
            {
                "code": row.code,
                "revision": row.revision,
                "generation": row.generation,
                **attribution,
            }
        ),
    )
    session.add(event)
    session.flush()
    session.add(
        EventContext(
            event_id=event.id,
            category=classify_event_kind(kind),
            scope="workspace",
            scope_source="explicit",
            taxonomy_version=TAXONOMY_VERSION,
        )
    )


def _finish(session, row: WorkAssignment, actor: AgentSession, action: str) -> dict:
    """Assignment, attempt, lease and scoped event commit or roll back together."""
    require_live_session(session, actor.id, action="work assignment", renew_lease=True)
    _record_event(session, row, action, session_id=actor.id, attribution={"actor_kind": "session"})
    result = _snapshot(session, row)
    session.commit()
    return result


def _create_assignment(
    session,
    workspace: Workspace,
    principal: Principal,
    title: str,
    spec: str,
    key: str,
    *,
    creator_kind: str,
    creator_session_id: str | None,
) -> tuple[WorkAssignment, bool]:
    request = {"title": title, "specification": json.loads(spec)}
    request_hash = _hash(_json({**request, "creator_kind": creator_kind}))
    row = (
        session.query(WorkAssignment)
        .filter(
            WorkAssignment.workspace_id == workspace.id,
            WorkAssignment.creator_operator_id == principal.operator_id,
            WorkAssignment.idempotency_key == key,
        )
        .one_or_none()
    )
    if row is not None:
        # Migration 154 hashes predate author kinds; preserve their replay fence.
        compatible_hashes = {request_hash}
        if creator_kind == "session":
            compatible_hashes.add(_hash(_json(request)))
        if row.creator_kind != creator_kind or row.request_hash not in compatible_hashes:
            raise ValueError("idempotency_key already used for a different request")
        return row, False
    now = utc_now()
    row = WorkAssignment(
        code=f"WA-{uuid4()}",
        workspace_id=workspace.id,
        creator_operator_id=principal.operator_id,
        creator_kind=creator_kind,
        creator_session_id=creator_session_id,
        idempotency_key=key,
        request_hash=request_hash,
        title=title,
        spec_version=1,
        specification_json=spec,
        specification_hash=_hash(spec),
        status="ready",
        revision=1,
        generation=0,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    return row, True


def create_work_assignment(
    workspace_path: str,
    title: str,
    specification: dict,
    *,
    session_id: str,
    idempotency_key: str,
) -> dict:
    title = _text(title, "title", 256, required=True)
    key = _text(idempotency_key, "idempotency_key", 128, required=True)
    spec = _specification(specification)
    with _with_session_lifecycle(session_id, write=True) as (session, actor, workspace, principal):
        _workspace_path(session, workspace, workspace_path)
        row, created = _create_assignment(
            session,
            workspace,
            principal,
            title,
            spec,
            key,
            creator_kind="session",
            creator_session_id=actor.id,
        )
        if not created:
            return _snapshot(session, row)
        return _finish(session, row, actor, "created")


def _operator_snapshot(session, row: WorkAssignment, principal: Principal) -> dict:
    result = _snapshot(session, row)
    if not principal.is_human_channel:
        reason = "human_channel_required"
    else:
        workspace = session.get(Workspace, row.workspace_id)
        if workspace is None:
            raise ValueError(_UNAVAILABLE)
        org_id = workspace.org_id
        if org_id is None:
            org_id = session.query(Org.id).filter(Org.slug == "default").scalar()
        if not principal.has_capability(CAP_ORG_WRITE, org_id):
            reason = "write_capability_required"
        elif row.status in ("cancelled", "cancel_requested") or (
            row.status == "uncertain" and row.cancel_requested_at is not None
        ):
            reason = "cancellation_already_requested"
        elif row.status not in ("ready", "accepted", "uncertain"):
            reason = "assignment_not_cancellable"
        else:
            reason = None
    result["permissions"] = {"can_cancel": reason is None, "reason": reason}
    return result


def _finish_operator(session, row: WorkAssignment, principal: Principal, action: str) -> dict:
    _record_event(
        session,
        row,
        action,
        session_id=None,
        attribution={
            "actor_kind": "operator",
            "operator_id": principal.operator_id,
            "channel": principal.channel,
        },
    )
    result = _operator_snapshot(session, row, principal)
    session.commit()
    return result


def list_operator_assignments(workspace_id: int, *, principal: Principal, limit: int = 50) -> list:
    """Read owned assignments without requiring or renewing a Session."""
    _integer(limit, "limit", 1, 200)
    with _with_operator_workspace(workspace_id, principal, write=False) as (session, workspace):
        rows = (
            session.query(WorkAssignment)
            .filter(
                WorkAssignment.workspace_id == workspace.id,
                WorkAssignment.creator_operator_id == principal.operator_id,
            )
            .order_by(WorkAssignment.created_at.desc(), WorkAssignment.code)
            .limit(limit)
            .all()
        )
        return [_operator_snapshot(session, row, principal) for row in rows]


def get_operator_assignment(workspace_id: int, code: str, *, principal: Principal) -> dict:
    with _with_operator_workspace(workspace_id, principal, write=False) as (session, workspace):
        row = _assignment(session, code, workspace, principal)
        return _operator_snapshot(session, row, principal)


def create_operator_assignment(
    workspace_id: int,
    title: str,
    specification: dict,
    *,
    principal: Principal,
    idempotency_key: str,
) -> dict:
    """Record human-authored work; acceptance still belongs to a live peer."""
    title = _text(title, "title", 256, required=True)
    key = _text(idempotency_key, "idempotency_key", 128, required=True)
    spec = _specification(specification)
    with _with_operator_workspace(workspace_id, principal, write=True) as (session, workspace):
        row, created = _create_assignment(
            session,
            workspace,
            principal,
            title,
            spec,
            key,
            creator_kind="operator",
            creator_session_id=None,
        )
        if not created:
            return _operator_snapshot(session, row, principal)
        return _finish_operator(session, row, principal, "created")


def cancel_operator_assignment(
    workspace_id: int, code: str, *, principal: Principal, expected_revision: int
) -> dict:
    """Cancel ready work or request cooperative cancellation; never stop a process."""
    with _with_operator_workspace(workspace_id, principal, write=True) as (session, workspace):
        row = _assignment(session, code, workspace, principal)
        action = _cancel_assignment(session, row, expected_revision, session_id=None)
        if action is None:
            return _operator_snapshot(session, row, principal)
        return _finish_operator(session, row, principal, action)


def get_work_assignment(code: str, *, session_id: str) -> dict:
    with _with_session_lifecycle(session_id, write=False) as (session, _, workspace, principal):
        return _snapshot(session, _assignment(session, code, workspace, principal))


def list_work_assignments(workspace_path: str, *, session_id: str, limit: int = 50) -> list:
    _integer(limit, "limit", 1, 200)
    with _with_session_lifecycle(session_id, write=False) as (session, _, workspace, principal):
        _workspace_path(session, workspace, workspace_path)
        rows = (
            session.query(WorkAssignment)
            .filter(
                WorkAssignment.workspace_id == workspace.id,
                WorkAssignment.creator_operator_id == principal.operator_id,
            )
            .order_by(WorkAssignment.created_at.desc(), WorkAssignment.code)
            .limit(limit)
            .all()
        )
        return [_snapshot(session, row) for row in rows]


def accept_work_assignment(code: str, *, session_id: str, expected_revision: int) -> dict:
    with _with_session_lifecycle(session_id, write=True) as (session, actor, workspace, principal):
        row = _assignment(session, code, workspace, principal)
        _revision(row, expected_revision)
        attempts = _attempts(session, row)
        if row.status == "accepted" and attempts[-1].source_session_id == actor.id:
            return _snapshot(session, row)
        if row.status != "ready" or any(a.status in UNRESOLVED for a in attempts):
            raise ValueError("work assignment is not ready for acceptance")
        spec = json.loads(row.specification_json)
        now = _aware(utc_now())
        budget = spec.get("max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS)
        deadline = now + timedelta(seconds=budget)
        if "deadline" in spec:
            deadline = min(deadline, _deadline(spec["deadline"]))
        if deadline <= now:
            raise ValueError("work assignment deadline exceeded")
        _cas(session, row, expected_revision, status="accepted", generation=row.generation + 1)
        session.add(
            WorkAssignmentAttempt(
                attempt_id=str(uuid4()),
                assignment_code=row.code,
                generation=row.generation,
                source_session_id=actor.id,
                tool=actor.tool,
                status="accepted",
                accepted_at=now,
                deadline_at=deadline,
                max_runtime_seconds=budget,
            )
        )
        return _finish(session, row, actor, "accepted")


def settle_work_assignment(
    code: str,
    attempt_id: str,
    outcome: str,
    evidence: str,
    *,
    session_id: str,
    expected_revision: int,
    result: str = "",
) -> dict:
    _text(attempt_id, "attempt_id", 36, required=True)
    if type(outcome) is not str or outcome not in (*CONCLUSIVE, "uncertain"):
        raise ValueError("outcome must be completed, failed, cancelled, or uncertain")
    _text(evidence, "evidence", TEXT_LIMIT, required=True)
    _text(result, "result", TEXT_LIMIT)
    with _with_session_lifecycle(session_id, write=True) as (session, actor, workspace, principal):
        row = _assignment(session, code, workspace, principal)
        _revision(row, expected_revision)
        attempt = session.get(WorkAssignmentAttempt, attempt_id)
        if attempt is None or (
            attempt.assignment_code != row.code
            or attempt.source_session_id != actor.id
            or attempt.generation != row.generation
        ):
            raise ValueError(_UNAVAILABLE)
        if attempt.status in (*CONCLUSIVE, "uncertain"):
            if (attempt.status, attempt.evidence, attempt.result) == (outcome, evidence, result):
                return _snapshot(session, row)
            raise ValueError("attempt already reported; reconciliation is not supported")
        if row.status not in ("accepted", "cancel_requested"):
            raise ValueError("work assignment cannot be settled")
        if row.status == "cancel_requested" and outcome == "completed":
            raise ValueError("completion after cancellation request is refused")
        _cas(session, row, expected_revision, status=outcome)
        attempt.status = outcome
        attempt.evidence = evidence
        attempt.result = result
        attempt.reported_at = utc_now()
        attempt.settled_at = attempt.reported_at if outcome in CONCLUSIVE else None
        return _finish(session, row, actor, "settled")


def cancel_work_assignment(code: str, *, session_id: str, expected_revision: int) -> dict:
    with _with_session_lifecycle(session_id, write=True) as (session, actor, workspace, principal):
        row = _assignment(session, code, workspace, principal)
        action = _cancel_assignment(session, row, expected_revision, session_id=actor.id)
        if action is None:
            return _snapshot(session, row)
        return _finish(session, row, actor, action)


def _cancel_assignment(
    session, row: WorkAssignment, expected_revision: int, *, session_id: str | None
) -> str | None:
    _revision(row, expected_revision)
    if row.status in ("cancelled", "cancel_requested"):
        return None
    if row.status not in ("ready", "accepted", "uncertain"):
        raise ValueError("work assignment cannot be cancelled")
    if row.status == "uncertain" and row.cancel_requested_at is not None:
        return None
    now = utc_now()
    status = (
        "cancelled"
        if row.status == "ready"
        else ("uncertain" if row.status == "uncertain" else "cancel_requested")
    )
    _cas(
        session,
        row,
        expected_revision,
        status=status,
        cancel_requested_at=now,
        cancel_requested_by_session_id=session_id,
    )
    if row.generation and status != "cancelled":
        attempt = _attempts(session, row)[-1]
        attempt.cancel_requested_at = now
        attempt.status = status
    return "cancelled" if status == "cancelled" else "cancel_requested"


def retry_work_assignment(code: str, *, session_id: str, expected_revision: int) -> dict:
    with _with_session_lifecycle(session_id, write=True) as (session, actor, workspace, principal):
        row = _assignment(session, code, workspace, principal)
        _revision(row, expected_revision)
        attempts = _attempts(session, row)
        if row.status not in ("failed", "cancelled") or any(
            a.status not in CONCLUSIVE or a.settled_at is None for a in attempts
        ):
            raise ValueError(
                "retry requires conclusive failure or cancellation; unresolved attempt"
            )
        _cas(
            session,
            row,
            expected_revision,
            status="ready",
            cancel_requested_at=None,
            cancel_requested_by_session_id=None,
        )
        return _finish(session, row, actor, "retried")


def reconcile_assignment_attempts(code: str) -> dict:
    """Reconcile expired or orphaned attempts for an assignment without process killing.

    Distinguishes requested cancellation (settled as cancelled), budget timeout
    (settled as failed with runtime_budget_exceeded), and session termination
    (settled as failed with source_session_terminated). Fences against late
    stale completions by finalizing the attempt generation.
    """
    now = _aware(utc_now())
    init_db()
    with SessionLocal() as session:
        row = session.get(WorkAssignment, code)
        if row is None:
            raise ValueError(_UNAVAILABLE)
        attempts = _attempts(session, row)
        if not attempts or row.status not in UNRESOLVED:
            return _snapshot(session, row)

        current = attempts[-1]
        if current.status not in UNRESOLVED:
            return _snapshot(session, row)

        source = session.get(AgentSession, current.source_session_id)
        lease = session.get(SessionLease, current.source_session_id)
        expired = now >= _aware(current.deadline_at)
        unavailable = (
            source is None
            or source.ended_at is not None
            or source.state in ("completed", "failed", "cancelled", "dormant")
            or (source.pid is None and lease is not None and _aware(lease.lease_expires_at) < now)
        )

        if not (expired or unavailable or row.status == "cancel_requested"):
            return _snapshot(session, row)

        if row.status == "cancel_requested" or current.status == "cancel_requested":
            settled_status = "cancelled"
            evidence = "cancellation requested and settled by reconciliation"
        elif expired:
            settled_status = "failed"
            evidence = f"runtime budget exceeded ({current.max_runtime_seconds}s deadline)"
        else:
            settled_status = "failed"
            evidence = "source session terminated or lease expired during execution"

        _cas(session, row, row.revision, status=settled_status)
        current.status = settled_status
        current.evidence = evidence
        current.reported_at = now
        current.settled_at = now
        session.commit()
        return _snapshot(session, row)

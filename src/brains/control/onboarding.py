"""Fresh-state onboarding: from an empty install to one real result (F6, BL-P1-04).

Onboarding is a sequence of real product writes, so its state is a real record.
A wizard that kept its progress in browser memory would lose it on a reload,
and - worse - could report success it never achieved. Here the server owns the
attempt: what was created, which step is next, and whether the run ended in a
Session or in an explicit, named blocker.

The rules this module enforces
------------------------------

**One attempt per operator at a time.** :func:`state` resumes the open attempt
if there is one and creates it otherwise, so a reload, a second tab, and a
machine that never connects all continue the same run.

**Derived from real entities.** Every read re-checks the rows the attempt
points at - the Org still exists, the Runtime is still online, the Persona is
still active, the Issue still exists, the Session is real - so the resumed
state is what the install actually is, not what the browser last remembered.

**Retry and defer are outcomes, not gaps.** A step records ``done``,
``deferred`` (the operator chose to skip connecting a machine) or ``failed``
with its error, and ``attempts`` counts how many times it was tried. Retrying
updates the same row.

**Completion requires a Session.** :func:`evaluate` marks an attempt
``completed`` only when an ``agent_sessions`` row exists for the Issue the
attempt created. If the Runtime was deferred or is unavailable, the attempt
ends ``blocked`` with a stable reason and a recovery action. There is no code
path that reports a finished onboarding without a Session.

**No fixtures.** Nothing here seeds an Org, Persona, Project, Issue or Session
to make the flow look finished. Every entity is created by an explicit call
from the console.

Pure control logic - no FastAPI.
"""

from __future__ import annotations

import uuid
from typing import Any

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    Issue,
    OnboardingAttempt,
    OnboardingStep,
    Operator,
    Org,
    Persona,
    Project,
    Runtime,
)

#: The ordered steps. ``runtime`` is the one step an operator may defer.
STEPS: tuple[str, ...] = ("org", "runtime", "persona", "work", "dispatch")

STEP_STATUSES = frozenset({"pending", "done", "deferred", "failed"})

#: Stable blocked reasons. Part of the API contract: the console maps each to a
#: recovery action and the tests assert them.
BLOCKED_REASONS = (
    "runtime_deferred",
    "runtime_unavailable",
    "dispatch_refused",
    "session_missing",
)

RECOVERY_ACTIONS: dict[str, dict[str, str]] = {
    "runtime_deferred": {
        "label": "Connect a machine",
        "route": "/runtimes",
        "detail": (
            "Machine setup was deferred, so no Runtime can execute the first Issue. "
            "Connect a machine, then retry the dispatch step."
        ),
    },
    "runtime_unavailable": {
        "label": "Check your Runtimes",
        "route": "/runtimes",
        "detail": (
            "The Persona's Runtime is not online, so the first Issue cannot start. "
            "Bring the daemon back online, then retry the dispatch step."
        ),
    },
    "dispatch_refused": {
        "label": "Open the Issue",
        "route": "/issues",
        "detail": (
            "Dispatch was refused. The Issue detail names the reason and the "
            "assignment that produced it."
        ),
    },
    "session_missing": {
        "label": "Retry the dispatch",
        "route": "/issues",
        "detail": "No Session exists for the first Issue yet, so onboarding is not complete.",
    },
}


class OnboardingError(ValueError):
    """A refused onboarding operation, with the operator-facing reason."""


def _operator_id(session, operator: str | None) -> int | None:
    if not operator:
        return None
    row = session.query(Operator).filter(Operator.slug == operator).one_or_none()
    return row.id if row is not None else None


def _step_dict(row: OnboardingStep) -> dict:
    return {
        "step": row.step,
        "status": row.status,
        "entity_ref": row.entity_ref,
        "detail": row.detail,
        "error": row.error,
        "attempts": row.attempts,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _steps(session, attempt_id: str) -> list[OnboardingStep]:
    rows = {
        row.step: row
        for row in session.query(OnboardingStep).filter(OnboardingStep.attempt_id == attempt_id)
    }
    for name in STEPS:
        if name not in rows:
            row = OnboardingStep(attempt_id=attempt_id, step=name, status="pending", attempts=0)
            session.add(row)
            rows[name] = row
    session.flush()
    return [rows[name] for name in STEPS]


def _open_attempt(session, operator_id: int | None) -> OnboardingAttempt | None:
    query = session.query(OnboardingAttempt).filter(
        OnboardingAttempt.status.in_(("in_progress", "blocked"))
    )
    query = (
        query.filter(OnboardingAttempt.operator_id.is_(None))
        if operator_id is None
        else query.filter(OnboardingAttempt.operator_id == operator_id)
    )
    return query.order_by(OnboardingAttempt.id.desc()).first()


def _install_is_fresh(session) -> bool:
    """Whether this install is still an empty state.

    "Fresh" is a fact about the store, not about the browser: an install with
    no Org, or with no active Persona, cannot execute anything yet and is still
    owed onboarding. Once an Org and a Persona exist the operator has left the
    empty state, and the console stops redirecting - being *blocked* partway is
    a state to recover from on the onboarding screen, not a reason to trap
    every other screen behind a redirect.
    """
    if session.query(Org).filter(Org.status == "active").count() == 0:
        return True
    return session.query(Persona).filter(Persona.status == "active").count() == 0


def _operator_left_onboarding(session, operator_id: int | None) -> bool:
    """Whether this operator already finished or deliberately left onboarding.

    A safe exit is honoured: an operator who abandoned onboarding is not
    redirected back into it on the next page load.
    """
    query = session.query(OnboardingAttempt).filter(
        OnboardingAttempt.status.in_(("completed", "abandoned"))
    )
    query = (
        query.filter(OnboardingAttempt.operator_id.is_(None))
        if operator_id is None
        else query.filter(OnboardingAttempt.operator_id == operator_id)
    )
    return query.count() > 0


def _entity_state(session, attempt: OnboardingAttempt) -> dict[str, Any]:
    """Re-read the rows the attempt points at, so resume reflects reality."""
    org = session.get(Org, attempt.org_id) if attempt.org_id else None
    runtime = session.get(Runtime, attempt.runtime_id) if attempt.runtime_id else None
    persona = session.get(Persona, attempt.persona_id) if attempt.persona_id else None
    project = session.get(Project, attempt.project_id) if attempt.project_id else None
    issue = session.get(Issue, attempt.issue_id) if attempt.issue_id else None
    agent_session = session.get(AgentSession, attempt.session_id) if attempt.session_id else None
    if agent_session is not None and (issue is None or agent_session.issue_id != issue.id):
        agent_session = None
    if agent_session is None and issue is not None:
        agent_session = (
            session.query(AgentSession)
            .filter(AgentSession.issue_id == issue.id)
            .order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
            .first()
        )
    return {
        "org": {"id": org.id, "slug": org.slug} if org is not None else None,
        "runtime": (
            {"id": runtime.id, "slug": runtime.slug, "status": runtime.status}
            if runtime is not None
            else None
        ),
        "persona": (
            {"id": persona.id, "slug": persona.slug, "status": persona.status}
            if persona is not None
            else None
        ),
        "project": {"id": project.id, "code": project.code} if project is not None else None,
        "issue": (
            {"id": issue.id, "code": issue.code, "status": issue.status}
            if issue is not None
            else None
        ),
        "session": (
            {
                "id": agent_session.id,
                "state": getattr(agent_session, "state", None),
                "ended": agent_session.ended_at is not None,
            }
            if agent_session is not None
            else None
        ),
    }


def _next_step(steps: list[OnboardingStep], entities: dict[str, Any]) -> str:
    for row in steps:
        if row.status == "done":
            continue
        if row.step == "runtime" and row.status == "deferred":
            continue
        return row.step
    return "dispatch" if entities.get("session") is None else "done"


def _evaluate(session, attempt: OnboardingAttempt) -> dict:
    """Recompute the attempt's derived state from persisted rows."""
    steps = _steps(session, attempt.attempt_id)
    entities = _entity_state(session, attempt)
    by_step = {row.step: row for row in steps}

    if entities["session"] is not None:
        attempt.status = "completed"
        attempt.session_id = entities["session"]["id"]
        attempt.blocked_reason = None
        attempt.blocked_detail = None
        attempt.current_step = "done"
        if attempt.completed_at is None:
            attempt.completed_at = utc_now()
        dispatch_step = by_step["dispatch"]
        if dispatch_step.status != "done":
            dispatch_step.status = "done"
            dispatch_step.entity_ref = entities["session"]["id"]
            dispatch_step.updated_at = utc_now()
    else:
        attempt.completed_at = None
        attempt.current_step = _next_step(steps, entities)
        reason: str | None = None
        if by_step["dispatch"].status == "failed":
            reason = "dispatch_refused"
        elif by_step["runtime"].status == "deferred" and entities["runtime"] is None:
            reason = "runtime_deferred"
        elif entities["runtime"] is not None and entities["runtime"]["status"] != "online":
            reason = "runtime_unavailable"
        elif by_step["dispatch"].status == "done":
            # A dispatch recorded done with no Session is not a completion.
            reason = "session_missing"
        if reason == "session_missing" or (
            reason is not None and attempt.current_step in ("dispatch", "done")
        ):
            attempt.status = "blocked"
            attempt.blocked_reason = reason
            attempt.blocked_detail = RECOVERY_ACTIONS[reason]["detail"]
        else:
            attempt.status = "in_progress"
            attempt.blocked_reason = None
            attempt.blocked_detail = None
    attempt.updated_at = utc_now()
    session.commit()
    return _attempt_dict(session, attempt, entities)


def _attempt_dict(
    session, attempt: OnboardingAttempt, entities: dict[str, Any] | None = None
) -> dict:
    steps = _steps(session, attempt.attempt_id)
    entities = entities if entities is not None else _entity_state(session, attempt)
    recovery = RECOVERY_ACTIONS.get(attempt.blocked_reason) if attempt.blocked_reason else None
    return {
        "attempt_id": attempt.attempt_id,
        "status": attempt.status,
        "current_step": attempt.current_step,
        "blocked_reason": attempt.blocked_reason,
        "blocked_detail": attempt.blocked_detail,
        "recovery": recovery,
        "steps": [_step_dict(row) for row in steps],
        "entities": entities,
        "org_id": attempt.org_id,
        "runtime_id": attempt.runtime_id,
        "persona_id": attempt.persona_id,
        "project_id": attempt.project_id,
        "issue_id": attempt.issue_id,
        "session_id": attempt.session_id,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
        "updated_at": attempt.updated_at.isoformat() if attempt.updated_at else None,
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
    }


def state(*, operator: str | None = None, create: bool | None = None) -> dict:
    """The operator's onboarding state, resuming the open attempt if there is one.

    ``required`` is the fresh-state decision the console's guard reads: an
    install with no Org or no active Persona, for an operator who has not
    already completed or deliberately left onboarding.

    An attempt is created on read only while the install is fresh, so simply
    opening the console on a working install does not manufacture onboarding
    records. ``create=True`` starts one explicitly (the operator asked for it);
    ``create=False`` never starts one.
    """
    init_db()
    with SessionLocal() as session:
        operator_id = _operator_id(session, operator)
        attempt = _open_attempt(session, operator_id)
        fresh = _install_is_fresh(session)
        left = _operator_left_onboarding(session, operator_id)
        should_create = create if create is not None else (fresh and not left)
        if attempt is None:
            if not should_create:
                return {
                    "required": fresh and not left,
                    "fresh_install": fresh,
                    "attempt": None,
                    "steps_order": list(STEPS),
                }
            attempt = OnboardingAttempt(
                attempt_id=f"onb_{uuid.uuid4().hex[:12]}",
                operator_id=operator_id,
                status="in_progress",
                current_step="org",
            )
            session.add(attempt)
            session.commit()
            session.refresh(attempt)
            append_event(
                "onboarding_started",
                f"onboarding attempt {attempt.attempt_id} started",
                metadata={"attempt_id": attempt.attempt_id},
            )
        view = _evaluate(session, attempt)
        return {
            "required": view["status"] != "completed" and fresh and not left,
            "fresh_install": fresh,
            "attempt": view,
            "steps_order": list(STEPS),
        }


def get_attempt(attempt_id: str) -> dict | None:
    init_db()
    with SessionLocal() as session:
        attempt = (
            session.query(OnboardingAttempt)
            .filter(OnboardingAttempt.attempt_id == attempt_id)
            .one_or_none()
        )
        if attempt is None:
            return None
        return _evaluate(session, attempt)


_ENTITY_FIELDS = {
    "org": "org_id",
    "runtime": "runtime_id",
    "persona": "persona_id",
    "project": "project_id",
    "issue": "issue_id",
}


def record_step(
    attempt_id: str,
    step: str,
    *,
    status: str = "done",
    entity_ref: str | None = None,
    detail: str | None = None,
    error: str | None = None,
    org_id: int | None = None,
    runtime_id: int | None = None,
    persona_id: int | None = None,
    project_id: int | None = None,
    issue_id: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Record the outcome of one step and re-derive the attempt's state.

    Retrying a step updates the same row and increments ``attempts``; it never
    appends a second history for the same step.
    """
    if step not in STEPS:
        raise OnboardingError(f"step must be one of {list(STEPS)}")
    if status not in STEP_STATUSES:
        raise OnboardingError(f"status must be one of {sorted(STEP_STATUSES)}")
    if status == "deferred" and step != "runtime":
        raise OnboardingError("only the runtime step may be deferred")
    init_db()
    with SessionLocal() as session:
        attempt = (
            session.query(OnboardingAttempt)
            .filter(OnboardingAttempt.attempt_id == attempt_id)
            .one_or_none()
        )
        if attempt is None:
            raise OnboardingError(f"unknown onboarding attempt: {attempt_id!r}")
        if attempt.status == "completed":
            return _attempt_dict(session, attempt)
        _steps(session, attempt_id)
        row = (
            session.query(OnboardingStep)
            .filter(OnboardingStep.attempt_id == attempt_id, OnboardingStep.step == step)
            .one()
        )
        row.status = status
        row.entity_ref = entity_ref if entity_ref is not None else row.entity_ref
        row.detail = detail
        row.error = error if status == "failed" else None
        row.attempts = (row.attempts or 0) + 1
        row.updated_at = utc_now()
        for field, value in (
            ("org_id", org_id),
            ("runtime_id", runtime_id),
            ("persona_id", persona_id),
            ("project_id", project_id),
            ("issue_id", issue_id),
            ("session_id", session_id),
        ):
            if value is not None:
                setattr(attempt, field, value)
        if status == "deferred" and step == "runtime":
            attempt.runtime_id = None
        session.commit()
        view = _evaluate(session, attempt)
    append_event(
        "onboarding_step",
        f"onboarding {attempt_id} step {step} -> {status}",
        metadata={"attempt_id": attempt_id, "step": step, "status": status},
    )
    return view


def abandon(attempt_id: str) -> dict:
    """Close an attempt without claiming it finished (safe exit)."""
    init_db()
    with SessionLocal() as session:
        attempt = (
            session.query(OnboardingAttempt)
            .filter(OnboardingAttempt.attempt_id == attempt_id)
            .one_or_none()
        )
        if attempt is None:
            raise OnboardingError(f"unknown onboarding attempt: {attempt_id!r}")
        if attempt.status != "completed":
            attempt.status = "abandoned"
            attempt.updated_at = utc_now()
            session.commit()
        return _attempt_dict(session, attempt)

"""Onboarding REST router (F6, BL-P1-04).

A thin HTTP shell over :mod:`brains.control.onboarding`. It exposes the durable
attempt the console resumes from, records each step's real outcome, and answers
the fresh-state question the browser guard asks on load.

Every route is scoped to the authenticated operator: an attempt belongs to the
principal that started it, so one operator's onboarding is never resumed,
advanced or abandoned by another. Nothing here creates an Org, Persona, Project,
Issue or Session - the console calls the existing product APIs for that and
reports the outcome back, so onboarding can never manufacture the success it is
supposed to prove.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from brains.authz import policy
from brains.authz.deps import require_operator_principal
from brains.authz.principal import Principal
from brains.control import onboarding as onboarding_ctl

router = APIRouter(prefix="/v1")


class StepBody(BaseModel):
    status: str = "done"
    entity_ref: str | None = None
    detail: str | None = None
    error: str | None = None
    org_id: int | None = None
    runtime_id: int | None = None
    persona_id: int | None = None
    project_id: int | None = None
    issue_id: int | None = None
    session_id: str | None = None


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _owned_attempt(principal: Principal, attempt_id: str) -> dict:
    """The attempt, or ``404`` when it is unknown or another operator's."""
    current = onboarding_ctl.state(operator=principal.operator_slug, create=False)
    attempt = current.get("attempt")
    if attempt is None or attempt["attempt_id"] != attempt_id:
        raise policy.not_found("onboarding attempt", attempt_id)
    return attempt


def _validate_step_scope(principal: Principal, attempt: dict, body: StepBody) -> None:
    """Keep every recorded entity inside one visible Org and Session scope."""
    target_org = attempt.get("org_id")
    if body.org_id is not None:
        policy.require_capability(
            principal,
            policy.CAP_ORG_READ,
            body.org_id,
            entity="org",
            ref=body.org_id,
        )
        if target_org is not None and body.org_id != target_org:
            raise policy.not_found("org", body.org_id)
        target_org = body.org_id

    resolved_orgs: list[int] = []

    def accept(kind: str, ref: object | None, org_id: int | None) -> None:
        nonlocal target_org
        if ref is None:
            return
        if org_id is None or not principal.can_see_org(org_id):
            raise policy.not_found(kind, ref)
        if target_org is not None and org_id != target_org:
            raise policy.not_found(kind, ref)
        target_org = org_id
        resolved_orgs.append(org_id)

    accept("runtime", body.runtime_id, policy.runtime_org_id(body.runtime_id))
    accept("persona", body.persona_id, policy.persona_org_id(body.persona_id))
    accept("project", body.project_id, policy.project_org_id(body.project_id))
    accept("issue", body.issue_id, policy.issue_org_id(body.issue_id))
    accept("session", body.session_id, policy.session_org_id(body.session_id))

    if body.session_id is not None:
        workspace_id = policy.session_workspace_id(body.session_id)
        if not policy.can_see_workspace(principal, workspace_id):
            raise policy.not_found("session", body.session_id)

    if attempt.get("org_id") is None and body.org_id is None and resolved_orgs:
        body.org_id = resolved_orgs[0]


@router.get("/onboarding/state")
def onboarding_state(
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """The operator's onboarding state, resuming an open attempt if there is one.

    ``required`` is the fresh-state decision the console's route guard reads.
    It is derived from the store - an install that has never produced a Session
    for an Issue is still owed onboarding - not from anything the browser
    remembers, so a reload, a new tab and a new machine all agree.
    """
    policy.require_operator(principal, operation="read onboarding state")
    return onboarding_ctl.state(operator=principal.operator_slug)


@router.post("/onboarding/attempts")
def start_attempt(
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Start or resume this operator's onboarding attempt (idempotent)."""
    policy.require_operator(principal, operation="start onboarding")
    return onboarding_ctl.state(operator=principal.operator_slug, create=True)


@router.post("/onboarding/attempts/{attempt_id}/steps/{step}")
def record_step(
    attempt_id: str,
    step: str,
    body: StepBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Record one step's outcome and re-derive the attempt from real rows.

    ``status`` is ``done``, ``failed`` (with its ``error``) or - for the
    machine step only - ``deferred``. Retrying a step updates the same row and
    increments its attempt count rather than appending a second history.
    """
    policy.require_operator(principal, operation="advance onboarding")
    attempt = _owned_attempt(principal, attempt_id)
    _validate_step_scope(principal, attempt, body)
    try:
        return onboarding_ctl.record_step(
            attempt_id,
            step,
            status=body.status,
            entity_ref=body.entity_ref,
            detail=body.detail,
            error=body.error,
            org_id=body.org_id,
            runtime_id=body.runtime_id,
            persona_id=body.persona_id,
            project_id=body.project_id,
            issue_id=body.issue_id,
            session_id=body.session_id,
        )
    except onboarding_ctl.OnboardingError as exc:
        raise _bad_request(exc) from exc


@router.post("/onboarding/attempts/{attempt_id}/abandon")
def abandon_attempt(
    attempt_id: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Leave onboarding without claiming it finished (safe exit)."""
    policy.require_operator(principal, operation="abandon onboarding")
    _owned_attempt(principal, attempt_id)
    try:
        return onboarding_ctl.abandon(attempt_id)
    except onboarding_ctl.OnboardingError as exc:
        raise _bad_request(exc) from exc

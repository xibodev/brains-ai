"""Orgs REST router (WS3 §1.1 + §2 onboard).

A thin HTTP shell over :mod:`brains.control.orgs`, with one explicit
authorization decision per route (:mod:`brains.authz.policy`):

* ``member`` reads the Org and its content.
* ``member`` creates Org *content* (Pods, Skills).
* ``admin`` administers the Org itself: rename/archive, membership, automation.
* ``owner`` grants or revokes the ``owner`` role.

Listing is filtered to the Orgs the principal is a member of, and an Org the
principal cannot read is answered ``404`` rather than ``403`` so Org IDs and
slugs cannot be enumerated. No business logic lives here.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from brains.api.pagination import paginate
from brains.authz import policy
from brains.authz.deps import require_operator_principal
from brains.authz.principal import (
    CAP_ORG_ADMIN,
    CAP_ORG_OWNER,
    CAP_ORG_READ,
    CAP_ORG_WRITE,
    Principal,
)
from brains.control import orgs as orgs_ctl
from brains.control import pods as pods_ctl
from brains.control import recurring as recurring_ctl
from brains.control import skills as skills_ctl

router = APIRouter(prefix="/v1")


class CreateOrgBody(BaseModel):
    slug: str
    name: str
    description: str | None = None


class PatchOrgBody(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None


class AddMemberBody(BaseModel):
    operator_id: str
    role: str = "member"


class CreatePodBody(BaseModel):
    slug: str
    name: str
    #: The Pod's leader **Persona** (id or slug). ``leader`` is the retained
    #: spelling and now names a Persona: Pod membership is Personas, not
    #: operator labels (BL-P1-03).
    leader_persona_id: str | None = None
    leader: str | None = None
    description: str | None = None


class PodMemberBody(BaseModel):
    #: The **Persona** to add (id or slug). ``persona`` is accepted as an alias.
    persona_id: str | None = None
    persona: str | None = None
    role: str | None = None


class PodLeaderBody(BaseModel):
    leader_persona_id: str | None = None
    leader: str | None = None
    status: str | None = None


class CreateAutopilotBody(BaseModel):
    name: str
    title_template: str
    cron_expr: str | None = None
    spawn_tool: str | None = None
    spawn_prompt: str | None = None


class AutopilotEnabledBody(BaseModel):
    enabled: bool


class CreateSkillBody(BaseModel):
    slug: str
    name: str
    content: str | None = None


class OnboardOrg(BaseModel):
    slug: str
    name: str
    description: str | None = None


class OnboardPod(BaseModel):
    name: str
    #: The Pod's leader **Persona** (id or slug); Pods are teams of Personas.
    leader_persona_id: str | None = None
    slug: str | None = None


class OnboardBody(BaseModel):
    org: OnboardOrg
    pod: OnboardPod | None = None
    runtime_slug: str | None = None


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _authorized_org(principal: Principal, ref: str, capability: str) -> dict:
    """Resolve an Org and authorize ``capability`` on it, or raise 404/403.

    An unknown Org and an Org the principal may not read produce the same
    ``404``, so Org slugs cannot be probed.
    """
    row = orgs_ctl.get_org(ref)
    if row is None:
        raise policy.not_found("org", ref)
    policy.require_capability(principal, capability, row["id"], entity="org", ref=ref)
    return row


def _autopilot_org_id(name: str) -> int | None:
    """The Org an autopilot's Workspace belongs to, or ``None`` when unknown."""
    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import RecurringTaskDefinition

    init_db()
    with SessionLocal() as session:
        row = (
            session.query(RecurringTaskDefinition)
            .filter(RecurringTaskDefinition.name == name)
            .one_or_none()
        )
        if row is None:
            return None
        return policy.workspace_org_id(row.workspace_id)


# --------------------------------------------------------------------------- #
# Orgs
# --------------------------------------------------------------------------- #


@router.get("/orgs")
def list_orgs(
    status: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    rows = orgs_ctl.list_orgs(include_archived=(status != "active"))
    if status is not None:
        rows = [o for o in rows if o["status"] == status]
    visible = principal.visible_org_ids()
    if visible is not None:
        rows = [o for o in rows if o["id"] in visible]
    return paginate(rows, limit=limit, cursor=cursor)


@router.post("/orgs")
def create_org(
    body: CreateOrgBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    try:
        org = orgs_ctl.create_org(body.slug, body.name, description=body.description or "")
    except ValueError as exc:
        if "already exists" in str(exc):
            raise _conflict(exc) from exc
        raise _bad_request(exc) from exc
    # The caller becomes ``owner`` (§1.1). This is *not* best-effort: without
    # it the creator would immediately lose access to the Org it just made.
    if not principal.operator_slug:
        raise policy.forbidden("an Org can only be created by a named operator")
    orgs_ctl.add_member(org["id"], principal.operator_slug, role="owner")
    return org


@router.get("/orgs/{org}")
def get_org(
    org: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    return _authorized_org(principal, org, CAP_ORG_READ)


@router.patch("/orgs/{org}")
def patch_org(
    org: str,
    body: PatchOrgBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_org(principal, org, CAP_ORG_ADMIN)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        return row
    try:
        return orgs_ctl.update_org(org, **fields)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/orgs/{org}/members")
def list_members(
    org: str,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    _authorized_org(principal, org, CAP_ORG_READ)
    rows = orgs_ctl.list_members(org)
    return paginate(rows, limit=limit, cursor=cursor)


@router.get("/orgs/{org}/usage")
def org_usage_summary(
    org: str,
    days: int = 30,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Usage dashboard data scoped to one Org (F9/AC-F9-04): token + cost
    totals and the top routed models over the window, restricted to gateway
    calls this Org's own Sessions were attributed (``usage_attributions``,
    migration 136).

    Unlike ``GET /v1/usage`` (install-wide, bootstrap-admin only), this reads
    are authorized the same way every other Org surface is - any principal
    with ``org.read`` on this Org - because the response never includes a
    call this Org did not make: an unattributed call, or one attributed to
    another Org, is excluded by the SQL join rather than filtered after the
    fact.
    """
    row = _authorized_org(principal, org, CAP_ORG_READ)
    from brains.router import savings

    try:
        totals = savings.org_totals(row["id"], days=days)
    except Exception:
        totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    try:
        top_models = savings.org_top_routed_models(row["id"], days=days)
    except Exception:
        top_models = []
    return {
        "days": days,
        "scope": "org",
        "org": org,
        "org_id": row["id"],
        "totals": totals,
        "top_models": top_models,
    }


@router.get("/orgs/{org}/pods")
def list_pods(
    org: str,
    status: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """List the Org's Pods with their Persona rosters (F5).

    Returns a clean empty list (never 404) when the Org has none. Archived
    Pods are excluded unless ``status=archived`` or ``status=all`` asks for
    them, so an archived Pod is out of the way without being erased.
    """
    row = _authorized_org(principal, org, CAP_ORG_READ)
    include_archived = status in ("archived", "all")
    pods = pods_ctl.list_pods(row["id"], include_archived=include_archived)
    if status == "archived":
        pods = [pod for pod in pods if pod["status"] == "archived"]
    return paginate(pods, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/pods")
def create_pod(
    org: str,
    body: CreatePodBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Create a Pod for the Org (F5). Its leader is a Persona in the same Org."""
    row = _authorized_org(principal, org, CAP_ORG_WRITE)
    leader = body.leader_persona_id or body.leader
    try:
        return pods_ctl.create_pod(
            row["id"],
            body.slug,
            body.name,
            leader_persona=leader,
            description=body.description or "",
            operator=principal.operator_slug,
        )
    except pods_ctl.PodError as exc:
        msg = str(exc)
        if "already exists" in msg:
            raise _conflict(exc) from exc
        raise _bad_request(exc) from exc


@router.get("/orgs/{org}/autopilots")
def list_autopilots(
    org: str,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """List the org's autopilots (recurring agent tasks, F10)."""
    row = _authorized_org(principal, org, CAP_ORG_READ)
    rows = [
        task
        for task in recurring_ctl.list_recurring_tasks()
        if _autopilot_org_id(task["name"]) == row["id"]
    ]
    return paginate(rows, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/autopilots")
def create_autopilot(
    org: str,
    body: CreateAutopilotBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Create an org autopilot (recurring agent task, F10)."""
    row = _authorized_org(principal, org, CAP_ORG_ADMIN)
    try:
        return recurring_ctl.create_autopilot(
            row["id"],
            body.name,
            body.title_template,
            cron_expr=body.cron_expr or "manual",
            spawn_tool=body.spawn_tool,
            spawn_prompt=body.spawn_prompt,
        )
    except ValueError as exc:
        if "already exists" in str(exc):
            raise _conflict(exc) from exc
        raise _bad_request(exc) from exc


@router.post("/autopilots/{name}/enabled")
def set_autopilot_enabled(
    name: str,
    body: AutopilotEnabledBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    org_id = _autopilot_org_id(name)
    if org_id is None:
        raise policy.not_found("autopilot", name)
    policy.require_capability(principal, CAP_ORG_ADMIN, org_id, entity="autopilot", ref=name)
    try:
        return recurring_ctl.set_recurring_enabled(name, body.enabled)
    except ValueError as exc:
        raise policy.not_found("autopilot", name) from exc


@router.post("/autopilots/{name}/fire")
def fire_autopilot(
    name: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Fire an autopilot now (manual trigger, F10)."""
    org_id = _autopilot_org_id(name)
    if org_id is None:
        raise policy.not_found("autopilot", name)
    policy.require_capability(principal, CAP_ORG_ADMIN, org_id, entity="autopilot", ref=name)
    try:
        return recurring_ctl.fire_recurring_task(name, source="manual")
    except ValueError as exc:
        raise policy.not_found("autopilot", name) from exc


@router.get("/orgs/{org}/skills")
def list_skills(
    org: str,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """List the org's skills (SKILL.md context packs, F10)."""
    row = _authorized_org(principal, org, CAP_ORG_READ)
    return paginate(skills_ctl.list_skills(row["id"]), limit=limit, cursor=cursor)


@router.post("/orgs/{org}/skills")
def create_skill(
    org: str,
    body: CreateSkillBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_org(principal, org, CAP_ORG_WRITE)
    try:
        return skills_ctl.create_skill(row["id"], body.slug, body.name, content=body.content or "")
    except ValueError as exc:
        if "already exists" in str(exc):
            raise _conflict(exc) from exc
        raise _bad_request(exc) from exc


@router.get("/pods/{pod_id}")
def get_pod(
    pod_id: int,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = pods_ctl.get_pod(pod_id)
    org_id = policy.pod_org_id(pod_id) if row is not None else None
    policy.require_capability(principal, CAP_ORG_READ, org_id, entity="pod", ref=pod_id)
    # ``require_capability`` always refuses a ``None`` Org, so an unknown pod
    # has already raised 404 by this line.
    return cast("dict", row)


@router.get("/pods/{pod_id}/dispatch-plan")
def pod_dispatch_plan(
    pod_id: int,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Which member Persona would run this Pod's work, or why none can (F5)."""
    org_id = policy.pod_org_id(pod_id)
    policy.require_capability(principal, CAP_ORG_READ, org_id, entity="pod", ref=pod_id)
    try:
        return pods_ctl.resolve_dispatch(pod_id)
    except pods_ctl.PodError as exc:
        raise policy.not_found("pod", pod_id) from exc


@router.post("/pods/{pod_id}/members")
def add_pod_member(
    pod_id: int,
    body: PodMemberBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Add a **Persona** to a Pod (F5). Cross-Org Personas are refused."""
    org_id = policy.pod_org_id(pod_id)
    policy.require_capability(principal, CAP_ORG_WRITE, org_id, entity="pod", ref=pod_id)
    persona = body.persona_id or body.persona
    if not persona:
        raise _bad_request(
            ValueError("persona_id is required: a Pod's members are Personas, not operators")
        )
    try:
        return pods_ctl.add_member(pod_id, persona, role=body.role or "member")
    except pods_ctl.PodError as exc:
        if str(exc).startswith("unknown persona"):
            raise policy.not_found("persona", persona) from exc
        raise _bad_request(exc) from exc


@router.delete("/pods/{pod_id}/members/{persona}")
def remove_pod_member(
    pod_id: int,
    persona: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Remove a Persona from a Pod (F5). The leader is refused with the reason."""
    org_id = policy.pod_org_id(pod_id)
    policy.require_capability(principal, CAP_ORG_WRITE, org_id, entity="pod", ref=pod_id)
    try:
        return pods_ctl.remove_member(pod_id, persona)
    except pods_ctl.PodError as exc:
        if str(exc).startswith("unknown persona"):
            raise policy.not_found("persona", persona) from exc
        raise _bad_request(exc) from exc


@router.patch("/pods/{pod_id}")
def update_pod(
    pod_id: int,
    body: PodLeaderBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Replace the Pod's leader Persona, or archive the Pod (F5)."""
    org_id = policy.pod_org_id(pod_id)
    policy.require_capability(principal, CAP_ORG_ADMIN, org_id, entity="pod", ref=pod_id)
    leader = body.leader_persona_id or body.leader
    if body.status is not None and body.status not in ("active", "archived"):
        raise _bad_request(ValueError("status must be 'archived'"))
    if leader is None and body.status is None:
        raise _bad_request(ValueError("leader_persona_id or status is required"))
    try:
        result = pods_ctl.get_pod(pod_id)
        if leader is not None:
            result = pods_ctl.set_leader(pod_id, leader)
        if body.status == "archived":
            result = pods_ctl.archive_pod(pod_id)
        return cast("dict", result)
    except pods_ctl.PodError as exc:
        if str(exc).startswith("unknown pod"):
            raise policy.not_found("pod", pod_id) from exc
        if str(exc).startswith("unknown persona"):
            raise policy.not_found("persona", leader) from exc
        raise _bad_request(exc) from exc


def _existing_member(org: str, operator_ref: str) -> dict | None:
    """The membership row named by ``operator_ref``, by slug **or** numeric id.

    The path and body both accept either spelling, so an owner guard that
    matched only the slug would be bypassed by naming the same member's id.
    """
    try:
        members = orgs_ctl.list_members(org)
    except ValueError:
        return None
    for member in members:
        if member["operator"] == operator_ref or str(member["operator_id"]) == str(operator_ref):
            return member
    return None


@router.post("/orgs/{org}/members")
def add_member(
    org: str,
    body: AddMemberBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_org(principal, org, CAP_ORG_ADMIN)
    existing = _existing_member(org, body.operator_id)
    if body.role == "owner" or (existing is not None and existing["role"] == "owner"):
        # Granting ownership is an owner-only act: an ``admin`` cannot promote
        # itself or anyone else past its own role. *Changing* an existing owner
        # is owner-only for the same reason - otherwise an ``admin`` could
        # demote every owner and take the Org.
        policy.require_capability(principal, CAP_ORG_OWNER, row["id"], entity="org", ref=org)
    try:
        return orgs_ctl.add_member(org, body.operator_id, role=body.role)
    except orgs_ctl.LastOwnerError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        msg = str(exc)
        if "unknown operator" in msg:
            raise policy.not_found("operator", body.operator_id) from exc
        raise _bad_request(exc) from exc


@router.delete("/orgs/{org}/members/{operator_id}")
def remove_member(
    org: str,
    operator_id: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_org(principal, org, CAP_ORG_ADMIN)
    existing = _existing_member(org, operator_id)
    if existing is not None and existing["role"] == "owner":
        policy.require_capability(principal, CAP_ORG_OWNER, row["id"], entity="org", ref=org)
    try:
        return orgs_ctl.remove_member(org, operator_id)
    except orgs_ctl.LastOwnerError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise policy.not_found("org member", operator_id) from exc


# --------------------------------------------------------------------------- #
# Onboarding (§2): create-org → caller owner → optional first Pod
# --------------------------------------------------------------------------- #


def _onboard(body: OnboardBody, principal: Principal) -> dict:
    if not principal.operator_slug:
        raise policy.forbidden("an Org can only be created by a named operator")
    try:
        org = orgs_ctl.create_org(
            body.org.slug, body.org.name, description=body.org.description or ""
        )
    except ValueError as exc:
        if "already exists" in str(exc):
            raise _conflict(exc) from exc
        raise _bad_request(exc) from exc
    orgs_ctl.add_member(org["id"], principal.operator_slug, role="owner")
    pod = None
    if body.pod is not None:
        slug = body.pod.slug or body.pod.name.lower().replace(" ", "-")[:62]
        try:
            pod = pods_ctl.create_pod(
                org["id"],
                slug,
                body.pod.name,
                leader_persona=body.pod.leader_persona_id,
                operator=principal.operator_slug,
            )
        except pods_ctl.PodError as exc:
            # The Org is created either way; the Pod's refusal is reported
            # rather than swallowed, so the caller is not told a Pod exists.
            pod = {"created": False, "reason": str(exc)}
    return {
        "org": org,
        "owner": principal.operator_slug,
        "pod": pod,
        "runtime_slug": body.runtime_slug,
    }


@router.post("/orgs/onboard")
def onboard_org(
    body: OnboardBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    return _onboard(body, principal)


@router.post("/onboard")
def onboard(
    body: OnboardBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    return _onboard(body, principal)

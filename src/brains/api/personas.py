"""Personas REST router (WS3 §1.3).

Thin HTTP shell over :mod:`brains.control.personas`, with one explicit
authorization decision per route. Org-scoped create/list live under
``/v1/orgs/{org}/personas``; per-persona reads/edits under
``/v1/personas/{persona}``.

Every route resolves the Persona's Org and applies a capability check: reads
and content writes need ``member``, and a Persona in an Org the principal
cannot read is answered ``404`` so Persona IDs cannot be enumerated across
Orgs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from brains.api.pagination import paginate
from brains.authz import policy
from brains.authz.deps import require_operator_principal
from brains.authz.principal import CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.control import assignments as assignments_ctl
from brains.control import orgs as orgs_ctl
from brains.control import personas as personas_ctl
from brains.control import runtimes as runtimes_ctl
from brains.control import sessions as sessions_ctl
from brains.control import skills as skills_ctl

router = APIRouter(prefix="/v1")


class AttachSkillBody(BaseModel):
    skill_id: int


class CreatePersonaBody(BaseModel):
    slug: str
    name: str
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    tool: str | None = None
    default_runtime_id: int | None = None
    operator: str | None = None
    color: str | None = None
    avatar: str | None = None


class PatchPersonaBody(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    tool: str | None = None
    default_runtime_id: int | None = None
    color: str | None = None
    avatar: str | None = None


class SpawnPersonaBody(BaseModel):
    issue_id: int | None = None
    runtime_id: str | int | None = None
    prompt: str | None = None


def _authorized_org_id(principal: Principal, org: str, capability: str) -> int:
    row = orgs_ctl.get_org(org)
    if row is None:
        raise policy.not_found("org", org)
    policy.require_capability(principal, capability, row["id"], entity="org", ref=org)
    return row["id"]


def _authorized_persona(principal: Principal, persona: str, capability: str) -> dict:
    row = personas_ctl.get_persona(persona)
    if row is None:
        raise policy.not_found("persona", persona)
    policy.require_capability(
        principal, capability, row.get("org_id"), entity="persona", ref=persona
    )
    return row


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/orgs/{org}/personas")
def list_personas(
    org: str,
    status: str | None = None,
    tool: str | None = None,
    default_runtime_id: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    org_id = _authorized_org_id(principal, org, CAP_ORG_READ)
    rows = personas_ctl.list_personas(
        org_id=org_id, include_archived=(status is not None and status != "active")
    )
    if status is not None:
        rows = [p for p in rows if p["status"] == status]
    if tool is not None:
        rows = [p for p in rows if p["tool"] == tool]
    if default_runtime_id is not None:
        rows = [p for p in rows if p["default_runtime_id"] == default_runtime_id]
    return paginate(rows, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/personas")
def create_persona(
    org: str,
    body: CreatePersonaBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    org_id = _authorized_org_id(principal, org, CAP_ORG_WRITE)
    if body.default_runtime_id is not None:
        _assert_runtime_in_org(principal, body.default_runtime_id, org_id)
    try:
        return personas_ctl.create_persona(
            org_id,
            body.slug,
            body.name,
            description=body.description or "",
            system_prompt=body.system_prompt or "",
            model=body.model,
            tool=body.tool,
            default_runtime_id=body.default_runtime_id,
            operator=body.operator,
            color=body.color,
            avatar=body.avatar,
        )
    except ValueError as exc:
        if "already exists" in str(exc):
            raise _conflict(exc) from exc
        raise _bad_request(exc) from exc


def _assert_runtime_in_org(principal: Principal, runtime_ref: str | int, org_id: int) -> dict:
    """A Persona may only bind a Runtime inside its own, readable Org."""
    rt = runtimes_ctl.get_runtime(runtime_ref)
    if rt is None:
        raise policy.not_found("runtime", runtime_ref)
    runtime_org = rt.get("org_id") or policy.default_org_id()
    if runtime_org != org_id or not principal.can_see_org(runtime_org):
        raise policy.not_found("runtime", runtime_ref)
    return rt


@router.get("/personas/{persona}")
def get_persona(
    persona: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    return _authorized_persona(principal, persona, CAP_ORG_READ)


@router.patch("/personas/{persona}")
def patch_persona(
    persona: str,
    body: PatchPersonaBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_persona(principal, persona, CAP_ORG_WRITE)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        return row
    if fields.get("default_runtime_id") is not None:
        _assert_runtime_in_org(principal, fields["default_runtime_id"], row["org_id"])
    try:
        return personas_ctl.update(persona, **fields)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.delete("/personas/{persona}")
def archive_persona(
    persona: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    _authorized_persona(principal, persona, CAP_ORG_WRITE)
    try:
        return personas_ctl.archive(persona)
    except ValueError as exc:
        raise policy.not_found("persona", persona) from exc


@router.get("/personas/{persona}/sessions")
def persona_sessions(
    persona: str,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Sessions this Persona has run, filtered by Workspace visibility.

    The Persona's Org grants the listing; each row still has to pass the same
    Workspace check ``/v1/sessions`` applies, or a ``private`` Workspace would
    be readable through the Persona that worked in it.
    """
    row = _authorized_persona(principal, persona, CAP_ORG_READ)
    rows = policy.scope_sessions(principal, sessions_ctl.list_agent_sessions(persona_id=row["id"]))
    return paginate(rows, limit=limit, cursor=cursor)


@router.post("/personas/{persona}/spawn")
def spawn_persona(
    persona: str,
    body: SpawnPersonaBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Spawn alias (F0.1): create a persona-bound ``agent_session`` and queue the
    spawn order so the session appears in ``GET /v1/sessions`` immediately.

    A thin shell: it resolves the persona (slug or id), opens a pending session
    via ``sessions.open_spawn_session`` (stamped with the persona/issue/runtime
    links), then queues the daemon-pull order via ``assignments.enqueue_spawn``.
    """
    row = _authorized_persona(principal, persona, CAP_ORG_WRITE)
    if body.issue_id is not None:
        issue_org = policy.issue_org_id(body.issue_id)
        if issue_org != row["org_id"] or not principal.can_see_org(issue_org):
            raise policy.not_found("issue", body.issue_id)
    runtime_ref = body.runtime_id if body.runtime_id is not None else row["default_runtime_id"]
    runtime_id: int | None = None
    workspace_path: str | None = None
    if runtime_ref is not None:
        rt = _assert_runtime_in_org(principal, runtime_ref, row["org_id"])
        runtime_id = rt["id"]
        workspace_path = rt["working_root"]
    try:
        session_row = sessions_ctl.open_spawn_session(
            persona_id=row["id"],
            tool=row["tool"] or "copilot",
            issue_id=body.issue_id,
            runtime_id=runtime_id,
            workspace_path=workspace_path,
            org_id=row["org_id"],
        )
        result = assignments_ctl.enqueue_spawn(
            persona_id=row["id"],
            issue_id=body.issue_id,
            runtime_id=body.runtime_id,
            prompt=body.prompt,
            session_id=session_row["id"],
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {**result, "session_id": session_row["id"]}


# --------------------------------------------------------------------------- #
# Skill attachment (BL-P1-08 / AC-F10-05) — enters this Persona's spawned
# Session context (brains.control.skills.resolve_context_for_session).
# --------------------------------------------------------------------------- #


@router.get("/personas/{persona}/skills")
def list_persona_skills(
    persona: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_persona(principal, persona, CAP_ORG_READ)
    return {"data": skills_ctl.list_persona_skills(row["id"])}


@router.post("/personas/{persona}/skills")
def attach_persona_skill(
    persona: str,
    body: AttachSkillBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Attach a Skill (same Org only) to this Persona. Idempotent: attaching an
    already-attached Skill returns the existing row rather than duplicating it."""
    row = _authorized_persona(principal, persona, CAP_ORG_WRITE)
    skill = skills_ctl.get_skill(body.skill_id)
    if (
        skill is None
        or skill["org_id"] != row["org_id"]
        or not principal.can_see_org(row["org_id"])
    ):
        raise policy.not_found("skill", body.skill_id)
    try:
        return skills_ctl.attach_to_persona(
            row["id"], skill["id"], attached_by_operator_id=principal.operator_id
        )
    except skills_ctl.SkillAttachmentError as exc:
        raise _bad_request(exc) from exc


@router.delete("/personas/{persona}/skills/{skill_id}")
def detach_persona_skill(
    persona: str,
    skill_id: int,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_persona(principal, persona, CAP_ORG_WRITE)
    try:
        skills_ctl.detach_from_persona(row["id"], skill_id)
    except skills_ctl.SkillAttachmentError as exc:
        raise policy.not_found("persona skill", skill_id) from exc
    return {"detached": True, "persona_id": row["id"], "skill_id": skill_id}

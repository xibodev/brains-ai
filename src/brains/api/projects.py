"""Projects REST router (WS3 §1.4).

Thin HTTP shell over :mod:`brains.control.projects`, with one explicit
authorization decision per route. Org-scoped create/list under
``/v1/orgs/{org}/projects``; per-project reads/edits + board view under
``/v1/projects/{project}``. The control layer mints the ``PRJ-`` code.

Reads and content writes need ``member`` in the Project's Org; a Project in
another Org is answered ``404``, and a Workspace can only be attached to a
Project when it belongs to the same Org.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from brains.api.pagination import paginate
from brains.authz import policy
from brains.authz.deps import require_operator_principal
from brains.authz.principal import CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.control import issues as issues_ctl
from brains.control import orgs as orgs_ctl
from brains.control import projects as projects_ctl
from brains.control import sessions as sessions_ctl
from brains.control import skills as skills_ctl

router = APIRouter(prefix="/v1")


class AttachSkillBody(BaseModel):
    skill_id: int


class CreateProjectBody(BaseModel):
    slug: str
    name: str
    description: str | None = None
    workspace_id: int | None = None
    assignee_pod_id: int | None = None


class PatchProjectBody(BaseModel):
    name: str | None = None
    description: str | None = None
    workspace_id: int | None = None
    assignee_pod_id: int | None = None
    status: str | None = None


def _authorized_org_id(principal: Principal, org: str, capability: str) -> int:
    row = orgs_ctl.get_org(org)
    if row is None:
        raise policy.not_found("org", org)
    policy.require_capability(principal, capability, row["id"], entity="org", ref=org)
    return row["id"]


def _authorized_project(principal: Principal, project: str, capability: str) -> dict:
    row = projects_ctl.get_project(project)
    if row is None:
        raise policy.not_found("project", project)
    policy.require_capability(
        principal, capability, row.get("org_id"), entity="project", ref=project
    )
    return row


def _assert_workspace_in_org(principal: Principal, workspace_id: int, org_id: int) -> None:
    workspace_org = policy.workspace_org_id(workspace_id)
    if workspace_org != org_id or not principal.can_see_org(workspace_org):
        raise policy.not_found("workspace", workspace_id)


def _assert_pod_in_org(principal: Principal, pod_id: int, org_id: int) -> None:
    pod_org = policy.pod_org_id(pod_id)
    if pod_org != org_id or not principal.can_see_org(pod_org):
        raise policy.not_found("pod", pod_id)


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/orgs/{org}/projects")
def list_projects(
    org: str,
    status: str | None = None,
    assignee_pod_id: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    org_id = _authorized_org_id(principal, org, CAP_ORG_READ)
    rows = projects_ctl.list_projects(org_id=org_id, status=status, include_archived=True)
    if assignee_pod_id is not None:
        rows = [p for p in rows if p["assignee_pod_id"] == assignee_pod_id]
    return paginate(rows, limit=limit, cursor=cursor)


@router.get("/orgs/{org}/workspaces")
def list_workspaces(
    org: str,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    org_id = _authorized_org_id(principal, org, CAP_ORG_READ)
    visible = policy.visible_workspace_ids(principal)
    rows = sessions_ctl.list_workspaces(org_id=org_id)
    if visible is not None:
        rows = [row for row in rows if row["id"] in visible]
    return paginate(rows, limit=limit, cursor=cursor)


@router.post("/orgs/{org}/projects")
def create_project(
    org: str,
    body: CreateProjectBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    org_id = _authorized_org_id(principal, org, CAP_ORG_WRITE)
    if body.workspace_id is not None:
        _assert_workspace_in_org(principal, body.workspace_id, org_id)
    if body.assignee_pod_id is not None:
        _assert_pod_in_org(principal, body.assignee_pod_id, org_id)
    try:
        return projects_ctl.create_project(
            org_id,
            body.slug,
            body.name,
            description=body.description or "",
            workspace_id=body.workspace_id,
            assignee_pod_id=body.assignee_pod_id,
        )
    except ValueError as exc:
        if "already exists" in str(exc):
            raise _conflict(exc) from exc
        raise _bad_request(exc) from exc


@router.get("/projects/{project}")
def get_project(
    project: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    return _authorized_project(principal, project, CAP_ORG_READ)


@router.patch("/projects/{project}")
def patch_project(
    project: str,
    body: PatchProjectBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_project(principal, project, CAP_ORG_WRITE)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        return row
    if fields.get("workspace_id") is not None:
        _assert_workspace_in_org(principal, fields["workspace_id"], row["org_id"])
    if fields.get("assignee_pod_id") is not None:
        _assert_pod_in_org(principal, fields["assignee_pod_id"], row["org_id"])
    try:
        return projects_ctl.update(project, **fields)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.delete("/projects/{project}")
def archive_project(
    project: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    _authorized_project(principal, project, CAP_ORG_WRITE)
    try:
        return projects_ctl.archive(project)
    except ValueError as exc:
        raise policy.not_found("project", project) from exc


@router.get("/projects/{project}/issues")
def project_issues(
    project: str,
    status: str | None = None,
    priority: str | None = None,
    assignee_persona_id: int | None = None,
    assignee_pod_id: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    proj = _authorized_project(principal, project, CAP_ORG_READ)
    rows = issues_ctl.list_issues(
        project_id=proj["id"],
        status=status,
        priority=priority,
        assignee_persona_id=assignee_persona_id,
        assignee_pod_id=assignee_pod_id,
    )
    return paginate(rows, limit=limit, cursor=cursor)


# --------------------------------------------------------------------------- #
# Skill attachment (BL-P1-08 / AC-F10-05) — enters this Project's spawned
# Session context (brains.control.skills.resolve_context_for_session).
# --------------------------------------------------------------------------- #


@router.get("/projects/{project}/skills")
def list_project_skills(
    project: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_project(principal, project, CAP_ORG_READ)
    return {"data": skills_ctl.list_project_skills(row["id"])}


@router.post("/projects/{project}/skills")
def attach_project_skill(
    project: str,
    body: AttachSkillBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Attach a Skill (same Org only) to this Project. Idempotent: attaching an
    already-attached Skill returns the existing row rather than duplicating it."""
    row = _authorized_project(principal, project, CAP_ORG_WRITE)
    skill = skills_ctl.get_skill(body.skill_id)
    if (
        skill is None
        or skill["org_id"] != row["org_id"]
        or not principal.can_see_org(row["org_id"])
    ):
        raise policy.not_found("skill", body.skill_id)
    try:
        return skills_ctl.attach_to_project(
            row["id"], skill["id"], attached_by_operator_id=principal.operator_id
        )
    except skills_ctl.SkillAttachmentError as exc:
        raise _bad_request(exc) from exc


@router.delete("/projects/{project}/skills/{skill_id}")
def detach_project_skill(
    project: str,
    skill_id: int,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_project(principal, project, CAP_ORG_WRITE)
    try:
        skills_ctl.detach_from_project(row["id"], skill_id)
    except skills_ctl.SkillAttachmentError as exc:
        raise policy.not_found("project skill", skill_id) from exc
    return {"detached": True, "project_id": row["id"], "skill_id": skill_id}

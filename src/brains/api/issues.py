"""Issues REST router (WS3 §1.5 + §2 action endpoints).

Thin HTTP shell over :mod:`brains.control.issues`, with one explicit
authorization decision per route. The board create lives under a project
(``/v1/projects/{project}/issues``); the cross-project list + per-issue
reads/edits + the assign / transition action endpoints live under
``/v1/issues``. Mutations emit best-effort ``issue.*`` bus events (WS3 §3.3).

The cross-project list is filtered to the Orgs the principal is a member of,
and an Issue in any other Org is answered ``404``, so Issue codes and IDs
cannot be enumerated across Orgs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from brains.api.pagination import paginate
from brains.api.realtime_publish import publish_issue
from brains.authz import policy
from brains.authz.deps import require_operator_principal
from brains.authz.principal import CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.control import issues as issues_ctl
from brains.control import projects as projects_ctl

router = APIRouter(prefix="/v1")


class CreateIssueBody(BaseModel):
    title: str
    body: str | None = None
    priority: str | None = None
    parent_issue_id: int | None = None
    workspace_id: int | None = None
    labels: str | None = None


class PatchIssueBody(BaseModel):
    title: str | None = None
    body: str | None = None
    priority: str | None = None
    workspace_id: int | None = None
    parent_issue_id: int | None = None
    agent_task_code: str | None = None
    labels: str | None = None
    status: str | None = None


class AssignIssueBody(BaseModel):
    persona_id: int | None = None
    pod_id: int | None = None
    operator_id: str | None = None


class TransitionIssueBody(BaseModel):
    status: str


class CommentBody(BaseModel):
    body: str
    author_kind: str = "operator"
    operator: str | None = None
    persona_id: int | None = None
    session_id: str | None = None


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _authorized_issue(principal: Principal, issue: str, capability: str) -> dict:
    row = issues_ctl.get_issue(issue)
    if row is None:
        raise policy.not_found("issue", issue)
    org_id = policy.project_org_id(row["project_id"])
    policy.require_capability(principal, capability, org_id, entity="issue", ref=issue)
    return row


def _authorized_project(principal: Principal, project: str, capability: str) -> dict:
    row = projects_ctl.get_project(project)
    if row is None:
        raise policy.not_found("project", project)
    policy.require_capability(
        principal, capability, row.get("org_id"), entity="project", ref=project
    )
    return row


def _scope_issue_rows(principal: Principal, rows: list[dict]) -> list[dict]:
    """Drop Issues whose Project belongs to an Org the principal cannot read."""
    visible = principal.visible_org_ids()
    if visible is None:
        return rows
    allowed_projects: dict[Any, bool] = {}
    out: list[dict] = []
    for row in rows:
        project_id = row.get("project_id")
        decision = allowed_projects.get(project_id)
        if decision is None:
            org_id = policy.project_org_id(project_id)
            decision = org_id is not None and org_id in visible
            allowed_projects[project_id] = decision
        if decision:
            out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Cross-project list + board create
# --------------------------------------------------------------------------- #


@router.get("/issues")
def list_issues(
    project_id: int | None = None,
    org_id: int | None = None,
    status: str | None = None,
    priority: str | None = None,
    assignee_persona_id: int | None = None,
    assignee_pod_id: int | None = None,
    assignee_operator_id: int | None = None,
    parent_issue_id: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if org_id is not None and not principal.can_see_org(org_id):
        raise policy.not_found("org", org_id)
    if project_id is not None:
        project_org = policy.project_org_id(project_id)
        if project_org is None or not principal.can_see_org(project_org):
            raise policy.not_found("project", project_id)
    rows = issues_ctl.list_issues(
        project_id=project_id,
        org_id=org_id,
        status=status,
        priority=priority,
        parent_issue_id=parent_issue_id,
        assignee_persona_id=assignee_persona_id,
        assignee_pod_id=assignee_pod_id,
        assignee_operator_id=assignee_operator_id,
    )
    return paginate(_scope_issue_rows(principal, rows), limit=limit, cursor=cursor)


@router.post("/projects/{project}/issues")
def create_issue(
    project: str,
    body: CreateIssueBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    proj = _authorized_project(principal, project, CAP_ORG_WRITE)
    if body.workspace_id is not None:
        workspace_org = policy.workspace_org_id(body.workspace_id)
        if workspace_org != proj["org_id"] or not principal.can_see_org(workspace_org):
            raise policy.not_found("workspace", body.workspace_id)
    if body.parent_issue_id is not None:
        parent_org = policy.issue_org_id(body.parent_issue_id)
        if parent_org != proj["org_id"] or not principal.can_see_org(parent_org):
            raise policy.not_found("issue", body.parent_issue_id)
    try:
        issue = issues_ctl.create_issue(
            proj["id"],
            body.title,
            body=body.body or "",
            priority=body.priority or "p2",
            workspace_id=body.workspace_id,
            parent_issue_id=body.parent_issue_id,
            labels=body.labels,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    publish_issue("issue.created", issue)
    return issue


@router.get("/issues/{issue}")
def get_issue(
    issue: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    return _authorized_issue(principal, issue, CAP_ORG_READ)


@router.patch("/issues/{issue}")
def patch_issue(
    issue: str,
    body: PatchIssueBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_issue(principal, issue, CAP_ORG_WRITE)
    org_id = policy.project_org_id(row["project_id"])
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if data.get("workspace_id") is not None:
        workspace_org = policy.workspace_org_id(data["workspace_id"])
        if workspace_org != org_id or not principal.can_see_org(workspace_org):
            raise policy.not_found("workspace", data["workspace_id"])
    if data.get("parent_issue_id") is not None:
        parent_org = policy.issue_org_id(data["parent_issue_id"])
        if parent_org != org_id or not principal.can_see_org(parent_org):
            raise policy.not_found("issue", data["parent_issue_id"])
    status = data.pop("status", None)
    result = None
    if data:
        try:
            result = issues_ctl.update(issue, **data)
        except ValueError as exc:
            raise _bad_request(exc) from exc
    if status is not None:
        try:
            result = issues_ctl.transition(issue, status)
        except ValueError as exc:
            raise _bad_request(exc) from exc
    if result is None:
        result = row
    publish_issue("issue.updated", result)
    return result


@router.delete("/issues/{issue}")
def cancel_issue(
    issue: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    _authorized_issue(principal, issue, CAP_ORG_WRITE)
    try:
        result = issues_ctl.transition(issue, "cancelled")
    except ValueError as exc:
        raise _bad_request(exc) from exc
    publish_issue("issue.updated", result)
    return result


@router.get("/issues/{issue}/sessions")
def issue_sessions(
    issue: str,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Sessions working this Issue, filtered by Workspace visibility.

    Reading the Issue is not enough to read its Sessions: a Session in a
    ``private`` Workspace is filtered out of ``/v1/sessions`` and refused by
    id, so it is filtered out here too rather than listed under the Issue.
    """
    from brains.control import sessions as sessions_ctl

    row = _authorized_issue(principal, issue, CAP_ORG_READ)
    rows = policy.scope_sessions(principal, sessions_ctl.list_agent_sessions(issue_id=row["id"]))
    return paginate(rows, limit=limit, cursor=cursor)


@router.get("/issues/{issue}/evidence")
def issue_evidence(
    issue: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """The Issue's reconciled execution evidence (F4, AC-F4-04/05).

    Sessions, durable events, Session commands, approval decisions and the
    gateway usage attributed to those Sessions, each de-duplicated by its own
    primary key so a retried dispatch or a re-published event is counted once.
    Sessions the principal may not read are filtered out and the response says
    how many, so the rollup never leaks another Workspace's activity and never
    silently under-reports either.
    """
    from brains.control import issue_evidence as evidence_ctl

    row = _authorized_issue(principal, issue, CAP_ORG_READ)
    try:
        rollup = evidence_ctl.rollup(row["id"])
    except evidence_ctl.IssueEvidenceError as exc:
        raise policy.not_found("issue", issue) from exc
    visible = policy.scope_sessions(principal, rollup["sessions"])
    hidden = len(rollup["sessions"]) - len(visible)
    rollup["sessions"] = visible
    rollup["links"]["sessions"] = [row["id"] for row in visible]
    rollup["totals"]["hidden_sessions"] = hidden
    return rollup


@router.get("/issues/{issue}/dispatch-plan")
def issue_dispatch_plan(
    issue: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """What dispatching this Issue would do, or the stable reason it cannot (F4)."""
    from brains.control import issue_evidence as evidence_ctl

    row = _authorized_issue(principal, issue, CAP_ORG_READ)
    try:
        return evidence_ctl.dispatch_plan(row["id"])
    except evidence_ctl.IssueEvidenceError as exc:
        raise policy.not_found("issue", issue) from exc


# --------------------------------------------------------------------------- #
# Action endpoints (§2)
# --------------------------------------------------------------------------- #


@router.post("/issues/{issue}/assign")
def assign_issue(
    issue: str,
    body: AssignIssueBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    row = _authorized_issue(principal, issue, CAP_ORG_WRITE)
    org_id = policy.project_org_id(row["project_id"])
    if body.persona_id is not None:
        persona_org = policy.persona_org_id(body.persona_id)
        if persona_org != org_id or not principal.can_see_org(persona_org):
            raise policy.not_found("persona", body.persona_id)
    if body.pod_id is not None:
        pod_org = policy.pod_org_id(body.pod_id)
        if pod_org != org_id or not principal.can_see_org(pod_org):
            raise policy.not_found("pod", body.pod_id)
    try:
        result = issues_ctl.assign(
            issue,
            persona_id=body.persona_id,
            pod_id=body.pod_id,
            operator=body.operator_id,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    publish_issue("issue.assigned", result)
    return result


@router.post("/issues/{issue}/transition")
def transition_issue(
    issue: str,
    body: TransitionIssueBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    _authorized_issue(principal, issue, CAP_ORG_WRITE)
    try:
        result = issues_ctl.transition(issue, body.status)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    publish_issue("issue.updated", result)
    return result


@router.get("/issues/{issue}/comments")
def list_issue_comments(
    issue: str,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    _authorized_issue(principal, issue, CAP_ORG_READ)
    rows = issues_ctl.list_comments(issue)
    return paginate(rows, limit=limit, cursor=cursor)


@router.post("/issues/{issue}/comments")
def add_issue_comment(
    issue: str,
    body: CommentBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Post a comment on an issue (F3.3). A persona/session can self-report here
    (e.g. a reasoned 'blocked' note) so its update surfaces on the issue.

    The comment is attributed to the authenticated principal unless it names a
    Persona in the same Org: an operator cannot post as another operator.
    """
    row = _authorized_issue(principal, issue, CAP_ORG_WRITE)
    org_id = policy.project_org_id(row["project_id"])
    if body.persona_id is not None:
        persona_org = policy.persona_org_id(body.persona_id)
        if persona_org != org_id or not principal.can_see_org(persona_org):
            raise policy.not_found("persona", body.persona_id)
    operator = body.operator
    if body.author_kind == "operator":
        operator = principal.operator_slug
    try:
        result = issues_ctl.add_comment(
            issue,
            body.body,
            author_kind=body.author_kind,
            operator=operator,
            persona_id=body.persona_id,
            session_id=body.session_id,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    publish_issue("issue.commented", {"issue": issue, "comment": result})
    return result


@router.post("/issues/{issue}/dispatch")
def dispatch_issue(
    issue: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Dispatch an Issue to the Persona its assignment resolves to (F4).

    Resolution is deterministic: a Persona assignee runs on its bound Runtime,
    and a Pod assignee runs on the first member Persona - leader first, then
    members by Persona id - that is active and bound to an online Runtime in
    the Pod's Org whose tool it can drive.

    The call is idempotent while an attempt is in flight: a retried dispatch
    returns the Session the first one created with ``duplicate: true`` instead
    of spawning a second. A refusal is a ``400`` carrying one stable
    ``blocked_reason``.
    """
    from brains.control import issue_evidence as evidence_ctl

    row = _authorized_issue(principal, issue, CAP_ORG_WRITE)
    plan = evidence_ctl.dispatch_plan(row["id"])
    try:
        result = evidence_ctl.dispatch(row["id"])
    except evidence_ctl.IssueEvidenceError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": str(exc),
                "blocked_reason": plan["blocked_reason"],
                "assignee_kind": plan["assignee_kind"],
                "candidates": plan["candidates"],
            },
        ) from exc
    if not result["duplicate"]:
        publish_issue("issue.dispatched", {"issue": issue, "session_id": result["session_id"]})
    return result


@router.post("/integrations/github/webhook")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    _principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Operator-authenticated compatibility alias for the public GitHub hook."""
    from brains.api.webhooks import process_github_webhook_request

    return await process_github_webhook_request(
        request,
        x_hub_signature_256,
        x_github_delivery,
        x_github_event,
    )

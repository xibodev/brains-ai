"""Workspace-first operator console API.

The CLI, MCP, and HTTP layers are sibling adapters over ``brains.control``.
This router supplies the typed HTTP parity the canonical browser console needs;
it never launches ``brains-ai`` or exposes a generic shell/MCP-call endpoint.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from brains.authz import policy
from brains.authz.deps import require_console_principal, require_operator_principal
from brains.authz.principal import CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentTask,
    MailboxMessage,
    Workspace,
)

router = APIRouter(prefix="/v1/operator")


class TaskCreateBody(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = ""
    priority: str = "p2"
    depends_on: str = ""
    tags: str = ""


class TaskSessionBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    summary: str = ""
    reason: str = ""


class ClaimBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    scope: str = Field(default="code", min_length=1, max_length=64)
    duration_minutes: int = Field(default=30, ge=1, le=1440)


class HandoffBody(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = ""
    session_id: str | None = None


class HandoffActionBody(BaseModel):
    session_id: str | None = None
    reason: str = ""


class MessageBody(BaseModel):
    subject: str = Field(min_length=1, max_length=256)
    body: str = ""
    kind: str = Field(default="info", min_length=1, max_length=32)
    from_session_id: str | None = None
    to_session_id: str | None = None
    route_to_current: bool = False


class TopicBody(BaseModel):
    topic: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=256)
    body: str = ""
    workspace: str | None = None
    from_session_id: str | None = None
    required_tool: str | None = None
    reply_to: int | None = None
    blast: bool = False


class KnowledgeBody(BaseModel):
    type: str
    title: str = Field(min_length=1, max_length=256)
    body: str = ""
    scope: str = "workspace"
    tags: str = ""
    confidence: str = "medium"
    severity: str = "info"
    evidence: str = ""
    provenance: str = "inferred"
    importance: float = Field(default=0.5, ge=0, le=1)
    session_id: str | None = None


class KnowledgeResolveBody(BaseModel):
    status: str = "resolved"


class PatternDecisionBody(BaseModel):
    approved: bool = True


class FeedbackReportBody(BaseModel):
    category: str
    severity: str
    summary: str = Field(min_length=1, max_length=500)
    evidence: str = ""
    reproduction: str = ""
    affected_version: str | None = Field(default=None, max_length=64)
    surface: str | None = Field(default=None, max_length=128)
    reporter_session_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackEnrichmentBody(BaseModel):
    reporter_session_id: str
    kind: str = "enrichment"
    note: str = ""
    evidence: str = ""
    reproduction: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackTriageBody(BaseModel):
    status: str
    note: str = ""


class FeedbackPromotionBody(BaseModel):
    target_kind: str
    backlog_ref: str | None = None


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _record_action(
    principal: Principal,
    action: str,
    *,
    workspace_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    from brains.audit import record

    record(
        actor=principal.describe(),
        action=action,
        workspace_id=workspace_id,
        payload=payload or {},
    )


def _visible_workspace_ids(principal: Principal) -> set[int] | None:
    return policy.visible_workspace_ids(principal)


def _workspace(principal: Principal, slug: str, capability: str = CAP_ORG_READ) -> dict[str, Any]:
    init_db()
    with SessionLocal() as session:
        row = session.query(Workspace).filter(Workspace.slug == slug).one_or_none()
        if row is None:
            raise policy.not_found("workspace", slug)
        policy.require_workspace_capability(
            principal, capability, row.id, entity="workspace", ref=slug
        )
        return {
            "id": row.id,
            "slug": row.slug,
            "name": row.name,
            "path": row.path,
            "status": row.status,
            "visibility": row.visibility,
            "org_id": row.org_id,
            "last_touched_at": row.last_touched_at.isoformat() if row.last_touched_at else None,
            "last_summary": row.last_summary,
        }


def _task_workspace(principal: Principal, code: str, capability: str) -> dict[str, Any]:
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(AgentTask, Workspace)
            .join(Workspace, Workspace.id == AgentTask.workspace_id)
            .filter(AgentTask.code == code)
            .one_or_none()
        )
        if row is None:
            raise policy.not_found("task", code)
        task, workspace = row
        policy.require_workspace_capability(
            principal, capability, workspace.id, entity="task", ref=code
        )
        return {"id": workspace.id, "slug": workspace.slug, "path": workspace.path}


def _session_in_workspace(principal: Principal, session_id: str, workspace_id: int) -> dict:
    from brains.control.sessions import get_agent_session

    row = get_agent_session(session_id)
    if row is None or row.get("workspace_id") != workspace_id:
        raise policy.not_found("session", session_id)
    policy.require_workspace_capability(
        principal, CAP_ORG_WRITE, workspace_id, entity="session", ref=session_id
    )
    if row.get("ended_at") is not None:
        raise _bad_request(ValueError(f"session {session_id} has ended"))
    return row


def _events(
    principal: Principal,
    limit: int = 50,
    *,
    workspace_id: int | None = None,
) -> list[dict[str, Any]]:
    from brains.control.events import list_events

    visible = _visible_workspace_ids(principal)
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "message": row.message,
            "workspace_id": row.workspace_id,
            "session_id": row.session_id,
            "created_at": row.created_at.isoformat(),
        }
        for row in list_events(workspace_id=workspace_id, limit=limit)
        if visible is None or row.workspace_id in visible
    ]


def _open_decisions(principal: Principal, *, workspace_id: int | None = None) -> list[dict]:
    from brains.control.decisions import list_open_decisions

    rows = list_open_decisions(limit=100)
    if workspace_id is not None:
        rows = [row for row in rows if row.get("workspace_id") == workspace_id]
    return rows


def _live_agents(principal: Principal, *, workspace_id: int | None = None) -> list[dict]:
    from brains.control.topics import live_agent_sessions

    visible = _visible_workspace_ids(principal)
    init_db()
    with SessionLocal() as session:
        allowed = {
            row.id: row.slug
            for row in session.query(Workspace).all()
            if visible is None or row.id in visible
        }
    rows = live_agent_sessions()
    if workspace_id is not None:
        selected = {slug for wid, slug in allowed.items() if wid == workspace_id}
        rows = [row for row in rows if row.get("workspace") in selected]
    else:
        rows = [row for row in rows if row.get("workspace") in set(allowed.values())]
    return rows


def _workspace_rows(principal: Principal) -> list[dict[str, Any]]:
    from brains.control.claims import list_workspace_claims
    from brains.control.handoffs import list_handoffs
    from brains.control.tasks import list_tasks

    visible = _visible_workspace_ids(principal)
    init_db()
    with SessionLocal() as session:
        query = session.query(Workspace).filter(Workspace.status == "active")
        if visible is not None:
            query = query.filter(Workspace.id.in_(visible))
        workspaces = query.order_by(Workspace.name, Workspace.slug).all()
        unread_workspace_ids: set[int] = {
            workspace_id
            for (workspace_id,) in session.query(MailboxMessage.workspace_id)
            .filter(MailboxMessage.read_at.is_(None), MailboxMessage.workspace_id.is_not(None))
            .distinct()
            .all()
            if workspace_id is not None
        }
    agents = _live_agents(principal)
    visible_slugs = {row.slug for row in workspaces}
    agents_by_workspace = Counter(row.get("workspace") for row in agents)
    claims = {
        row["workspace"]: row
        for row in list_workspace_claims()
        if row.get("workspace") in visible_slugs
    }
    tasks = [row for row in list_tasks(limit=500) if row.get("workspace") in visible_slugs]
    task_counts: dict[str, Counter] = {}
    for task in tasks:
        task_counts.setdefault(task.get("workspace") or "", Counter())[
            task.get("status") or ""
        ] += 1
    decisions = Counter(row["workspace"] for row in _open_decisions(principal))
    handoffs = Counter(
        row["workspace"]
        for row in list_handoffs(active_only=True)
        if row.get("workspace") in visible_slugs
    )

    return [
        {
            "id": row.id,
            "slug": row.slug,
            "name": row.name,
            "path": row.path,
            "status": row.status,
            "visibility": row.visibility,
            "org_id": row.org_id,
            "last_touched_at": row.last_touched_at.isoformat() if row.last_touched_at else None,
            "last_summary": row.last_summary,
            "live_agents": agents_by_workspace.get(row.slug, 0),
            "claim": claims.get(row.slug),
            "tasks": dict(task_counts.get(row.slug, Counter())),
            "open_decisions": decisions.get(row.slug, 0),
            "active_handoffs": handoffs.get(row.slug, 0),
            "unread_messages": 1 if row.id in unread_workspace_ids else 0,
        }
        for row in workspaces
    ]


@router.get("/overview")
def overview(principal: Principal = Depends(require_operator_principal)) -> dict:
    from brains.audit import chain_status
    from brains.control.claims import list_workspace_claims
    from brains.control.handoffs import list_handoffs
    from brains.control.operations import readiness_report
    from brains.control.tasks import list_tasks

    workspaces = _workspace_rows(principal)
    visible_slugs = {row["slug"] for row in workspaces}
    decisions = _open_decisions(principal)
    handoffs = [
        row for row in list_handoffs(active_only=True) if row.get("workspace") in visible_slugs
    ]
    tasks = [row for row in list_tasks(limit=500) if row.get("workspace") in visible_slugs]
    claims = [row for row in list_workspace_claims() if row.get("workspace") in visible_slugs]
    agents = _live_agents(principal)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "situation": {
            "workspaces": len(workspaces),
            "live_agents": len(agents),
            "active_claims": len(claims),
            "open_decisions": len(decisions),
            "active_handoffs": len(handoffs),
            "blocked_tasks": len([row for row in tasks if row.get("status") == "blocked"]),
        },
        "workspaces": workspaces,
        "attention": {
            "decisions": decisions[:5],
            "handoffs": handoffs[:5],
        },
        "live_agents": agents,
        "recent_events": _events(principal, limit=30),
        "readiness": readiness_report() if principal.is_bootstrap_admin else None,
        "audit": chain_status() if principal.is_bootstrap_admin else None,
    }


@router.get("/workspaces")
def workspaces(principal: Principal = Depends(require_operator_principal)) -> dict:
    return {"data": _workspace_rows(principal)}


@router.get("/workspaces/{slug}")
def workspace_detail(
    slug: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.claims import list_workspace_claims
    from brains.control.handoffs import list_handoffs
    from brains.control.knowledge import search_knowledge
    from brains.control.signals import list_signals
    from brains.control.tasks import list_tasks

    workspace = _workspace(principal, slug)
    return {
        "workspace": workspace,
        "live_agents": _live_agents(principal, workspace_id=workspace["id"]),
        "sessions": _workspace_sessions(principal, workspace["id"]),
        "claims": list_workspace_claims(workspace_path=workspace["path"]),
        "tasks": list_tasks(workspace_path=workspace["path"], limit=200),
        "decisions": _open_decisions(principal, workspace_id=workspace["id"]),
        "handoffs": list_handoffs(workspace_path=workspace["path"], active_only=False),
        "knowledge": search_knowledge(workspace_path=workspace["path"], limit=50),
        "signals": list_signals(workspace_path=workspace["path"], limit=50),
        "events": _events(principal, limit=200, workspace_id=workspace["id"]),
    }


@router.get("/coordination")
def coordination(principal: Principal = Depends(require_operator_principal)) -> dict:
    from brains.control.claims import list_workspace_claims
    from brains.control.handoffs import list_handoffs
    from brains.control.knowledge import search_knowledge
    from brains.control.patterns import list_patterns
    from brains.control.signals import list_signals
    from brains.control.tasks import list_tasks
    from brains.control.topics import read_topic

    workspace_rows = _workspace_rows(principal)
    visible_slugs = {row["slug"] for row in workspace_rows}
    visible_paths = {row["path"] for row in workspace_rows}
    visible = _visible_workspace_ids(principal)
    topic_posts = read_topic(limit=100)
    if visible is not None:
        topic_posts = [row for row in topic_posts if row.get("from_workspace") in visible_slugs]
    topic_counts: dict[str, dict[str, Any]] = {}
    for post in topic_posts:
        topic = str(post["topic"])
        current = topic_counts.setdefault(
            topic,
            {"topic": topic, "posts": 0, "last_post_at": post.get("created_at")},
        )
        current["posts"] += 1
    knowledge = search_knowledge(limit=100)
    if visible is not None:
        knowledge = [
            row
            for row in knowledge
            if row.get("workspace") in visible_slugs or row.get("scope") in {"shared", "global"}
        ]
        signals = [
            signal
            for path in visible_paths
            for signal in list_signals(workspace_path=path, limit=50)
        ][:100]
    else:
        signals = list_signals(limit=100)

    return {
        "tasks": [row for row in list_tasks(limit=500) if row.get("workspace") in visible_slugs],
        "claims": [row for row in list_workspace_claims() if row.get("workspace") in visible_slugs],
        "handoffs": [
            row for row in list_handoffs(active_only=False) if row.get("workspace") in visible_slugs
        ],
        "topics": list(topic_counts.values()),
        "topic_posts": topic_posts[:50],
        "knowledge": knowledge,
        "signals": signals,
        "patterns": list_patterns(status="all", limit=100),
        "live_agents": _live_agents(principal),
    }


def _workspace_sessions(principal: Principal, workspace_id: int) -> list[dict]:
    from brains.control.sessions import list_agent_sessions

    return policy.scope_sessions(
        principal,
        list_agent_sessions(workspace_id=workspace_id, limit=100),
    )


@router.get("/governance")
def governance(principal: Principal = Depends(require_operator_principal)) -> dict:
    from brains.audit import chain_status, list_entries
    from brains.govern import list_governed_actions

    visible = _visible_workspace_ids(principal)
    actions = list_governed_actions(limit=100)
    if visible is not None:
        actions = [row for row in actions if row.get("workspace_id") in visible]
    audit = list_entries(limit=100)
    if visible is not None:
        audit = [row for row in audit if row.get("workspace_id") in visible]
    return {
        "decisions": _open_decisions(principal),
        "actions": actions,
        "audit": audit,
        "chain": chain_status() if principal.is_bootstrap_admin else None,
    }


@router.get("/operations")
def operations(principal: Principal = Depends(require_operator_principal)) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("install operations are available to the bootstrap admin only")
    from brains.control.operations import operations_snapshot

    return operations_snapshot()


@router.get("/capabilities")
def capabilities(principal: Principal = Depends(require_operator_principal)) -> dict:
    from brains.experimental import ui_labs_enabled

    native = [
        ("task.create", "Create task", "coordination", "workspace", False),
        ("task.claim", "Claim task", "coordination", "session", False),
        ("task.complete", "Complete task", "coordination", "session", False),
        ("task.release", "Release task", "coordination", "session", False),
        ("workspace.claim", "Claim workspace", "coordination", "workspace", False),
        ("workspace.release", "Release workspace", "coordination", "session", False),
        ("handoff.set", "Set handoff", "coordination", "workspace", False),
        ("handoff.pick", "Pick handoff", "coordination", "session", False),
        ("handoff.clear", "Clear handoff", "coordination", "workspace", False),
        ("message.send", "Send coordination message", "coordination", "workspace", False),
        ("topic.post", "Post topic", "coordination", "workspace", False),
        ("knowledge.add", "Add knowledge", "coordination", "workspace", False),
        ("knowledge.resolve", "Resolve knowledge", "coordination", "workspace", False),
        ("pattern.decide", "Approve or reject pattern", "coordination", "brain", True),
        ("decision.resolve", "Resolve decision", "governance", "workspace", False),
        ("session.stop", "Stop session", "governance", "session", False),
        ("audit.verify", "Verify audit chain", "governance", "install", True),
        ("tool.verify", "Verify registered tool", "operations", "install", True),
        ("queue.repair.preview", "Preview queue repair", "operations", "install", True),
    ]
    rows = [
        {
            "key": key,
            "label": label,
            "category": category,
            "scope": scope,
            "transport": "native_http",
            "enabled": not admin_only or principal.is_bootstrap_admin,
            **(
                {"reason": "this action is available to the bootstrap admin only"}
                if admin_only and not principal.is_bootstrap_admin
                else {}
            ),
        }
        for key, label, category, scope, admin_only in native
    ]
    rows.extend(
        [
            {
                "key": "service.restart",
                "label": "Restart service",
                "category": "operations",
                "scope": "install",
                "transport": "host_contract",
                "enabled": False,
                "reason": "a typed install-admin host contract is required",
            },
            {
                "key": "backup.create",
                "label": "Create backup",
                "category": "operations",
                "scope": "install",
                "transport": "thin_adapter",
                "enabled": False,
                "reason": "backup control exists but has no browser HTTP adapter yet",
            },
        ]
    )
    return {
        "data": rows,
        "labs_enabled": ui_labs_enabled(),
        "install_admin": principal.is_bootstrap_admin,
    }


@router.post("/workspaces/{slug}/tasks")
def create_task(
    slug: str,
    body: TaskCreateBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.tasks import create_task as create

    workspace = _workspace(principal, slug, CAP_ORG_WRITE)
    try:
        return create(
            workspace["path"],
            title=body.title,
            body=body.body,
            priority=body.priority,
            depends_on=body.depends_on,
            tags=body.tags,
            actor=principal.describe(),
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/tasks/{code}/claim")
def claim_task(
    code: str,
    body: TaskSessionBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.tasks import claim_task as claim

    workspace = _task_workspace(principal, code, CAP_ORG_WRITE)
    _session_in_workspace(principal, body.session_id, workspace["id"])
    try:
        return claim(code, session_id=body.session_id, actor=principal.describe())
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/tasks/{code}/complete")
def complete_task(
    code: str,
    body: TaskSessionBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.tasks import complete_task as complete

    workspace = _task_workspace(principal, code, CAP_ORG_WRITE)
    _session_in_workspace(principal, body.session_id, workspace["id"])
    try:
        return complete(
            code,
            session_id=body.session_id,
            summary=body.summary,
            actor=principal.describe(),
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/tasks/{code}/release")
def release_task(
    code: str,
    body: TaskSessionBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.tasks import release_task as release

    workspace = _task_workspace(principal, code, CAP_ORG_WRITE)
    _session_in_workspace(principal, body.session_id, workspace["id"])
    try:
        return release(
            code,
            session_id=body.session_id,
            reason=body.reason,
            actor=principal.describe(),
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/workspaces/{slug}/claims")
def claim_workspace(
    slug: str,
    body: ClaimBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.claims import claim_workspace as claim

    workspace = _workspace(principal, slug, CAP_ORG_WRITE)
    _session_in_workspace(principal, body.session_id, workspace["id"])
    try:
        result = claim(
            workspace["path"],
            session_id=body.session_id,
            scope=body.scope,
            duration_minutes=body.duration_minutes,
            metadata={"requested_by": principal.describe(), "channel": "browser"},
        )
        _record_action(
            principal,
            "workspace.claim",
            workspace_id=workspace["id"],
            payload={"scope": body.scope, "session_id": body.session_id},
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/workspaces/{slug}/claims/{session_id}")
def release_workspace_claim(
    slug: str,
    session_id: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.claims import release_workspace

    workspace = _workspace(principal, slug, CAP_ORG_WRITE)
    _session_in_workspace(principal, session_id, workspace["id"])
    result = release_workspace(workspace["path"], session_id=session_id)
    _record_action(
        principal,
        "workspace.release",
        workspace_id=workspace["id"],
        payload={"session_id": session_id, "released": result["released"]},
    )
    return result


@router.post("/workspaces/{slug}/handoffs")
def set_handoff(
    slug: str,
    body: HandoffBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.handoffs import set_handoff as set_one

    workspace = _workspace(principal, slug, CAP_ORG_WRITE)
    if body.session_id:
        _session_in_workspace(principal, body.session_id, workspace["id"])
    result = set_one(workspace["path"], body.title, body.body, session_id=body.session_id)
    _record_action(
        principal,
        "handoff.set",
        workspace_id=workspace["id"],
        payload={"handoff_id": result["handoff_id"], "title": body.title},
    )
    return result


@router.post("/workspaces/{slug}/handoffs/pick")
def pick_handoff(
    slug: str,
    body: HandoffActionBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.handoffs import pick_handoff as pick

    workspace = _workspace(principal, slug, CAP_ORG_WRITE)
    if body.session_id:
        _session_in_workspace(principal, body.session_id, workspace["id"])
    try:
        result = pick(workspace["path"], session_id=body.session_id)
        _record_action(
            principal,
            "handoff.pick",
            workspace_id=workspace["id"],
            payload={"handoff_id": result["handoff_id"], "session_id": body.session_id},
        )
        return result
    except (PermissionError, ValueError) as exc:
        raise _bad_request(exc) from exc


@router.delete("/workspaces/{slug}/handoffs")
def clear_handoff(
    slug: str,
    body: HandoffActionBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.handoffs import clear_handoff as clear

    workspace = _workspace(principal, slug, CAP_ORG_WRITE)
    if body.session_id:
        _session_in_workspace(principal, body.session_id, workspace["id"])
    try:
        result = clear(workspace["path"], body.reason, session_id=body.session_id)
        _record_action(
            principal,
            "handoff.clear",
            workspace_id=workspace["id"],
            payload={"handoff_id": result["handoff_id"], "reason": body.reason},
        )
        return result
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/workspaces/{slug}/messages")
def send_message(
    slug: str,
    body: MessageBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.mailbox import send_message as send

    workspace = _workspace(principal, slug, CAP_ORG_WRITE)
    for session_id in (body.from_session_id, body.to_session_id):
        if session_id:
            _session_in_workspace(principal, session_id, workspace["id"])
    try:
        result = send(
            body.subject,
            body.body,
            from_session_id=body.from_session_id,
            to_session_id=body.to_session_id,
            workspace_path=workspace["path"],
            kind=body.kind,
            route_to_current=body.route_to_current,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    _record_action(
        principal,
        "message.send",
        workspace_id=workspace["id"],
        payload={"message_id": result["id"], "kind": body.kind},
    )
    return result


@router.post("/topics")
def post_topic(
    body: TopicBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.topics import post_topic as post

    workspace_path = None
    workspace_id = None
    if body.workspace:
        workspace = _workspace(principal, body.workspace, CAP_ORG_WRITE)
        workspace_path = workspace["path"]
        workspace_id = workspace["id"]
    if body.from_session_id:
        if workspace_id is None:
            session_workspace = policy.session_workspace_id(body.from_session_id)
            if session_workspace is None:
                raise policy.not_found("session", body.from_session_id)
            policy.require_workspace_capability(
                principal,
                CAP_ORG_WRITE,
                session_workspace,
                entity="session",
                ref=body.from_session_id,
            )
        else:
            _session_in_workspace(principal, body.from_session_id, workspace_id)
    if workspace_id is None and body.from_session_id is None:
        raise _bad_request(ValueError("topic posts require a workspace or originating session"))
    try:
        result = post(
            body.topic,
            body.subject,
            body.body,
            from_session_id=body.from_session_id,
            workspace_path=workspace_path,
            required_tool=body.required_tool,
            reply_to=body.reply_to,
            blast=body.blast,
        )
        _record_action(
            principal,
            "topic.post",
            workspace_id=workspace_id,
            payload={"post_id": result["id"], "topic": body.topic, "blast": body.blast},
        )
        return result
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/workspaces/{slug}/knowledge")
def add_knowledge(
    slug: str,
    body: KnowledgeBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.knowledge import add_knowledge_entry

    workspace = _workspace(principal, slug, CAP_ORG_WRITE)
    if body.session_id:
        _session_in_workspace(principal, body.session_id, workspace["id"])
    try:
        result = add_knowledge_entry(
            workspace["path"],
            body.type,
            body.title,
            body=body.body,
            scope=body.scope,
            tags=body.tags,
            confidence=body.confidence,
            severity=body.severity,
            evidence=body.evidence,
            provenance=body.provenance,
            importance=body.importance,
            session_id=body.session_id,
            operator_id=principal.operator_id,
        )
        _record_action(
            principal,
            "knowledge.add",
            workspace_id=workspace["id"],
            payload={"code": result["code"], "type": body.type, "scope": body.scope},
        )
        return result
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/knowledge/{code}/resolve")
def resolve_knowledge(
    code: str,
    body: KnowledgeResolveBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.knowledge import resolve_knowledge_entry, search_knowledge

    row = next((item for item in search_knowledge(limit=500) if item["code"] == code), None)
    if row is None or not row.get("workspace"):
        raise policy.not_found("knowledge", code)
    _workspace(principal, row["workspace"], CAP_ORG_WRITE)
    try:
        result = resolve_knowledge_entry(code, status=body.status)
        workspace = _workspace(principal, row["workspace"], CAP_ORG_WRITE)
        _record_action(
            principal,
            "knowledge.resolve",
            workspace_id=workspace["id"],
            payload={"code": code, "status": body.status},
        )
        return result
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/feedback")
def feedback_list(
    workspace: str | None = None,
    status: str | None = None,
    category: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.feedback import list_feedback

    workspace_path = None
    if workspace:
        workspace_path = _workspace(principal, workspace, CAP_ORG_READ)["path"]
    return {
        "data": list_feedback(
            workspace_path,
            status=status,
            category=category,
            limit=limit,
        )
    }


@router.get("/feedback/{code}")
def feedback_get(
    code: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.feedback import get_feedback

    result = get_feedback(code)
    if result is None:
        raise policy.not_found("feedback", code)
    _workspace(principal, result["workspace"], CAP_ORG_READ)
    return result


@router.post("/workspaces/{slug}/feedback")
def feedback_report(
    slug: str,
    body: FeedbackReportBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.feedback import file_feedback

    workspace = _workspace(principal, slug, CAP_ORG_WRITE)
    if body.reporter_session_id:
        _session_in_workspace(principal, body.reporter_session_id, workspace["id"])
    try:
        return file_feedback(
            workspace["path"],
            body.category,
            body.severity,
            body.summary,
            evidence=body.evidence,
            reproduction=body.reproduction,
            affected_version=body.affected_version,
            surface=body.surface,
            reporter_session_id=body.reporter_session_id,
            metadata=body.metadata,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/feedback/{code}/enrich")
def feedback_enrich(
    code: str,
    body: FeedbackEnrichmentBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    from brains.control.feedback import enrich_feedback, get_feedback

    report = get_feedback(code)
    if report is None:
        raise policy.not_found("feedback", code)
    workspace = _workspace(principal, report["workspace"], CAP_ORG_WRITE)
    _session_in_workspace(principal, body.reporter_session_id, workspace["id"])
    try:
        return enrich_feedback(
            code,
            reporter_session_id=body.reporter_session_id,
            kind=body.kind,
            note=body.note,
            evidence=body.evidence,
            reproduction=body.reproduction,
            metadata=body.metadata,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/feedback/{code}/triage")
def feedback_triage(
    code: str,
    body: FeedbackTriageBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    from brains.control.feedback import get_feedback, triage_feedback

    report = get_feedback(code)
    if report is None:
        raise policy.not_found("feedback", code)
    _workspace(principal, report["workspace"], CAP_ORG_WRITE)
    try:
        return triage_feedback(code, body.status, note=body.note, principal=principal)
    except PermissionError as exc:
        raise policy.forbidden(str(exc)) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/feedback/{code}/promote")
def feedback_promote(
    code: str,
    body: FeedbackPromotionBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    from brains.control.feedback import get_feedback, promote_feedback

    report = get_feedback(code)
    if report is None:
        raise policy.not_found("feedback", code)
    _workspace(principal, report["workspace"], CAP_ORG_WRITE)
    try:
        return promote_feedback(
            code,
            body.target_kind,
            backlog_ref=body.backlog_ref,
            principal=principal,
        )
    except PermissionError as exc:
        raise policy.forbidden(str(exc)) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/patterns/{name}/decision")
def decide_pattern(
    name: str,
    body: PatternDecisionBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("global pattern approval is available to the bootstrap admin only")
    from brains.control.patterns import approve_pattern

    try:
        result = approve_pattern(name, approved=body.approved)
        _record_action(
            principal,
            "pattern.approve" if body.approved else "pattern.reject",
            payload={"name": name},
        )
        return result
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/tools/{name}/verify")
def verify_tool(
    name: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("tool verification is available to the bootstrap admin only")
    from brains.control.tool_registry import verify_tool as verify

    try:
        result = verify(name)
        _record_action(
            principal,
            "tool.verify",
            payload={"name": name, "available": result["is_available"]},
        )
        return result
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/audit")
def audit_entries(
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("install audit entries are available to the bootstrap admin only")
    from brains.audit import list_entries

    return {"data": list_entries(limit=limit)}


@router.get("/audit/verify")
def audit_verify(principal: Principal = Depends(require_operator_principal)) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("audit verification is available to the bootstrap admin only")
    from brains.audit import chain_status

    return chain_status()


__all__ = ["router"]

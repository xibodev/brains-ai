from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func

from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    AgentTask,
    ApprovalRequest,
    Event,
    EventContext,
    FeedbackReport,
    Handoff,
    HelpRequest,
    Issue,
    KnowledgeEntry,
    Project,
    Snapshot,
    Workspace,
)

TAXONOMY_VERSION = 1
_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("session_", "session"),
    ("spawn_session", "session"),
    ("spawn_", "session"),
    ("workspace_", "workspace"),
    ("task_", "task"),
    ("handoff", "handoff"),
    ("message_", "messaging"),
    ("inbox_", "messaging"),
    ("mailbox_", "messaging"),
    ("help_", "help"),
    ("feedback_", "feedback"),
    ("decision_", "governance"),
    ("approval_", "governance"),
    ("governed_", "governance"),
    ("audit.", "governance"),
    ("knowledge_", "knowledge"),
    ("pattern_", "knowledge"),
    ("topic_", "topic"),
    ("tool_", "tool"),
    ("runtime_", "runtime"),
    ("enrol", "runtime"),
    ("assignment_", "issue"),
    ("org_", "org"),
    ("persona_", "persona"),
    ("project_", "project"),
    ("issue_", "issue"),
    ("pod_", "pod"),
    ("squad_", "pod"),
    ("recurring_", "automation"),
    ("job_", "automation"),
    ("webhook_", "integration"),
    ("integration_", "integration"),
    ("email_", "integration"),
    ("github_", "integration"),
    ("relay_", "integration"),
    ("bridge_", "integration"),
    ("freshness_", "retrieval"),
    ("docs_", "retrieval"),
    ("repo_", "retrieval"),
    ("index_", "retrieval"),
    ("graph_", "retrieval"),
    ("source_", "retrieval"),
    ("memory_", "knowledge"),
    ("snapshot_", "snapshot"),
    ("checkpoint_", "checkpoint"),
    ("operator_", "identity"),
    ("credential_", "identity"),
    ("service_", "operations"),
    ("config_", "operations"),
    ("backup_", "operations"),
    ("restore_", "operations"),
    ("queue_", "operations"),
    ("recovery_", "operations"),
    ("provider_", "provider"),
    ("route_", "routing"),
    ("trace_", "routing"),
    ("usage_", "usage"),
    ("onboarding_", "onboarding"),
    ("execution_", "execution"),
    ("gate_", "governance"),
)
_GLOBAL_CATEGORIES = {
    "automation",
    "identity",
    "integration",
    "operations",
    "org",
    "provider",
    "routing",
    "runtime",
    "system",
    "tool",
    "usage",
}
_EXACT_CATEGORIES = {
    "blocked": "session",
}


def classify_event_kind(kind: str) -> str:
    normalized = (kind or "").strip().lower()
    if not _KIND_PATTERN.fullmatch(normalized):
        raise ValueError(
            "event kind must be 1-64 lowercase letters, digits, dot, dash, or underscore"
        )
    if normalized in _EXACT_CATEGORIES:
        return _EXACT_CATEGORIES[normalized]
    for prefix, category in _CATEGORY_PREFIXES:
        if normalized.startswith(prefix):
            return category
    return "extension"


def _workspace_from_code(session, model, code: Any) -> int | None:
    if not isinstance(code, str) or not code.strip():
        return None
    row = session.query(model.workspace_id).filter(model.code == code.strip()).one_or_none()
    return int(row[0]) if row is not None and row[0] is not None else None


def _issue_workspace(session, ref: Any) -> int | None:
    query = session.query(Issue.workspace_id, Issue.project_id)
    if isinstance(ref, int):
        row = query.filter(Issue.id == ref).one_or_none()
    elif isinstance(ref, str) and ref.strip():
        row = query.filter(Issue.code == ref.strip()).one_or_none()
    else:
        row = None
    if row is None:
        return None
    if row.workspace_id is not None:
        return int(row.workspace_id)
    project_workspace = (
        session.query(Project.workspace_id).filter(Project.id == row.project_id).scalar()
    )
    return int(project_workspace) if project_workspace is not None else None


def _project_workspace(session, ref: Any) -> int | None:
    query = session.query(Project.workspace_id)
    if isinstance(ref, int):
        value = query.filter(Project.id == ref).scalar()
    elif isinstance(ref, str) and ref.strip():
        value = query.filter(Project.code == ref.strip()).scalar()
    else:
        value = None
    return int(value) if value is not None else None


def _infer_workspace(
    session,
    category: str,
    session_id: str | None,
    metadata: dict[str, Any],
) -> tuple[int | None, str]:
    if session_id:
        agent = session.get(AgentSession, session_id)
        if agent is not None:
            return agent.workspace_id, "session"
    raw_workspace = metadata.get("workspace_id")
    if isinstance(raw_workspace, int) and session.get(Workspace, raw_workspace) is not None:
        return raw_workspace, "metadata.workspace_id"
    raw_workspace_slug = metadata.get("workspace")
    if isinstance(raw_workspace_slug, str) and raw_workspace_slug.strip():
        workspace_id = (
            session.query(Workspace.id)
            .filter(Workspace.slug == raw_workspace_slug.strip())
            .scalar()
        )
        if workspace_id is not None:
            return int(workspace_id), "metadata.workspace"
    references: tuple[tuple[str, Any, str], ...] = (
        ("task_code", AgentTask, "metadata.task_code"),
        ("task", AgentTask, "metadata.task"),
        ("approval_code", ApprovalRequest, "metadata.approval_code"),
        ("feedback_code", FeedbackReport, "metadata.feedback_code"),
        ("knowledge_code", KnowledgeEntry, "metadata.knowledge_code"),
        ("project_code", Project, "metadata.project_code"),
    )
    for key, model, source in references:
        workspace_id = _workspace_from_code(session, model, metadata.get(key))
        if workspace_id is not None:
            return workspace_id, source
    category_models = {
        "task": AgentTask,
        "governance": ApprovalRequest,
        "feedback": FeedbackReport,
        "knowledge": KnowledgeEntry,
        "project": Project,
    }
    model = category_models.get(category)
    if model is not None:
        workspace_id = _workspace_from_code(session, model, metadata.get("code"))
        if workspace_id is not None:
            return workspace_id, f"metadata.code:{category}"
    if category == "help":
        help_workspace = (
            session.query(HelpRequest.from_workspace_id)
            .filter(HelpRequest.code == metadata.get("code"))
            .scalar()
        )
        if help_workspace is not None:
            return int(help_workspace), "metadata.code:help"
    if category == "issue":
        workspace_id = _issue_workspace(session, metadata.get("code"))
        if workspace_id is not None:
            return workspace_id, "metadata.code:issue"
    for key in ("issue_id", "issue_code"):
        workspace_id = _issue_workspace(session, metadata.get(key))
        if workspace_id is not None:
            return workspace_id, f"metadata.{key}"
    raw_project_id = metadata.get("project_id")
    workspace_id = _project_workspace(session, raw_project_id)
    if workspace_id is not None:
        return workspace_id, "metadata.project_id"
    raw_handoff = metadata.get("handoff_id")
    if isinstance(raw_handoff, int):
        row = session.query(Handoff.workspace_id).filter(Handoff.id == raw_handoff).one_or_none()
        if row is not None:
            return int(row[0]), "metadata.handoff_id"
    raw_snapshot = metadata.get("snapshot_id")
    if isinstance(raw_snapshot, int):
        row = session.query(Snapshot.workspace_id).filter(Snapshot.id == raw_snapshot).one_or_none()
        if row is not None:
            return int(row[0]), "metadata.snapshot_id"
    return None, "unresolved"


def _event_scope(category: str, workspace_id: int | None, source: str) -> tuple[str, str]:
    if workspace_id is not None:
        return "workspace", source
    if category in _GLOBAL_CATEGORIES:
        return "global", "category"
    return "unresolved", source


def append_event(
    kind: str,
    message: str,
    *,
    workspace_id: int | None = None,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    renew_session: bool = True,
) -> Event:
    init_db()
    now = datetime.now(UTC)
    category = classify_event_kind(kind)
    normalized_session_id = session_id.strip() if isinstance(session_id, str) else session_id
    if not normalized_session_id:
        normalized_session_id = None
    with SessionLocal() as session:
        resolved_workspace_id = workspace_id
        scope_source = "explicit" if workspace_id is not None else "unresolved"
        if workspace_id is not None and session.get(Workspace, workspace_id) is None:
            raise ValueError(f"unknown workspace id for event: {workspace_id}")
        if resolved_workspace_id is None:
            resolved_workspace_id, scope_source = _infer_workspace(
                session, category, normalized_session_id, metadata or {}
            )
        scope, scope_source = _event_scope(category, resolved_workspace_id, scope_source)
        row = Event(
            workspace_id=resolved_workspace_id,
            session_id=normalized_session_id,
            kind=kind,
            message=message,
            metadata_json=json.dumps(metadata or {}),
        )
        session.add(row)
        session.flush()
        session.add(
            EventContext(
                event_id=row.id,
                category=category,
                scope=scope,
                scope_source=scope_source,
                taxonomy_version=TAXONOMY_VERSION,
            )
        )
        # Heartbeat: stamp last_activity_at on the originating session so
        # the reaper / resume UI can show how fresh it is, without
        # forcing every agent to call a dedicated heartbeat tool. Cheap
        # — same transaction as the event insert, single UPDATE by PK.
        if normalized_session_id and renew_session:
            agent = session.get(AgentSession, normalized_session_id)
            if agent is not None:
                from brains.control.session_liveness import renew_session_lease

                lease = renew_session_lease(session, agent, now=now, reactivate=False)
                if lease is None and agent.state != "dormant":
                    agent.last_activity_at = now
        session.commit()
        session.refresh(row)
        return row


def event_scope_report() -> dict[str, Any]:
    """Bounded operational counts for typed, global, and unresolved events."""
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(
                EventContext.scope,
                EventContext.category,
                func.count(EventContext.event_id),
            )
            .group_by(EventContext.scope, EventContext.category)
            .order_by(EventContext.scope, EventContext.category)
            .all()
        )
        events_total = session.query(func.count(Event.id)).scalar() or 0
        contexts_total = session.query(func.count(EventContext.event_id)).scalar() or 0
        unresolved = (
            session.query(Event, EventContext)
            .join(EventContext, EventContext.event_id == Event.id)
            .filter(EventContext.scope == "unresolved")
            .order_by(Event.id.desc())
            .limit(20)
            .all()
        )
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "events_total": int(events_total),
        "contexts_total": int(contexts_total),
        "missing_contexts": max(0, int(events_total) - int(contexts_total)),
        "unresolved_total": sum(
            int(count) for scope, _category, count in rows if scope == "unresolved"
        ),
        "counts": [
            {"scope": scope, "category": category, "count": int(count)}
            for scope, category, count in rows
        ],
        "unresolved": [
            {
                "event_id": event.id,
                "kind": event.kind,
                "category": context.category,
                "scope_source": context.scope_source,
            }
            for event, context in unresolved
        ],
    }


def list_events(workspace_id: int | None = None, limit: int = 100) -> list[Event]:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    # Events without a ``workspace_id`` (e.g. system-wide signals) are
    # always visible; only workspace-scoped events get filtered.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        query = session.query(Event).outerjoin(EventContext, EventContext.event_id == Event.id)
        if workspace_id is not None:
            query = query.filter(Event.workspace_id == workspace_id)
        if visible is not None:
            query = query.filter(
                (Event.workspace_id.in_(visible)) | (EventContext.scope == "global")
            )
        return query.order_by(Event.created_at.desc(), Event.id.desc()).limit(limit).all()


def get_event_context(event_id: int) -> dict[str, Any] | None:
    init_db()
    with SessionLocal() as session:
        row = session.get(EventContext, event_id)
        if row is None:
            return None
        return {
            "event_id": row.event_id,
            "category": row.category,
            "scope": row.scope,
            "scope_source": row.scope_source,
            "taxonomy_version": row.taxonomy_version,
        }

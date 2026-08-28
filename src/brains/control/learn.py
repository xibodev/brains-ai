from __future__ import annotations

from collections.abc import Iterable

import brains.storage.db as _db_module
from brains.control.knowledge import add_knowledge_entry
from brains.control.sessions import get_workspace
from brains.storage.migrations import init_db
from brains.storage.models import AgentTask, Event, KnowledgeEntry, Workspace

EVENT_BLOCKER_KINDS = {
    "blocked",
    "decision_filed",
    "help_cancelled",
    "help_expired",
    "job_failed",
    "session_reaped",
}
EVENT_WORKAROUND_KINDS = {
    "decision",
    "decision_resolved",
    "fix",
}
EVENT_SIGNAL_KINDS = EVENT_BLOCKER_KINDS | EVENT_WORKAROUND_KINDS
TASK_SIGNAL_STATUSES = {"blocked"}


def _clean_title(value: str, *, fallback: str) -> str:
    title = (value or "").strip().splitlines()[0].strip()
    if not title:
        title = fallback
    return title[:300]


def _proposal_type_for_event(kind: str) -> str:
    if kind in EVENT_WORKAROUND_KINDS:
        return "workaround"
    return "blocker"


def _visible_filter(query, column, visible: set[int] | None):
    if visible is None:
        return query
    return query.filter(column.in_(visible))


def _workspace_filter(session, workspace_path: str | None) -> int | None:
    if workspace_path is None:
        return None
    try:
        workspace = get_workspace(path=workspace_path)
    except ValueError:
        return -1
    return workspace.id


def _existing_titles(session, workspace_ids: Iterable[int]) -> set[tuple[int, str]]:
    ids = {workspace_id for workspace_id in workspace_ids if workspace_id is not None}
    if not ids:
        return set()
    rows = (
        session.query(KnowledgeEntry.workspace_id, KnowledgeEntry.title)
        .filter(KnowledgeEntry.workspace_id.in_(ids))
        .all()
    )
    return {(workspace_id, title.strip().lower()) for workspace_id, title in rows if title}


def _event_proposal(event: Event, workspace: Workspace) -> dict:
    proposal_type = _proposal_type_for_event(event.kind)
    title = _clean_title(event.message, fallback=f"{event.kind} event {event.id}")
    return {
        "type": proposal_type,
        "title": title,
        "body": f"Inferred from event {event.id} ({event.kind}): {event.message}",
        "provenance": "inferred",
        "confidence": "low",
        "workspace": workspace.slug,
    }


def _task_proposal(task: AgentTask, workspace: Workspace) -> dict:
    title = _clean_title(task.title, fallback=f"{task.status} task {task.code}")
    body_parts = [f"Inferred from task {task.code} with status {task.status}."]
    if task.body:
        body_parts.append(task.body)
    if task.completion_summary:
        body_parts.append(f"Summary: {task.completion_summary}")
    return {
        "type": "blocker",
        "title": title,
        "body": "\n\n".join(body_parts),
        "provenance": "inferred",
        "confidence": "low",
        "workspace": workspace.slug,
    }


def propose_from_history(
    workspace_path: str | None = None,
    *,
    apply: bool = False,
    limit: int = 20,
) -> dict:
    """Mine recent coordination history into human-gated knowledge proposals."""
    from brains.control.memberships import visible_workspace_ids_for_current

    init_db()
    visible = visible_workspace_ids_for_current()
    max_results = max(0, limit)
    if max_results == 0:
        return {"proposals": [], "applied": []}

    proposals: list[dict] = []
    proposal_workspaces: list[Workspace] = []
    seen: set[tuple[int, str]] = set()

    with _db_module.SessionLocal() as session:
        workspace_id = _workspace_filter(session, workspace_path)
        if workspace_id == -1:
            return {"proposals": [], "applied": []}
        if visible is not None and workspace_id is not None and workspace_id not in visible:
            return {"proposals": [], "applied": []}

        workspace_by_id = {row.id: row for row in session.query(Workspace).all()}

        event_query = session.query(Event).filter(
            Event.workspace_id.is_not(None),
            Event.kind.in_(EVENT_SIGNAL_KINDS),
        )
        task_query = session.query(AgentTask).filter(AgentTask.status.in_(TASK_SIGNAL_STATUSES))
        if workspace_id is not None:
            event_query = event_query.filter(Event.workspace_id == workspace_id)
            task_query = task_query.filter(AgentTask.workspace_id == workspace_id)
        event_query = _visible_filter(event_query, Event.workspace_id, visible)
        task_query = _visible_filter(task_query, AgentTask.workspace_id, visible)

        events = (
            event_query.order_by(Event.created_at.desc(), Event.id.desc()).limit(max_results).all()
        )
        tasks = (
            task_query.order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
            .limit(max_results)
            .all()
        )

        candidate_workspace_ids = [
            *(event.workspace_id for event in events if event.workspace_id is not None),
            *(task.workspace_id for task in tasks if task.workspace_id is not None),
        ]
        existing = _existing_titles(session, candidate_workspace_ids)

        for event in events:
            workspace = workspace_by_id.get(event.workspace_id)
            if workspace is None:
                continue
            proposal = _event_proposal(event, workspace)
            key = (workspace.id, proposal["title"].strip().lower())
            if key in existing or key in seen:
                continue
            seen.add(key)
            proposals.append(proposal)
            proposal_workspaces.append(workspace)
            if len(proposals) >= max_results:
                break

        if len(proposals) < max_results:
            for task in tasks:
                workspace = workspace_by_id.get(task.workspace_id)
                if workspace is None:
                    continue
                proposal = _task_proposal(task, workspace)
                key = (workspace.id, proposal["title"].strip().lower())
                if key in existing or key in seen:
                    continue
                seen.add(key)
                proposals.append(proposal)
                proposal_workspaces.append(workspace)
                if len(proposals) >= max_results:
                    break

    applied: list[str] = []
    if apply:
        for proposal, workspace in zip(proposals, proposal_workspaces, strict=True):
            entry = add_knowledge_entry(
                workspace.path,
                proposal["type"],
                proposal["title"],
                body=proposal["body"],
                confidence=proposal["confidence"],
                provenance=proposal["provenance"],
            )
            applied.append(entry["code"])

    return {"proposals": proposals, "applied": applied}


__all__ = [
    "EVENT_BLOCKER_KINDS",
    "EVENT_SIGNAL_KINDS",
    "EVENT_WORKAROUND_KINDS",
    "TASK_SIGNAL_STATUSES",
    "propose_from_history",
]

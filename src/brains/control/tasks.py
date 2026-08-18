from __future__ import annotations

from brains.audit import record as audit_record
from brains.control.common import (
    insert_with_code_retry,
    next_sequential_code,
    utc_now,
)
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentTask, Workspace


def _audit_actor(session_id: str | None) -> str:
    """Map a session id to the audit-log ``actor`` field.

    Sessions identify themselves to brains via ``session_id``, so the
    natural actor is ``session:<id>``. Background callers without a
    session (CLI verbs, the recurring-task scheduler) fall back to
    ``"system"`` so the audit log always has a non-empty actor.
    """
    if session_id:
        return f"session:{session_id}"
    return "system"


TASK_STATUSES = {"available", "in_progress", "blocked", "done", "archived"}
TASK_PRIORITIES = {"p0", "p1", "p2", "p3"}


def _next_code(session) -> str:
    return next_sequential_code(session, AgentTask.code, "TASK")


def _task_to_dict(row: AgentTask, workspace_slug: str | None = None) -> dict:
    return {
        "code": row.code,
        "workspace": workspace_slug,
        "title": row.title,
        "body": row.body,
        "priority": row.priority,
        "status": row.status,
        "created_by_session_id": row.created_by_session_id,
        "claimed_by_session_id": row.claimed_by_session_id,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "completion_summary": row.completion_summary,
        "depends_on": row.depends_on,
        "tags": row.tags,
        "created_at": row.created_at.isoformat(),
    }


def create_task(
    workspace_path: str,
    title: str,
    body: str = "",
    priority: str = "p2",
    depends_on: str = "",
    tags: str = "",
    session_id: str | None = None,
) -> dict:
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(TASK_PRIORITIES)}")
    workspace = register_workspace(workspace_path)
    init_db()

    def build(session):
        row = AgentTask(
            code=_next_code(session),
            workspace_id=workspace.id,
            title=title,
            body=body or None,
            priority=priority,
            status="available",
            created_by_session_id=session_id,
            depends_on=depends_on or None,
            tags=tags or None,
        )
        session.add(row)
        return row

    # Retry on the unique-code race so two sessions creating tasks at the
    # same instant on a shared (Postgres) DB can't both mint TASK-000N.
    result = insert_with_code_retry(build, lambda _s, row: _task_to_dict(row, workspace.slug))
    code = result["code"]
    append_event(
        "task_created",
        f"{code}: {title}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"code": code, "priority": priority},
    )
    audit_record(
        actor=_audit_actor(session_id),
        action="task.create",
        workspace_id=workspace.id,
        payload={"code": code, "title": title, "priority": priority},
    )
    return result


def get_task(task_code: str) -> dict | None:
    """Return a single task by code, or None if missing.

    Honours the Layer-2 workspace visibility filter — if the caller
    can't see the task's workspace, this returns None as if the task
    didn't exist, matching how the list view hides it.
    """
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(AgentTask, Workspace)
            .join(Workspace, Workspace.id == AgentTask.workspace_id)
            .filter(AgentTask.code == task_code)
            .one_or_none()
        )
        if row is None:
            return None
        task_row, workspace = row
        if visible is not None and task_row.workspace_id not in visible:
            return None
        return _task_to_dict(task_row, workspace.slug)


def list_tasks(
    workspace_path: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
    limit: int = 100,
) -> list[dict]:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        query = session.query(AgentTask, Workspace).join(
            Workspace, Workspace.id == AgentTask.workspace_id
        )
        if workspace_path:
            workspace = register_workspace(workspace_path)
            query = query.filter(AgentTask.workspace_id == workspace.id)
        if visible is not None:
            query = query.filter(AgentTask.workspace_id.in_(visible))
        if status:
            query = query.filter(AgentTask.status == status)
        else:
            query = query.filter(AgentTask.status != "archived")
        if priority:
            query = query.filter(AgentTask.priority == priority)
        if tags:
            for tag in tags.split(","):
                needle = tag.strip()
                if needle:
                    query = query.filter(AgentTask.tags.contains(needle))
        rows = (
            query.order_by(AgentTask.priority.asc(), AgentTask.created_at.asc()).limit(limit).all()
        )
        return [_task_to_dict(row, workspace.slug) for row, workspace in rows]


def claim_task(task_code: str, session_id: str) -> dict:
    now = utc_now()
    init_db()
    with SessionLocal() as session:
        row = session.query(AgentTask).filter(AgentTask.code == task_code).one_or_none()
        if row is None:
            raise ValueError(f"unknown task: {task_code}")
        if row.status != "available":
            raise ValueError(
                f"task {task_code} is {row.status}, claimed by {row.claimed_by_session_id}"
            )
        if row.depends_on:
            for dependency_code in [item.strip() for item in row.depends_on.split(",")]:
                if not dependency_code:
                    continue
                dependency = (
                    session.query(AgentTask).filter(AgentTask.code == dependency_code).one_or_none()
                )
                if dependency and dependency.status != "done":
                    raise ValueError(
                        f"dependency {dependency_code} is {dependency.status}, not done"
                    )
        row.status = "in_progress"
        row.claimed_by_session_id = session_id
        row.claimed_at = now
        workspace = session.query(Workspace).filter(Workspace.id == row.workspace_id).one()
        session.commit()
        result = _task_to_dict(row, workspace.slug)
        workspace_id = row.workspace_id
        title = row.title
    append_event(
        "task_claimed",
        f"{task_code}: {title}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"code": task_code},
    )
    return result


def complete_task(task_code: str, session_id: str, summary: str = "") -> dict:
    now = utc_now()
    init_db()
    with SessionLocal() as session:
        row = session.query(AgentTask).filter(AgentTask.code == task_code).one_or_none()
        if row is None:
            raise ValueError(f"unknown task: {task_code}")
        if row.status == "done":
            raise ValueError(f"task {task_code} is already done")
        if row.claimed_by_session_id and row.claimed_by_session_id != session_id:
            raise ValueError(f"task {task_code} is claimed by {row.claimed_by_session_id}")
        row.status = "done"
        row.completed_at = now
        row.completion_summary = summary
        workspace = session.query(Workspace).filter(Workspace.id == row.workspace_id).one()
        session.commit()
        result = _task_to_dict(row, workspace.slug)
        workspace_id = row.workspace_id
        title = row.title
    append_event(
        "task_completed",
        f"{task_code}: {summary or title}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"code": task_code},
    )
    audit_record(
        actor=_audit_actor(session_id),
        action="task.complete",
        workspace_id=workspace_id,
        payload={"code": task_code, "summary": summary},
    )
    return result


def release_task(task_code: str, session_id: str, reason: str = "") -> dict:
    init_db()
    with SessionLocal() as session:
        row = session.query(AgentTask).filter(AgentTask.code == task_code).one_or_none()
        if row is None:
            raise ValueError(f"unknown task: {task_code}")
        if row.claimed_by_session_id and row.claimed_by_session_id != session_id:
            raise ValueError(f"task {task_code} is claimed by {row.claimed_by_session_id}")
        row.status = "available"
        row.claimed_by_session_id = None
        row.claimed_at = None
        workspace_id = row.workspace_id
        title = row.title
        session.commit()
    append_event(
        "task_released",
        f"{task_code}: {reason or title}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"code": task_code, "reason": reason},
    )
    audit_record(
        actor=_audit_actor(session_id),
        action="task.release",
        workspace_id=workspace_id,
        payload={"code": task_code, "reason": reason},
    )
    return {"code": task_code, "status": "available"}


def handoff_task(
    from_task_code: str,
    *,
    title: str,
    session_id: str,
    body: str = "",
    priority: str = "p2",
    extra_depends_on: str = "",
    tags: str = "",
    completion_summary: str = "",
) -> dict:
    """Atomic agent-to-agent task handoff.

    Marks ``from_task_code`` as done and creates a follow-up task in the
    same workspace in a single DB transaction. The new task's
    ``depends_on`` is auto-prepended with ``from_task_code`` so a
    receiving agent cannot start it before the predecessor is recorded
    done — matching the roadmap pattern ``"I finished chunk A, here's
    the diff, you take chunk B"``.

    Same authority rules as :func:`complete_task`: the caller's
    ``session_id`` must either be the claim-holder of the predecessor
    or the predecessor must be unclaimed. Predecessor and follow-up
    must share a workspace (the follow-up inherits its workspace from
    ``from_task_code`` so the caller cannot accidentally hand off
    across workspace boundaries).

    Returns ``{"completed": <predecessor>, "next": <new task>}``.
    """
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(TASK_PRIORITIES)}")
    now = utc_now()
    init_db()
    holder: dict = {}

    def build(session):
        predecessor = (
            session.query(AgentTask).filter(AgentTask.code == from_task_code).one_or_none()
        )
        if predecessor is None:
            raise ValueError(f"unknown task: {from_task_code}")
        if predecessor.status == "done":
            raise ValueError(f"task {from_task_code} is already done")
        if predecessor.claimed_by_session_id and predecessor.claimed_by_session_id != session_id:
            raise ValueError(
                f"task {from_task_code} is claimed by {predecessor.claimed_by_session_id}"
            )
        predecessor.status = "done"
        predecessor.completed_at = now
        predecessor.completion_summary = completion_summary

        deps = [from_task_code]
        for extra in (extra_depends_on or "").split(","):
            cleaned = extra.strip()
            if cleaned and cleaned not in deps:
                deps.append(cleaned)
        depends_on = ",".join(deps)

        follow_up = AgentTask(
            code=_next_code(session),
            workspace_id=predecessor.workspace_id,
            title=title,
            body=body or None,
            priority=priority,
            status="available",
            created_by_session_id=session_id,
            depends_on=depends_on,
            tags=tags or None,
        )
        session.add(follow_up)
        holder["predecessor"] = predecessor
        return follow_up

    def finalize(session, follow_up):
        predecessor = holder["predecessor"]
        workspace = session.query(Workspace).filter(Workspace.id == predecessor.workspace_id).one()
        holder["workspace_id"] = predecessor.workspace_id
        holder["from_title"] = predecessor.title
        holder["next_code"] = follow_up.code
        return {
            "completed": _task_to_dict(predecessor, workspace.slug),
            "next": _task_to_dict(follow_up, workspace.slug),
        }

    # Same unique-code retry as create_task: the follow-up task must not
    # collide with a task another session mints at the same instant.
    result = insert_with_code_retry(build, finalize)
    workspace_id = holder["workspace_id"]
    from_title = holder["from_title"]
    next_code = holder["next_code"]
    append_event(
        "task_completed",
        f"{from_task_code}: {completion_summary or from_title}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"code": from_task_code, "handoff": True},
    )
    append_event(
        "task_handoff",
        f"{from_task_code} -> {next_code}: {title}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={
            "from_code": from_task_code,
            "to_code": next_code,
            "priority": priority,
        },
    )
    audit_record(
        actor=_audit_actor(session_id),
        action="task.handoff",
        workspace_id=workspace_id,
        payload={
            "from_code": from_task_code,
            "to_code": next_code,
            "title": title,
            "priority": priority,
            "completion_summary": completion_summary,
        },
    )
    return result

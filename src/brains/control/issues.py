"""Issues — units of work on the issue board (native-battalion WS2).

An *issue* has a minted ``ISS-NNNN`` code (same generator as ``agent_tasks``),
belongs to a project, and supports tri-modal assignment (persona / pod /
operator). ``closed_at`` is stamped when status enters a terminal state
(``done`` / ``cancelled``). An optional ``agent_task_code`` bridges to the
existing pull-based task engine.

Issue creation is **structured only** (AC-F4-07): a title, an optional body, a
priority, and explicit links. Brains ships no natural-language Issue parser and
no route that turns a sentence into an Issue, because a parser that guessed a
title, a priority and an assignee from prose would create work nobody reviewed.
The console's create form and the API body are the same fields, so what an
operator types is what is stored.

Execution evidence for an Issue - the Sessions, events, commands, decisions and
attributed usage it caused - lives in :mod:`brains.control.issue_evidence`.

Pure control logic — no FastAPI.
"""

from __future__ import annotations

from brains.control.common import (
    insert_with_code_retry,
    next_sequential_code,
    utc_now,
)
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    Issue,
    IssueComment,
    Operator,
    Persona,
    Project,
    Squad,
)

ISSUE_STATUSES = {
    "open",
    "in_progress",
    "blocked",
    "in_review",
    "done",
    "cancelled",
}
TERMINAL_STATUSES = {"done", "cancelled"}
ISSUE_PRIORITIES = {"p0", "p1", "p2", "p3"}


def _next_code(session) -> str:
    return next_sequential_code(session, Issue.code, "ISS")


def _issue_to_dict(i: Issue) -> dict:
    return {
        "id": i.id,
        "code": i.code,
        "project_id": i.project_id,
        "workspace_id": i.workspace_id,
        "parent_issue_id": i.parent_issue_id,
        "title": i.title,
        "body": i.body,
        "status": i.status,
        "priority": i.priority,
        "assignee_persona_id": i.assignee_persona_id,
        "assignee_pod_id": i.assignee_pod_id,
        "assignee_operator_id": i.assignee_operator_id,
        "agent_task_code": i.agent_task_code,
        "labels": i.labels,
        "created_by_session_id": i.created_by_session_id,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "updated_at": i.updated_at.isoformat() if i.updated_at else None,
        "closed_at": i.closed_at.isoformat() if i.closed_at else None,
    }


def create_issue(
    project_id: int,
    title: str,
    *,
    body: str = "",
    priority: str = "p2",
    workspace_id: int | None = None,
    parent_issue_id: int | None = None,
    agent_task_code: str | None = None,
    labels: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Create an issue under a project, minting an ``ISS-NNNN`` code.

    Raises ``ValueError`` on an unknown project or a bad priority.
    """
    if priority not in ISSUE_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(ISSUE_PRIORITIES)}")
    init_db()
    with SessionLocal() as session:
        if session.get(Project, project_id) is None:
            raise ValueError(f"unknown project id: {project_id!r}")

    def build(session):
        row = Issue(
            code=_next_code(session),
            project_id=project_id,
            workspace_id=workspace_id,
            parent_issue_id=parent_issue_id,
            title=title,
            body=body or None,
            priority=priority,
            status="open",
            agent_task_code=agent_task_code,
            labels=labels,
            created_by_session_id=session_id,
        )
        session.add(row)
        return row

    result = insert_with_code_retry(build, lambda _s, row: _issue_to_dict(row))
    append_event(
        "issue_created",
        f"{result['code']}: {title}",
        session_id=session_id,
        metadata={"code": result["code"], "project_id": project_id, "priority": priority},
    )
    return result


def get_issue(ref: str | int) -> dict | None:
    """Look up an issue by id (int) or ``ISS-NNNN`` code."""
    init_db()
    with SessionLocal() as session:
        row = _get_issue_row(session, ref)
        return _issue_to_dict(row) if row is not None else None


def _get_issue_row(session, ref: str | int) -> Issue | None:
    if isinstance(ref, int):
        return session.get(Issue, ref)
    if isinstance(ref, str):
        if ref.isdigit():
            return session.get(Issue, int(ref))
        return session.query(Issue).filter(Issue.code == ref).one_or_none()
    return None


def _comment_to_dict(c: IssueComment) -> dict:
    return {
        "id": c.id,
        "issue_id": c.issue_id,
        "body": c.body,
        "author_kind": c.author_kind,
        "author_operator_id": c.author_operator_id,
        "author_persona_id": c.author_persona_id,
        "session_id": c.session_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def add_comment(
    issue_ref: str | int,
    body: str,
    *,
    author_kind: str = "operator",
    operator: str | None = None,
    persona_id: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Post a comment on an issue (F3.3).

    A persona/session can self-report here (e.g. a reasoned 'blocked' note) so its
    update surfaces on the issue alongside human comments. ``author_kind`` is one
    of ``operator|persona|system``. Raises ``ValueError`` for an unknown issue or
    empty body.
    """
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if author_kind not in {"operator", "persona", "system"}:
        raise ValueError(f"invalid author_kind: {author_kind!r}")
    init_db()
    with SessionLocal() as session:
        issue = _get_issue_row(session, issue_ref)
        if issue is None:
            raise ValueError(f"unknown issue: {issue_ref!r}")
        operator_id = None
        if operator is not None:
            op = session.query(Operator).filter(Operator.slug == operator).one_or_none()
            if op is None:
                raise ValueError(f"unknown operator: {operator!r}")
            operator_id = op.id
        if persona_id is not None and session.get(Persona, persona_id) is None:
            raise ValueError(f"unknown persona: {persona_id!r}")
        row = IssueComment(
            issue_id=issue.id,
            body=body.strip(),
            author_kind=author_kind,
            author_operator_id=operator_id,
            author_persona_id=persona_id,
            session_id=session_id,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        result = _comment_to_dict(row)
        issue_code = issue.code
    append_event(
        "issue_comment",
        f"comment on {issue_code} ({author_kind})",
        session_id=session_id,
        metadata={"issue_id": result["issue_id"], "comment_id": result["id"]},
    )
    return result


def list_comments(issue_ref: str | int, *, limit: int = 200) -> list[dict]:
    """Comments on an issue, oldest-first (reading order)."""
    init_db()
    with SessionLocal() as session:
        issue = _get_issue_row(session, issue_ref)
        if issue is None:
            raise ValueError(f"unknown issue: {issue_ref!r}")
        rows = (
            session.query(IssueComment)
            .filter(IssueComment.issue_id == issue.id)
            .order_by(IssueComment.created_at.asc(), IssueComment.id.asc())
            .limit(limit)
            .all()
        )
        return [_comment_to_dict(c) for c in rows]


def comment_exists(issue_ref: str | int, body: str) -> bool:
    """Return whether an Issue already has this exact comment body."""
    init_db()
    with SessionLocal() as session:
        issue = _get_issue_row(session, issue_ref)
        if issue is None:
            raise ValueError(f"unknown issue: {issue_ref!r}")
        return (
            session.query(IssueComment.id)
            .filter(
                IssueComment.issue_id == issue.id,
                IssueComment.body == body,
            )
            .first()
            is not None
        )


def list_issues(
    *,
    project_id: int | None = None,
    org_id: int | None = None,
    status: str | None = None,
    priority: str | None = None,
    parent_issue_id: int | None = None,
    assignee_persona_id: int | None = None,
    assignee_pod_id: int | None = None,
    assignee_operator_id: int | None = None,
) -> list[dict]:
    init_db()
    with SessionLocal() as session:
        query = session.query(Issue)
        if project_id is not None:
            query = query.filter(Issue.project_id == project_id)
        if org_id is not None:
            # Issues are org-scoped via their project (WORKSPACE group).
            query = query.join(Project, Project.id == Issue.project_id).filter(
                Project.org_id == org_id
            )
        if status is not None:
            query = query.filter(Issue.status == status)
        if priority is not None:
            query = query.filter(Issue.priority == priority)
        if parent_issue_id is not None:
            query = query.filter(Issue.parent_issue_id == parent_issue_id)
        if assignee_persona_id is not None:
            query = query.filter(Issue.assignee_persona_id == assignee_persona_id)
        if assignee_pod_id is not None:
            query = query.filter(Issue.assignee_pod_id == assignee_pod_id)
        if assignee_operator_id is not None:
            query = query.filter(Issue.assignee_operator_id == assignee_operator_id)
        return [_issue_to_dict(i) for i in query.order_by(Issue.code).all()]


def assign(
    issue_ref: str | int,
    *,
    persona_id: int | None = None,
    pod_id: int | None = None,
    operator: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Assign an issue tri-modally. Exactly one of ``persona_id`` / ``pod_id`` /
    ``operator`` must be provided. Validates the assignee exists.

    Assignment is not mutually exclusive at the schema level, but this control
    entrypoint sets exactly one assignee target per call (clearing the others)
    so the precedence is unambiguous.
    """
    provided = [x for x in (persona_id, pod_id, operator) if x is not None]
    if len(provided) != 1:
        raise ValueError("exactly one of persona_id, pod_id, operator must be provided")
    init_db()
    with SessionLocal() as session:
        row = _get_issue_row(session, issue_ref)
        if row is None:
            raise ValueError(f"unknown issue: {issue_ref!r}")
        target = ""
        operator_id = None
        if persona_id is not None:
            persona = session.get(Persona, persona_id)
            if persona is None:
                raise ValueError(f"unknown persona id: {persona_id!r}")
            if persona.status != "active":
                raise ValueError(
                    f"persona {persona.slug!r} is archived and cannot be assigned work"
                )
            target = f"persona:{persona_id}"
        elif pod_id is not None:
            pod = session.get(Squad, pod_id)
            if pod is None:
                raise ValueError(f"unknown pod id: {pod_id!r}")
            if pod.status != "active":
                raise ValueError(f"pod {pod.slug!r} is archived and cannot be assigned work")
            target = f"pod:{pod_id}"
        else:
            op = session.query(Operator).filter(Operator.slug == operator).one_or_none()
            if op is None:
                raise ValueError(f"unknown operator: {operator!r}")
            operator_id = op.id
            target = f"operator:{operator}"
        # Set exactly one assignee, clear the others.
        row.assignee_persona_id = persona_id
        row.assignee_pod_id = pod_id
        row.assignee_operator_id = operator_id
        row.updated_at = utc_now()
        session.commit()
        session.refresh(row)
        result = _issue_to_dict(row)
    append_event(
        "issue_assigned",
        f"{result['code']} -> {target}",
        session_id=session_id,
        metadata={"code": result["code"], "assignee": target},
    )
    return result


def transition(
    issue_ref: str | int,
    status: str,
    *,
    session_id: str | None = None,
) -> dict:
    """Move an issue to ``status``. Stamps ``closed_at`` on terminal states and
    clears it when leaving a terminal state. Raises on an unknown status."""
    if status not in ISSUE_STATUSES:
        raise ValueError(f"status must be one of {sorted(ISSUE_STATUSES)}")
    init_db()
    now = utc_now()
    with SessionLocal() as session:
        row = _get_issue_row(session, issue_ref)
        if row is None:
            raise ValueError(f"unknown issue: {issue_ref!r}")
        row.status = status
        if status in TERMINAL_STATUSES:
            row.closed_at = now
        else:
            row.closed_at = None
        row.updated_at = now
        session.commit()
        session.refresh(row)
        result = _issue_to_dict(row)
    append_event(
        "issue_transitioned",
        f"{result['code']} -> {status}",
        session_id=session_id,
        metadata={"code": result["code"], "status": status},
    )
    return result


_UPDATABLE = {
    "title",
    "body",
    "priority",
    "workspace_id",
    "parent_issue_id",
    "agent_task_code",
    "labels",
}


def update(issue_ref: str | int, *, session_id: str | None = None, **fields) -> dict:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")
    if "priority" in fields and fields["priority"] not in ISSUE_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(ISSUE_PRIORITIES)}")
    init_db()
    with SessionLocal() as session:
        row = _get_issue_row(session, issue_ref)
        if row is None:
            raise ValueError(f"unknown issue: {issue_ref!r}")
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utc_now()
        session.commit()
        session.refresh(row)
        return _issue_to_dict(row)

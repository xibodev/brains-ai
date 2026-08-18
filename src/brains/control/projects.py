"""Projects — work containers (native-battalion WS2).

A *project* is scoped to an org, has a minted ``PRJ-NNNN`` code (same generator
as ``agent_tasks``), a primary repo (``workspace_id``), and an optional owning
Pod (``assignee_pod_id`` → ``squads.id``).

Pure control logic — no FastAPI.
"""

from __future__ import annotations

import re

from brains.control.common import (
    insert_with_code_retry,
    next_sequential_code,
    utc_now,
)
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Org, Project

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
PROJECT_STATUSES = {"active", "paused", "archived"}


def _next_code(session) -> str:
    return next_sequential_code(session, Project.code, "PRJ")


def _project_to_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "code": p.code,
        "org_id": p.org_id,
        "slug": p.slug,
        "name": p.name,
        "description": p.description,
        "workspace_id": p.workspace_id,
        "status": p.status,
        "assignee_pod_id": p.assignee_pod_id,
        "created_by_session_id": p.created_by_session_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def create_project(
    org_id: int,
    slug: str,
    name: str,
    *,
    description: str = "",
    workspace_id: int | None = None,
    assignee_pod_id: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Create a project under an org, minting a ``PRJ-NNNN`` code.

    Raises ``ValueError`` on a bad slug, an unknown org, or a duplicate
    ``(org, slug)``.
    """
    if not SLUG_PATTERN.match(slug):
        raise ValueError("project slug must be lowercase alphanumeric with - or _ (max 63 chars)")
    init_db()
    with SessionLocal() as session:
        if session.get(Org, org_id) is None:
            raise ValueError(f"unknown org id: {org_id!r}")
        existing = (
            session.query(Project)
            .filter(Project.org_id == org_id, Project.slug == slug)
            .one_or_none()
        )
        if existing is not None:
            raise ValueError(f"project {slug!r} already exists in org {org_id}")

    def build(session):
        row = Project(
            code=_next_code(session),
            org_id=org_id,
            slug=slug,
            name=name,
            description=description or None,
            workspace_id=workspace_id,
            assignee_pod_id=assignee_pod_id,
            created_by_session_id=session_id,
        )
        session.add(row)
        return row

    result = insert_with_code_retry(build, lambda _s, row: _project_to_dict(row))
    append_event(
        "project_created",
        f"{result['code']}: {name}",
        session_id=session_id,
        metadata={"code": result["code"], "org_id": org_id, "slug": slug},
    )
    return result


def get_project(ref: str | int, *, org_id: int | None = None) -> dict | None:
    """Look up a project by id (int), ``PRJ-NNNN`` code, or slug.

    A bare numeric string is treated as an id; a value starting ``PRJ-`` as a
    code; anything else as a slug (``org_id`` required for slug lookup).
    """
    init_db()
    with SessionLocal() as session:
        row = _get_project_row(session, ref, org_id=org_id)
        return _project_to_dict(row) if row is not None else None


def _get_project_row(session, ref: str | int, *, org_id: int | None = None) -> Project | None:
    if isinstance(ref, int):
        return session.get(Project, ref)
    if isinstance(ref, str):
        if ref.isdigit():
            return session.get(Project, int(ref))
        if ref.upper().startswith("PRJ-"):
            return session.query(Project).filter(Project.code == ref).one_or_none()
        query = session.query(Project).filter(Project.slug == ref)
        if org_id is not None:
            query = query.filter(Project.org_id == org_id)
        return query.one_or_none()
    return None


def list_projects(
    *,
    org_id: int | None = None,
    status: str | None = None,
    include_archived: bool = False,
) -> list[dict]:
    init_db()
    with SessionLocal() as session:
        query = session.query(Project)
        if org_id is not None:
            query = query.filter(Project.org_id == org_id)
        if status is not None:
            query = query.filter(Project.status == status)
        elif not include_archived:
            query = query.filter(Project.status != "archived")
        return [_project_to_dict(p) for p in query.order_by(Project.code).all()]


_UPDATABLE = {"name", "description", "workspace_id", "assignee_pod_id", "status"}


def update(
    project_ref: str | int, *, org_id: int | None = None, session_id: str | None = None, **fields
) -> dict:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")
    if "status" in fields and fields["status"] not in PROJECT_STATUSES:
        raise ValueError(f"status must be one of {sorted(PROJECT_STATUSES)}")
    init_db()
    with SessionLocal() as session:
        row = _get_project_row(session, project_ref, org_id=org_id)
        if row is None:
            raise ValueError(f"unknown project: {project_ref!r}")
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utc_now()
        session.commit()
        session.refresh(row)
        return _project_to_dict(row)


def archive(
    project_ref: str | int, *, org_id: int | None = None, session_id: str | None = None
) -> dict:
    init_db()
    with SessionLocal() as session:
        row = _get_project_row(session, project_ref, org_id=org_id)
        if row is None:
            raise ValueError(f"unknown project: {project_ref!r}")
        row.status = "archived"
        row.updated_at = utc_now()
        session.commit()
        session.refresh(row)
        result = _project_to_dict(row)
    append_event(
        "project_archived",
        f"{result['code']} archived",
        session_id=session_id,
        metadata={"code": result["code"]},
    )
    return result

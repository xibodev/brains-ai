"""Knowledge patterns — a deliberately GLOBAL, cross-operator shared library.

Unlike sessions / decisions / handoffs / tasks / mail (which are workspace-
scoped and visibility-filtered per decision record 0002), patterns have no
``workspace_id``: an approved pattern is reusable knowledge meant to be shared
across every operator and workspace. Keep pattern bodies generic and never
embed workspace-specific or sensitive content. Workspace-scoped, visibility-
aware knowledge belongs in the knowledge ledger described in
``docs/product/FEATURE_CONTRACT.md``, not here.
"""

from __future__ import annotations

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, KnowledgePattern

PATTERN_STATUSES = {"proposed", "approved", "rejected"}


def _workspace_id_for_session(session, session_id: str | None) -> int | None:
    if not session_id:
        return None
    row = session.query(AgentSession).filter(AgentSession.id == session_id).one_or_none()
    return row.workspace_id if row else None


def _pattern_to_dict(row: KnowledgePattern) -> dict:
    return {
        "name": row.name,
        "category": row.category,
        "description": row.description,
        "example": row.example,
        "applies_to": row.applies_to,
        "status": row.status,
        "proposed_by_session_id": row.proposed_by_session_id,
        "proposed_at": row.proposed_at.isoformat(),
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "usage_count": row.usage_count,
    }


def propose_pattern(
    name: str,
    category: str,
    description: str,
    *,
    example: str = "",
    applies_to: str = "",
    session_id: str | None = None,
) -> dict:
    init_db()
    with SessionLocal() as session:
        existing = (
            session.query(KnowledgePattern).filter(KnowledgePattern.name == name).one_or_none()
        )
        if existing is not None:
            raise ValueError(f"pattern name is a global id and already exists: {name}")
        row = KnowledgePattern(
            name=name,
            category=category,
            description=description,
            example=example or None,
            applies_to=applies_to or None,
            status="proposed",
            proposed_by_session_id=session_id,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        result = _pattern_to_dict(row)
        workspace_id = _workspace_id_for_session(session, session_id)
    append_event(
        "pattern_proposed",
        f"{name}: {category}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"name": name, "category": category},
    )
    return result


def approve_pattern(name: str, approved: bool = True) -> dict:
    init_db()
    with SessionLocal() as session:
        row = session.query(KnowledgePattern).filter(KnowledgePattern.name == name).one_or_none()
        if row is None:
            raise ValueError(f"unknown pattern: {name}")
        row.status = "approved" if approved else "rejected"
        row.approved_at = utc_now() if approved else None
        session.commit()
        result = _pattern_to_dict(row)
    append_event(
        "pattern_approved" if approved else "pattern_rejected",
        f"{name}: {result['status']}",
        metadata={"name": name, "status": result["status"]},
    )
    return result


def list_patterns(
    category: str | None = None,
    status: str = "approved",
    limit: int = 100,
) -> list[dict]:
    if status != "all" and status not in PATTERN_STATUSES:
        raise ValueError(f"status must be all or one of {sorted(PATTERN_STATUSES)}")
    init_db()
    with SessionLocal() as session:
        query = session.query(KnowledgePattern)
        if status != "all":
            query = query.filter(KnowledgePattern.status == status)
        if category:
            query = query.filter(KnowledgePattern.category == category)
        rows = (
            query.order_by(KnowledgePattern.usage_count.desc(), KnowledgePattern.name.asc())
            .limit(limit)
            .all()
        )
        return [_pattern_to_dict(row) for row in rows]


def use_pattern(name: str, session_id: str | None = None) -> dict:
    """Record that ``name`` was used. ``session_id`` is optional but
    strongly recommended — adoption queries join ``pattern_used`` events
    back to ``session_start`` events via ``session_id`` to compute the
    "of sessions offered an applicable pattern, how many actually used
    one?" rate.
    """
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(KnowledgePattern)
            .filter(
                KnowledgePattern.name == name,
                KnowledgePattern.status == "approved",
            )
            .one_or_none()
        )
        if row is None:
            raise ValueError(f"approved pattern not found: {name}")
        row.usage_count += 1
        session.commit()
        result = _pattern_to_dict(row)
    append_event(
        "pattern_used",
        name,
        session_id=session_id,
        metadata={"name": name, "usage_count": result["usage_count"]},
    )
    return result

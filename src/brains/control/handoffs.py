from __future__ import annotations

import os
from datetime import timedelta

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Handoff, Workspace

# Default age at which an active handoff becomes "stale" — overridable
# via ``BRAINS_HANDOFF_STALE_HOURS`` so installations can tighten or
# loosen the window without a code change. The default (24h) matches the
# original recommendation from the brains v0.0.1 forensic review.
DEFAULT_HANDOFF_STALE_HOURS = 24


def _stale_after_hours() -> int:
    try:
        return max(
            1, int(os.environ.get("BRAINS_HANDOFF_STALE_HOURS", DEFAULT_HANDOFF_STALE_HOURS))
        )
    except ValueError:
        return DEFAULT_HANDOFF_STALE_HOURS


def mark_stale_handoffs(max_age_hours: int | None = None) -> int:
    """Flip active handoffs older than ``max_age_hours`` to ``stale``.

    Returns the count flipped. Called opportunistically by
    ``list_handoffs`` so the read path always shows fresh data without a
    background scheduler tick — the cost is one extra DB write on the
    rare case where a stale row is observed.

    A handoff in ``stale`` status is still inspectable (``list_handoffs
    active_only=False``), it just no longer surfaces as "active" to a
    new session via ``start_session`` or the welcome packet.
    """
    if max_age_hours is None:
        max_age_hours = _stale_after_hours()
    init_db()
    cutoff = utc_now() - timedelta(hours=max_age_hours)
    with SessionLocal() as session:
        q = session.query(Handoff).filter(
            Handoff.status == "active",
            Handoff.set_at < cutoff,
        )
        count = q.count()
        if count:
            q.update({"status": "stale"}, synchronize_session=False)
            session.commit()
    if count:
        append_event(
            "handoffs_marked_stale",
            f"{count} handoff(s) older than {max_age_hours}h marked stale",
            metadata={"count": count, "max_age_hours": max_age_hours},
        )
    return count


def set_handoff(
    workspace_path: str,
    title: str,
    body: str = "",
    session_id: str | None = None,
) -> dict:
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        active = (
            session.query(Handoff)
            .filter(Handoff.workspace_id == workspace.id, Handoff.status == "active")
            .order_by(Handoff.set_at.desc(), Handoff.id.desc())
            .first()
        )
        if active is not None and (
            active.title,
            active.body or "",
            active.set_by_session_id,
        ) == (title, body, session_id):
            if session_id:
                from brains.control.session_liveness import renew_session_lease
                from brains.storage.models import AgentSession

                agent = session.get(AgentSession, session_id)
                if agent is not None:
                    renew_session_lease(session, agent)
                    session.commit()
            return {
                "handoff_id": active.id,
                "status": "active",
                "workspace": workspace.slug,
                "duplicate": True,
            }
        (
            session.query(Handoff)
            .filter(Handoff.workspace_id == workspace.id, Handoff.status == "active")
            .update({"status": "cleared"})
        )
        row = Handoff(
            workspace_id=workspace.id,
            title=title,
            body=body,
            set_by_session_id=session_id,
            status="active",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    append_event(
        "handoff_set",
        f"handoff set: {title}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"handoff_id": row.id},
    )
    try:
        from brains.control.views import refresh_views

        refresh_views(workspace.path)
    except Exception:
        pass
    return {
        "handoff_id": row.id,
        "status": "active",
        "workspace": workspace.slug,
        "duplicate": False,
    }


def pick_handoff(workspace_path: str, session_id: str | None = None) -> dict:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    # Refuse the pick if the resolved operator can't see the target
    # workspace. ``raise PermissionError`` so callers get a clear signal
    # rather than the misleading "no active handoff" message from the
    # query path below.
    from brains.control.memberships import (
        operator_can_see_workspace,
        visible_workspace_ids_for_current,
    )
    from brains.control.operators import resolve_current_operator

    workspace = register_workspace(workspace_path)
    visible = visible_workspace_ids_for_current()
    if visible is not None and workspace.id not in visible:
        op = resolve_current_operator()
        raise PermissionError(
            f"operator {op['slug']!r} cannot pick from workspace {workspace.slug!r} (not a member)"
        )
    # Belt + suspenders: also call the pointwise check so a future
    # caller that bypasses ``visible_workspace_ids_for_current`` still
    # gets the right answer.
    op = resolve_current_operator()
    if not operator_can_see_workspace(op.get("id"), workspace.id):
        raise PermissionError(
            f"operator {op['slug']!r} cannot pick from workspace {workspace.slug!r} (not a member)"
        )
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(Handoff)
            .filter(Handoff.workspace_id == workspace.id, Handoff.status == "active")
            .order_by(Handoff.set_at.desc(), Handoff.id.desc())
            .first()
        )
        if row is None:
            raise ValueError(f"no active handoff for {workspace.slug}")
        row.status = "picked_up"
        row.picked_up_by_session_id = session_id
        row.picked_up_at = utc_now()
        result = {
            "handoff_id": row.id,
            "title": row.title,
            "body": row.body,
            "status": row.status,
            "workspace": workspace.slug,
        }
        session.commit()
    append_event(
        "handoff_picked",
        f"handoff picked: {result['title']}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"handoff_id": result["handoff_id"]},
    )
    try:
        from brains.control.views import refresh_views

        refresh_views(workspace.path)
    except Exception:
        pass
    return result


def clear_handoff(
    workspace_path: str,
    reason: str = "",
    session_id: str | None = None,
) -> dict:
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(Handoff)
            .filter(Handoff.workspace_id == workspace.id, Handoff.status == "active")
            .order_by(Handoff.set_at.desc(), Handoff.id.desc())
            .first()
        )
        if row is None:
            raise ValueError(f"no active handoff for {workspace.slug}")
        row.status = "cleared"
        result = {"handoff_id": row.id, "status": "cleared", "workspace": workspace.slug}
        session.commit()
    append_event(
        "handoff_cleared",
        reason or "handoff cleared",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"handoff_id": result["handoff_id"]},
    )
    try:
        from brains.control.views import refresh_views

        refresh_views(workspace.path)
    except Exception:
        pass
    return result


def get_handoff(handoff_id: int) -> dict | None:
    """Return a single handoff by id, or None if missing.

    Honours the Layer-2 workspace visibility filter — if the caller
    can't see the handoff's workspace, this returns None as if the
    handoff didn't exist, matching how the list view hides it.
    """
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(Handoff, Workspace)
            .join(Workspace, Workspace.id == Handoff.workspace_id)
            .filter(Handoff.id == handoff_id)
            .one_or_none()
        )
        if row is None:
            return None
        handoff, workspace = row
        if visible is not None and handoff.workspace_id not in visible:
            return None
        return {
            "handoff_id": handoff.id,
            "workspace": workspace.slug,
            "title": handoff.title,
            "body": handoff.body,
            "status": handoff.status,
            "set_by_session_id": handoff.set_by_session_id,
            "set_at": handoff.set_at.isoformat(),
            "picked_up_by_session_id": handoff.picked_up_by_session_id,
            "picked_up_at": (handoff.picked_up_at.isoformat() if handoff.picked_up_at else None),
        }


def list_handoffs(workspace_path: str | None = None, active_only: bool = True) -> list[dict]:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    # Opportunistic staleness sweep so the read path is always honest.
    mark_stale_handoffs()
    with SessionLocal() as session:
        query = session.query(Handoff, Workspace).join(
            Workspace, Workspace.id == Handoff.workspace_id
        )
        if workspace_path:
            workspace = register_workspace(workspace_path)
            query = query.filter(Handoff.workspace_id == workspace.id)
        if active_only:
            query = query.filter(Handoff.status == "active")
        if visible is not None:
            query = query.filter(Handoff.workspace_id.in_(visible))
        rows = query.order_by(Handoff.set_at.desc(), Handoff.id.desc()).all()
        return [
            {
                "handoff_id": row.id,
                "workspace": workspace.slug,
                "title": row.title,
                "body": row.body,
                "status": row.status,
                "set_at": row.set_at.isoformat(),
            }
            for row, workspace in rows
        ]

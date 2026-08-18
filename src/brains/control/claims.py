from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Workspace, WorkspaceClaim


def _claim_to_dict(row: WorkspaceClaim, workspace_slug: str | None = None) -> dict:
    return {
        "workspace": workspace_slug,
        "session_id": row.session_id,
        "scope": row.scope,
        "claimed_at": row.claimed_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "metadata": json.loads(row.metadata_json or "{}"),
    }


def _expire_claims(session) -> int:
    now = utc_now()
    expired = session.query(WorkspaceClaim).filter(WorkspaceClaim.expires_at < now)
    count = expired.count()
    expired.delete(synchronize_session=False)
    return count


def claim_workspace(
    workspace_path: str,
    session_id: str,
    scope: str = "code",
    duration_minutes: int = 30,
    metadata: dict | None = None,
) -> dict:
    workspace = register_workspace(workspace_path)
    # Reap zombie sessions before evaluating whether the workspace is
    # already claimed — otherwise a dead session can hold a claim
    # forever, blocking every new session that tries to coordinate.
    try:
        from brains.control.sessions import reap_zombie_sessions

        reap_zombie_sessions()
    except Exception:
        pass
    now = utc_now()
    expires_at = now + timedelta(minutes=duration_minutes)
    init_db()
    with SessionLocal() as session:
        _expire_claims(session)
        existing = (
            session.query(WorkspaceClaim)
            .filter(WorkspaceClaim.workspace_id == workspace.id)
            .one_or_none()
        )
        if existing and existing.session_id != session_id:
            raise ValueError(
                f"workspace {workspace.slug} is claimed by {existing.session_id} "
                f"until {existing.expires_at.isoformat()}"
            )
        if existing:
            existing.scope = scope
            existing.expires_at = expires_at
            existing.metadata_json = json.dumps(metadata or {})
            row = existing
            session.commit()
        else:
            row = WorkspaceClaim(
                workspace_id=workspace.id,
                session_id=session_id,
                scope=scope,
                claimed_at=now,
                expires_at=expires_at,
                metadata_json=json.dumps(metadata or {}),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # A concurrent claimer committed between our existence check
                # and this insert (TOCTOU). The workspace_id primary key
                # guarantees only one claim survives; surface the same
                # graceful refusal the sequential path returns instead of
                # leaking the raw IntegrityError to the caller.
                session.rollback()
                winner = (
                    session.query(WorkspaceClaim)
                    .filter(WorkspaceClaim.workspace_id == workspace.id)
                    .one_or_none()
                )
                if winner is not None:
                    raise ValueError(
                        f"workspace {workspace.slug} is claimed by {winner.session_id} "
                        f"until {winner.expires_at.isoformat()}"
                    ) from None
                raise
        result = _claim_to_dict(row, workspace.slug)
    append_event(
        "workspace_claimed",
        f"{workspace.slug} claimed for {scope}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"scope": scope, "expires_at": expires_at.isoformat()},
    )
    return result


def release_workspace(workspace_path: str, session_id: str) -> dict:
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        count = (
            session.query(WorkspaceClaim)
            .filter(
                WorkspaceClaim.workspace_id == workspace.id,
                WorkspaceClaim.session_id == session_id,
            )
            .delete(synchronize_session=False)
        )
        session.commit()
    append_event(
        "workspace_released",
        f"{workspace.slug} released",
        workspace_id=workspace.id,
        session_id=session_id,
    )
    return {"workspace": workspace.slug, "released": count > 0}


def list_workspace_claims(
    workspace_path: str | None = None,
    include_expired: bool = False,
) -> list[dict]:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        if not include_expired:
            _expire_claims(session)
            session.commit()
        query = session.query(WorkspaceClaim, Workspace).join(
            Workspace, Workspace.id == WorkspaceClaim.workspace_id
        )
        if workspace_path:
            workspace = register_workspace(workspace_path)
            query = query.filter(WorkspaceClaim.workspace_id == workspace.id)
        if visible is not None:
            query = query.filter(WorkspaceClaim.workspace_id.in_(visible))
        rows = query.order_by(WorkspaceClaim.expires_at.asc()).all()
        return [_claim_to_dict(row, workspace.slug) for row, workspace in rows]

from __future__ import annotations

import json
from typing import Any

from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Snapshot


def _coerce_json_data(data: dict[str, Any] | str) -> str:
    parsed = json.loads(data) if isinstance(data, str) else data
    return json.dumps(parsed, sort_keys=True)


def _snapshot_to_dict(row: Snapshot, workspace_slug: str) -> dict:
    return {
        "id": row.id,
        "workspace": workspace_slug,
        "kind": row.kind,
        "data": json.loads(row.data_json),
        "captured_at": row.captured_at.isoformat(),
    }


def capture_snapshot(
    workspace_path: str,
    kind: str,
    data: dict[str, Any] | str,
    session_id: str | None = None,
) -> dict:
    workspace = register_workspace(workspace_path)
    payload = _coerce_json_data(data)
    init_db()
    with SessionLocal() as session:
        row = Snapshot(workspace_id=workspace.id, kind=kind, data_json=payload)
        session.add(row)
        session.commit()
        session.refresh(row)
        result = _snapshot_to_dict(row, workspace.slug)
    append_event(
        "snapshot_captured",
        f"{kind} snapshot captured",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"kind": kind, "snapshot_id": result["id"]},
    )
    return result


def latest_snapshot(workspace_path: str, kind: str) -> dict | None:
    from brains.control.memberships import visible_workspace_ids_for_current

    workspace = register_workspace(workspace_path)
    visible = visible_workspace_ids_for_current()
    if visible is not None and workspace.id not in visible:
        # Caller's operator has no visibility into this workspace — do not
        # leak its snapshot (decision record 0002, workspace = privacy boundary).
        return None
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(Snapshot)
            .filter(Snapshot.workspace_id == workspace.id, Snapshot.kind == kind)
            .order_by(Snapshot.captured_at.desc(), Snapshot.id.desc())
            .first()
        )
        return _snapshot_to_dict(row, workspace.slug) if row else None

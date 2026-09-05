from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from brains.control.sessions import register_workspace
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.models import Workspace


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_operator_workspace_list_hides_archived_alias_duplicates(auth_headers, tmp_path) -> None:
    client = TestClient(app)
    workspace = register_workspace(str(tmp_path / "archived-alias"), slug=_slug("archived"))
    with SessionLocal() as session:
        session.get(Workspace, workspace.id).status = "archived"
        session.commit()

    response = client.get("/v1/operator/workspaces", headers=auth_headers)
    assert response.status_code == 200
    assert workspace.id not in {row["id"] for row in response.json()["data"]}

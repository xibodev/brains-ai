from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from brains.control.sessions import register_workspace
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.models import Workspace


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_org_workspace_list_is_protected_and_org_scoped(auth_headers, tmp_path) -> None:
    client = TestClient(app)
    org_slug = _slug("org")
    other_slug = _slug("other")
    org = client.post(
        "/v1/orgs",
        json={"slug": org_slug, "name": "Workspace Org"},
        headers=auth_headers,
    ).json()
    other = client.post(
        "/v1/orgs",
        json={"slug": other_slug, "name": "Other Org"},
        headers=auth_headers,
    ).json()
    expected = register_workspace(
        str(tmp_path / "expected"),
        slug=_slug("workspace"),
        name="Expected workspace",
        org_id=org["id"],
    )
    register_workspace(
        str(tmp_path / "other"),
        slug=_slug("workspace"),
        name="Other workspace",
        org_id=other["id"],
    )

    assert client.get(f"/v1/orgs/{org_slug}/workspaces").status_code == 401
    response = client.get(f"/v1/orgs/{org_slug}/workspaces", headers=auth_headers)

    assert response.status_code == 200
    rows = response.json()["data"]
    assert [row["id"] for row in rows] == [expected.id]
    assert rows[0]["path"] == str(tmp_path / "expected")


def test_operator_workspace_list_hides_archived_alias_duplicates(auth_headers, tmp_path) -> None:
    client = TestClient(app)
    workspace = register_workspace(str(tmp_path / "archived-alias"), slug=_slug("archived"))
    with SessionLocal() as session:
        session.get(Workspace, workspace.id).status = "archived"
        session.commit()

    response = client.get("/v1/operator/workspaces", headers=auth_headers)
    assert response.status_code == 200
    assert workspace.id not in {row["id"] for row in response.json()["data"]}

"""Workspace-first operator console API: scope, parity, and attribution."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from brains.audit import list_entries
from brains.authz import credentials as creds
from brains.control import orgs as orgs_ctl
from brains.control.handoffs import set_handoff
from brains.control.operators import add_operator, ensure_admin_operator
from brains.control.sessions import register_workspace, start_session
from brains.control.tasks import create_task
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import KnowledgeEntry


@pytest.fixture(autouse=True)
def _bootstrap():
    init_db()
    ensure_admin_operator()
    creds.sync_local_credentials()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _operator(client: TestClient, org_id: int) -> dict[str, str]:
    record, key = add_operator(_slug("console"))
    orgs_ctl.add_member(org_id, record["slug"], role="member")
    creds.sync_local_credentials()
    return {"Authorization": f"Bearer {key}"}


def test_operator_console_routes_require_auth(client):
    for path in (
        "/v1/operator/overview",
        "/v1/operator/workspaces",
        "/v1/operator/coordination",
        "/v1/operator/governance",
        "/v1/operator/capabilities",
    ):
        assert client.get(path).status_code == 401


def test_overview_and_workspace_detail_are_cross_workspace_but_scoped(
    client, auth_headers, tmp_path
):
    own_org = orgs_ctl.create_org(_slug("own"), "Own")
    other_org = orgs_ctl.create_org(_slug("other"), "Other")
    own = register_workspace(str(tmp_path / "own"), slug=_slug("own-ws"), org_id=own_org["id"])
    other = register_workspace(
        str(tmp_path / "other"), slug=_slug("other-ws"), org_id=other_org["id"]
    )
    headers = _operator(client, own_org["id"])

    overview = client.get("/v1/operator/overview", headers=headers)
    assert overview.status_code == 200, overview.text
    slugs = {row["slug"] for row in overview.json()["workspaces"]}
    assert own.slug in slugs
    assert other.slug not in slugs
    assert client.get(f"/v1/operator/workspaces/{own.slug}", headers=headers).status_code == 200
    assert client.get(f"/v1/operator/workspaces/{other.slug}", headers=headers).status_code == 404

    # Bootstrap admin keeps its documented install-wide view.
    admin_slugs = {
        row["slug"]
        for row in client.get("/v1/operator/overview", headers=auth_headers).json()["workspaces"]
    }
    assert {own.slug, other.slug} <= admin_slugs


def test_operator_aggregates_do_not_leak_other_org_coordination(client, tmp_path):
    own_org = orgs_ctl.create_org(_slug("own"), "Own")
    other_org = orgs_ctl.create_org(_slug("other"), "Other")
    own = register_workspace(str(tmp_path / "own"), slug=_slug("own-ws"), org_id=own_org["id"])
    other = register_workspace(
        str(tmp_path / "other"), slug=_slug("other-ws"), org_id=other_org["id"]
    )
    create_task(own.path, "Visible task")
    create_task(other.path, "Hidden task")
    set_handoff(other.path, "Hidden handoff")
    headers = _operator(client, own_org["id"])

    overview = client.get("/v1/operator/overview", headers=headers).json()
    assert overview["situation"]["active_handoffs"] == 0
    coordination = client.get("/v1/operator/coordination", headers=headers).json()
    assert {row["title"] for row in coordination["tasks"]} == {"Visible task"}
    assert all(row["workspace"] == own.slug for row in coordination["tasks"])
    assert coordination["handoffs"] == []


def test_task_create_uses_control_layer_and_attributes_browser_operator(
    client, auth_headers, tmp_path
):
    org = orgs_ctl.create_org(_slug("org"), "Org")
    workspace = register_workspace(str(tmp_path / "tasks"), slug=_slug("tasks"), org_id=org["id"])
    response = client.post(
        f"/v1/operator/workspaces/{workspace.slug}/tasks",
        json={"title": "Build the operator console", "priority": "p1"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["workspace"] == workspace.slug
    matching = [
        row
        for row in list_entries(action_prefix="task.create")
        if row["payload"].get("code") == task["code"]
    ]
    assert matching
    assert matching[0]["actor"] == "operator:admin"


def test_task_claim_requires_a_live_session_in_the_same_workspace(client, auth_headers, tmp_path):
    org = orgs_ctl.create_org(_slug("org"), "Org")
    first = register_workspace(str(tmp_path / "first"), slug=_slug("first"), org_id=org["id"])
    second = register_workspace(str(tmp_path / "second"), slug=_slug("second"), org_id=org["id"])
    task = client.post(
        f"/v1/operator/workspaces/{first.slug}/tasks",
        json={"title": "Scoped task"},
        headers=auth_headers,
    ).json()
    first_session = start_session(first.path, tool="pytest")["session_id"]
    second_session = start_session(second.path, tool="pytest")["session_id"]

    refused = client.post(
        f"/v1/operator/tasks/{task['code']}/claim",
        json={"session_id": second_session},
        headers=auth_headers,
    )
    assert refused.status_code == 404
    claimed = client.post(
        f"/v1/operator/tasks/{task['code']}/claim",
        json={"session_id": first_session},
        headers=auth_headers,
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "in_progress"
    audit = [
        row
        for row in list_entries(action_prefix="task.claim")
        if row["payload"].get("code") == task["code"]
    ]
    assert audit and audit[0]["actor"] == "operator:admin"


def test_operations_is_install_admin_only(client, auth_headers):
    org = orgs_ctl.create_org(_slug("org"), "Org")
    member_headers = _operator(client, org["id"])
    assert client.get("/v1/operator/operations", headers=member_headers).status_code == 403
    assert client.get("/v1/operator/operations", headers=auth_headers).status_code == 200


def test_capability_catalog_never_presents_shell_execution(client, auth_headers):
    response = client.get("/v1/operator/capabilities", headers=auth_headers)
    assert response.status_code == 200
    rows = response.json()["data"]
    assert response.json()["labs_enabled"] is False
    assert response.json()["install_admin"] is True
    assert rows
    assert {row["transport"] for row in rows} <= {
        "native_http",
        "thin_adapter",
        "host_contract",
    }
    assert all("command" not in row and "argv" not in row for row in rows)
    assert all(not row["enabled"] for row in rows if row["transport"] != "native_http")


def test_member_capabilities_disable_install_and_global_admin_actions(client):
    org = orgs_ctl.create_org(_slug("org"), "Org")
    headers = _operator(client, org["id"])
    body = client.get("/v1/operator/capabilities", headers=headers).json()
    assert body["install_admin"] is False
    by_key = {row["key"]: row for row in body["data"]}
    for key in ("pattern.decide", "audit.verify", "tool.verify", "queue.repair.preview"):
        assert by_key[key]["enabled"] is False
        assert "bootstrap admin" in by_key[key]["reason"]


def test_browser_topic_post_is_workspace_scoped_without_default_blast(
    client, auth_headers, tmp_path
):
    org = orgs_ctl.create_org(_slug("org"), "Org")
    workspace = register_workspace(str(tmp_path / "topics"), slug=_slug("topics"), org_id=org["id"])
    response = client.post(
        "/v1/operator/topics",
        json={"workspace": workspace.slug, "topic": "status", "subject": "Scoped update"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["notified_workspaces"] == []


def test_browser_knowledge_persists_authenticated_operator(client, auth_headers, tmp_path):
    org = orgs_ctl.create_org(_slug("org"), "Org")
    workspace = register_workspace(
        str(tmp_path / "knowledge"), slug=_slug("knowledge"), org_id=org["id"]
    )
    response = client.post(
        f"/v1/operator/workspaces/{workspace.slug}/knowledge",
        json={"type": "caveat", "title": "Browser-authored caveat"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        row = session.query(KnowledgeEntry).filter_by(code=response.json()["code"]).one()
        assert row.created_by_operator_id is not None


def test_unscoped_browser_topic_post_is_refused(client, auth_headers):
    response = client.post(
        "/v1/operator/topics",
        json={"topic": "release", "subject": "Status"},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "workspace" in response.text

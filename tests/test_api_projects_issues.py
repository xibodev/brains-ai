"""HTTP tests for the WS3 projects + issues routers (``/v1/projects/*``, ``/v1/issues/*``).

Covers CRUD happy paths, 401-without-auth, unique conflicts, the
``{data, next_cursor}`` wrapper, the board view, cross-project list filters, and
the assign (tri-modal) + transition action endpoints — asserting each emits the
right ``issue.*`` bus topic and that transition stamps ``closed_at``.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from fastapi.testclient import TestClient

from brains.control import orgs as orgs_ctl
from brains.control import personas as personas_ctl
from brains.control import squads as squads_ctl
from brains.control.operators import add_operator, ensure_admin_operator
from brains.events.bus import bus
from brains.main import app


@pytest.fixture(autouse=True)
def _bootstrap():
    from brains.storage.migrations import init_db

    init_db()
    ensure_admin_operator()
    for slug in ("alice", "bob"):
        with contextlib.suppress(Exception):
            add_operator(slug)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def org():
    return orgs_ctl.create_org(_slug("org"), "Acme")


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_list_issues_requires_auth(client):
    assert client.get("/v1/issues").status_code in (401, 403)


def test_create_project_requires_auth(client, org):
    resp = client.post(f"/v1/orgs/{org['slug']}/projects", json={"slug": "x", "name": "X"})
    assert resp.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Projects CRUD
# --------------------------------------------------------------------------- #


def test_create_project_mints_code(client, auth_headers, org):
    slug = _slug("proj")
    resp = client.post(
        f"/v1/orgs/{org['slug']}/projects",
        json={"slug": slug, "name": "Apollo"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    proj = resp.json()
    assert proj["code"].startswith("PRJ-")
    got = client.get(f"/v1/projects/{proj['code']}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == proj["id"]


def test_create_project_duplicate_slug_conflicts(client, auth_headers, org):
    slug = _slug("proj")
    client.post(
        f"/v1/orgs/{org['slug']}/projects",
        json={"slug": slug, "name": "A"},
        headers=auth_headers,
    )
    dup = client.post(
        f"/v1/orgs/{org['slug']}/projects",
        json={"slug": slug, "name": "B"},
        headers=auth_headers,
    )
    assert dup.status_code == 409


def test_patch_and_archive_project(client, auth_headers, org):
    proj = client.post(
        f"/v1/orgs/{org['slug']}/projects",
        json={"slug": _slug("proj"), "name": "Mercury"},
        headers=auth_headers,
    ).json()
    patched = client.patch(
        f"/v1/projects/{proj['id']}", json={"status": "paused"}, headers=auth_headers
    )
    assert patched.json()["status"] == "paused"
    archived = client.delete(f"/v1/projects/{proj['id']}", headers=auth_headers)
    assert archived.json()["status"] == "archived"


def test_list_projects_pagination_wrapper(client, auth_headers, org):
    client.post(
        f"/v1/orgs/{org['slug']}/projects",
        json={"slug": _slug("proj"), "name": "A"},
        headers=auth_headers,
    )
    body = client.get(f"/v1/orgs/{org['slug']}/projects", headers=auth_headers).json()
    assert set(body.keys()) == {"data", "next_cursor"}


# --------------------------------------------------------------------------- #
# Issues CRUD + board + cross-project list
# --------------------------------------------------------------------------- #


@pytest.fixture
def project(client, auth_headers, org):
    return client.post(
        f"/v1/orgs/{org['slug']}/projects",
        json={"slug": _slug("proj"), "name": "Proj"},
        headers=auth_headers,
    ).json()


def test_create_issue_mints_code_and_open(client, auth_headers, project):
    resp = client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "Fix the thruster", "body": "broken"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    issue = resp.json()
    assert issue["code"].startswith("ISS-")
    assert issue["status"] == "open"
    assert issue["closed_at"] is None


def test_board_view_and_cross_project_list_filters(client, auth_headers, project):
    a = client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "One", "priority": "p1"},
        headers=auth_headers,
    ).json()
    client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "Two", "priority": "p2"},
        headers=auth_headers,
    )
    # Board view (project-scoped) wraps in pagination envelope.
    board = client.get(f"/v1/projects/{project['code']}/issues", headers=auth_headers).json()
    assert set(board.keys()) == {"data", "next_cursor"}
    assert len(board["data"]) == 2
    # Cross-project list filtered by priority.
    p1 = client.get(
        "/v1/issues", params={"project_id": project["id"], "priority": "p1"}, headers=auth_headers
    ).json()
    assert [i["code"] for i in p1["data"]] == [a["code"]]


def test_patch_issue_status_stamps_closed_at(client, auth_headers, project):
    issue = client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "Land it"},
        headers=auth_headers,
    ).json()
    done = client.patch(
        f"/v1/issues/{issue['code']}", json={"status": "done"}, headers=auth_headers
    )
    assert done.json()["status"] == "done"
    assert done.json()["closed_at"] is not None


def test_cancel_issue_soft_deletes(client, auth_headers, project):
    issue = client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "Scrap"},
        headers=auth_headers,
    ).json()
    resp = client.delete(f"/v1/issues/{issue['code']}", headers=auth_headers)
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["closed_at"] is not None


# --------------------------------------------------------------------------- #
# Action endpoints + bus events
# --------------------------------------------------------------------------- #


def _drain(sub) -> list[dict]:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def test_assign_persona_emits_issue_assigned_event(client, auth_headers, project, org):
    issue = client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "Carry the sky"},
        headers=auth_headers,
    ).json()
    persona = personas_ctl.create_persona(org["id"], _slug("p"), "Titan")
    sub = bus.subscribe([f"org/{org['id']}/issues", f"issue/{issue['code']}"])
    try:
        resp = client.post(
            f"/v1/issues/{issue['code']}/assign",
            json={"persona_id": persona["id"]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["assignee_persona_id"] == persona["id"]
        events = _drain(sub)
    finally:
        sub.close()
    types = {e["type"] for e in events}
    assert "issue.assigned" in types


def test_assign_pod_tri_modal(client, auth_headers, project, org, tmp_path):
    issue = client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "Route me"},
        headers=auth_headers,
    ).json()
    squads_ctl.create_squad(str(tmp_path), _slug("pod"), "Ops Pod", leader="alice")
    from brains.storage.db import SessionLocal
    from brains.storage.models import Squad, Workspace

    with SessionLocal() as s:
        pod = s.query(Squad).order_by(Squad.id.desc()).first()
        pod_id = pod.id
        # A Pod may only be assigned to an Issue in its own Org (BL-P0-01), so
        # bind the pod's workspace to the Issue's Org.
        workspace = s.get(Workspace, pod.workspace_id)
        workspace.org_id = org["id"]
        s.commit()
    resp = client.post(
        f"/v1/issues/{issue['code']}/assign",
        json={"pod_id": pod_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["assignee_pod_id"] == pod_id
    assert resp.json()["assignee_persona_id"] is None


def test_assign_pod_from_another_org_is_not_found(client, auth_headers, project, tmp_path):
    """Cross-Org write denial: a Pod outside the Issue's Org is not addressable."""
    issue = client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "Route me elsewhere"},
        headers=auth_headers,
    ).json()
    squads_ctl.create_squad(str(tmp_path), _slug("pod"), "Other Pod", leader="alice")
    from brains.storage.db import SessionLocal
    from brains.storage.models import Squad

    with SessionLocal() as s:
        pod_id = s.query(Squad).order_by(Squad.id.desc()).first().id
    resp = client.post(
        f"/v1/issues/{issue['code']}/assign",
        json={"pod_id": pod_id},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_transition_emits_event_and_stamps_closed(client, auth_headers, project, org):
    issue = client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "Move me"},
        headers=auth_headers,
    ).json()
    sub = bus.subscribe([f"org/{org['id']}/issues"])
    try:
        resp = client.post(
            f"/v1/issues/{issue['code']}/transition",
            json={"status": "done", "reason": "shipped"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["closed_at"] is not None
        events = _drain(sub)
    finally:
        sub.close()
    assert any(e["type"] == "issue.updated" for e in events)


def test_transition_invalid_status_400(client, auth_headers, project):
    issue = client.post(
        f"/v1/projects/{project['code']}/issues",
        json={"title": "Bad move"},
        headers=auth_headers,
    ).json()
    resp = client.post(
        f"/v1/issues/{issue['code']}/transition",
        json={"status": "frozen"},
        headers=auth_headers,
    )
    assert resp.status_code == 400

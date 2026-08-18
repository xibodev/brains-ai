"""HTTP tests for the WS3 coordination router (``/v1/asks/*``, ``/v1/approvals/*``,
``/v1/sessions*``).

Covers: answering an ``ask_human`` ticket resolves the decision; approving /
denying a gated request resolves it (one ``ApprovalRequest`` store, two verb
paths); ``/v1/sessions/spawn`` enqueues an assignment the daemon poll surfaces;
and the session reads return the pagination wrapper. 401-without-auth throughout.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from brains.control import assignments as assignments_ctl
from brains.control import decisions as decisions_ctl
from brains.control import issues as issues_ctl
from brains.control import orgs as orgs_ctl
from brains.control import personas as personas_ctl
from brains.control import projects as projects_ctl
from brains.control import runtimes as runtimes_ctl
from brains.control.operators import ensure_admin_operator
from brains.main import app


@pytest.fixture(autouse=True)
def _bootstrap():
    from brains.storage.migrations import init_db

    init_db()
    ensure_admin_operator()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def org():
    return orgs_ctl.create_org(_slug("org"), "Acme")


@pytest.fixture
def spawn_target(org, tmp_path):
    """Returns ``(runtime, persona, issue)`` ready for a spawn."""
    machine = f"machine-{uuid.uuid4().hex[:8]}"
    rt = runtimes_ctl.register_runtime(
        machine, "copilot", working_root=str(tmp_path), status="online"
    )
    persona = personas_ctl.create_persona(
        org["id"],
        _slug("p"),
        "Forge",
        model="claude-opus-4.8",
        tool="copilot",
        default_runtime_id=rt["id"],
    )
    proj = projects_ctl.create_project(org["id"], _slug("proj"), "Proj")
    issue = issues_ctl.create_issue(proj["id"], "Fix the thruster", body="broken")
    return rt, persona, issue


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_asks_requires_auth(client):
    assert client.get("/v1/asks").status_code in (401, 403)


def test_approvals_requires_auth(client):
    assert client.get("/v1/approvals").status_code in (401, 403)


def test_spawn_requires_auth(client):
    assert client.post("/v1/sessions/spawn", json={}).status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Asks
# --------------------------------------------------------------------------- #


def test_answer_ask_resolves_decision(client, auth_headers, tmp_path):
    filed = decisions_ctl.file_decision_request(
        str(tmp_path), "Which database?", metadata={"kind": "ask_human"}
    )
    code = filed["code"]
    listed = client.get("/v1/asks", headers=auth_headers).json()
    assert any(a["code"] == code for a in listed["data"])
    resp = client.post(f"/v1/asks/{code}/answer", json={"answer": "postgres"}, headers=auth_headers)
    assert resp.status_code == 200
    state = decisions_ctl.get_decision(code)
    assert state["status"] == "resolved"
    assert state["chosen"] == "postgres"


def test_answer_unknown_ask_404(client, auth_headers):
    resp = client.post("/v1/asks/ASK-9999/answer", json={"answer": "x"}, headers=auth_headers)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #


def test_approve_resolves_request(client, auth_headers, tmp_path):
    filed = decisions_ctl.file_decision_request(
        str(tmp_path), "[gate] approve push", metadata={"kind": "action_gate"}
    )
    code = filed["code"]
    one = client.get(f"/v1/approvals/{code}", headers=auth_headers)
    assert one.status_code == 200
    assert one.json()["status"] == "open"
    resp = client.post(
        f"/v1/approvals/{code}/resolve", json={"decision": "approve"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert decisions_ctl.get_decision(code)["status"] == "resolved"


def test_deny_rejects_request(client, auth_headers, tmp_path):
    filed = decisions_ctl.file_decision_request(str(tmp_path), "[gate] approve deploy")
    code = filed["code"]
    resp = client.post(
        f"/v1/approvals/{code}/resolve", json={"decision": "deny"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert decisions_ctl.get_decision(code)["status"] == "rejected"


def test_resolve_already_resolved_conflicts(client, auth_headers, tmp_path):
    filed = decisions_ctl.file_decision_request(str(tmp_path), "[gate] approve once")
    code = filed["code"]
    client.post(f"/v1/approvals/{code}/resolve", json={"decision": "approve"}, headers=auth_headers)
    again = client.post(
        f"/v1/approvals/{code}/resolve", json={"decision": "approve"}, headers=auth_headers
    )
    assert again.status_code == 409


def test_approvals_list_pagination_wrapper(client, auth_headers, tmp_path):
    decisions_ctl.file_decision_request(str(tmp_path), "[gate] something")
    body = client.get("/v1/approvals", headers=auth_headers).json()
    assert set(body.keys()) == {"data", "next_cursor"}


# --------------------------------------------------------------------------- #
# Spawn → enqueue assignment
# --------------------------------------------------------------------------- #


def test_spawn_enqueues_assignment(client, auth_headers, spawn_target):
    rt, persona, issue = spawn_target
    resp = client.post(
        "/v1/sessions/spawn",
        json={"issue_id": issue["id"], "persona_id": persona["id"], "runtime_id": rt["id"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "spawning"
    assert body["assignment_id"] == f"as_{issue['id']}"
    # The daemon's assignment poll now surfaces this issue for the runtime.
    pending = assignments_ctl.list_assignments_for_runtime(rt["id"])
    assert any(a["issue_id"] == issue["id"] for a in pending)


def test_spawn_without_runtime_or_default_400(client, auth_headers, org):
    proj = projects_ctl.create_project(org["id"], _slug("proj"), "Proj")
    issue = issues_ctl.create_issue(proj["id"], "No runtime")
    persona = personas_ctl.create_persona(org["id"], _slug("p"), "P")  # no default_runtime_id
    resp = client.post(
        "/v1/sessions/spawn",
        json={"issue_id": issue["id"], "persona_id": persona["id"]},
        headers=auth_headers,
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Sessions reads
# --------------------------------------------------------------------------- #


def test_list_sessions_pagination_wrapper(client, auth_headers):
    body = client.get("/v1/sessions", headers=auth_headers).json()
    assert set(body.keys()) == {"data", "next_cursor"}


def test_get_unknown_session_404(client, auth_headers):
    assert client.get("/v1/sessions/ses_nope", headers=auth_headers).status_code == 404

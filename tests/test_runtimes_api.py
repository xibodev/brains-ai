"""HTTP tests for the WS1 hub runtime router (``/v1/runtimes/*``).

Exercises the wire protocol end-to-end against the real FastAPI app on the
conftest-isolated tmp DB: registration (idempotent upsert), batched + single
heartbeat liveness, list filters, assignment poll → claim atomicity, ack,
session-open FK stamping, event ingest, and graceful deregister. Every route is
behind ``require_api_key`` — the no-header case must 401.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from brains.control import issues, orgs, personas, projects, runtimes
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


def _machine() -> str:
    return f"machine-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def org():
    return orgs.create_org(f"org-{uuid.uuid4().hex[:8]}", "Acme")


@pytest.fixture
def assigned_issue(org, tmp_path):
    """An open issue assigned to a persona whose default runtime is a freshly
    registered copilot runtime. Returns ``(runtime, persona, issue)``."""
    machine = _machine()
    rt = runtimes.register_runtime(machine, "copilot", working_root=str(tmp_path), status="online")
    persona = personas.create_persona(
        org["id"],
        f"forge-{uuid.uuid4().hex[:6]}",
        "Forge",
        model="claude-opus-4.8",
        tool="copilot",
        default_runtime_id=rt["id"],
    )
    proj = projects.create_project(org["id"], f"proj-{uuid.uuid4().hex[:6]}", "Proj")
    issue = issues.create_issue(proj["id"], "Fix the thruster", body="It is broken")
    issues.assign(issue["code"], persona_id=persona["id"])
    return rt, persona, issue


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_register_requires_auth(client):
    resp = client.post("/v1/runtimes/register", json={"machine_id": "m", "tools": []})
    assert resp.status_code in (401, 403)


def test_list_requires_auth(client):
    assert client.get("/v1/runtimes").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_register_returns_ids_slugs_and_intervals(client, auth_headers):
    machine = _machine()
    body = {
        "machine_id": machine,
        "machine_label": "studio-pc",
        "os": "linux",
        "working_root": "/work",
        "tools": [
            {"tool": "copilot", "display_name": "Copilot CLI", "capabilities": {"version": "1.0"}},
            {"tool": "claude", "display_name": "Claude Code"},
        ],
    }
    resp = client.post("/v1/runtimes/register", json=body, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["heartbeat_interval_s"] == 15
    assert data["ttl_s"] == 45
    assert data["assignments_poll_s"] == 3
    tools = {r["tool"]: r for r in data["runtimes"]}
    assert set(tools) == {"copilot", "claude"}
    assert tools["copilot"]["status"] == "online"
    assert isinstance(tools["copilot"]["id"], int)
    assert tools["copilot"]["slug"]


def test_register_is_idempotent_upsert(client, auth_headers):
    machine = _machine()
    body = {"machine_id": machine, "tools": [{"tool": "copilot"}]}
    a = client.post("/v1/runtimes/register", json=body, headers=auth_headers).json()
    b = client.post("/v1/runtimes/register", json=body, headers=auth_headers).json()
    assert a["runtimes"][0]["id"] == b["runtimes"][0]["id"]
    listed = client.get(
        "/v1/runtimes", params={"machine_id": machine, "tool": "copilot"}, headers=auth_headers
    ).json()["runtimes"]
    assert len(listed) == 1


# --------------------------------------------------------------------------- #
# Heartbeat liveness
# --------------------------------------------------------------------------- #


def test_batched_heartbeat_updates_status(client, auth_headers):
    machine = _machine()
    reg = client.post(
        "/v1/runtimes/register",
        json={"machine_id": machine, "tools": [{"tool": "copilot"}]},
        headers=auth_headers,
    ).json()
    rid = reg["runtimes"][0]["id"]
    resp = client.post(
        "/v1/runtimes/heartbeat",
        json={"machine_id": machine, "runtimes": [{"id": rid, "status": "draining"}]},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["runtimes"][0]["status"] == "draining"


def test_batched_heartbeat_by_tool_ref(client, auth_headers):
    machine = _machine()
    client.post(
        "/v1/runtimes/register",
        json={"machine_id": machine, "tools": [{"tool": "copilot"}]},
        headers=auth_headers,
    )
    resp = client.post(
        "/v1/runtimes/heartbeat",
        json={"machine_id": machine, "runtimes": [{"tool": "copilot", "health": "degraded"}]},
        headers=auth_headers,
    )
    assert resp.json()["runtimes"][0]["health"] == "degraded"


def test_single_heartbeat_brings_offline_online(client, auth_headers):
    machine = _machine()
    rt = runtimes.register_runtime(machine, "copilot")
    runtimes.mark_offline(rt["id"])
    resp = client.post(
        f"/v1/runtimes/{rt['id']}/heartbeat",
        json={"status": "online", "health": "healthy"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "online"


def test_single_heartbeat_unknown_runtime_404(client, auth_headers):
    resp = client.post(
        "/v1/runtimes/99999/heartbeat", json={"status": "online"}, headers=auth_headers
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# List filters + get
# --------------------------------------------------------------------------- #


def test_list_filters_by_tool_and_machine(client, auth_headers):
    machine = _machine()
    client.post(
        "/v1/runtimes/register",
        json={"machine_id": machine, "tools": [{"tool": "copilot"}, {"tool": "claude"}]},
        headers=auth_headers,
    )
    only_claude = client.get(
        "/v1/runtimes", params={"machine_id": machine, "tool": "claude"}, headers=auth_headers
    ).json()["runtimes"]
    assert len(only_claude) == 1
    assert only_claude[0]["tool"] == "claude"


def test_get_one_runtime(client, auth_headers):
    rt = runtimes.register_runtime(_machine(), "copilot")
    resp = client.get(f"/v1/runtimes/{rt['id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == rt["id"]


# --------------------------------------------------------------------------- #
# Assignments: poll → claim (atomic) → ack
# --------------------------------------------------------------------------- #


def test_assignment_poll_surfaces_open_issue(client, auth_headers, assigned_issue):
    rt, persona, issue = assigned_issue
    resp = client.get(f"/v1/runtimes/{rt['id']}/assignments", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()["assignments"]
    assert len(items) == 1
    a = items[0]
    assert a["issue_id"] == issue["id"]
    assert a["persona_id"] == persona["id"]
    assert a["tool"] == "copilot"
    assert a["assignment_id"] == f"as_{issue['id']}"


def test_claim_is_atomic_one_winner(client, auth_headers, assigned_issue):
    rt, _persona, issue = assigned_issue
    aid = f"as_{issue['id']}"
    first = client.post(
        f"/v1/runtimes/{rt['id']}/assignments/{aid}/claim", headers=auth_headers
    ).json()
    second = client.post(
        f"/v1/runtimes/{rt['id']}/assignments/{aid}/claim", headers=auth_headers
    ).json()
    assert first["claimed"] is True
    assert first["issue_id"] == issue["id"]
    assert first["session_token"]
    assert second["claimed"] is False
    # Once claimed the issue left the open pool.
    remaining = client.get(f"/v1/runtimes/{rt['id']}/assignments", headers=auth_headers).json()[
        "assignments"
    ]
    assert remaining == []


def test_ack_finished_moves_issue_to_review(client, auth_headers, assigned_issue):
    rt, _persona, issue = assigned_issue
    aid = f"as_{issue['id']}"
    client.post(f"/v1/runtimes/{rt['id']}/assignments/{aid}/claim", headers=auth_headers)
    resp = client.post(
        f"/v1/runtimes/{rt['id']}/assignments/{aid}/ack",
        json={"state": "finished", "returncode": 0},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["issue_status"] == "in_review"
    assert issues.get_issue(issue["id"])["status"] == "in_review"


def test_ack_aborted_reopens_issue(client, auth_headers, assigned_issue):
    rt, _persona, issue = assigned_issue
    aid = f"as_{issue['id']}"
    client.post(f"/v1/runtimes/{rt['id']}/assignments/{aid}/claim", headers=auth_headers)
    client.post(
        f"/v1/runtimes/{rt['id']}/assignments/{aid}/ack",
        json={"state": "aborted"},
        headers=auth_headers,
    )
    assert issues.get_issue(issue["id"])["status"] == "open"


# --------------------------------------------------------------------------- #
# Sessions + events
# --------------------------------------------------------------------------- #


def test_open_session_stamps_link_columns(client, auth_headers, assigned_issue, tmp_path):
    rt, persona, issue = assigned_issue
    resp = client.post(
        f"/v1/runtimes/{rt['id']}/sessions",
        json={
            "persona_id": persona["id"],
            "issue_id": issue["id"],
            "workspace_path": str(tmp_path),
            "tool": "copilot",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["runtime_id"] == rt["id"]
    assert data["persona_id"] == persona["id"]
    assert data["issue_id"] == issue["id"]
    assert data["session_id"].startswith("ses_")


def test_event_ingest_records(client, auth_headers, assigned_issue, tmp_path):
    rt, persona, issue = assigned_issue
    session = client.post(
        f"/v1/runtimes/{rt['id']}/sessions",
        json={
            "persona_id": persona["id"],
            "issue_id": issue["id"],
            "workspace_path": str(tmp_path),
        },
        headers=auth_headers,
    ).json()
    sid = session["session_id"]
    resp = client.post(
        f"/v1/runtimes/{rt['id']}/sessions/{sid}/events",
        json={"seq": 1, "stream": "stdout", "chunk": "hello\n"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is True


# --------------------------------------------------------------------------- #
# Deregister
# --------------------------------------------------------------------------- #


def test_deregister_marks_offline_not_deleted(client, auth_headers):
    rt = runtimes.register_runtime(_machine(), "copilot")
    resp = client.delete(f"/v1/runtimes/{rt['id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["deregistered"] is True
    # Row preserved (FK history) — still fetchable, now offline.
    assert runtimes.get_runtime(rt["id"])["status"] == "offline"

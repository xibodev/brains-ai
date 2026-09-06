"""HTTP tests for the WS3 coordination router (``/v1/asks/*``, ``/v1/approvals/*``,
``/v1/sessions*``).

Covers: answering an ``ask_human`` ticket resolves the decision; approving /
denying a gated request resolves it (one ``ApprovalRequest`` store, two verb
paths); and the session reads return the pagination wrapper. 401-without-auth
throughout. The withdrawn session-spawn surface is asserted absent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from brains.control import decisions as decisions_ctl
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


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_asks_requires_auth(client):
    assert client.get("/v1/asks").status_code in (401, 403)


def test_approvals_requires_auth(client):
    assert client.get("/v1/approvals").status_code in (401, 403)


def test_spawn_requires_auth(client):
    assert client.post("/v1/sessions/spawn", json={}).status_code in (404, 405)


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
# Sessions reads
# --------------------------------------------------------------------------- #


def test_list_sessions_pagination_wrapper(client, auth_headers):
    body = client.get("/v1/sessions", headers=auth_headers).json()
    assert set(body.keys()) == {"data", "next_cursor"}


def test_get_unknown_session_404(client, auth_headers):
    assert client.get("/v1/sessions/ses_nope", headers=auth_headers).status_code == 404

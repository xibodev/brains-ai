"""Acceptance evidence for the shipped core product boundary."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from brains.control.decisions import file_decision_request
from brains.control.sessions import start_session
from brains.main import app

pytestmark = pytest.mark.acceptance
AUTH = {"Authorization": "Bearer local-dev-key"}


def _rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


@pytest.fixture(autouse=True)
def _bootstrap() -> None:
    from brains.control.operators import ensure_admin_operator
    from brains.storage.migrations import init_db

    init_db()
    ensure_admin_operator()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_f0_core_health_and_coordination_are_live(client: TestClient, tmp_path) -> None:
    session = start_session(str(tmp_path / "workspace"), tool="codex")

    health = client.get("/health")
    assert health.status_code == 200
    sessions = client.get("/v1/sessions", headers=AUTH)
    assert sessions.status_code == 200
    assert any(row["id"] == session["session_id"] for row in _rows(sessions.json()))


def test_f3_governed_ask_surfaces_and_resolves_locally(client: TestClient, tmp_path) -> None:
    filed = file_decision_request(
        workspace_path=str(tmp_path),
        title="Approve a governed action",
        body="Synthetic local acceptance request",
        proposed_answer="approve",
        metadata={"kind": "action_gate", "action_type": "synthetic"},
    )
    code = filed["code"]

    pending = client.get("/v1/approvals", headers=AUTH)
    assert pending.status_code == 200
    assert any(row["code"] == code for row in _rows(pending.json()))

    resolved = client.post(
        f"/v1/approvals/{code}/resolve",
        json={"chosen": "approve"},
        headers=AUTH,
    )
    assert resolved.status_code == 200
    after = client.get("/v1/approvals", headers=AUTH)
    assert all(row["code"] != code for row in _rows(after.json()))


def _assert_absent(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path, headers=AUTH).status_code == 404


def test_f1_runtime_activation_is_absent(client: TestClient) -> None:
    _assert_absent(client, "post", "/v1/runtimes/enrol")


def test_f2_persona_activation_is_absent(client: TestClient) -> None:
    _assert_absent(client, "get", "/v1/personas/example")


def test_f4_project_and_issue_activation_is_absent(client: TestClient) -> None:
    _assert_absent(client, "get", "/v1/projects/example")
    _assert_absent(client, "get", "/v1/issues/example")


def test_f5_pod_activation_is_absent(client: TestClient) -> None:
    _assert_absent(client, "get", "/v1/pods/example")


def test_f6_execution_onboarding_is_absent(client: TestClient) -> None:
    _assert_absent(client, "get", "/v1/onboarding/state")


def test_f7_withdrawn_config_activation_is_absent(client: TestClient) -> None:
    _assert_absent(client, "get", "/v1/config/summary")


def test_f8_github_ingress_is_absent(client: TestClient) -> None:
    _assert_absent(client, "post", "/hooks/github")


def test_f9_multiuser_membership_mutation_is_absent(client: TestClient) -> None:
    _assert_absent(client, "post", "/v1/orgs/example/members")


def test_f10_automation_activation_is_absent(client: TestClient) -> None:
    _assert_absent(client, "post", "/v1/recurring")

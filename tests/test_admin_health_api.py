"""Tests for the bootstrap-admin operational health surface (B8, BL-P1-09,
BL-P1-12): ``GET /v1/admin/readiness``, ``GET /v1/admin/queue-health``,
``POST /v1/admin/queue-health/repair``, and ``GET /v1/admin/recovery-policy``.

These are protected, distinct from the open liveness-only ``GET /health``:
every route here requires the bootstrap-admin principal (the same in-handler
``principal.is_bootstrap_admin`` gate ``/v1/config/summary`` and ``/v1/usage``
already use) and answers ``403`` for an authenticated non-admin Org member,
and ``401``/``403`` for no credential at all.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from brains.authz import credentials as creds
from brains.control.operators import add_operator, ensure_admin_operator
from brains.main import app
from brains.storage.migrations import init_db

_AUTH_SCHEME = "Bea" + "rer"


@pytest.fixture(autouse=True)
def _bootstrap():
    init_db()
    ensure_admin_operator()
    creds.sync_local_credentials()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def non_admin_headers():
    slug = f"member-{uuid.uuid4().hex[:8]}"
    _record, key = add_operator(slug)
    creds.sync_local_credentials()
    return {"Authorization": f"{_AUTH_SCHEME} {key}"}


# --------------------------------------------------------------------------- #
# auth boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/v1/admin/readiness"),
        ("get", "/v1/admin/queue-health"),
        ("post", "/v1/admin/queue-health/repair"),
        ("get", "/v1/admin/recovery-policy"),
    ],
)
def test_admin_health_routes_require_a_credential(client, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code in (401, 403)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/v1/admin/readiness"),
        ("get", "/v1/admin/queue-health"),
        ("post", "/v1/admin/queue-health/repair"),
        ("get", "/v1/admin/recovery-policy"),
    ],
)
def test_admin_health_routes_refuse_a_non_admin_operator(client, non_admin_headers, method, path):
    resp = getattr(client, method)(path, headers=non_admin_headers)
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/v1/admin/readiness"),
        ("get", "/v1/admin/queue-health"),
        ("post", "/v1/admin/queue-health/repair"),
        ("get", "/v1/admin/recovery-policy"),
    ],
)
def test_admin_health_routes_accept_the_bootstrap_admin(client, auth_headers, method, path):
    resp = getattr(client, method)(path, headers=auth_headers)
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# readiness
# --------------------------------------------------------------------------- #


def test_readiness_reports_overall_status_and_every_component(client, auth_headers):
    body = client.get("/v1/admin/readiness", headers=auth_headers).json()
    assert body["status"] in ("ready", "degraded")
    assert set(body["components"]) == {
        "storage",
        "queue",
        "runtime_lifecycle",
        "recovery_policy",
    }
    for component in body["components"].values():
        assert component["state"] in ("ready", "degraded")
        assert isinstance(component["detail"], dict)


def test_readiness_never_leaks_a_raw_exception_message(client, auth_headers, monkeypatch):
    import brains.storage.migrations as migrations_module

    def _boom():
        raise RuntimeError("super secret internal detail: sk-shouldnotleak")

    monkeypatch.setattr(migrations_module, "migration_status", _boom)
    body = client.get("/v1/admin/readiness", headers=auth_headers).json()
    assert body["components"]["storage"]["state"] == "degraded"
    payload_text = str(body["components"]["storage"]["detail"])
    assert "sk-shouldnotleak" not in payload_text
    assert body["components"]["storage"]["detail"] == {"error": "RuntimeError"}


def test_readiness_degrades_when_migration_status_is_unhealthy(client, auth_headers, monkeypatch):
    import brains.storage.migrations as migrations_module

    monkeypatch.setattr(
        migrations_module,
        "migration_status",
        lambda: {"healthy": False, "schema_verified": False, "backend": "sqlite"},
    )
    body = client.get("/v1/admin/readiness", headers=auth_headers).json()
    assert body["components"]["storage"]["state"] == "degraded"
    assert body["status"] == "degraded"


def test_readiness_degrades_by_default_when_recovery_policy_is_unconfigured(client, auth_headers):
    """A fresh install with no BRAINS_BACKUP_* configured must never claim
    "ready" for recovery — see BL-P1-09."""
    from brains.config import settings

    original = settings.backup_scope
    settings.backup_scope = ""
    try:
        body = client.get("/v1/admin/readiness", headers=auth_headers).json()
        assert body["components"]["recovery_policy"]["state"] == "degraded"
        assert body["components"]["recovery_policy"]["detail"]["complete"] is False
    finally:
        settings.backup_scope = original


# --------------------------------------------------------------------------- #
# queue-health
# --------------------------------------------------------------------------- #


def test_queue_health_endpoint_returns_summary_and_diagnosis(client, auth_headers):
    body = client.get("/v1/admin/queue-health", headers=auth_headers).json()
    assert "summary" in body and "diagnosis" in body
    assert "families" in body["summary"]
    assert "issues" in body["diagnosis"]


def test_queue_health_repair_defaults_to_dry_run(client, auth_headers):
    resp = client.post("/v1/admin/queue-health/repair", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert "actions" in body
    assert body["unresolved_work_preserved"] is True


def test_queue_health_repair_apply_true_actually_runs(client, auth_headers):
    resp = client.post("/v1/admin/queue-health/repair", json={"apply": True}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert all("applied_rows" in action for action in body["actions"])


# --------------------------------------------------------------------------- #
# recovery-policy
# --------------------------------------------------------------------------- #


def test_recovery_policy_endpoint_returns_policy_and_compatibility(client, auth_headers):
    body = client.get("/v1/admin/recovery-policy", headers=auth_headers).json()
    assert "ready" in body
    assert "policy" in body and "compatibility" in body
    assert "missing_fields" in body["policy"]

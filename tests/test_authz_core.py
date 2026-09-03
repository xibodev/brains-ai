"""Authorization assertions for the retained single-operator core boundary."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from brains.api.auth import mint_browser_token
from brains.authz import credentials as creds
from brains.authz.resolver import principal_for_secret
from brains.config import settings
from brains.control.operators import add_operator, ensure_admin_operator
from brains.main import app
from brains.storage.migrations import init_db


@pytest.fixture(autouse=True)
def _bootstrap():
    init_db()
    ensure_admin_operator()
    creds.sync_local_credentials()


@pytest.fixture
def client():
    return TestClient(app)


def _operator() -> tuple[dict, str, dict[str, str]]:
    record, key = add_operator(f"operator-{uuid.uuid4().hex[:8]}")
    creds.sync_local_credentials()
    return record, key, {"Authorization": f"Bearer {key}"}


def test_missing_and_unknown_credentials_are_rejected(client):
    assert client.get("/v1/orgs").status_code == 401
    assert (
        client.get("/v1/orgs", headers={"Authorization": "Bearer not-a-real-key"}).status_code
        == 401
    )


def test_bootstrap_key_resolves_one_explicit_principal():
    principal = principal_for_secret(settings.api_key)
    assert principal is not None
    assert principal.actor_kind == "operator"
    assert principal.operator_slug == "admin"
    assert principal.credential_id
    assert principal.is_bootstrap_admin is True


def test_operator_key_is_attributed_and_deny_by_default():
    record, key, _headers = _operator()
    principal = principal_for_secret(key)
    assert principal is not None
    assert principal.operator_id == record["id"]
    assert principal.operator_slug == record["slug"]
    assert principal.visible_org_ids() == set()


def test_revoked_and_expired_credentials_stop_authenticating(client):
    _record, key, headers = _operator()
    assert client.get("/v1/orgs", headers=headers).status_code == 200
    credential = principal_for_secret(key)
    assert credential is not None
    creds.revoke_credential(credential.credential_id)
    assert client.get("/v1/orgs", headers=headers).status_code == 401
    _expired, raw = creds.mint_runtime_credential(
        org_id=None, machine_id=f"historical-{uuid.uuid4().hex[:8]}", ttl_seconds=-1
    )
    assert principal_for_secret(raw) is None


def test_secret_is_never_stored_in_cleartext():
    record, raw = creds.mint_runtime_credential(
        org_id=None, machine_id=f"historical-{uuid.uuid4().hex[:8]}"
    )
    stored = creds.get_credential(record["credential_id"])
    assert all(raw not in str(value) for value in stored.values())


def test_browser_cookie_is_opaque_and_bound_to_live_credential(client):
    _record, key, _headers = _operator()
    cookie = mint_browser_token(key)
    assert key not in cookie
    client.cookies.set("brains_admin_key", cookie)
    assert client.get("/v1/orgs").status_code == 200
    principal = principal_for_secret(key)
    assert principal is not None
    creds.revoke_credential(principal.credential_id)
    assert client.get("/v1/orgs").status_code == 401


def test_forged_browser_cookie_is_rejected(client):
    client.cookies.set("brains_admin_key", "v1.invalid.0.invalid")
    assert client.get("/v1/orgs").status_code == 401


def test_negative_credential_cache_is_bounded_and_never_holds_secrets():
    creds.invalidate_source_cache()
    secret = f"unknown-{uuid.uuid4().hex}"
    creds.resolve_secret(secret)
    assert secret not in creds._negative_cache
    assert creds.hash_secret(secret) in creds._negative_cache
    for index in range(creds.NEGATIVE_CACHE_MAX_ENTRIES + 50):
        creds.resolve_secret(f"unknown-{index}")
    assert len(creds._negative_cache) <= creds.NEGATIVE_CACHE_MAX_ENTRIES

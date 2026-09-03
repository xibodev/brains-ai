"""HTTP tests for the retained organization identity boundary."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

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


def test_list_orgs_requires_auth(client):
    assert client.get("/v1/orgs").status_code in (401, 403)


def test_create_org_requires_auth(client):
    assert client.post("/v1/orgs", json={"slug": "x", "name": "X"}).status_code in (401, 403)


def test_create_and_get_org(client, auth_headers):
    slug = _slug("org")
    created = client.post("/v1/orgs", json={"slug": slug, "name": "Acme"}, headers=auth_headers)
    assert created.status_code == 200
    fetched = client.get(f"/v1/orgs/{slug}", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created.json()["id"]


def test_create_org_duplicate_slug_conflicts(client, auth_headers):
    slug = _slug("org")
    client.post("/v1/orgs", json={"slug": slug, "name": "A"}, headers=auth_headers)
    duplicate = client.post("/v1/orgs", json={"slug": slug, "name": "B"}, headers=auth_headers)
    assert duplicate.status_code == 409


def test_get_unknown_org_404(client, auth_headers):
    assert client.get("/v1/orgs/does-not-exist", headers=auth_headers).status_code == 404


def test_patch_org(client, auth_headers):
    slug = _slug("org")
    client.post("/v1/orgs", json={"slug": slug, "name": "Old"}, headers=auth_headers)
    response = client.patch(
        f"/v1/orgs/{slug}", json={"name": "New", "description": "d"}, headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["name"] == "New"


def test_list_orgs_pagination(client, auth_headers):
    for _ in range(3):
        client.post("/v1/orgs", json={"slug": _slug("org"), "name": "A"}, headers=auth_headers)
    first = client.get("/v1/orgs", params={"limit": 1}, headers=auth_headers).json()
    second = client.get(
        "/v1/orgs", params={"limit": 1, "cursor": first["next_cursor"]}, headers=auth_headers
    ).json()
    assert len(first["data"]) == len(second["data"]) == 1
    assert first["data"][0]["id"] != second["data"][0]["id"]

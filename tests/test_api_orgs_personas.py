"""HTTP tests for the WS3 orgs + personas routers (``/v1/orgs/*``, ``/v1/personas/*``).

Exercises the thin routers against the real FastAPI app on the conftest-isolated
tmp DB: CRUD happy paths, 401-without-auth, unique-constraint conflicts, the
``{data, next_cursor}`` pagination wrapper shape, and onboarding.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from fastapi.testclient import TestClient

from brains.control.operators import add_operator, ensure_admin_operator
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


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_list_orgs_requires_auth(client):
    assert client.get("/v1/orgs").status_code in (401, 403)


def test_create_org_requires_auth(client):
    resp = client.post("/v1/orgs", json={"slug": "x", "name": "X"})
    assert resp.status_code in (401, 403)


def test_personas_list_requires_auth(client):
    assert client.get("/v1/orgs/default/personas").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Orgs CRUD + pagination wrapper
# --------------------------------------------------------------------------- #


def test_create_and_get_org_makes_caller_owner(client, auth_headers):
    slug = _slug("org")
    resp = client.post("/v1/orgs", json={"slug": slug, "name": "Acme"}, headers=auth_headers)
    assert resp.status_code == 200
    org = resp.json()
    assert org["slug"] == slug
    # Caller (admin) is the owner.
    members = client.get(f"/v1/orgs/{slug}/members", headers=auth_headers).json()
    owners = [m for m in members["data"] if m["role"] == "owner"]
    assert owners and owners[0]["operator"] == "admin"
    # Fetch by slug.
    got = client.get(f"/v1/orgs/{slug}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == org["id"]


def test_create_org_duplicate_slug_conflicts(client, auth_headers):
    slug = _slug("org")
    client.post("/v1/orgs", json={"slug": slug, "name": "A"}, headers=auth_headers)
    dup = client.post("/v1/orgs", json={"slug": slug, "name": "B"}, headers=auth_headers)
    assert dup.status_code == 409
    assert dup.json()["error"]["type"] == "invalid_request_error"


def test_get_unknown_org_404(client, auth_headers):
    resp = client.get("/v1/orgs/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


def test_patch_org_updates_name_and_status(client, auth_headers):
    slug = _slug("org")
    client.post("/v1/orgs", json={"slug": slug, "name": "Old"}, headers=auth_headers)
    resp = client.patch(
        f"/v1/orgs/{slug}", json={"name": "New", "description": "d"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"
    archived = client.patch(f"/v1/orgs/{slug}", json={"status": "archived"}, headers=auth_headers)
    assert archived.json()["status"] == "archived"


def test_list_orgs_returns_pagination_wrapper(client, auth_headers):
    slug = _slug("org")
    client.post("/v1/orgs", json={"slug": slug, "name": "A"}, headers=auth_headers)
    body = client.get("/v1/orgs", headers=auth_headers).json()
    assert set(body.keys()) == {"data", "next_cursor"}
    assert isinstance(body["data"], list)


def test_orgs_pagination_cursor_pages(client, auth_headers):
    for _ in range(3):
        client.post("/v1/orgs", json={"slug": _slug("org"), "name": "A"}, headers=auth_headers)
    page1 = client.get("/v1/orgs", params={"limit": 1}, headers=auth_headers).json()
    assert len(page1["data"]) == 1
    assert page1["next_cursor"] is not None
    page2 = client.get(
        "/v1/orgs", params={"limit": 1, "cursor": page1["next_cursor"]}, headers=auth_headers
    ).json()
    assert len(page2["data"]) == 1
    assert page1["data"][0]["id"] != page2["data"][0]["id"]


def test_add_and_remove_member(client, auth_headers):
    slug = _slug("org")
    client.post("/v1/orgs", json={"slug": slug, "name": "A"}, headers=auth_headers)
    add = client.post(
        f"/v1/orgs/{slug}/members",
        json={"operator_id": "alice", "role": "admin"},
        headers=auth_headers,
    )
    assert add.status_code == 200
    members = client.get(f"/v1/orgs/{slug}/members", headers=auth_headers).json()["data"]
    assert any(m["operator"] == "alice" for m in members)
    rm = client.delete(f"/v1/orgs/{slug}/members/alice", headers=auth_headers)
    assert rm.status_code == 200
    members2 = client.get(f"/v1/orgs/{slug}/members", headers=auth_headers).json()["data"]
    assert all(m["operator"] != "alice" for m in members2)


def test_add_member_unknown_operator_404(client, auth_headers):
    slug = _slug("org")
    client.post("/v1/orgs", json={"slug": slug, "name": "A"}, headers=auth_headers)
    resp = client.post(
        f"/v1/orgs/{slug}/members", json={"operator_id": "nobody"}, headers=auth_headers
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Onboarding
# --------------------------------------------------------------------------- #


def test_onboard_creates_org_and_owner(client, auth_headers):
    slug = _slug("org")
    resp = client.post(
        "/v1/onboard",
        json={"org": {"slug": slug, "name": "Onboarded"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["org"]["slug"] == slug
    assert body["owner"] == "admin"


def test_onboard_org_alias_path(client, auth_headers):
    slug = _slug("org")
    resp = client.post(
        "/v1/orgs/onboard",
        json={"org": {"slug": slug, "name": "Onboarded"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["org"]["slug"] == slug


# --------------------------------------------------------------------------- #
# Personas CRUD
# --------------------------------------------------------------------------- #


@pytest.fixture
def org(client, auth_headers):
    slug = _slug("org")
    return client.post("/v1/orgs", json={"slug": slug, "name": "Acme"}, headers=auth_headers).json()


def test_create_persona_and_unique_conflict(client, auth_headers, org):
    slug = _slug("persona")
    resp = client.post(
        f"/v1/orgs/{org['slug']}/personas",
        json={"slug": slug, "name": "Mason", "model": "claude-opus-4.8", "tool": "copilot"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    persona = resp.json()
    assert persona["slug"] == slug
    assert persona["operator_id"] is None
    dup = client.post(
        f"/v1/orgs/{org['slug']}/personas",
        json={"slug": slug, "name": "Dup"},
        headers=auth_headers,
    )
    assert dup.status_code == 409


def test_get_patch_archive_persona(client, auth_headers, org):
    slug = _slug("persona")
    persona = client.post(
        f"/v1/orgs/{org['slug']}/personas",
        json={"slug": slug, "name": "Ranger"},
        headers=auth_headers,
    ).json()
    pid = persona["id"]
    got = client.get(f"/v1/personas/{pid}", headers=auth_headers)
    assert got.status_code == 200
    patched = client.patch(
        f"/v1/personas/{pid}", json={"model": "gpt-5", "color": "#ff0000"}, headers=auth_headers
    )
    assert patched.json()["model"] == "gpt-5"
    archived = client.delete(f"/v1/personas/{pid}", headers=auth_headers)
    assert archived.json()["status"] == "archived"


def test_list_personas_pagination_wrapper(client, auth_headers, org):
    client.post(
        f"/v1/orgs/{org['slug']}/personas",
        json={"slug": _slug("persona"), "name": "P"},
        headers=auth_headers,
    )
    body = client.get(f"/v1/orgs/{org['slug']}/personas", headers=auth_headers).json()
    assert set(body.keys()) == {"data", "next_cursor"}
    assert len(body["data"]) >= 1


def test_persona_sessions_empty_list(client, auth_headers, org):
    persona = client.post(
        f"/v1/orgs/{org['slug']}/personas",
        json={"slug": _slug("persona"), "name": "P"},
        headers=auth_headers,
    ).json()
    body = client.get(f"/v1/personas/{persona['id']}/sessions", headers=auth_headers).json()
    assert body == {"data": [], "next_cursor": None}

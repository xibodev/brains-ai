"""End-to-end coverage for the workspaces dashboard listing.

Exercises the new query params on ``/dashboard/workspaces`` and its
``/dashboard/api/workspaces`` JSON counterpart: search, status filter,
hide-test toggle, and offset/limit pagination.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from brains.dashboard.app import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Workspace


def _reset_and_seed():
    init_db()
    with SessionLocal() as session:
        session.execute(
            text(
                "DELETE FROM workspaces WHERE slug LIKE 'dashfix-%' OR slug LIKE 'test-dashfix-%' OR slug LIKE 'adopt-dashfix-%'"
            )
        )
        session.commit()
        rows = [
            Workspace(slug="dashfix-real-app", path="/repos/dashfix-real-app", status="active"),
            Workspace(slug="dashfix-real-app2", path="/repos/dashfix-real-app2", status="archived"),
            # Test-fixture slugs and pytest paths — exactly the shape that
            # historically polluted the live DB.
            Workspace(slug="test-dashfix-a", path="/tmp/pytest-of-x/dashfix-a", status="active"),
            Workspace(slug="test-dashfix-b", path="/tmp/pytest-of-x/dashfix-b", status="active"),
            Workspace(slug="adopt-dashfix-z", path="/tmp/dashfix/z", status="active"),
        ]
        for row in rows:
            session.add(row)
        session.commit()


def test_api_workspaces_paginates_and_returns_total(auth_headers):
    _reset_and_seed()
    client = TestClient(app)
    response = client.get(
        "/dashboard/api/workspaces",
        params={"q": "dashfix", "per_page": 2, "page": 1, "hide_test": "0"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["rows"]) == 2
    assert body["page"] == 1
    assert body["per_page"] == 2


def test_api_workspaces_hide_test_filters_pytest_paths(auth_headers):
    _reset_and_seed()
    client = TestClient(app)
    response = client.get(
        "/dashboard/api/workspaces",
        params={"q": "dashfix", "hide_test": "1", "per_page": 100},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    slugs = {row["slug"] for row in body["rows"]}
    # Real workspaces survive
    assert "dashfix-real-app" in slugs
    assert "dashfix-real-app2" in slugs
    # Test fixtures (prefix `test-`, `adopt-`, or path containing `pytest`) are hidden
    assert "test-dashfix-a" not in slugs
    assert "test-dashfix-b" not in slugs
    assert "adopt-dashfix-z" not in slugs


def test_api_workspaces_status_filter(auth_headers):
    _reset_and_seed()
    client = TestClient(app)
    response = client.get(
        "/dashboard/api/workspaces",
        params={"q": "dashfix", "status": "archived", "hide_test": "0"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    slugs = {row["slug"] for row in body["rows"]}
    assert slugs == {"dashfix-real-app2"}


def test_html_workspaces_page_renders_with_filters(auth_headers):
    _reset_and_seed()
    client = TestClient(app)
    response = client.get(
        "/dashboard/workspaces",
        params={"q": "dashfix", "hide_test": "1"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    html = response.text
    assert "Workspaces" in html
    # The chrome rendered with the operator's search term echoed back
    assert "dashfix" in html
    # The cleanup hint is present so operators discover the prune CLI
    assert "workspaces prune" in html

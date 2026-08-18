"""Smoke tests for the listing-chrome rollout to dashboard tabs.

Each of Sessions/Tasks/Decisions/Handoffs/Events/Patterns must:
  * render 200 OK with no query params
  * honour ``?q=needle`` without throwing
  * honour ``?page=999&per_page=10`` and still render gracefully

This is a regression net for the chrome refactor — not exhaustive
coverage of every filter combination.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from brains.dashboard.app import app

ROUTES = [
    "/dashboard/sessions",
    "/dashboard/tasks",
    "/dashboard/decisions",
    "/dashboard/handoffs",
    "/dashboard/events",
    "/dashboard/patterns",
]


@pytest.mark.parametrize("route", ROUTES)
def test_route_renders_with_no_params(route, auth_headers):
    client = TestClient(app)
    response = client.get(route, headers=auth_headers)
    assert response.status_code == 200, f"{route} returned {response.status_code}"
    # Sanity: the chrome injects a query input or a "Showing" footer for
    # routes that paginate. Sessions/Decisions/Handoffs/Events/Patterns
    # all paginate; Tasks shows a search input via the chrome helper.
    body = response.text.lower()
    assert "search" in body or "showing" in body or "filter" in body or "rows" in body


@pytest.mark.parametrize("route", ROUTES)
def test_route_accepts_search_query(route, auth_headers):
    client = TestClient(app)
    response = client.get(route, params={"q": "nonexistent-needle"}, headers=auth_headers)
    assert response.status_code == 200


@pytest.mark.parametrize("route", ROUTES)
def test_route_clamps_pagination(route, auth_headers):
    # Tasks doesn't paginate (kept as kanban) — passing page/per_page is
    # harmless because the chrome helper clamps them.
    client = TestClient(app)
    response = client.get(
        route,
        params={"page": 999, "per_page": 10},
        headers=auth_headers,
    )
    assert response.status_code == 200


def test_sessions_state_filter_is_accepted(auth_headers):
    client = TestClient(app)
    response = client.get(
        "/dashboard/sessions",
        params={"state": "active"},
        headers=auth_headers,
    )
    assert response.status_code == 200


def test_tasks_priority_filter_is_accepted(auth_headers):
    client = TestClient(app)
    response = client.get(
        "/dashboard/tasks",
        params={"priority": "p1"},
        headers=auth_headers,
    )
    assert response.status_code == 200


def test_patterns_preserves_status_tab(auth_headers):
    client = TestClient(app)
    response = client.get(
        "/dashboard/patterns",
        params={"status": "approved", "q": "test"},
        headers=auth_headers,
    )
    assert response.status_code == 200

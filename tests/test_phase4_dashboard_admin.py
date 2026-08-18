"""Tests for Phase 4 of the consolidation plan.

Covers:

* ``/admin/api/sessions/reap`` reaps zombie agent sessions and is gated
  by ``require_browser_auth``.
* ``/dashboard/recurring`` renders the recurring-task list, and the
  matching ``/dashboard/api/recurring`` JSON endpoint returns the same
  data.
* Both surfaces require auth (unauthenticated requests get a 401).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import brains.context.repo_indexer as repo_indexer_module
import brains.control.events as events_module
import brains.control.recurring as recurring_module
import brains.control.sessions as sessions_module
import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.api.auth import mint_browser_token, reset_rate_limit_state
from brains.config import settings
from brains.control.recurring import create_recurring_task
from brains.control.sessions import start_session
from brains.dashboard.app import app
from brains.storage.models import AgentSession


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase4.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (
        db_module,
        migrations_module,
        sessions_module,
        events_module,
        recurring_module,
        repo_indexer_module,
    ):
        if hasattr(module, "engine"):
            monkeypatch.setattr(module, "engine", engine)
        if hasattr(module, "SessionLocal"):
            monkeypatch.setattr(module, "SessionLocal", SessionLocal)
    yield db_path


def _authed_client() -> TestClient:
    reset_rate_limit_state()
    client = TestClient(app)
    client.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    return client


# ---------- admin reap endpoint ----------


def test_admin_reap_requires_auth(isolated_db) -> None:
    reset_rate_limit_state()
    client = TestClient(app)
    response = client.post("/admin/api/sessions/reap")
    assert response.status_code == 401


def test_admin_reap_returns_empty_when_no_zombies(isolated_db, tmp_path) -> None:
    client = _authed_client()
    response = client.post("/admin/api/sessions/reap")
    assert response.status_code == 200
    body = response.json()
    assert body["reaped"] == []
    assert body["count"] == 0


def test_admin_reap_closes_zombie_sessions(isolated_db, tmp_path) -> None:
    workspace = tmp_path / "ws-reap"
    workspace.mkdir()
    result = start_session(str(workspace), tool="pytest", pid=999999999)
    # Force the row to look like a zombie: live status + impossible PID.
    with db_module.SessionLocal() as session:
        row = session.query(AgentSession).filter(AgentSession.id == result["session_id"]).one()
        row.pid = 999999999
        row.ended_at = None
        session.commit()

    client = _authed_client()
    response = client.post("/admin/api/sessions/reap")
    assert response.status_code == 200
    body = response.json()
    assert result["session_id"] in body["reaped"]
    assert body["count"] >= 1


# ---------- recurring dashboard ----------


def test_recurring_page_requires_auth(isolated_db) -> None:
    reset_rate_limit_state()
    client = TestClient(app)
    # HTML dashboard pages now bounce unauthed browsers to /admin/login
    # via a 303 redirect (see require_browser_auth_html). JSON endpoints
    # under /dashboard/api/* still return 401 — covered separately by
    # test_recurring_api_requires_auth below.
    response = client.get("/dashboard/recurring", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login?next=")


def test_recurring_page_renders_empty_state(isolated_db) -> None:
    client = _authed_client()
    response = client.get("/dashboard/recurring")
    assert response.status_code == 200
    assert "Recurring tasks" in response.text or "Autopilots" in response.text
    assert "No schedules yet" in response.text


def test_recurring_page_shows_definitions_with_spawn_config(isolated_db, tmp_path) -> None:
    workspace = tmp_path / "ws-rec"
    workspace.mkdir()
    create_recurring_task(
        str(workspace),
        name="weekly-review",
        title_template="Weekly review {date}",
        body_template="Run weekly review",
        cron_expr="daily",
        spawn_tool="codex",
        spawn_args='["exec"]',
        spawn_prompt="Review repo",
    )
    client = _authed_client()
    response = client.get("/dashboard/recurring")
    assert response.status_code == 200
    assert "weekly-review" in response.text
    assert "Weekly review" in response.text
    assert "codex" in response.text
    assert "active" in response.text


def test_recurring_api_returns_payload(isolated_db, tmp_path) -> None:
    workspace = tmp_path / "ws-api"
    workspace.mkdir()
    create_recurring_task(
        str(workspace),
        name="daily-sweep",
        title_template="Daily sweep {date}",
        body_template="Sweep",
        cron_expr="daily",
    )
    client = _authed_client()
    response = client.get("/dashboard/api/recurring")
    assert response.status_code == 200
    payload = response.json()
    names = {row["name"] for row in payload}
    assert "daily-sweep" in names


def test_recurring_api_requires_auth(isolated_db) -> None:
    reset_rate_limit_state()
    client = TestClient(app)
    response = client.get("/dashboard/api/recurring")
    assert response.status_code == 401

from fastapi.testclient import TestClient

from brains.api.auth import mint_browser_token, reset_rate_limit_state
from brains.config import settings
from brains.control.operators import add_operator
from brains.control.recurring import create_recurring_task
from brains.control.sessions import register_workspace
from brains.control.squads import add_member, create_squad
from brains.control.webhooks import create_webhook_trigger
from brains.dashboard.app import app


def _client() -> TestClient:
    reset_rate_limit_state()
    client = TestClient(app)
    client.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    return client


def test_autopilots_page_renders_all_sections(tmp_path):
    workspace = str(tmp_path)
    register_workspace(workspace)
    create_recurring_task(workspace, "nightly-audit-page", "Audit {date}", cron_expr="manual")
    create_webhook_trigger("ci-page", "nightly-audit-page")

    client = _client()
    page = client.get("/dashboard/recurring")
    assert page.status_code == 200
    body = page.text
    # The page is now the unified Autopilots view with three sections.
    assert "Autopilots" in body
    assert "Schedules" in body
    assert "Webhook triggers" in body
    assert "Recent runs" in body
    # The schedule and webhook we created appear.
    assert "nightly-audit-page" in body
    assert "/hooks/ci-page" in body


def test_squads_page_renders_board(tmp_path):
    import contextlib

    workspace = str(tmp_path)
    register_workspace(workspace)
    for slug in ("ally", "ben"):
        with contextlib.suppress(Exception):
            add_operator(slug)
    create_squad(workspace, "frontend-page", "Frontend", leader="ally")
    add_member(workspace, "frontend-page", "ben", role="react")

    client = _client()
    page = client.get("/dashboard/squads")
    assert page.status_code == 200
    body = page.text
    assert "Squads" in body
    assert "@ally" in body
    assert "@ben" in body

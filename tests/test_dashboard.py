import uuid

from fastapi.testclient import TestClient

from brains.api.auth import mint_browser_token, reset_rate_limit_state
from brains.config import settings
from brains.context.code_graph import build_code_graph
from brains.control.decisions import file_decision_request
from brains.control.handoffs import set_handoff
from brains.control.knowledge import add_knowledge_entry
from brains.control.sessions import start_session
from brains.control.tasks import create_task
from brains.dashboard.app import app


def _client() -> TestClient:
    reset_rate_limit_state()
    client = TestClient(app)
    client.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    return client


def test_dashboard_overview_and_workspace_detail(tmp_path):
    workspace = str(tmp_path)
    start_session(workspace, tool="pytest")
    create_task(workspace, title="Dashboard task")
    set_handoff(workspace, title="Dashboard handoff", body="resume")

    client = _client()
    overview = client.get("/dashboard")
    assert overview.status_code == 200
    assert "brains" in overview.text.lower()
    assert "Overview" in overview.text

    workspaces = client.get("/dashboard/api/workspaces")
    assert workspaces.status_code == 200
    payload = workspaces.json()
    # /dashboard/api/workspaces now returns a paginated envelope
    # ``{"rows": [...], "total": N, "page": P, "per_page": Q}``.
    rows = payload["rows"] if isinstance(payload, dict) else payload
    workspace_slug = next(row["slug"] for row in rows if row["path"] == workspace)

    detail = client.get(f"/dashboard/api/workspaces/{workspace_slug}")
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["workspace"]["path"] == workspace
    assert detail_payload["active_tasks"]

    html_detail = client.get(f"/dashboard/workspaces/{workspace_slug}")
    assert html_detail.status_code == 200
    assert workspace_slug in html_detail.text


def test_dashboard_decision_detail_and_resolve(tmp_path):
    workspace = str(tmp_path)
    start_session(workspace, tool="pytest")
    ask = file_decision_request(workspace, title="Dashboard decision", body="review")

    client = _client()
    detail = client.get(f"/dashboard/decisions/{ask['code']}")
    assert detail.status_code == 200
    assert ask["code"] in detail.text

    resolved = client.post(
        f"/dashboard/decisions/{ask['code']}/resolve",
        data={"chosen": "approved", "status": "resolved", "reasoning": "ok"},
        follow_redirects=False,
    )
    assert resolved.status_code == 303


def test_dashboard_requires_auth_when_no_cookie():
    reset_rate_limit_state()
    client = TestClient(app)
    # /dashboard is an HTML route gated by require_browser_auth_html, which
    # converts an unauthed 401 into a 303 redirect to /admin/login so a
    # browser sees the login form instead of a bare JSON error.
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert "/admin/login" in response.headers.get("location", "")


def test_dashboard_login_sets_cookie_and_grants_access():
    reset_rate_limit_state()
    client = TestClient(app)
    login = client.post(
        "/admin/login",
        data={"key": settings.api_key},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert "brains_admin_key" in login.headers.get("set-cookie", "")
    follow = client.get("/dashboard")
    assert follow.status_code == 200


def test_dashboard_tabs_render(tmp_path):
    workspace = str(tmp_path)
    start_session(workspace, tool="pytest")
    create_task(workspace, title="Tab test")
    client = _client()
    for path in (
        "/dashboard/sessions",
        "/dashboard/tasks",
        "/dashboard/handoffs",
        "/dashboard/routes",
        "/dashboard/graph",
        "/dashboard/events",
        "/dashboard/knowledge",
        "/dashboard/patterns",
        "/dashboard/tools",
        "/dashboard/workspaces",
    ):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.text[:200]}"


def test_dashboard_knowledge_page_requires_auth(tmp_path):
    title = f"Dashboard blocker {uuid.uuid4().hex}"
    add_knowledge_entry(str(tmp_path), "blocker", title)

    client = _client()
    response = client.get("/dashboard/knowledge")
    assert response.status_code == 200
    assert title in response.text

    reset_rate_limit_state()
    unauthenticated = TestClient(app)
    rejected = unauthenticated.get("/dashboard/knowledge", follow_redirects=False)
    assert rejected.status_code == 303
    assert "/admin/login" in rejected.headers.get("location", "")


def test_dashboard_graph_page_requires_auth(tmp_path):
    (tmp_path / "graph_app.py").write_text(
        "def graph_entry():\n    return 1\n",
        encoding="utf-8",
    )
    build_code_graph(str(tmp_path))

    client = _client()
    response = client.get("/dashboard/graph", params={"workspace": str(tmp_path)})
    assert response.status_code == 200
    assert "Graph" in response.text
    assert "graph_entry" in response.text

    api_response = client.get("/dashboard/api/graph", params={"workspace": str(tmp_path)})
    assert api_response.status_code == 200
    assert any(row["name"] == "graph_entry" for row in api_response.json()["nodes"])

    reset_rate_limit_state()
    unauthenticated = TestClient(app)
    rejected = unauthenticated.get("/dashboard/graph", follow_redirects=False)
    assert rejected.status_code == 303
    assert "/admin/login" in rejected.headers.get("location", "")


# ---------- FIX-003: GET on mutating endpoints is rejected ----------


def test_dashboard_resolve_requires_post(tmp_path):
    workspace = str(tmp_path)
    start_session(workspace, tool="pytest")
    ask = file_decision_request(workspace, title="POST-only", body="x")
    client = _client()
    response = client.get(
        f"/dashboard/decisions/{ask['code']}/resolve",
        params={"chosen": "approved"},
    )
    # GET on a POST-only endpoint = 405.
    assert response.status_code == 405


def test_dashboard_tools_verify_requires_post():
    client = _client()
    # Old query-param trigger no longer mutates. Page still renders.
    page = client.get("/dashboard/tools?verify=1")
    assert page.status_code == 200
    # The POST-only verify endpoint accepts the action.
    verified = client.post("/dashboard/tools/verify", follow_redirects=False)
    assert verified.status_code == 303
    assert verified.headers["location"] == "/dashboard/tools"


# ---------- FIX-005: cookie is an opaque signed token, not the raw key ----------


def test_login_cookie_is_opaque_signed_token():
    reset_rate_limit_state()
    client = TestClient(app)
    login = client.post(
        "/admin/login",
        data={"key": settings.api_key},
        follow_redirects=False,
    )
    assert login.status_code == 303
    set_cookie = login.headers.get("set-cookie", "")
    # The raw API key MUST NOT appear in the cookie value.
    assert settings.api_key not in set_cookie
    # And the cookie value starts with the v1 prefix the minter uses.
    assert "brains_admin_key=v1." in set_cookie
    # samesite=strict, FIX-003.
    assert "samesite=strict" in set_cookie.lower()


def test_raw_api_key_cookie_no_longer_authenticates():
    reset_rate_limit_state()
    client = TestClient(app)
    # Pre-FIX-005 the raw key worked. After FIX-005 it must not.
    client.cookies.set("brains_admin_key", settings.api_key)
    # HTML route -> rejected requests redirect to /admin/login (303), not 401.
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert "/admin/login" in response.headers.get("location", "")


def test_tampered_signed_token_is_rejected():
    reset_rate_limit_state()
    client = TestClient(app)
    # Mint a real token then flip the last byte of the signature.
    real = mint_browser_token(settings.api_key)
    tampered = real[:-1] + ("0" if real[-1] != "0" else "1")
    client.cookies.set("brains_admin_key", tampered)
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert "/admin/login" in response.headers.get("location", "")


# ---------- FIX-007: JSON API routes also require auth ----------


def test_dashboard_api_paths_require_auth():
    reset_rate_limit_state()
    client = TestClient(app)
    for path in (
        "/dashboard/api/overview",
        "/dashboard/api/workspaces",
        "/dashboard/api/decisions",
        "/dashboard/api/handoffs",
        "/dashboard/api/routes",
        "/dashboard/api/graph?workspace=.",
        "/dashboard/api/events",
    ):
        response = client.get(path)
        assert response.status_code == 401, f"{path}: {response.status_code}"

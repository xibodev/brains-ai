from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from scripts.check_core_surface import inventory, violations


def test_generated_core_surface_inventory_has_no_withdrawn_activation() -> None:
    assert violations(inventory()) == []
    assert inventory()["legacy_browser_source"] == []


def test_realtime_inventory_rejects_historical_execution_topics() -> None:
    snapshot = inventory()
    assert snapshot["realtime_org_channels"] == ["inbox", "sessions"]
    assert snapshot["withdrawn_realtime_topics_accepted"] == []


def test_withdrawn_installer_and_extra_registry_fail_closed() -> None:
    from brains.extras import EXTRAS, ExtraNotInstalledError, require_extra
    from brains.install import VALID_FEATURES, plan_changes

    assert EXTRAS == {}
    assert VALID_FEATURES == ()
    with pytest.raises(ExtraNotInstalledError, match="withdrawn"):
        require_extra("postgres", "storage")
    with pytest.raises(ValueError, match="withdrawn"):
        plan_changes(enable=["telegram"])


@pytest.mark.parametrize("backend", ["postgres", "postgresql"])
def test_postgres_runtime_selection_fails_closed(backend: str) -> None:
    from brains.storage.backends import resolve_db_url

    candidate = SimpleNamespace(
        db_url="postgresql+psycopg://placeholder.invalid/brains",
        subsystems=SimpleNamespace(storage=SimpleNamespace(backend=backend)),
    )
    with pytest.raises(ValueError, match="withdrawn|Unsupported"):
        resolve_db_url(candidate)


def test_postgres_restore_target_fails_closed(tmp_path) -> None:
    from brains.backup import UnsupportedBackend, restore_backup

    archive = tmp_path / "historical.tar.gz"
    archive.touch()
    with pytest.raises(UnsupportedBackend, match="SQLite"):
        restore_backup(archive, target_url="postgresql://placeholder.invalid/brains")


@pytest.mark.parametrize("bridge", ["telegram", "slack", "whatsapp", "whatsapp_web"])
def test_legacy_enabled_bridge_config_fails_closed(bridge: str) -> None:
    from brains.config import Settings, _enforce_subsystem_extras

    candidate = Settings()
    getattr(candidate.subsystems.bridges, bridge).enabled = True
    with pytest.raises(ValueError, match="withdrawn"):
        _enforce_subsystem_extras(candidate)


def test_governed_approval_notification_cannot_call_legacy_bridge(monkeypatch) -> None:
    from brains.exec import relay
    from brains.govern import GovernedRequest, _notify

    monkeypatch.setattr(
        relay,
        "notify_pending_approval",
        lambda *_args, **_kwargs: pytest.fail("approval escaped through a bridge"),
    )
    _notify(
        "ASK-0001",
        GovernedRequest(action="test", summary="local only", actor="operator", tool="test"),
    )


def test_ask_human_cannot_call_legacy_bridge(monkeypatch) -> None:
    import time

    from brains.control import decisions
    from brains.exec import relay
    from brains.mcp.tools import ask_human

    monkeypatch.setattr(relay, "_active_bridge_senders", lambda: pytest.fail("bridge activated"))
    monkeypatch.setattr(decisions, "list_open_requests", lambda: [])
    monkeypatch.setattr(decisions, "file_decision_request", lambda **_kwargs: {"code": "ASK-0001"})
    monkeypatch.setattr(decisions, "get_decision", lambda _code: {"status": "open"})
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(time, "time", lambda: next(ticks))
    result = ask_human("local decision", timeout_seconds=1)
    assert result["status"] == "pending"


@pytest.mark.parametrize(
    "name",
    ["search_semantic", "graph_query", "feedback_report", "session_message", "register_tool"],
)
def test_withdrawn_mcp_direct_dispatch_fails_closed(monkeypatch, name: str) -> None:
    monkeypatch.setenv("BRAINS_MCP_EXPERIMENTAL", "1")
    import brains.mcp.server as server

    server = importlib.reload(server)
    with pytest.raises(ValueError, match="unknown Brains tool"):
        server.call_tool(f"brains_{name}")


@pytest.mark.parametrize("name", ["features", "dashboard", "search-semantic", "recurring-fire"])
def test_withdrawn_cli_command_is_unknown(name: str) -> None:
    from brains.cli.app import app

    result = CliRunner().invoke(app, [name])
    assert result.exit_code != 0
    assert "No such command" in result.output


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/models"),
        ("post", "/v1/runtimes/enrol"),
        ("get", "/v1/personas/example"),
        ("get", "/v1/projects/example"),
        ("get", "/v1/issues/example"),
        ("post", "/hooks/example"),
        ("post", "/relay/reply"),
        ("get", "/v1/config/summary"),
        ("get", "/admin"),
        ("get", "/static/brains/brains.css"),
    ],
)
def test_withdrawn_http_direct_calls_fail_closed(method: str, path: str) -> None:
    from brains.main import app

    with TestClient(app) as client:
        response = client.request(method, path)
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/dashboard"),
        ("get", "/dashboard/operators"),
        ("get", "/admin"),
        ("get", "/admin/overview"),
        ("get", "/admin/config"),
        ("post", "/admin/config"),
        ("get", "/admin/test"),
        ("get", "/admin/secrets"),
        ("get", "/admin/healthz"),
        ("get", "/admin/api/config"),
        ("post", "/admin/api/config"),
        ("get", "/static/brains/brains.css"),
    ],
)
def test_former_legacy_opt_in_cannot_restore_browser(
    method: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAINS_LEGACY_SURFACES", "1")
    from brains.main import app

    with TestClient(app) as client:
        response = client.request(method, path)
    assert response.status_code == 404


def test_route_inventory_retains_only_modern_cookie_endpoints_under_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAINS_LEGACY_SURFACES", "1")
    from brains.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert {path for path in paths if path.startswith("/admin")} == {
        "/admin/login",
        "/admin/logout",
    }
    assert not any(path.startswith(("/dashboard", "/static/brains")) for path in paths)


def test_modern_app_authentication_survives_legacy_deletion() -> None:
    from brains.main import app

    with TestClient(app) as client:
        assert client.get("/admin/login").status_code == 200
        signed_in = client.post(
            "/admin/login",
            data={"key": "local-dev-key"},
            follow_redirects=False,
        )
        assert signed_in.status_code == 303
        assert signed_in.headers["location"] == "/app"
        assert client.get("/app").status_code == 200


def test_only_modern_login_template_remains() -> None:
    root = Path(__file__).resolve().parents[1]
    templates = root / "src/brains/web/templates"
    assert sorted(path.relative_to(templates).as_posix() for path in templates.rglob("*.html")) == [
        "admin/login.html"
    ]

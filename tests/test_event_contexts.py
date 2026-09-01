"""Typed event taxonomy and automatic Workspace inference."""

from __future__ import annotations

import ast
import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from brains.authz.resolver import principal_for_operator_slug, set_current_principal
from brains.control.events import (
    append_event,
    classify_event_kind,
    event_scope_report,
    get_event_context,
)
from brains.control.feedback import file_feedback
from brains.control.operators import add_operator
from brains.control.projects import create_project
from brains.control.sessions import register_workspace, start_session
from brains.control.tasks import create_task
from brains.main import app
from brains.mcp import server as mcp_server
from brains.mcp import tools as mcp_tools


def test_session_infers_workspace_and_core_category(tmp_path) -> None:
    started = start_session(str(tmp_path), tool="opencode")
    row = append_event("checkpoint_written", "saved", session_id=started["session_id"])
    context = get_event_context(row.id)

    assert row.workspace_id is not None
    assert context == {
        "event_id": row.id,
        "category": "checkpoint",
        "scope": "workspace",
        "scope_source": "session",
        "taxonomy_version": 1,
    }


def test_entity_metadata_infers_workspace(tmp_path) -> None:
    workspace = str(tmp_path)
    task = create_task(workspace, "scoped task")
    row = append_event("task_probe", "probe", metadata={"code": task["code"]})
    context = get_event_context(row.id)

    assert row.workspace_id is not None
    assert context["category"] == "task"
    assert context["scope_source"] == "metadata.code:task"


def test_issue_and_project_metadata_infer_workspace(tmp_path) -> None:
    workspace = register_workspace(str(tmp_path))
    project = create_project(
        workspace.org_id,
        f"project-{uuid.uuid4().hex[:8]}",
        "Scoped project",
        workspace_id=workspace.id,
    )
    row = append_event("project_probe", "probe", metadata={"code": project["code"]})
    assert row.workspace_id == workspace.id
    assert get_event_context(row.id)["scope_source"] == "metadata.code:project"


def test_feedback_metadata_infers_workspace(tmp_path) -> None:
    reporter = start_session(str(tmp_path), tool="opencode")
    feedback = file_feedback(
        str(tmp_path),
        "defect",
        "medium",
        f"scope probe {uuid.uuid4().hex[:8]}",
        reporter_session_id=reporter["session_id"],
    )
    row = append_event("feedback_probe", "probe", metadata={"code": feedback["code"]})
    assert row.workspace_id is not None
    assert get_event_context(row.id)["scope_source"] == "metadata.code:feedback"


def test_global_and_unknown_extension_scope_are_distinct() -> None:
    global_event = append_event("tool_probe", "global")
    extension = append_event("custom.extension", "unknown scope")

    assert get_event_context(global_event.id)["scope"] == "global"
    extension_context = get_event_context(extension.id)
    assert extension_context["category"] == "extension"
    assert extension_context["scope"] == "unresolved"


def test_every_literal_product_event_kind_has_a_core_category() -> None:
    root = Path(__file__).parents[1] / "src" / "brains"
    kinds: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            first = node.args[0]
            if (
                name == "append_event"
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                kinds.add(first.value)
    extensions = sorted(kind for kind in kinds if classify_event_kind(kind) == "extension")
    assert extensions == []


def test_explicit_unknown_workspace_and_invalid_kind_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown workspace"):
        append_event("custom.event", "bad scope", workspace_id=999_999_999)
    with pytest.raises(ValueError, match="event kind"):
        append_event("Bad Kind!", "bad grammar")


def test_scope_report_is_complete_and_bounded() -> None:
    append_event("custom.unresolved", "sample")
    report = event_scope_report()
    assert report["contexts_total"] == report["events_total"]
    assert report["missing_contexts"] == 0
    assert report["unresolved_total"] >= 1
    assert len(report["unresolved"]) <= 20


def test_migration_backfills_session_workspace_and_context(tmp_path) -> None:
    database = tmp_path / "legacy-events.sqlite"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(
            """
            CREATE TABLE workspaces (id INTEGER PRIMARY KEY);
            CREATE TABLE agent_sessions (id VARCHAR(32) PRIMARY KEY, workspace_id INTEGER NOT NULL);
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                workspace_id INTEGER,
                session_id VARCHAR(32),
                kind VARCHAR(64) NOT NULL,
                message TEXT NOT NULL
            );
            INSERT INTO workspaces (id) VALUES (7);
            INSERT INTO agent_sessions (id, workspace_id) VALUES ('ses-legacy', 7);
            INSERT INTO events (id, workspace_id, session_id, kind, message)
            VALUES (1, NULL, 'ses-legacy', 'legacy_kind', 'legacy');
            INSERT INTO events (id, workspace_id, session_id, kind, message)
            VALUES (2, NULL, NULL, 'legacy_global', 'unknown');
            """
        )
        path = (
            Path(__file__).parents[1]
            / "src"
            / "brains"
            / "storage"
            / "sql_migrations"
            / "147_event_contexts.py"
        )
        spec = importlib.util.spec_from_file_location("event_context_migration", path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.upgrade(conn)
        migration.upgrade(conn)
        conn.commit()

        assert conn.execute("SELECT workspace_id FROM events WHERE id = 1").fetchone() == (7,)
        assert conn.execute(
            "SELECT category, scope, scope_source FROM event_contexts WHERE event_id = 1"
        ).fetchone() == ("legacy", "workspace", "legacy_session")
        assert conn.execute(
            "SELECT category, scope, scope_source FROM event_contexts WHERE event_id = 2"
        ).fetchone() == ("legacy", "unresolved", "legacy_unresolved")
        assert conn.execute("SELECT COUNT(*) FROM event_contexts").fetchone() == (2,)
    finally:
        conn.close()


def test_http_and_mcp_scope_surfaces_are_wired(auth_headers) -> None:
    response = TestClient(app).get("/v1/admin/event-scope", headers=auth_headers)
    assert response.status_code == 200
    assert {"event_context", "event_scope_report"} <= set(mcp_server.TOOL_REGISTRY)


def test_mcp_event_scope_surfaces_require_install_admin() -> None:
    operator, _key = add_operator(f"event-reader-{uuid.uuid4().hex[:8]}")
    principal = principal_for_operator_slug(operator["slug"])
    assert principal is not None
    token = set_current_principal(principal)
    try:
        with pytest.raises(HTTPException) as context_denial:
            mcp_tools.event_context_tool(1)
        with pytest.raises(HTTPException) as report_denial:
            mcp_tools.event_scope_report_tool()
    finally:
        if token is not None:
            from brains.authz.resolver import current_principal

            current_principal.reset(token)

    assert context_denial.value.status_code == 403
    assert report_denial.value.status_code == 403

"""Tests for the Phase 8 Python AST code graph."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.storage.migrations import init_db


@pytest.fixture
def isolated_brains(tmp_path, monkeypatch):
    """Per-test DB + state isolation (mirrors tests/test_knowledge.py)."""
    db_path = tmp_path / "isolated.sqlite"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    monkeypatch.setenv("BRAINS_AUDIT_KEY_FILE", str(tmp_path / "audit-key"))
    monkeypatch.delenv("BRAINS_AUDIT_KEY", raising=False)
    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)

    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", SessionLocal)

    import brains.control.events as events_module
    import brains.control.sessions as sessions_module

    for mod in (events_module, sessions_module):
        monkeypatch.setattr(mod, "SessionLocal", SessionLocal, raising=False)

    init_db()
    yield tmp_path


def _write_sample_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "utils.py").write_text(
        """
def helper(value):
    return value + 1


class Utility:
    def double(self, value):
        return helper(value) * 2
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "service.py").write_text(
        """
from utils import helper


class Service:
    def run(self, value):
        return helper(value)


def make_value(raw):
    return helper(raw)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        """
import service
from utils import helper


async def bootstrap():
    return helper(1)


def entrypoint():
    svc = service.Service()
    return svc.run(helper(2))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (root / ".venv").mkdir()
    (root / ".venv" / "ignored.py").write_text(
        "def ignored():\n    return None\n",
        encoding="utf-8",
    )
    return root


def test_init_db_creates_code_graph_tables(isolated_brains):
    conn = sqlite3.connect(str(isolated_brains / "isolated.sqlite"))
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"code_graph_nodes", "code_graph_edges"} <= tables
        node_cols = {row[1] for row in conn.execute("PRAGMA table_info(code_graph_nodes)")}
        edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(code_graph_edges)")}
        assert {"workspace_id", "kind", "name", "path", "lineno", "subsystem_id"} <= node_cols
        assert {"workspace_id", "src_id", "dst_id", "relation", "confidence"} <= edge_cols
    finally:
        conn.close()


def test_build_code_graph_extracts_nodes_edges_and_rebuilds(isolated_brains):
    from brains.context.code_graph import build_code_graph
    from brains.storage.models import CodeGraphEdge, CodeGraphNode

    repo = _write_sample_repo(isolated_brains / "sample-repo")
    result = build_code_graph(str(repo))
    assert result["files"] == 3
    assert result["nodes"] >= 10
    assert result["edges"] >= 8

    second = build_code_graph(str(repo))
    assert second == result

    with db_module.SessionLocal() as session:
        nodes = session.query(CodeGraphNode).all()
        edges = session.query(CodeGraphEdge).all()
        node_by_id = {node.id: node for node in nodes}

    assert len(nodes) == result["nodes"]
    assert len(edges) == result["edges"]
    node_names = {(node.kind, node.name) for node in nodes}
    assert ("file", "main.py") in node_names
    assert ("module", "service") in node_names
    assert ("class", "Service") in node_names
    assert ("function", "Service.run") in node_names
    assert ("function", "helper") in node_names

    relations = {edge.relation for edge in edges}
    assert {"contains", "imports", "calls"} <= relations
    assert any(
        edge.relation == "calls"
        and edge.confidence == "inferred"
        and node_by_id[edge.dst_id].name == "helper"
        for edge in edges
    )
    assert any(edge.relation == "imports" and edge.confidence == "extracted" for edge in edges)
    assert all(node.subsystem_id is not None for node in nodes)


def test_graph_auto_builds_on_first_query(isolated_brains):
    """graph_* must self-bootstrap: a query on a never-built workspace auto-builds
    the graph (pure-local AST parse) instead of returning a silent empty."""
    from brains.context.code_graph import graph_neighbors, graph_query
    from brains.storage.models import CodeGraphNode

    repo = _write_sample_repo(isolated_brains / "auto-graph-repo")

    # NO build_code_graph() call — the query itself should build it.
    output = graph_query(str(repo), "helper entrypoint", depth=1, token_budget=40)
    assert output.strip(), "expected graph_query to auto-build and return content"

    with db_module.SessionLocal() as session:
        assert session.query(CodeGraphNode).first() is not None, "graph should now exist"

    neighbors = graph_neighbors(str(repo), "Service.run", relation="calls")
    assert any(row["name"] == "helper" for row in neighbors)


def test_graph_auto_build_respects_disable_flag(isolated_brains, monkeypatch):
    from brains.context import code_graph
    from brains.context.code_graph import graph_query

    monkeypatch.setattr(code_graph.settings, "graph_auto_build", False)
    repo = _write_sample_repo(isolated_brains / "no-auto-graph")
    # auto-build disabled + never built -> no real graph content (placeholder only).
    assert "helper" not in graph_query(str(repo), "helper", depth=1, token_budget=40)

    from brains.context.code_graph import (
        build_code_graph,
        graph_neighbors,
        graph_path,
        graph_query,
        list_subsystems,
    )

    repo = _write_sample_repo(isolated_brains / "sample-repo")
    build_code_graph(str(repo))

    neighbors = graph_neighbors(str(repo), "Service.run", relation="calls")
    assert any(row["name"] == "helper" and row["confidence"] == "inferred" for row in neighbors)

    path = graph_path(str(repo), "entrypoint", "helper")
    assert path is not None
    assert path[0]["name"] == "entrypoint"
    assert path[-1]["name"] == "helper"

    output = graph_query(str(repo), "helper entrypoint", depth=1, token_budget=40)
    assert output.strip()
    assert "helper" in output
    assert len(output) <= 160

    subsystems = list_subsystems(str(repo))
    assert subsystems
    assert subsystems[0]["size"] >= 1
    assert subsystems[0]["sample_node_names"]


def test_private_workspace_hidden_from_non_member(isolated_brains, monkeypatch):
    from brains.context.code_graph import (
        build_code_graph,
        graph_neighbors,
        graph_path,
        graph_query,
        list_subsystems,
    )
    from brains.control.memberships import set_workspace_visibility
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.sessions import register_workspace

    ensure_admin_operator()
    add_operator("alice")
    repo = _write_sample_repo(isolated_brains / "private-graph")
    register_workspace(str(repo), slug="private-graph")
    set_workspace_visibility("private-graph", "private")
    build_code_graph(str(repo))

    monkeypatch.setenv("BRAINS_OPERATOR", "alice")
    assert graph_neighbors(str(repo), "helper") == []
    assert graph_path(str(repo), "entrypoint", "helper") is None
    assert graph_query(str(repo), "helper") == ""
    assert list_subsystems(str(repo)) == []

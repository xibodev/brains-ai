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

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA optimize")


def _write_sample_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "alpha.py").write_text(
        """
def helper(value):
    return value + 1


def entrypoint(raw):
    return helper(raw)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "beta.py").write_text(
        """
from alpha import helper


class Worker:
    def run(self, value):
        return helper(value)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return root


def test_render_graph_svg_contains_nodes_and_respects_visibility(isolated_brains, monkeypatch):
    from brains.context.code_graph import build_code_graph
    from brains.context.graph_viz import render_graph_svg
    from brains.control.memberships import set_workspace_visibility
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.sessions import register_workspace

    ensure_admin_operator()
    add_operator("alice")
    repo = _write_sample_repo(isolated_brains / "private-graph")
    register_workspace(str(repo), slug="private-graph")
    build_code_graph(str(repo))

    svg = render_graph_svg(str(repo))
    assert svg is not None
    assert svg.startswith("<svg")
    assert "helper" in svg
    assert "entrypoint" in svg

    set_workspace_visibility("private-graph", "private")
    monkeypatch.setenv("BRAINS_OPERATOR", "alice")
    assert render_graph_svg(str(repo)) is None


def test_graph_export_writes_local_first_svg_and_html(isolated_brains):
    from brains.context.code_graph import build_code_graph
    from brains.context.graph_viz import graph_export

    repo = _write_sample_repo(isolated_brains / "export-graph")
    build_code_graph(str(repo))

    result = graph_export(str(repo), str(isolated_brains / "exports"))
    svg_path = Path(result["svg_path"])
    html_path = Path(result["html_path"])

    assert result["nodes"] > 0
    assert result["edges"] > 0
    assert svg_path.exists()
    assert svg_path.stat().st_size > 0
    assert html_path.exists()
    assert html_path.stat().st_size > 0

    html = html_path.read_text(encoding="utf-8")
    assert "<svg" in html
    assert 'src="http' not in html
    assert 'href="http' not in html

"""Tests for background pre-indexing on session start."""

from __future__ import annotations

from brains.context import prewarm


def _write_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "svc.py").write_text(
        "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    return root


def test_schedule_prewarm_disabled_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(prewarm.settings, "prewarm_index_on_session", False)
    assert prewarm.schedule_prewarm(str(_write_repo(tmp_path / "r"))) is False


def test_schedule_prewarm_skips_non_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(prewarm.settings, "prewarm_index_on_session", True)
    assert prewarm.schedule_prewarm(str(tmp_path / "does-not-exist")) is False


def test_prewarm_run_builds_graph(tmp_path, monkeypatch):
    """_run (the worker body, called synchronously here) should build the code
    graph for an un-indexed repo — pure local AST, no embed model needed."""
    monkeypatch.setattr(prewarm.settings, "graph_auto_build", True)
    monkeypatch.setattr(prewarm.settings, "semantic_auto_embed", False)
    repo = _write_repo(tmp_path / "graphrepo")

    from brains.control.sessions import register_workspace
    from brains.storage.db import SessionLocal
    from brains.storage.models import CodeGraphNode

    prewarm._run(str(repo))

    with SessionLocal() as session:
        ws = register_workspace(str(repo))
        nodes = session.query(CodeGraphNode).filter(CodeGraphNode.workspace_id == ws.id).count()
    assert nodes > 0, "prewarm should have built the code graph"


def test_start_session_schedules_prewarm(tmp_path, monkeypatch):
    """Opening a session must invoke the prewarm scheduler with the workspace path."""
    calls: list[str] = []
    monkeypatch.setattr(prewarm, "schedule_prewarm", lambda path: calls.append(path) or True)

    from brains.control.sessions import start_session

    repo = _write_repo(tmp_path / "sessrepo")
    start_session(str(repo), tool="codex")
    assert calls == [str(repo)]

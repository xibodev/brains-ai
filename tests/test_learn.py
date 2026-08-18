from __future__ import annotations

import pytest
from sqlalchemy import create_engine

import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.storage.migrations import init_db


@pytest.fixture
def isolated_brains(tmp_path, monkeypatch):
    """Per-test DB + audit/state isolation (mirrors tests/test_knowledge.py)."""
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

    import brains.audit as audit_module
    import brains.control.events as events_module
    import brains.control.sessions as sessions_module

    for mod in (audit_module, events_module, sessions_module):
        monkeypatch.setattr(mod, "SessionLocal", SessionLocal, raising=False)

    from brains.audit import _reset_key_cache

    _reset_key_cache()
    init_db()
    yield tmp_path
    _reset_key_cache()


def _make_workspace(path, slug: str) -> int:
    from brains.control.sessions import register_workspace

    path.mkdir(parents=True, exist_ok=True)
    ws = register_workspace(str(path), slug=slug)
    return ws.id


def test_propose_from_history_dry_run_writes_nothing(isolated_brains, tmp_path):
    from brains.control.events import append_event
    from brains.control.knowledge import search_knowledge
    from brains.control.learn import propose_from_history

    ws = tmp_path / "payments-api"
    workspace_id = _make_workspace(ws, "payments-api")
    append_event("blocked", "Dependency mirror is unavailable", workspace_id=workspace_id)

    result = propose_from_history(str(ws))

    assert result["applied"] == []
    assert result["proposals"]
    proposal = result["proposals"][0]
    assert proposal["type"] == "blocker"
    assert proposal["title"] == "Dependency mirror is unavailable"
    assert proposal["body"].endswith("(blocked): Dependency mirror is unavailable")
    assert proposal["provenance"] == "inferred"
    assert proposal["confidence"] == "low"
    assert proposal["workspace"] == "payments-api"
    assert search_knowledge() == []


def test_propose_from_history_apply_writes_searchable_entry(isolated_brains, tmp_path):
    from brains.control.events import append_event
    from brains.control.knowledge import search_knowledge
    from brains.control.learn import propose_from_history

    ws = tmp_path / "payments-api"
    workspace_id = _make_workspace(ws, "payments-api")
    append_event("blocked", "Dependency mirror is unavailable", workspace_id=workspace_id)

    result = propose_from_history(str(ws), apply=True)

    assert len(result["applied"]) == 1
    matches = search_knowledge(query="Dependency mirror")
    assert [row["code"] for row in matches] == result["applied"]
    assert matches[0]["provenance"] == "inferred"
    assert matches[0]["confidence"] == "low"

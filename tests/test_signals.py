"""Tests for Phase 3 advisory coordination signals."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.storage.migrations import init_db


@pytest.fixture
def isolated_brains(tmp_path, monkeypatch):
    """Per-test DB + audit/state isolation (mirrors tests/test_memberships.py)."""
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
    import brains.control.handoffs as handoffs_module
    import brains.control.sessions as sessions_module

    for mod in (audit_module, events_module, handoffs_module, sessions_module):
        monkeypatch.setattr(mod, "SessionLocal", SessionLocal, raising=False)

    from brains.audit import _reset_key_cache

    _reset_key_cache()
    init_db()
    yield tmp_path
    _reset_key_cache()


def _make_workspace(path, slug: str, visibility: str = "shared") -> int:
    from brains.control.memberships import set_workspace_visibility
    from brains.control.sessions import register_workspace

    path.mkdir(parents=True, exist_ok=True)
    ws = register_workspace(str(path), slug=slug)
    if visibility != "shared":
        set_workspace_visibility(slug, visibility)
    return ws.id


def _set_current_operator(monkeypatch, slug: str) -> None:
    monkeypatch.setenv("BRAINS_OPERATOR", slug)


def _by_type(signals: list[dict], type: str) -> dict | None:
    return next((signal for signal in signals if signal["type"] == type), None)


def test_blocker_entry_produces_blocked_signal(isolated_brains, tmp_path):
    from brains.control.knowledge import add_knowledge_entry
    from brains.control.signals import list_signals

    ws = tmp_path / "payments-api"
    _make_workspace(ws, "payments-api")
    add_knowledge_entry(str(ws), "blocker", "CI is blocked")

    signal = _by_type(list_signals(str(ws)), "blocked")
    assert signal is not None
    assert signal["count"] >= 1
    assert signal["scope"] == "workspace"
    assert signal["workspace"] == "payments-api"
    assert signal["last_at"] is not None


def test_open_handoff_produces_handoff_available_signal(isolated_brains, tmp_path):
    from brains.control.handoffs import set_handoff
    from brains.control.signals import list_signals

    ws = tmp_path / "customer-api"
    _make_workspace(ws, "customer-api")
    set_handoff(str(ws), "Continue auth smoke test")

    signal = _by_type(list_signals(str(ws)), "handoff_available")
    assert signal is not None
    assert signal["count"] == 1
    assert signal["workspace"] == "customer-api"
    assert signal["last_at"] is not None


def test_duplicate_work_signal_for_recent_concurrent_sessions(isolated_brains, tmp_path):
    from brains.control.sessions import start_session
    from brains.control.signals import list_signals

    ws = tmp_path / "billing-api"
    _make_workspace(ws, "billing-api")
    start_session(str(ws), tool="codex")
    start_session(str(ws), tool="copilot")

    signal = _by_type(list_signals(str(ws)), "duplicate_work")
    assert signal is not None
    assert signal["count"] == 2
    assert signal["scope"] == "workspace"
    assert signal["workspace"] == "billing-api"
    assert signal["last_at"] is not None


def test_signals_respect_visibility_but_include_shared_scope(
    isolated_brains,
    tmp_path,
    monkeypatch,
):
    from brains.control.knowledge import add_knowledge_entry
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.signals import list_signals

    ensure_admin_operator()
    add_operator("alice")
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_private, "ws-private", visibility="private")

    add_knowledge_entry(str(ws_private), "blocker", "private blocker", scope="workspace")
    add_knowledge_entry(str(ws_private), "blocker", "shared blocker", scope="shared")

    admin_signal = _by_type(list_signals(), "blocked")
    assert admin_signal is not None
    assert admin_signal["count"] == 2

    _set_current_operator(monkeypatch, "alice")
    alice_signal = _by_type(list_signals(), "blocked")
    assert alice_signal is not None
    assert alice_signal["count"] == 1
    assert alice_signal["workspace"] is None

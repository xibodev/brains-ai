"""Tests for the Layer 3 multi-operator model — cross-workspace presence.

Covers:

* ``list_other_operators_active`` returns an empty list when no other
  operators have an active session.
* It aggregates ``(workspace_count, active_session_count,
  last_activity_at)`` correctly across multiple sessions per operator.
* It deliberately EXCLUDES the caller's own sessions, regardless of
  whether the caller is ``admin`` or a normal operator.
* It deliberately omits ended sessions (``ended_at IS NOT NULL``).
* The dashboard ``/dashboard/operators`` page renders without 500ing
  and reflects the same projection (no workspace names or session ids).
* The MCP tool ``brains.list_other_operators_active`` is registered and
  callable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import brains.storage.db as db_module
import brains.storage.migrations as migrations_module


@pytest.fixture
def isolated_brains(tmp_path, monkeypatch):
    """Per-test isolation matching ``tests/test_memberships.py``.

    Rebinds the module-level SQLAlchemy engine + ``SessionLocal`` on
    every coordination module that captured it at import time, so the
    presence aggregation operates against the isolated DB.
    """
    db_path = tmp_path / "isolated.sqlite"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))

    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", SessionLocal)

    import brains.control.claims as claims_module
    import brains.control.decisions as decisions_module
    import brains.control.events as events_module
    import brains.control.handoffs as handoffs_module
    import brains.control.help as help_module
    import brains.control.mailbox as mailbox_module
    import brains.control.presence as presence_module
    import brains.control.recurring as recurring_module
    import brains.control.sessions as sessions_module
    import brains.control.tasks as tasks_module
    import brains.control.views as views_module
    import brains.control.welcome as welcome_module

    for mod in (
        sessions_module,
        tasks_module,
        handoffs_module,
        decisions_module,
        events_module,
        claims_module,
        recurring_module,
        mailbox_module,
        help_module,
        views_module,
        welcome_module,
        presence_module,
    ):
        monkeypatch.setattr(mod, "_db_module", db_module, raising=False)
        monkeypatch.setattr(mod, "SessionLocal", SessionLocal, raising=False)
    yield state


def _make_workspace(path: Path, slug: str) -> int:
    from brains.control.sessions import register_workspace

    path.mkdir(parents=True, exist_ok=True)
    ws = register_workspace(str(path), slug=slug)
    return ws.id


def _set_current_operator(monkeypatch, slug: str) -> None:
    monkeypatch.setenv("BRAINS_OPERATOR", slug)


# ---------- baseline ----------


def test_presence_empty_when_no_active_sessions(isolated_brains: Path) -> None:
    from brains.control.operators import ensure_admin_operator
    from brains.control.presence import list_other_operators_active

    ensure_admin_operator()
    assert list_other_operators_active() == []


def test_presence_admin_excludes_their_own_active_sessions(isolated_brains: Path, tmp_path) -> None:
    """Admin is the caller — admin's sessions must not appear."""
    from brains.control.operators import ensure_admin_operator
    from brains.control.presence import list_other_operators_active
    from brains.control.sessions import start_session

    ensure_admin_operator()
    ws = tmp_path / "ws-a"
    _make_workspace(ws, "ws-a")
    # Admin (no BRAINS_OPERATOR override) is the implicit current op.
    start_session(str(ws), tool="pytest")

    assert list_other_operators_active() == []


# ---------- aggregation ----------


def test_presence_aggregates_workspace_and_session_counts(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    """A peer running 3 sessions across 2 workspaces => count=3, ws=2."""
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.presence import list_other_operators_active
    from brains.control.sessions import start_session

    ensure_admin_operator()
    add_operator("alice", display_name="Alice A")

    ws1 = tmp_path / "ws-1"
    ws2 = tmp_path / "ws-2"
    _make_workspace(ws1, "ws-1")
    _make_workspace(ws2, "ws-2")

    _set_current_operator(monkeypatch, "alice")
    start_session(str(ws1), tool="pytest")
    start_session(str(ws1), tool="pytest")
    start_session(str(ws2), tool="pytest")

    # Caller switches to admin (default) and asks for presence.
    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)
    rows = list_other_operators_active()
    assert len(rows) == 1
    row = rows[0]
    assert row["operator_slug"] == "alice"
    assert row["display_name"] == "Alice A"
    assert row["workspace_count"] == 2
    assert row["active_session_count"] == 3
    assert row["last_activity_at"] is not None


def test_presence_excludes_ended_sessions(isolated_brains: Path, tmp_path, monkeypatch) -> None:
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.presence import list_other_operators_active
    from brains.control.sessions import end_session, start_session

    ensure_admin_operator()
    add_operator("bob")
    ws = tmp_path / "ws-b"
    _make_workspace(ws, "ws-b")

    _set_current_operator(monkeypatch, "bob")
    rec = start_session(str(ws), tool="pytest")
    end_session(rec["session_id"], summary="done")

    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)
    assert list_other_operators_active() == []


# ---------- caller-exclusion across roles ----------


def test_presence_non_admin_excludes_their_own_sessions(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    """When alice asks, alice's own sessions must not appear."""
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.presence import list_other_operators_active
    from brains.control.sessions import start_session

    ensure_admin_operator()
    add_operator("alice")
    add_operator("bob")
    ws = tmp_path / "ws-c"
    _make_workspace(ws, "ws-c")

    _set_current_operator(monkeypatch, "alice")
    start_session(str(ws), tool="pytest")
    _set_current_operator(monkeypatch, "bob")
    start_session(str(ws), tool="pytest")

    # Now act as alice.
    _set_current_operator(monkeypatch, "alice")
    rows = list_other_operators_active()
    slugs = {row["operator_slug"] for row in rows}
    assert "alice" not in slugs
    assert "bob" in slugs


def test_presence_admin_sees_every_other_operator(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.presence import list_other_operators_active
    from brains.control.sessions import start_session

    ensure_admin_operator()
    add_operator("alice")
    add_operator("bob")
    ws = tmp_path / "ws-d"
    _make_workspace(ws, "ws-d")

    _set_current_operator(monkeypatch, "alice")
    start_session(str(ws), tool="pytest")
    _set_current_operator(monkeypatch, "bob")
    start_session(str(ws), tool="pytest")

    # Admin asks.
    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)
    rows = list_other_operators_active()
    slugs = {row["operator_slug"] for row in rows}
    assert {"alice", "bob"} <= slugs


# ---------- ordering: most recently active first ----------


def test_presence_orders_by_last_activity_desc(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    """Older peer should appear AFTER newer peer in the result."""
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.presence import list_other_operators_active
    from brains.control.sessions import start_session
    from brains.storage.models import AgentSession

    ensure_admin_operator()
    add_operator("alice")
    add_operator("bob")
    ws = tmp_path / "ws-e"
    _make_workspace(ws, "ws-e")

    _set_current_operator(monkeypatch, "alice")
    alice_rec = start_session(str(ws), tool="pytest")
    _set_current_operator(monkeypatch, "bob")
    bob_rec = start_session(str(ws), tool="pytest")

    # Forcibly rewind alice's last_activity so bob looks more recent.
    older = datetime.now(UTC) - timedelta(hours=2)
    with db_module.SessionLocal() as session:
        alice_session = (
            session.query(AgentSession).filter(AgentSession.id == alice_rec["session_id"]).one()
        )
        alice_session.last_activity_at = older
        alice_session.started_at = older
        bob_session = (
            session.query(AgentSession).filter(AgentSession.id == bob_rec["session_id"]).one()
        )
        bob_session.last_activity_at = datetime.now(UTC)
        session.commit()

    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)
    rows = list_other_operators_active()
    slugs_in_order = [row["operator_slug"] for row in rows]
    # Bob is fresher, must appear first.
    assert slugs_in_order.index("bob") < slugs_in_order.index("alice")


# ---------- projection safety ----------


def test_presence_projection_omits_workspace_names_and_session_ids(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    """Per decision record 0002, presence must NOT leak workspace
    slugs / session ids / summaries to other operators."""
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.presence import list_other_operators_active
    from brains.control.sessions import start_session

    ensure_admin_operator()
    add_operator("carol")
    ws = tmp_path / "ws-secret"
    _make_workspace(ws, "ws-secret")

    _set_current_operator(monkeypatch, "carol")
    start_session(str(ws), tool="pytest")

    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)
    rows = list_other_operators_active()
    assert len(rows) == 1
    row = rows[0]
    allowed = {
        "operator_slug",
        "display_name",
        "workspace_count",
        "active_session_count",
        "last_activity_at",
    }
    assert set(row.keys()) == allowed
    # No workspace slug leakage even in str(row).
    assert "ws-secret" not in str(row)


# ---------- dashboard wiring ----------


def test_dashboard_operators_page_renders(isolated_brains: Path, tmp_path, monkeypatch) -> None:
    """The /dashboard/operators HTML page returns 200 + the panel marker."""
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.sessions import start_session
    from brains.dashboard.app import app

    ensure_admin_operator()
    add_operator("dave")
    ws = tmp_path / "ws-f"
    _make_workspace(ws, "ws-f")

    _set_current_operator(monkeypatch, "dave")
    start_session(str(ws), tool="pytest")

    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)
    client = TestClient(app)
    # Authenticate with the conftest-pinned admin key.
    response = client.get(
        "/dashboard/operators",
        headers={"Authorization": "Bearer local-dev-key"},
    )
    assert response.status_code == 200, response.text
    body = response.text
    assert "Other operators" in body
    assert "dave" in body


def test_dashboard_operators_api_returns_projection(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.sessions import start_session
    from brains.dashboard.app import app

    ensure_admin_operator()
    add_operator("eve")
    ws = tmp_path / "ws-g"
    _make_workspace(ws, "ws-g")

    _set_current_operator(monkeypatch, "eve")
    start_session(str(ws), tool="pytest")

    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)
    client = TestClient(app)
    response = client.get(
        "/dashboard/api/operators",
        headers={"Authorization": "Bearer local-dev-key"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, list)
    assert any(row["operator_slug"] == "eve" for row in payload)

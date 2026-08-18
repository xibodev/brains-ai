"""Tests for the Layer 2 multi-operator model — workspaces + membership.

Covers:

* Disk migration ``060_workspace_membership.py`` is discoverable and
  produces the expected table + column shape on existing installs.
* ``visible_workspace_ids`` returns ``None`` (no filter) for admin, the
  shared+member union for normal operators, and refreshes when
  visibility flips or a membership is added/removed.
* ``operator_can_see_workspace`` short-circuits on admin, allows shared
  workspaces unconditionally, and requires an explicit row for private
  workspaces.
* ``add_membership`` / ``remove_membership`` are idempotent, validate
  the slug, refuse invalid roles, return useful errors, and resolve
  workspaces by slug AND by absolute path. Inviting ``admin`` is a
  no-op (no row written).
* ``list_memberships`` filters by workspace / operator and never
  includes admin.
* Coordination ``list_*`` functions filter by visibility — admin sees
  everything, non-members never see private workspaces, and shared
  workspaces remain visible to every operator (back-compat default).
* ``pick_handoff`` raises ``PermissionError`` when the resolved
  operator isn't a member of the target private workspace.
* The CLI ``brains workspace invite/uninvite/members/visibility/show``
  verbs round-trip via Typer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from typer.testing import CliRunner

import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.storage.migrations import _list_disk_migrations, init_db


@pytest.fixture
def isolated_brains(tmp_path, monkeypatch):
    """Same per-test isolation as ``tests/test_operators.py``.

    Rebinds the module-level SQLAlchemy engine to a temp SQLite file
    and points ``BRAINS_STATE_DIR`` at a temp dir. Also rebinds the
    ``SessionLocal`` symbol on every coordination module that captured
    it at import time, so the visibility filter operates against the
    isolated DB.
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

    # Every coordination module captured SessionLocal at import; rebind.
    import brains.control.claims as claims_module
    import brains.control.decisions as decisions_module
    import brains.control.events as events_module
    import brains.control.handoffs as handoffs_module
    import brains.control.help as help_module
    import brains.control.mailbox as mailbox_module
    import brains.control.recurring as recurring_module
    import brains.control.sessions as sessions_module
    import brains.control.snapshots as snapshots_module
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
        snapshots_module,
    ):
        monkeypatch.setattr(mod, "SessionLocal", SessionLocal, raising=False)
    yield state


def _make_workspace(path: Path, slug: str, visibility: str = "shared") -> int:
    """Register a workspace and (optionally) flip its visibility."""
    from brains.control.memberships import set_workspace_visibility
    from brains.control.sessions import register_workspace

    path.mkdir(parents=True, exist_ok=True)
    ws = register_workspace(str(path), slug=slug)
    if visibility != "shared":
        set_workspace_visibility(slug, visibility)
    return ws.id


def _join_default_org(slug: str, role: str = "member") -> None:
    """Grant ``slug`` membership of the default Org.

    Since BL-P0-01, Workspace visibility is bounded by Org membership: an
    org-less Workspace resolves to the ``default`` Org, and an operator who is
    not a member of it sees nothing there. Migration ``130`` backfills every
    operator that existed at upgrade time; an operator created afterwards - as
    these tests do - is invited explicitly.
    """
    from brains.control.orgs import add_member, ensure_default_org

    org = ensure_default_org()
    add_member(org["id"], slug, role=role)


# ---------- migration shape ----------


def test_disk_migration_060_is_discoverable() -> None:
    names = {p.name for p in _list_disk_migrations()}
    assert "060_workspace_membership.py" in names


def test_init_db_creates_membership_table_and_visibility_column(
    isolated_brains: Path,
) -> None:
    init_db()
    db_path = isolated_brains.parent / "isolated.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "workspace_memberships" in tables
        cols = {row[1] for row in conn.execute("PRAGMA table_info(workspaces)")}
        assert "visibility" in cols
        membership_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(workspace_memberships)")
        }
        assert {"operator_id", "workspace_id", "role", "created_at"} <= membership_cols
    finally:
        conn.close()


# ---------- visibility resolver ----------


def test_visible_workspace_ids_none_for_admin(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import visible_workspace_ids
    from brains.control.operators import ensure_admin_operator

    admin = ensure_admin_operator()
    _make_workspace(tmp_path / "ws-a", "ws-a", visibility="private")
    assert visible_workspace_ids(admin["id"]) is None


def test_visible_workspace_ids_shared_default_visible_to_non_admin(
    isolated_brains: Path, tmp_path
) -> None:
    from brains.control.memberships import visible_workspace_ids
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    alice, _ = add_operator("alice")
    shared_id = _make_workspace(tmp_path / "ws-shared", "ws-shared", visibility="shared")
    private_id = _make_workspace(tmp_path / "ws-private", "ws-private", visibility="private")
    visible = visible_workspace_ids(alice["id"])
    assert shared_id in visible
    assert private_id not in visible


def test_visible_workspace_ids_includes_membership_rows(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import add_membership, visible_workspace_ids
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    alice, _ = add_operator("alice")
    private_id = _make_workspace(tmp_path / "ws-private", "ws-private", visibility="private")
    add_membership("ws-private", "alice")
    visible = visible_workspace_ids(alice["id"])
    assert private_id in visible


def test_operator_can_see_workspace_rules(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import (
        add_membership,
        operator_can_see_workspace,
    )
    from brains.control.operators import add_operator, ensure_admin_operator

    admin = ensure_admin_operator()
    alice, _ = add_operator("alice")
    bob, _ = add_operator("bob")

    shared_id = _make_workspace(tmp_path / "ws-shared", "ws-shared", visibility="shared")
    private_id = _make_workspace(tmp_path / "ws-private", "ws-private", visibility="private")

    # Admin sees everything.
    assert operator_can_see_workspace(admin["id"], shared_id) is True
    assert operator_can_see_workspace(admin["id"], private_id) is True
    # Shared is open to everyone.
    assert operator_can_see_workspace(alice["id"], shared_id) is True
    # Private is gated.
    assert operator_can_see_workspace(alice["id"], private_id) is False
    assert operator_can_see_workspace(bob["id"], private_id) is False
    # After inviting alice, she sees it; bob still doesn't.
    add_membership("ws-private", "alice")
    assert operator_can_see_workspace(alice["id"], private_id) is True
    assert operator_can_see_workspace(bob["id"], private_id) is False


# ---------- add / remove / list ----------


def test_add_membership_is_idempotent_and_updates_role(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import add_membership, list_memberships
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    _make_workspace(tmp_path / "ws", "ws", visibility="private")

    first = add_membership("ws", "alice")
    second = add_membership("ws", "alice")
    assert first["id"] == second["id"]
    assert first["role"] == "member"

    promoted = add_membership("ws", "alice", role="owner")
    assert promoted["id"] == first["id"]
    assert promoted["role"] == "owner"

    rows = list_memberships(workspace="ws")
    assert len(rows) == 1
    assert rows[0]["role"] == "owner"


def test_add_membership_admin_is_noop(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import add_membership, list_memberships
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    _make_workspace(tmp_path / "ws", "ws", visibility="private")
    record = add_membership("ws", "admin")
    assert record["operator_slug"] == "admin"
    # No row written — admin has implicit membership everywhere.
    assert list_memberships(workspace="ws") == []


def test_add_membership_rejects_bad_role(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import (
        MembershipRoleError,
        add_membership,
    )
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    _make_workspace(tmp_path / "ws", "ws")
    with pytest.raises(MembershipRoleError):
        add_membership("ws", "alice", role="superuser")


def test_add_membership_rejects_unknown_workspace_or_operator(
    isolated_brains: Path, tmp_path
) -> None:
    from brains.control.memberships import (
        OperatorLookupError,
        WorkspaceLookupError,
        add_membership,
    )
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    _make_workspace(tmp_path / "ws", "ws")
    with pytest.raises(WorkspaceLookupError):
        add_membership("nope", "alice")
    with pytest.raises(OperatorLookupError):
        add_membership("ws", "nobody")


def test_add_membership_resolves_workspace_by_path(isolated_brains: Path, tmp_path) -> None:
    from brains.control.common import normalize_path
    from brains.control.memberships import add_membership, list_memberships
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    ws_path = tmp_path / "ws"
    _make_workspace(ws_path, "ws-slug", visibility="private")
    # Use the normalized absolute path, matching how register_workspace
    # stores the row, so the lookup hits the path branch (not the slug).
    add_membership(normalize_path(str(ws_path)), "alice")
    rows = list_memberships(operator="alice")
    assert rows and rows[0]["workspace_slug"] == "ws-slug"


def test_remove_membership_round_trip(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import (
        MembershipNotFoundError,
        add_membership,
        list_memberships,
        remove_membership,
    )
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    _make_workspace(tmp_path / "ws", "ws", visibility="private")
    add_membership("ws", "alice")
    removed = remove_membership("ws", "alice")
    assert removed["operator_slug"] == "alice"
    assert list_memberships(workspace="ws") == []
    with pytest.raises(MembershipNotFoundError):
        remove_membership("ws", "alice")


def test_list_memberships_filters_and_omits_admin(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import (
        add_membership,
        list_memberships,
    )
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    add_operator("bob")
    _make_workspace(tmp_path / "ws1", "ws1", visibility="private")
    _make_workspace(tmp_path / "ws2", "ws2", visibility="private")
    add_membership("ws1", "alice")
    add_membership("ws1", "bob")
    add_membership("ws2", "alice")
    # Admin invite is a no-op so admin must never appear.
    add_membership("ws1", "admin")

    rows_ws1 = list_memberships(workspace="ws1")
    assert {r["operator_slug"] for r in rows_ws1} == {"alice", "bob"}
    rows_alice = list_memberships(operator="alice")
    assert {r["workspace_slug"] for r in rows_alice} == {"ws1", "ws2"}


# ---------- visibility flip ----------


def test_set_workspace_visibility_validates_value(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import (
        WorkspaceVisibilityError,
        set_workspace_visibility,
    )
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    _make_workspace(tmp_path / "ws", "ws")
    with pytest.raises(WorkspaceVisibilityError):
        set_workspace_visibility("ws", "secret")


def test_set_workspace_visibility_round_trip(isolated_brains: Path, tmp_path) -> None:
    from brains.control.memberships import (
        set_workspace_visibility,
        visible_workspace_ids,
    )
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    alice, _ = add_operator("alice")
    ws_id = _make_workspace(tmp_path / "ws", "ws", visibility="shared")
    assert ws_id in visible_workspace_ids(alice["id"])
    set_workspace_visibility("ws", "private")
    assert ws_id not in visible_workspace_ids(alice["id"])
    set_workspace_visibility("ws", "shared")
    assert ws_id in visible_workspace_ids(alice["id"])


# ---------- coordination tool filtering ----------


def _set_current_operator(monkeypatch, slug: str) -> None:
    """Pin ``BRAINS_OPERATOR`` for the rest of the test.

    The resolver checks the env var on every call, so this gives us
    process-level "I am acting as ``slug``" semantics without needing
    to push the ContextVar in every test.
    """
    monkeypatch.setenv("BRAINS_OPERATOR", slug)


def test_list_sessions_admin_sees_all_private_workspaces(isolated_brains: Path, tmp_path) -> None:
    from brains.control.operators import ensure_admin_operator
    from brains.control.sessions import list_sessions, start_session

    ensure_admin_operator()
    ws_shared = tmp_path / "ws-shared"
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_shared, "ws-shared", visibility="shared")
    _make_workspace(ws_private, "ws-private", visibility="private")
    start_session(str(ws_shared), tool="pytest")
    start_session(str(ws_private), tool="pytest")

    rows = list_sessions(limit=100)
    workspaces = {row["workspace"] for row in rows}
    assert {"ws-shared", "ws-private"} <= workspaces


def test_list_sessions_non_member_loses_private_rows(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.sessions import list_sessions, start_session

    ensure_admin_operator()
    add_operator("alice")
    _join_default_org("alice")
    ws_shared = tmp_path / "ws-shared"
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_shared, "ws-shared", visibility="shared")
    _make_workspace(ws_private, "ws-private", visibility="private")
    # Sessions live in both workspaces — written by admin.
    start_session(str(ws_shared), tool="pytest")
    start_session(str(ws_private), tool="pytest")

    # Now act as alice and re-query.
    _set_current_operator(monkeypatch, "alice")
    rows = list_sessions(limit=100)
    workspaces = {row["workspace"] for row in rows}
    assert "ws-shared" in workspaces
    assert "ws-private" not in workspaces


def test_list_sessions_membership_restores_private_access(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.memberships import add_membership
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.sessions import list_sessions, start_session

    ensure_admin_operator()
    add_operator("alice")
    _join_default_org("alice")
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_private, "ws-private", visibility="private")
    start_session(str(ws_private), tool="pytest")
    add_membership("ws-private", "alice")

    _set_current_operator(monkeypatch, "alice")
    rows = list_sessions(limit=100)
    assert any(row["workspace"] == "ws-private" for row in rows)


def test_list_tasks_filters_private_workspaces(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.tasks import create_task, list_tasks

    ensure_admin_operator()
    add_operator("alice")
    _join_default_org("alice")
    ws_shared = tmp_path / "ws-shared"
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_shared, "ws-shared", visibility="shared")
    _make_workspace(ws_private, "ws-private", visibility="private")
    create_task(str(ws_shared), title="shared-task")
    create_task(str(ws_private), title="private-task")

    _set_current_operator(monkeypatch, "alice")
    rows = list_tasks(limit=100)
    titles = {row["title"] for row in rows}
    assert "shared-task" in titles
    assert "private-task" not in titles


def test_list_handoffs_filters_private_workspaces(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.handoffs import list_handoffs, set_handoff
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    _join_default_org("alice")
    ws_shared = tmp_path / "ws-shared"
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_shared, "ws-shared", visibility="shared")
    _make_workspace(ws_private, "ws-private", visibility="private")
    set_handoff(str(ws_shared), title="shared-handoff", body="body")
    set_handoff(str(ws_private), title="private-handoff", body="body")

    _set_current_operator(monkeypatch, "alice")
    rows = list_handoffs()
    titles = {row["title"] for row in rows}
    assert "shared-handoff" in titles
    assert "private-handoff" not in titles


def test_list_open_decisions_filters_private_workspaces(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.decisions import file_decision_request, list_open_decisions
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    _join_default_org("alice")
    ws_shared = tmp_path / "ws-shared"
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_shared, "ws-shared", visibility="shared")
    _make_workspace(ws_private, "ws-private", visibility="private")
    file_decision_request(str(ws_shared), title="shared-ask", body="b")
    file_decision_request(str(ws_private), title="private-ask", body="b")

    _set_current_operator(monkeypatch, "alice")
    rows = list_open_decisions(limit=100)
    titles = {row["title"] for row in rows}
    assert "shared-ask" in titles
    assert "private-ask" not in titles


def test_pick_handoff_refuses_non_member(isolated_brains: Path, tmp_path, monkeypatch) -> None:
    from brains.control.handoffs import pick_handoff, set_handoff
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_private, "ws-private", visibility="private")
    set_handoff(str(ws_private), title="t", body="b")

    _set_current_operator(monkeypatch, "alice")
    with pytest.raises(PermissionError):
        pick_handoff(str(ws_private))


def test_latest_snapshot_hidden_from_non_member(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.snapshots import capture_snapshot, latest_snapshot

    ensure_admin_operator()
    add_operator("alice")
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_private, "ws-private", visibility="private")
    capture_snapshot(str(ws_private), kind="state", data={"k": 1})

    # Admin (the default resolved operator) sees the snapshot.
    assert latest_snapshot(str(ws_private), "state") is not None

    # Alice has no membership on the private workspace → no leak.
    _set_current_operator(monkeypatch, "alice")
    assert latest_snapshot(str(ws_private), "state") is None


def test_refresh_views_hidden_from_non_member_but_admin_can_write(
    isolated_brains: Path, tmp_path, monkeypatch
) -> None:
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.views import refresh_views

    ensure_admin_operator()
    add_operator("alice")
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_private, "ws-private", visibility="private")
    views_dir = ws_private / ".brains" / "views"

    _set_current_operator(monkeypatch, "alice")
    refresh_views(str(ws_private))
    assert not views_dir.exists() or not any(views_dir.iterdir())

    _set_current_operator(monkeypatch, "admin")
    refresh_views(str(ws_private))
    assert (views_dir / "STATE.md").exists()


# ---------- CLI ----------


def test_cli_workspace_invite_uninvite_round_trip(isolated_brains: Path, tmp_path) -> None:
    from brains.cli.app import app as cli_app

    runner = CliRunner()
    # Bootstrap admin + alice + a private workspace via direct calls.
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    _make_workspace(tmp_path / "ws", "ws", visibility="private")

    result = runner.invoke(cli_app, ["workspace", "invite", "ws", "alice"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output

    result = runner.invoke(cli_app, ["workspace", "members", "ws"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output

    result = runner.invoke(cli_app, ["workspace", "uninvite", "ws", "alice"])
    assert result.exit_code == 0, result.output

    # Second uninvite -> not-found -> exit 1.
    result = runner.invoke(cli_app, ["workspace", "uninvite", "ws", "alice"])
    assert result.exit_code == 1


def test_cli_workspace_visibility_flip(isolated_brains: Path, tmp_path) -> None:
    from brains.cli.app import app as cli_app
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    _make_workspace(tmp_path / "ws", "ws", visibility="shared")
    runner = CliRunner()

    result = runner.invoke(cli_app, ["workspace", "visibility", "ws", "private"])
    assert result.exit_code == 0, result.output
    assert "private" in result.output

    result = runner.invoke(cli_app, ["workspace", "show", "ws"])
    assert result.exit_code == 0, result.output
    assert "private" in result.output


def test_cli_workspace_invite_rejects_unknown_workspace(
    isolated_brains: Path,
) -> None:
    from brains.cli.app import app as cli_app
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    runner = CliRunner()
    result = runner.invoke(cli_app, ["workspace", "invite", "nope", "alice"])
    assert result.exit_code == 2
    assert "error" in result.output.lower()


def test_cli_workspace_visibility_rejects_bad_value(isolated_brains: Path, tmp_path) -> None:
    from brains.cli.app import app as cli_app
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    _make_workspace(tmp_path / "ws", "ws")
    runner = CliRunner()
    result = runner.invoke(cli_app, ["workspace", "visibility", "ws", "secret"])
    assert result.exit_code == 2

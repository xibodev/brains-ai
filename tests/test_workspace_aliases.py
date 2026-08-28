from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import text

from brains.control import sessions as sessions_ctl
from brains.storage.db import SessionLocal
from brains.storage.models import Event, Workspace, WorkspaceAlias


def test_git_worktree_aliases_converge_on_oldest_workspace(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical"
    worktree = tmp_path / "worktree"
    canonical.mkdir()
    worktree.mkdir()
    identity = "git:/repos/shared/.git"
    monkeypatch.setattr(sessions_ctl, "workspace_identity", lambda _path: identity)
    monkeypatch.setattr(
        sessions_ctl,
        "_git_worktree_paths",
        lambda _path: (str(canonical.resolve()), str(worktree.resolve())),
    )

    original = sessions_ctl.register_workspace(str(canonical), slug="alias-canonical")
    with SessionLocal() as session:
        duplicate = Workspace(
            slug="alias-duplicate",
            path=str(worktree.resolve()),
            name="Alias duplicate",
            status="active",
            org_id=original.org_id,
        )
        session.add(duplicate)
        session.flush()
        session.add(
            WorkspaceAlias(
                workspace_id=duplicate.id,
                path=duplicate.path,
                identity_key=f"path:{duplicate.path}",
            )
        )
        session.add(
            WorkspaceAlias(
                workspace_id=duplicate.id,
                path=str(worktree.resolve() / "nested-alias"),
                identity_key=f"path:{worktree.resolve()}",
            )
        )
        session.execute(
            text(
                "INSERT INTO events (workspace_id, kind, message, created_at) "
                "VALUES (:workspace_id, 'workspace_alias_probe', 'kept', CURRENT_TIMESTAMP)"
            ),
            {"workspace_id": duplicate.id},
        )
        session.commit()
        duplicate_id = duplicate.id

    assert sessions_ctl.register_workspace(str(worktree)).id == original.id
    assert sessions_ctl.get_workspace(path=str(worktree)).id == original.id
    with SessionLocal() as session:
        assert session.get(Workspace, duplicate_id).status == "archived"
        assert (
            session.execute(
                text("SELECT COUNT(*) FROM events WHERE workspace_id = :workspace_id"),
                {"workspace_id": duplicate_id},
            ).scalar_one()
            == 1
        )
        alias = (
            session.query(WorkspaceAlias)
            .filter(WorkspaceAlias.path == str(worktree.resolve()))
            .one()
        )
        assert alias.workspace_id == original.id
        assert (
            session.query(WorkspaceAlias)
            .filter(WorkspaceAlias.path == str(worktree.resolve() / "nested-alias"))
            .one()
            .workspace_id
            == original.id
        )
        event = (
            session.query(Event)
            .filter(Event.kind == "workspace_alias_converged", Event.workspace_id == original.id)
            .one()
        )
        assert str(duplicate_id) in (event.metadata_json or "")


def test_alias_registration_refuses_cross_org_convergence(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(sessions_ctl, "workspace_identity", lambda _path: "git:/shared")
    monkeypatch.setattr(
        sessions_ctl,
        "_git_worktree_paths",
        lambda _path: (str(first.resolve()), str(second.resolve())),
    )
    workspace = sessions_ctl.register_workspace(str(first), slug="alias-first")

    with SessionLocal() as session:
        other_org = session.execute(
            text(
                "INSERT INTO orgs (slug, name, status, created_at, updated_at) "
                "VALUES ('alias-other-org', 'Other', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                "RETURNING id"
            )
        ).scalar_one()
        duplicate = Workspace(
            slug="alias-other",
            path=str(second.resolve()),
            status="active",
            org_id=other_org,
        )
        session.add(duplicate)
        session.flush()
        session.add(
            WorkspaceAlias(
                workspace_id=duplicate.id,
                path=duplicate.path,
                identity_key="git:/shared",
            )
        )
        session.commit()

    try:
        sessions_ctl.register_workspace(str(first), org_id=workspace.org_id)
    except ValueError as exc:
        assert "multiple organizations" in str(exc)
    else:
        raise AssertionError("cross-Org aliases must not converge")


def test_migration_backfills_existing_workspace_paths(tmp_path):
    import sqlite3

    migration_path = (
        Path(__file__).parents[1]
        / "src"
        / "brains"
        / "storage"
        / "sql_migrations"
        / "148_workspace_aliases.py"
    )
    spec = importlib.util.spec_from_file_location("workspace_aliases_148", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    database = tmp_path / "aliases.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, path VARCHAR(1024) NOT NULL)"
        )
        connection.execute("INSERT INTO workspaces (id, path) VALUES (7, '/repo/example')")
        migration.upgrade(connection)
        assert connection.execute(
            "SELECT workspace_id, path, identity_key FROM workspace_aliases"
        ).fetchall() == [(7, "/repo/example", "path:/repo/example")]
    finally:
        connection.close()


def test_linked_worktree_metadata_has_one_identity_and_lists_both_roots(tmp_path):
    common = tmp_path / "repo" / ".git"
    canonical = common.parent
    linked = tmp_path / "linked"
    worktree_meta = common / "worktrees" / "linked"
    worktree_meta.mkdir(parents=True)
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {worktree_meta}\n", encoding="utf-8")
    (worktree_meta / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree_meta / "gitdir").write_text(str(linked / ".git") + "\n", encoding="utf-8")

    assert sessions_ctl.workspace_identity(str(canonical)) == sessions_ctl.workspace_identity(
        str(linked)
    )
    assert set(sessions_ctl._git_worktree_paths(str(linked))) == {
        str(canonical.resolve()),
        str(linked.resolve()),
    }

"""Tests for the ``brains init`` and ``brains workspace-import`` CLI commands.

These are introduced in Phase 3 of the consolidation plan: they replace
hive-mind's first-time setup ("create the SQLite file, register the cwd
as a workspace") and the bulk-import seed flow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

import brains.control.events as events_module
import brains.control.sessions as sessions_module
import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.cli.app import app
from brains.storage.models import Workspace


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "init.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (db_module, migrations_module, sessions_module, events_module):
        if hasattr(module, "engine"):
            monkeypatch.setattr(module, "engine", engine)
        if hasattr(module, "SessionLocal"):
            monkeypatch.setattr(module, "SessionLocal", SessionLocal)
    yield db_path


def _invoke(args: list[str]):
    runner = CliRunner()
    return runner.invoke(app, args)


def _fresh_home_env(home) -> dict[str, str]:  # noqa: ANN001
    env = {key: value for key, value in os.environ.items() if not key.startswith("BRAINS_")}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return env


def test_version_does_not_create_state_directory_on_fresh_home(tmp_path) -> None:
    home = tmp_path / "fresh-version-home"
    home.mkdir()
    state = home / ".brains"

    result = subprocess.run(
        [sys.executable, "-m", "brains", "--version"],
        cwd=tmp_path,
        env=_fresh_home_env(home),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not state.exists()


def test_setup_creates_default_state_directory_on_fresh_home(tmp_path) -> None:
    home = tmp_path / "fresh-home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    state = home / ".brains"
    assert not state.exists()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "brains",
            "setup",
            "--path",
            str(workspace),
            "--no-wire",
            "--json",
        ],
        cwd=tmp_path,
        env=_fresh_home_env(home),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (state / "brains.db").is_file()
    assert (state / "admin-key").is_file()
    assert not (tmp_path / "brains.db").exists()


def test_init_creates_db_and_registers_workspace(isolated_db, tmp_path) -> None:
    workspace = tmp_path / "wsA"
    workspace.mkdir()
    result = _invoke(["init", str(workspace)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["workspace"]["status"] == "active"
    assert payload["workspace"]["path"].endswith("wsA")
    # 020_rag_chunks_meta should be recorded as part of init_db().
    assert "020_rag_chunks_meta" in payload["schema_versions"]
    # DB file exists after init.
    assert isolated_db.exists()


def test_init_is_idempotent(isolated_db, tmp_path) -> None:
    workspace = tmp_path / "wsB"
    workspace.mkdir()
    first = _invoke(["init", str(workspace)])
    second = _invoke(["init", str(workspace)])
    assert first.exit_code == 0 and second.exit_code == 0
    p1 = json.loads(first.output)
    p2 = json.loads(second.output)
    assert p1["workspace"]["id"] == p2["workspace"]["id"]
    assert p1["workspace"]["slug"] == p2["workspace"]["slug"]


def test_workspace_import_bulk_registers(isolated_db, tmp_path) -> None:
    ws1 = tmp_path / "alpha"
    ws2 = tmp_path / "beta"
    ws1.mkdir()
    ws2.mkdir()
    seeds = tmp_path / "seeds.json"
    seeds.write_text(
        json.dumps(
            [
                {"path": str(ws1), "name": "Alpha"},
                {"path": str(ws2), "slug": "beta-fixed"},
            ]
        ),
        encoding="utf-8",
    )
    result = _invoke(["workspace-import", str(seeds)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["imported"] == 2
    slugs = {w["slug"] for w in payload["workspaces"]}
    assert "beta-fixed" in slugs
    with db_module.SessionLocal() as session:
        rows = session.query(Workspace).all()
        assert {os.path.normcase(r.path) for r in rows} == {
            os.path.normcase(str(ws1.resolve())),
            os.path.normcase(str(ws2.resolve())),
        }


def test_workspace_import_rejects_non_array(isolated_db, tmp_path) -> None:
    seeds = tmp_path / "seeds.json"
    seeds.write_text('{"path": "/tmp"}', encoding="utf-8")
    result = _invoke(["workspace-import", str(seeds)])
    assert result.exit_code != 0
    assert "JSON array" in result.output or "JSON array" in str(result.exception)


def test_workspace_import_rejects_missing_path(isolated_db, tmp_path) -> None:
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps([{"name": "no-path"}]), encoding="utf-8")
    result = _invoke(["workspace-import", str(seeds)])
    assert result.exit_code != 0

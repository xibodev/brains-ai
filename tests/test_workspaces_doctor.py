"""Tests for ``brains.control.sessions.has_project_marker`` and the
``brains workspaces doctor`` CLI.

The conftest already isolates the test DB to a tmp path.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from brains.cli.app import app
from brains.control.events import list_events
from brains.control.sessions import (
    WORKSPACE_PROJECT_MARKERS,
    has_project_marker,
    register_workspace,
)
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Workspace


@pytest.fixture(autouse=True)
def _clean_doctor_rows():
    init_db()
    with SessionLocal() as session:
        # Wipe events that reference doctor-* workspaces (live ones plus
        # orphans whose workspace_id has since been reused). We can't
        # join by slug for orphans because the workspace row is gone, so
        # we also clear events whose message mentions a doctor-* slug —
        # belt + braces.
        session.execute(
            text(
                "DELETE FROM events WHERE workspace_id IN ("
                "SELECT id FROM workspaces WHERE slug LIKE 'doctor-%'"
                ") OR message LIKE '%doctor-%'"
            )
        )
        session.execute(text("DELETE FROM workspaces WHERE slug LIKE 'doctor-%'"))
        session.commit()
    yield
    with SessionLocal() as session:
        session.execute(
            text(
                "DELETE FROM events WHERE workspace_id IN ("
                "SELECT id FROM workspaces WHERE slug LIKE 'doctor-%'"
                ") OR message LIKE '%doctor-%'"
            )
        )
        session.execute(text("DELETE FROM workspaces WHERE slug LIKE 'doctor-%'"))
        session.commit()


def test_has_project_marker_returns_false_for_missing_path(tmp_path):
    missing = tmp_path / "no-such-dir"
    assert has_project_marker(str(missing)) is False


def test_has_project_marker_returns_false_for_empty_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert has_project_marker(str(empty)) is False


@pytest.mark.parametrize(
    "marker",
    [".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod"],
)
def test_has_project_marker_recognises_canonical_markers(tmp_path, marker):
    root = tmp_path / f"project-{marker.replace('.', '_')}"
    root.mkdir()
    if marker.startswith("."):
        # .git, .hg, .svn are dirs in real life — create as dir
        (root / marker).mkdir()
    else:
        (root / marker).write_text("")
    assert has_project_marker(str(root)) is True


def test_has_project_marker_recognises_dotnet_suffixes(tmp_path):
    root = tmp_path / "dotnet"
    root.mkdir()
    (root / "MyApp.csproj").write_text("")
    assert has_project_marker(str(root)) is True


def test_has_project_marker_constant_is_non_empty():
    # Guard against accidental deletion of the marker list.
    assert len(WORKSPACE_PROJECT_MARKERS) >= 10
    assert ".git" in WORKSPACE_PROJECT_MARKERS
    assert "pyproject.toml" in WORKSPACE_PROJECT_MARKERS


def test_register_workspace_emits_no_marker_warning(tmp_path):
    umbrella = tmp_path / "umbrella"
    umbrella.mkdir()
    # Intentionally no marker file — simulates an agent registering a
    # parent directory of several repos.
    ws = register_workspace(str(umbrella), slug="doctor-umbrella")
    assert ws.slug == "doctor-umbrella"

    events = list_events(workspace_id=ws.id, limit=20)
    kinds = [e.kind for e in events]
    assert "workspace_registered_no_marker" in kinds
    assert "workspace_registered" in kinds


def test_register_workspace_does_not_warn_for_real_project(tmp_path):
    real = tmp_path / "real-project"
    real.mkdir()
    (real / "pyproject.toml").write_text("[project]\nname='x'\n")
    ws = register_workspace(str(real), slug="doctor-real")

    events = list_events(workspace_id=ws.id, limit=20)
    kinds = [e.kind for e in events]
    assert "workspace_registered" in kinds
    assert "workspace_registered_no_marker" not in kinds


def _seed(slug: str, path: str) -> int:
    with SessionLocal() as session:
        ws = Workspace(slug=slug, path=path, status="active")
        session.add(ws)
        session.commit()
        return ws.id


def test_doctor_classifies_rows(tmp_path):
    # Seed three rows: one ok (with marker), one no_marker (path exists,
    # no marker), one missing (path absent).
    ok_path = tmp_path / "doctor-ok"
    ok_path.mkdir()
    (ok_path / "pyproject.toml").write_text("")
    _seed("doctor-ok", str(ok_path))

    no_marker_path = tmp_path / "doctor-bare"
    no_marker_path.mkdir()
    _seed("doctor-no-marker", str(no_marker_path))

    missing_path = tmp_path / "doctor-gone"
    # do NOT mkdir — simulates a stale row
    _seed("doctor-missing", str(missing_path))

    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "doctor"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    slugs_missing = {row["slug"] for row in payload["missing"]}
    slugs_no_marker = {row["slug"] for row in payload["no_marker"]}

    assert "doctor-missing" in slugs_missing
    assert "doctor-no-marker" in slugs_no_marker
    assert "doctor-ok" not in slugs_missing
    assert "doctor-ok" not in slugs_no_marker


def test_doctor_prune_missing_dry_run(tmp_path):
    missing_path = tmp_path / "doctor-dry-gone"
    _seed("doctor-dry-missing", str(missing_path))

    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "doctor", "--prune-missing"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["pruned_missing"]["dry_run"] is True
    assert payload["pruned_missing"]["would_delete"] >= 1

    # Row should still be present (dry-run = no deletion)
    with SessionLocal() as session:
        rows = session.query(Workspace.slug).filter(Workspace.slug == "doctor-dry-missing").all()
        assert len(rows) == 1


def test_doctor_prune_missing_apply_deletes_row(tmp_path):
    missing_path = tmp_path / "doctor-apply-gone"
    _seed("doctor-apply-missing", str(missing_path))

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["workspaces", "doctor", "--prune-missing", "--apply"],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["pruned_missing"]["dry_run"] is False
    assert payload["pruned_missing"]["deleted"] >= 1

    # Row is gone
    with SessionLocal() as session:
        rows = session.query(Workspace.slug).filter(Workspace.slug == "doctor-apply-missing").all()
        assert rows == []


def test_doctor_does_not_prune_no_marker_rows(tmp_path):
    # The doctor command must NEVER auto-prune no_marker rows — they may
    # be legitimate transient registrations. Only missing-on-disk rows
    # are prune-eligible.
    no_marker_path = tmp_path / "doctor-bare-survives"
    no_marker_path.mkdir()
    _seed("doctor-no-marker-survives", str(no_marker_path))

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["workspaces", "doctor", "--prune-missing", "--apply"],
    )
    assert result.exit_code == 0, result.output

    with SessionLocal() as session:
        rows = (
            session.query(Workspace.slug)
            .filter(Workspace.slug == "doctor-no-marker-survives")
            .all()
        )
        assert len(rows) == 1, "no_marker row must not be pruned by --prune-missing"

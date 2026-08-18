"""Tests for the ``brains workspaces prune`` CLI command.

The conftest already isolates the test DB to a tmp path, so we can write
to it freely. We seed a mix of "real" and "pytest fixture" workspaces,
run prune in dry-run mode, confirm nothing changed, then run with
``--apply`` and confirm both the workspace and its dependent rows are
gone.
"""

from __future__ import annotations

import json

from sqlalchemy import text
from typer.testing import CliRunner

from brains.cli.app import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Workspace


def _seed(slug: str, path: str) -> int:
    with SessionLocal() as session:
        ws = Workspace(slug=slug, path=path, status="active")
        session.add(ws)
        session.commit()
        return ws.id


def _ws_slugs() -> list[str]:
    with SessionLocal() as session:
        return [w.slug for w in session.query(Workspace).order_by(Workspace.slug).all()]


def test_prune_requires_at_least_one_pattern():
    init_db()
    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "prune"])
    assert result.exit_code == 2
    assert "at least one --slug-prefix" in (result.stderr or result.output)


def test_prune_dry_run_does_not_delete():
    init_db()
    # Wipe any prior state from earlier tests in this module
    with SessionLocal() as session:
        session.execute(text("DELETE FROM workspaces WHERE slug LIKE 'fixture-%'"))
        session.commit()

    _seed("fixture-keep", "/repos/real")
    _seed("fixture-test-a", "/tmp/pytest-of-x/case1")
    _seed("fixture-test-b", "/tmp/pytest-of-x/case2")

    runner = CliRunner()
    result = runner.invoke(app, ["workspaces", "prune", "--slug-prefix", "fixture-test-"])
    assert result.exit_code == 0, result.output
    # The JSON report ends the stdout — parse the last JSON object
    payload = json.loads(
        result.stdout.strip().splitlines()[-1]
        if "{" not in result.stdout.splitlines()[0]
        else result.stdout
    )
    assert payload["dry_run"] is True
    assert payload["matched"] == 2
    assert "deleted" not in payload  # only set on apply
    # DB state is unchanged
    slugs = _ws_slugs()
    assert "fixture-keep" in slugs
    assert "fixture-test-a" in slugs
    assert "fixture-test-b" in slugs


def test_prune_apply_deletes_matched_rows():
    init_db()
    with SessionLocal() as session:
        session.execute(text("DELETE FROM workspaces WHERE slug LIKE 'fixture2-%'"))
        session.commit()

    _seed("fixture2-keep", "/repos/keep")
    _seed("fixture2-test-x", "/tmp/pytest-of-x/y")
    _seed("fixture2-test-y", "/tmp/pytest-of-x/z")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["workspaces", "prune", "--slug-prefix", "fixture2-test-", "--apply"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["matched"] == 2
    assert payload["deleted"] == 2
    assert payload["deleted_by_table"]["workspaces"] == 2

    remaining = _ws_slugs()
    assert "fixture2-keep" in remaining
    assert "fixture2-test-x" not in remaining
    assert "fixture2-test-y" not in remaining


def test_prune_matches_on_path_substring():
    init_db()
    with SessionLocal() as session:
        session.execute(text("DELETE FROM workspaces WHERE slug LIKE 'fixture3-%'"))
        session.commit()

    _seed("fixture3-a", "/repos/keep-fixture3")
    _seed("fixture3-b", "/users/example/AppData/Local/Temp/pytest-of-x/foo")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["workspaces", "prune", "--path-contains", "pytest", "--apply"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # We can't assert exact `matched` because earlier tests in the same
    # session may also have seeded pytest-pathed rows; assert behaviour
    # instead — fixture3-b is gone and fixture3-a survives.
    assert payload["matched"] >= 1
    assert payload["deleted"] >= 1

    remaining = _ws_slugs()
    assert "fixture3-a" in remaining
    assert "fixture3-b" not in remaining

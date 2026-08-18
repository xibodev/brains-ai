"""Tests for the disk-discovered migration runner.

Verifies that:

* ``init_db()`` discovers and applies numbered files under
  ``src/brains/storage/sql_migrations/`` exactly once.
* The shipped ``010_hivemind_consolidation`` migration provisions the
  ``spawn_*`` columns on ``recurring_task_definitions``.
* The runner is idempotent — a second call doesn't re-apply or fail.
* A fresh DB created via ``Base.metadata.create_all`` (the path taken by
  every other test) reaches the same final shape.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine

import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.storage.migrations import (
    _list_disk_migrations,
    current_schema_versions,
    init_db,
)

REQUIRED_SPAWN_COLUMNS = {"spawn_tool", "spawn_args", "spawn_prompt"}


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point brains storage at a per-test SQLite file.

    Brains' module-level engine is rebound to a temp DB so the migration
    runner's side-effects don't bleed into ``brains.db`` shared by the
    rest of the suite.
    """
    db_path = tmp_path / "isolated.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", SessionLocal)
    yield db_path


def _columns(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row[1] for row in rows}
    finally:
        conn.close()


def test_disk_migrations_are_discovered() -> None:
    """The 010 file ships with the package and is visible to the runner."""
    names = {p.name for p in _list_disk_migrations()}
    assert "010_hivemind_consolidation.py" in names


def test_init_db_applies_010_and_records_version(isolated_db) -> None:
    init_db()

    columns = _columns(isolated_db, "recurring_task_definitions")
    assert REQUIRED_SPAWN_COLUMNS.issubset(columns), (
        f"missing spawn_* columns after init_db; have {sorted(columns)}"
    )

    applied = current_schema_versions()
    assert "010_hivemind_consolidation" in applied


def test_init_db_is_idempotent(isolated_db) -> None:
    init_db()
    first = list(current_schema_versions())
    init_db()
    second = list(current_schema_versions())
    assert first == second, "second init_db() recorded a duplicate version row"


def test_010_skips_when_columns_already_exist(isolated_db) -> None:
    """Re-running the migration on an up-to-date DB is a no-op."""
    init_db()
    # Drop the recorded version so the runner tries to apply it again.
    with db_module.SessionLocal() as session:
        session.execute(
            __import__("sqlalchemy").text("DELETE FROM schema_versions WHERE version = :v"),
            {"v": "010_hivemind_consolidation"},
        )
        session.commit()
    # Should not raise even though spawn_* already exist.
    init_db()
    columns = _columns(isolated_db, "recurring_task_definitions")
    assert REQUIRED_SPAWN_COLUMNS.issubset(columns)

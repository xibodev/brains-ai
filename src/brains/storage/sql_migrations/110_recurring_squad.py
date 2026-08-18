"""Recurring-task squad routing — additive column.

Adds one nullable column to ``recurring_task_definitions``:

* ``squad`` — when set, the slug of a squad in the same workspace. A fired
  recurring task is then tagged ``squad:<slug>`` so it routes to that squad
  (the squad's leader delegates it to a member), wiring scheduled/triggered
  work into the leader-routed team-assignment model.

Idempotent: the ``ALTER TABLE`` is conditional on ``PRAGMA table_info`` so the
migration is safe to re-run and safe on databases where
``Base.metadata.create_all`` has already provisioned the column from the model.
"""

from __future__ import annotations

import sqlite3


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def upgrade(conn: sqlite3.Connection) -> None:
    table = "recurring_task_definitions"
    existing = _existing_columns(conn, table)
    if not existing:
        # Table not yet created; create_all will provision the column.
        return
    if "squad" not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN squad TEXT")

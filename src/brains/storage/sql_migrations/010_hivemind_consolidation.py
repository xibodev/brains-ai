"""Hivemind consolidation — additive columns for recurring task auto-spawn.

Adds three nullable columns to ``recurring_task_definitions``:

* ``spawn_tool``    — registered tool name (e.g. ``claude``, ``codex``).
* ``spawn_args``    — JSON-encoded list of CLI arguments to pass to the tool.
* ``spawn_prompt``  — first-message prompt seed for the spawned agent.

These columns enable the upcoming "fire recurring → launch headless agent"
flow (Phase 2 PR-2 of the consolidation plan). The actual spawning logic
remains gated behind ``BRAINS_ALLOW_RECURRING_SPAWN=1`` once that PR lands;
this migration only ensures the schema is in place.

Idempotent: each ``ALTER TABLE`` is conditional on
``PRAGMA table_info`` so the migration is safe to re-run and is also safe
on databases where ``Base.metadata.create_all`` has already provisioned
the columns from the updated SQLAlchemy model.
"""

from __future__ import annotations

import sqlite3

_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("spawn_tool", "TEXT"),
    ("spawn_args", "TEXT"),
    ("spawn_prompt", "TEXT"),
)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def upgrade(conn: sqlite3.Connection) -> None:
    table = "recurring_task_definitions"
    existing = _existing_columns(conn, table)
    if not existing:
        # Table doesn't exist yet — Base.metadata.create_all hasn't run on
        # this DB. Nothing to do; create_all will provision the columns
        # next time init_db() is called.
        return
    for column, sql_type in _NEW_COLUMNS:
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

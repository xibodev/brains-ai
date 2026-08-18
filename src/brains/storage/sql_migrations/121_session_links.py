"""WS2 native-battalion — Session ↔ Issue / Persona / Runtime links.

Adds three nullable FK columns to ``agent_sessions`` so a live execution is
traceable to the issue it runs, the persona that ran it, and the runtime it ran
on. ``Base.metadata.create_all`` only provisions these on fresh DBs; this disk
migration patches already-existing SQLite installs.

Idempotent: each ``ALTER TABLE`` is guarded by ``PRAGMA table_info`` and each
index uses ``IF NOT EXISTS``, so the migration is safe to re-run and safe on
databases where ``create_all`` already provisioned the columns.
"""

from __future__ import annotations

import sqlite3


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def upgrade(conn: sqlite3.Connection) -> None:
    table = "agent_sessions"
    existing = _existing_columns(conn, table)
    if not existing:
        # Table not yet created; create_all will provision the columns.
        return

    for column in ("issue_id", "persona_id", "runtime_id"):
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_agent_sessions_{column} ON {table} ({column})")

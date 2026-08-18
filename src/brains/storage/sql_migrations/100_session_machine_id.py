"""Phase 4 machine-aware sessions.

Add ``agent_sessions.machine_id`` so the zombie reaper can distinguish
same-machine PIDs from foreign-machine heartbeats. Fresh databases get the
column from SQLAlchemy ``create_all``; this migration patches existing SQLite
databases idempotently.
"""

from __future__ import annotations

import sqlite3


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def upgrade(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "agent_sessions", "machine_id"):
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN machine_id VARCHAR(64)")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_sessions_machine_id ON agent_sessions(machine_id)"
    )

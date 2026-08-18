"""F3.2 native-battalion — explicit session state lifecycle.

Adds ``agent_sessions.state`` so a live execution carries a real lifecycle
(``spawning`` -> ``running`` -> ``blocked`` | ``completed`` | ``failed``) instead
of only the derived running/ended split. ``Base.metadata.create_all`` provisions
it on fresh DBs; this disk migration patches existing SQLite installs.

Idempotent: the ``ALTER TABLE`` is guarded by ``PRAGMA table_info`` and the index
uses ``IF NOT EXISTS``. Existing rows default to ``running`` (the prior implied
state for a non-ended session); the app stamps terminal states going forward.
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
        # Table not yet created; create_all will provision the column.
        return
    if "state" not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN state TEXT DEFAULT 'running'")
    conn.execute(f"CREATE INDEX IF NOT EXISTS ix_agent_sessions_state ON {table} (state)")

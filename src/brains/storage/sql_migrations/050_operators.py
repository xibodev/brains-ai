"""Layer 1 of the multi-operator model: operators table + session attribution.

Two additive changes:

1. ``operators`` table — named principals (slug + display_name +
   key_fingerprint). On fresh SQLAlchemy installs ``create_all`` already
   provisions it; this migration only matters for existing databases.
2. ``agent_sessions.created_by_operator_id`` column — nullable FK so
   pre-existing rows remain valid. New sessions written by
   ``brains.control.sessions.start_session`` are stamped with the
   resolved operator (falling back to the auto-provisioned ``admin``
   row when the caller's key matches the admin fingerprint).

No data migration is required: existing rows keep
``created_by_operator_id IS NULL``, and any code that consumes the
column treats ``NULL`` as "admin" for back-compat. The intentional
back-fill happens lazily — the resolver in ``ensure_admin_operator``
runs on first dashboard / gateway startup and records the admin row.

See ``docs/ARCHITECTURE.md`` for the current identity and trust boundary.
"""

from __future__ import annotations

import sqlite3


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def upgrade(conn: sqlite3.Connection) -> None:
    # 1. operators table.
    if not _table_exists(conn, "operators"):
        conn.execute(
            """
            CREATE TABLE operators (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                slug              VARCHAR(64) NOT NULL,
                display_name      VARCHAR(128),
                key_fingerprint   VARCHAR(64),
                created_at        DATETIME NOT NULL
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_operators_slug ON operators(slug)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_operators_key_fingerprint ON operators(key_fingerprint)"
        )

    # 2. agent_sessions.created_by_operator_id.
    if not _column_exists(conn, "agent_sessions", "created_by_operator_id"):
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN created_by_operator_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_agent_sessions_created_by_operator_id "
            "ON agent_sessions(created_by_operator_id)"
        )

"""Explicit session-handle successor links (SQLite)."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_successors (
            predecessor_session_id VARCHAR(32) NOT NULL,
            successor_session_id VARCHAR(32) NOT NULL,
            linked_at DATETIME NOT NULL,
            PRIMARY KEY (predecessor_session_id),
            FOREIGN KEY(predecessor_session_id) REFERENCES agent_sessions(id),
            FOREIGN KEY(successor_session_id) REFERENCES agent_sessions(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_successors_successor_session_id "
        "ON session_successors (successor_session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_successors_linked_at "
        "ON session_successors (linked_at)"
    )

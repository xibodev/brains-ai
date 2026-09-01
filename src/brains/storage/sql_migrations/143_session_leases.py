"""Renewable liveness leases for PID-less Sessions (SQLite)."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_leases (
            session_id VARCHAR(32) NOT NULL,
            lease_expires_at DATETIME NOT NULL,
            renewed_at DATETIME NOT NULL,
            PRIMARY KEY (session_id),
            FOREIGN KEY(session_id) REFERENCES agent_sessions(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_leases_lease_expires_at "
        "ON session_leases (lease_expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_leases_renewed_at ON session_leases (renewed_at)"
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO session_leases (session_id, lease_expires_at, renewed_at)
        SELECT id,
               datetime(COALESCE(last_activity_at, started_at), '+1 hour'),
               COALESCE(last_activity_at, started_at)
        FROM agent_sessions
        WHERE ended_at IS NULL
          AND pid IS NULL
          AND runtime_id IS NULL
          AND issue_id IS NULL
          AND persona_id IS NULL
        """
    )

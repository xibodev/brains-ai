"""Human-owned approval assignment and escalation metadata (SQLite)."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_routing (
            approval_request_id INTEGER NOT NULL,
            assigned_operator_id INTEGER,
            priority VARCHAR(16) NOT NULL DEFAULT 'p2',
            due_at DATETIME,
            escalation_level INTEGER NOT NULL DEFAULT 0,
            escalation_reason TEXT,
            updated_by_operator_id INTEGER,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (approval_request_id),
            FOREIGN KEY(approval_request_id) REFERENCES approval_requests(id),
            FOREIGN KEY(assigned_operator_id) REFERENCES operators(id),
            FOREIGN KEY(updated_by_operator_id) REFERENCES operators(id)
        )
        """
    )
    for column in ("assigned_operator_id", "priority", "due_at", "updated_at"):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS ix_approval_routing_{column} "
            f"ON approval_routing ({column})"
        )

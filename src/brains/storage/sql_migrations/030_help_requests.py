"""Cross-session peer help RPC table.

Adds ``help_requests`` to back the long-poll ``ask_peer`` /
``wait_for_request`` / ``answer_request`` MCP tools. ``create_all``
already provisions this on fresh databases; existing databases need this
migration to gain the table.

Idempotent: ``CREATE TABLE IF NOT EXISTS`` plus the matching indexes.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS help_requests (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            code                   VARCHAR(32) NOT NULL UNIQUE,
            from_session_id        VARCHAR(32),
            from_workspace_id      INTEGER,
            to_workspace           VARCHAR(128),
            to_session_id          VARCHAR(32),
            subject                VARCHAR(256) NOT NULL,
            question               TEXT NOT NULL,
            context                TEXT,
            status                 VARCHAR(32) NOT NULL DEFAULT 'open',
            claimed_by_session_id  VARCHAR(32),
            claimed_at             DATETIME,
            answer                 TEXT,
            evidence               TEXT,
            answered_at            DATETIME,
            ask_depth              INTEGER NOT NULL DEFAULT 1,
            created_at             DATETIME NOT NULL,
            expires_at             DATETIME NOT NULL
        )
        """
    )
    # Indexes for the matcher hot path (status + routing).
    conn.execute("CREATE INDEX IF NOT EXISTS ix_help_requests_status ON help_requests(status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_help_requests_to_workspace ON help_requests(to_workspace)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_help_requests_to_session_id ON help_requests(to_session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_help_requests_from_session_id ON help_requests(from_session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_help_requests_expires_at ON help_requests(expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_help_requests_created_at ON help_requests(created_at)"
    )

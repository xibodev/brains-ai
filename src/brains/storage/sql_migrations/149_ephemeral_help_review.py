"""Fenced on-demand ephemeral help review execution (SQLite)."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS help_request_executions (
            request_code VARCHAR(32) NOT NULL,
            mode VARCHAR(16) NOT NULL,
            source_workspace_id INTEGER NOT NULL,
            required_tool VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'queued',
            runtime_id INTEGER,
            review_session_id VARCHAR(32),
            attempt INTEGER NOT NULL DEFAULT 0,
            launch_after DATETIME NOT NULL,
            lease_expires_at DATETIME,
            started_at DATETIME,
            completed_at DATETIME,
            error_code VARCHAR(64),
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (request_code),
            FOREIGN KEY(request_code) REFERENCES help_requests(code),
            FOREIGN KEY(source_workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(runtime_id) REFERENCES runtimes(id),
            FOREIGN KEY(review_session_id) REFERENCES agent_sessions(id)
        )
        """
    )
    for column in (
        "mode",
        "source_workspace_id",
        "required_tool",
        "status",
        "runtime_id",
        "review_session_id",
        "launch_after",
        "lease_expires_at",
    ):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS ix_help_request_executions_{column} "
            f"ON help_request_executions ({column})"
        )

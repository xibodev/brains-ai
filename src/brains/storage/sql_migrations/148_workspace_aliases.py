"""Durable Workspace path aliases (SQLite)."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_aliases (
            id INTEGER NOT NULL,
            workspace_id INTEGER NOT NULL,
            path VARCHAR(1024) NOT NULL,
            identity_key VARCHAR(1100) NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (path),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_aliases_workspace_id "
        "ON workspace_aliases (workspace_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_workspace_aliases_path ON workspace_aliases (path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_aliases_identity_key "
        "ON workspace_aliases (identity_key)"
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO workspace_aliases (workspace_id, path, identity_key, created_at)
        SELECT id, path, 'path:' || path, CURRENT_TIMESTAMP FROM workspaces
        """
    )

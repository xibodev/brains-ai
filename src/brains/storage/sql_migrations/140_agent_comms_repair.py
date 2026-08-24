"""Converge the one pre-release agent-comms schema draft (SQLite).

Before migration 139 was committed, one local development probe applied a
draft that stored ``required_tool`` directly on the frozen ``help_requests``
table. The released 139 correctly uses a separate additive
``help_request_constraints`` table. This migration is idempotent for both
shapes:

* released 139 -> no data move; ensure the final table/indexes exist;
* leaked draft 139 -> create/backfill the constraint table, remove the draft
  column, and add the index omitted by that draft.

The runner accepts only the exact leaked checksum; arbitrary edited migrations
remain fatal.
"""

from __future__ import annotations

import sqlite3


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS help_request_constraints (
            request_code VARCHAR(32) NOT NULL,
            required_tool VARCHAR(64) NOT NULL,
            PRIMARY KEY (request_code),
            FOREIGN KEY(request_code) REFERENCES help_requests (code)
        )
        """
    )

    if _column_exists(conn, "help_requests", "required_tool"):
        conn.execute(
            """
            INSERT OR IGNORE INTO help_request_constraints (request_code, required_tool)
            SELECT code, required_tool
            FROM help_requests
            WHERE required_tool IS NOT NULL AND TRIM(required_tool) <> ''
            """
        )
        conn.execute("ALTER TABLE help_requests DROP COLUMN required_tool")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_topic_posts_from_workspace_id "
        "ON topic_posts (from_workspace_id)"
    )

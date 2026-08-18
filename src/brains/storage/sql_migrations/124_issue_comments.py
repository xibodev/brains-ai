"""F3.3 native-battalion — issue comments.

Adds the ``issue_comments`` table so a human, a persona, or a running session can
post a comment on an issue (an agent self-reporting why it blocked, etc.).
``Base.metadata.create_all`` provisions it on fresh DBs; this disk migration
creates it on existing SQLite installs. Idempotent (``CREATE TABLE IF NOT
EXISTS`` + ``CREATE INDEX IF NOT EXISTS``).
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_comments (
            id INTEGER PRIMARY KEY,
            issue_id INTEGER NOT NULL REFERENCES issues(id),
            body TEXT NOT NULL,
            author_kind TEXT DEFAULT 'operator',
            author_operator_id INTEGER REFERENCES operators(id),
            author_persona_id INTEGER REFERENCES personas(id),
            session_id TEXT REFERENCES agent_sessions(id),
            created_at TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_issue_comments_issue_created "
        "ON issue_comments (issue_id, created_at)"
    )

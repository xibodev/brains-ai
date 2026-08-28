"""Typed event taxonomy and scope provenance (SQLite)."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_contexts (
            event_id INTEGER NOT NULL,
            category VARCHAR(32) NOT NULL,
            scope VARCHAR(16) NOT NULL,
            scope_source VARCHAR(64) NOT NULL,
            taxonomy_version INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (event_id),
            FOREIGN KEY(event_id) REFERENCES events(id)
        )
        """
    )
    for column in ("category", "scope", "scope_source"):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS ix_event_contexts_{column} ON event_contexts ({column})"
        )
    conn.execute(
        """
        UPDATE events
        SET workspace_id = (
            SELECT agent_sessions.workspace_id
            FROM agent_sessions
            WHERE agent_sessions.id = events.session_id
        )
        WHERE workspace_id IS NULL
          AND session_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM agent_sessions WHERE agent_sessions.id = events.session_id
          )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO event_contexts (
            event_id, category, scope, scope_source, taxonomy_version
        )
        SELECT id,
               'legacy',
               CASE WHEN workspace_id IS NULL THEN 'unresolved' ELSE 'workspace' END,
               CASE
                   WHEN workspace_id IS NULL THEN 'legacy_unresolved'
                   WHEN session_id IS NOT NULL THEN 'legacy_session'
                   ELSE 'legacy_explicit'
               END,
               1
        FROM events
        """
    )

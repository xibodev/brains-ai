"""Session resume support — heartbeat column + link table + checkpoints.

Three additive changes:

1. ``agent_sessions.last_activity_at`` column (nullable DATETIME) so the
   reaper and the resume UI can show how fresh a session actually is.
   Backfilled to ``started_at`` for existing rows so historical sessions
   don't look freshly idle.
2. ``tool_session_links`` table — many-to-one mapping from a tool's own
   session id to a brain ``AgentSession``.
3. ``session_checkpoints`` table — agent-authored cairns surfaced on
   resume.

Idempotent: each ``ADD COLUMN`` / ``CREATE TABLE`` guards itself.
``create_all`` already provisions these on fresh databases; this
migration only matters for existing installs.
"""

from __future__ import annotations

import sqlite3


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def upgrade(conn: sqlite3.Connection) -> None:
    # 1. last_activity_at on agent_sessions.
    if not _column_exists(conn, "agent_sessions", "last_activity_at"):
        conn.execute("ALTER TABLE agent_sessions ADD COLUMN last_activity_at DATETIME")
        # Backfill so existing sessions don't look freshly idle. Live
        # sessions (ended_at IS NULL) get started_at as a baseline;
        # ended sessions get ended_at so any future reaper run treats
        # them as already finalized.
        conn.execute(
            """
            UPDATE agent_sessions
               SET last_activity_at = COALESCE(ended_at, started_at)
             WHERE last_activity_at IS NULL
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_agent_sessions_last_activity_at "
            "ON agent_sessions(last_activity_at)"
        )

    # 2. tool_session_links.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_session_links (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            brain_session_id  VARCHAR(32) NOT NULL,
            tool              VARCHAR(64) NOT NULL,
            tool_session_id   VARCHAR(256) NOT NULL,
            linked_at         DATETIME NOT NULL,
            linked_by         VARCHAR(16) NOT NULL DEFAULT 'auto'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_session_links_brain_session_id "
        "ON tool_session_links(brain_session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_session_links_tool ON tool_session_links(tool)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_tool_session_links_tool_session_id "
        "ON tool_session_links(tool_session_id)"
    )
    # Composite uniqueness so re-linking the same triple is a no-op.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_tool_session_links_triple "
        "ON tool_session_links(brain_session_id, tool, tool_session_id)"
    )

    # 3. session_checkpoints.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_checkpoints (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       VARCHAR(32) NOT NULL,
            workspace_id     INTEGER,
            summary          TEXT NOT NULL,
            next_action      TEXT,
            blockers         TEXT,
            scratchpad_path  TEXT,
            metadata_json    TEXT,
            created_at       DATETIME NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_checkpoints_session_id "
        "ON session_checkpoints(session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_checkpoints_workspace_id "
        "ON session_checkpoints(workspace_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_checkpoints_created_at "
        "ON session_checkpoints(created_at)"
    )

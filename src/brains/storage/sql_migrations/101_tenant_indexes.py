"""Phase 4 tenant-scoped composite indexes.

Fresh databases get these indexes from SQLAlchemy ``create_all``. This
migration adds the same indexes to existing SQLite databases idempotently.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_sessions_ws_activity "
        "ON agent_sessions(workspace_id, last_activity_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_tasks_ws_status ON agent_tasks(workspace_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_ws_created ON events(workspace_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_ws_status "
        "ON knowledge_entries(workspace_id, status)"
    )

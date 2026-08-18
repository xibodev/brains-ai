"""Layer 2 of the multi-operator model: workspace memberships + visibility.

Two additive changes (idempotent, sqlite-safe):

1. ``workspaces.visibility`` column — ``'shared'`` (default) preserves
   today's behaviour where every operator can see every workspace, or
   ``'private'`` which restricts the workspace to the auto-provisioned
   ``admin`` operator plus any operator with a ``workspace_memberships``
   row. Existing rows back-fill to ``'shared'`` so single-operator and
   unbounded multi-operator installs see no change.

2. ``workspace_memberships`` table — explicit (operator_id, workspace_id)
   grants with a free-form ``role`` column. The unique constraint
   prevents duplicate invites. ``admin`` never needs a row (the
   resolver short-circuits on slug).

On fresh SQLAlchemy installs ``create_all`` already provisions both
shapes; this migration only matters for databases that pre-date Layer 2.

See ``docs/ARCHITECTURE.md`` for the current workspace trust boundary.
"""

from __future__ import annotations

import sqlite3


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def upgrade(conn: sqlite3.Connection) -> None:
    # 1. workspaces.visibility — default 'shared' so back-fill is implicit.
    if not _column_exists(conn, "workspaces", "visibility"):
        conn.execute(
            "ALTER TABLE workspaces ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT 'shared'"
        )

    # 2. workspace_memberships table.
    if not _table_exists(conn, "workspace_memberships"):
        conn.execute(
            """
            CREATE TABLE workspace_memberships (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id   INTEGER NOT NULL REFERENCES operators(id),
                workspace_id  INTEGER NOT NULL REFERENCES workspaces(id),
                role          VARCHAR(32) NOT NULL DEFAULT 'member',
                created_at    DATETIME NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_memberships_op_ws "
            "ON workspace_memberships(operator_id, workspace_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_workspace_memberships_operator_id "
            "ON workspace_memberships(operator_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_workspace_memberships_workspace_id "
            "ON workspace_memberships(workspace_id)"
        )

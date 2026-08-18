"""WS2 native-battalion — Org ↔ Workspace back-reference + default org.

Patches existing SQLite databases so they gain the additive ``workspaces.org_id``
column that ``Base.metadata.create_all`` only provisions on *fresh* DBs. Then it
seeds a single default org and backfills org-less workspaces onto it so
pre-pivot single-machine installs keep working with zero operator action.

Every step is guarded (``PRAGMA table_info`` / ``SELECT`` before ``INSERT`` /
``IF NOT EXISTS``) so the migration is safe to re-run and safe on databases
where ``create_all`` already provisioned the column. The ``orgs`` /
``runtimes`` / etc. tables are created by ``create_all`` *before* the disk
migrations run (``init_db``: create_all → _apply_pending → _apply_disk), so we
rely on ``orgs`` already existing here.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def upgrade(conn: sqlite3.Connection) -> None:
    workspaces = _existing_columns(conn, "workspaces")
    if not workspaces:
        # workspaces table absent on a brand-new DB before create_all — nothing
        # to ALTER; create_all provisions the column from the model.
        return

    # 1. Additive ADD COLUMN (no NOT-NULL, no default rewrite).
    if "org_id" not in workspaces:
        conn.execute("ALTER TABLE workspaces ADD COLUMN org_id INTEGER")

    # 2. Index (idempotent).
    conn.execute("CREATE INDEX IF NOT EXISTS ix_workspaces_org_id ON workspaces (org_id)")

    # 3. Seed one default org + backfill — only when the orgs table exists
    #    (create_all runs before disk migrations, so it normally does).
    if not _table_exists(conn, "orgs"):
        return

    now = datetime.now(UTC).isoformat()
    existing = conn.execute("SELECT id FROM orgs WHERE slug = 'default'").fetchone()
    if existing is None:
        any_org = conn.execute("SELECT 1 FROM orgs LIMIT 1").fetchone()
        if any_org is None:
            conn.execute(
                "INSERT INTO orgs (slug, name, description, status, "
                "created_at, updated_at) VALUES "
                "('default', 'Default Org', NULL, 'active', ?, ?)",
                (now, now),
            )
        existing = conn.execute("SELECT id FROM orgs WHERE slug = 'default'").fetchone()

    if existing is not None:
        default_org_id = existing[0]
        conn.execute(
            "UPDATE workspaces SET org_id = ? WHERE org_id IS NULL",
            (default_org_id,),
        )

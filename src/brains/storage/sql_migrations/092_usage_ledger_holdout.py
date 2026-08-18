"""092 — Deterministic savings holdout marker."""

from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def upgrade(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "usage_ledger"):
        return
    if not _column_exists(conn, "usage_ledger", "is_holdout"):
        conn.execute("ALTER TABLE usage_ledger ADD COLUMN is_holdout INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_usage_ledger_is_holdout ON usage_ledger(is_holdout)"
        )

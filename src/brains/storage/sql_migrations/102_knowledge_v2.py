"""Phase 7 knowledge ledger enrichment.

Fresh databases get these columns from SQLAlchemy ``create_all``. This
migration patches existing SQLite databases idempotently.
"""

from __future__ import annotations

import sqlite3


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def upgrade(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "knowledge_entries", "provenance"):
        conn.execute(
            "ALTER TABLE knowledge_entries ADD COLUMN provenance VARCHAR(16) DEFAULT 'inferred'"
        )
    if not _column_exists(conn, "knowledge_entries", "importance"):
        conn.execute("ALTER TABLE knowledge_entries ADD COLUMN importance FLOAT DEFAULT 0.5")
    if not _column_exists(conn, "knowledge_entries", "valid_until"):
        conn.execute("ALTER TABLE knowledge_entries ADD COLUMN valid_until DATETIME")
    if not _column_exists(conn, "knowledge_entries", "promoted_from"):
        conn.execute(
            "ALTER TABLE knowledge_entries "
            "ADD COLUMN promoted_from INTEGER REFERENCES knowledge_entries(id)"
        )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_entries_importance "
        "ON knowledge_entries(importance)"
    )

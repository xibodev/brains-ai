"""080 — Chunk embeddings.

Adds the ``embedding`` BLOB column to ``chunks`` so the semantic retrieval
layer can store one packed little-endian float32 vector per chunk.

Idempotent and sqlite-safe; ``Base.metadata.create_all`` provisions the
column on fresh installs, so this migration only matters for databases that
pre-date the semantic layer.
"""

from __future__ import annotations

import sqlite3


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def upgrade(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "chunks", "embedding"):
        conn.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")

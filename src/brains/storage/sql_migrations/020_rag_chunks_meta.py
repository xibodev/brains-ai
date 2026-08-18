"""RAG embedding metadata table.

Adds ``chunks_meta`` (one-row-per-DB record of embed model + dim) so the
upcoming embedding-aware retrieval layer can detect a model mismatch
before it returns garbage similarity scores.

Idempotent: ``CREATE TABLE IF NOT EXISTS`` plus a single-row sentinel
constraint. Safe on fresh databases where ``Base.metadata.create_all``
has already provisioned the table.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks_meta (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            embed_model TEXT    NOT NULL,
            embed_dim   INTEGER NOT NULL,
            updated_at  TEXT    NOT NULL
        )
        """
    )

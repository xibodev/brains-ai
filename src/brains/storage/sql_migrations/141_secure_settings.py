"""Encrypted local configuration store (SQLite).

Ciphertext is stored in the Brains database; the key is derived from the
admin key at runtime. No plaintext value or derived key is persisted.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS secure_settings (
            name VARCHAR(128) NOT NULL,
            ciphertext BLOB NOT NULL,
            nonce BLOB NOT NULL,
            salt BLOB NOT NULL,
            version INTEGER NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (name)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_secure_settings_updated_at ON secure_settings (updated_at)"
    )

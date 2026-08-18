"""F1 Connect-a-machine — enrolment_tokens (single-use connect tokens).

Provisions the ``enrolment_tokens`` table on already-existing SQLite installs.
``Base.metadata.create_all`` only provisions it on *fresh* DBs; this disk
migration patches pre-existing databases with an additive
``CREATE TABLE IF NOT EXISTS``.

An enrolment token is the credential a new machine presents to register its
CLIs without an operator API key. We store ONLY a sha256 hash of the raw token
(never the raw value), an expiry, and single-use redemption stamps.

Idempotent: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` so
the migration is safe to re-run and safe on databases where ``create_all``
already provisioned the table.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrolment_tokens (
            id INTEGER PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            label TEXT,
            org_id INTEGER,
            created_by_operator_id INTEGER,
            created_at TIMESTAMP,
            expires_at TIMESTAMP,
            redeemed_at TIMESTAMP,
            redeemed_machine_id TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_enrolment_tokens_token_hash ON enrolment_tokens (token_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_enrolment_tokens_org_id ON enrolment_tokens (org_id)"
    )

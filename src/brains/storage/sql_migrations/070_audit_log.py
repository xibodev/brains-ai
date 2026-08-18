"""070 — Signed audit log.

Adds the ``audit_log`` table that backs :mod:`brains.audit`. Entries
form an HMAC-SHA256 hash chain so any out-of-band tamper (row delete,
row mutation, row insert) is detectable by
:func:`brains.audit.verify_chain`. See the audit module docstring for
the on-the-wire layout and threat model.

Idempotent and sqlite-safe; ``Base.metadata.create_all`` provisions the
table on fresh installs, so this migration is only needed for
databases that pre-date the audit feature.
"""

from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def upgrade(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "audit_log"):
        return
    conn.execute(
        """
        CREATE TABLE audit_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    DATETIME NOT NULL,
            actor         VARCHAR(128) NOT NULL,
            action        VARCHAR(64) NOT NULL,
            workspace_id  INTEGER NULL REFERENCES workspaces(id),
            payload_json  TEXT NOT NULL,
            prev_hash     VARCHAR(64) NOT NULL,
            entry_hash    VARCHAR(64) NOT NULL UNIQUE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON audit_log(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_action ON audit_log(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_actor ON audit_log(actor)")

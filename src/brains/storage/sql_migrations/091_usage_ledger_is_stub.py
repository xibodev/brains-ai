"""091 — Mark stub-provider rows in the usage ledger.

Adds the ``is_stub`` column to ``usage_ledger`` so the savings
dashboard can exclude synthetic traffic (e.g. the built-in ``echo``
dev provider) from its headline totals. Existing rows are
backfilled with ``is_stub=0`` (the SQL default), then any row whose
``provider`` matches a known stub name is flipped to ``1`` so
historical echo calls drop out of the dashboard once this migration
runs.

Idempotent and sqlite-safe; ``Base.metadata.create_all`` provisions
the column on fresh installs, so this migration only matters for
databases that already had ``usage_ledger`` from migration 090.
"""

from __future__ import annotations

import sqlite3

# Provider names whose rows should be flipped to ``is_stub=1`` during
# the one-shot backfill. Kept as a static tuple rather than imported
# from :mod:`brains.providers.registry` so the migration stays
# self-contained — running migrations must never depend on the
# application import graph being healthy.
_KNOWN_STUB_PROVIDERS: tuple[str, ...] = ("echo",)


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
    if not _column_exists(conn, "usage_ledger", "is_stub"):
        conn.execute("ALTER TABLE usage_ledger ADD COLUMN is_stub INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_usage_ledger_is_stub ON usage_ledger(is_stub)")
    if _KNOWN_STUB_PROVIDERS:
        placeholders = ",".join("?" for _ in _KNOWN_STUB_PROVIDERS)
        conn.execute(
            f"UPDATE usage_ledger SET is_stub = 1 "
            f"WHERE is_stub = 0 AND provider IN ({placeholders})",
            _KNOWN_STUB_PROVIDERS,
        )

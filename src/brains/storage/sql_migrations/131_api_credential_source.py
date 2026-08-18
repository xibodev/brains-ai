"""Record where an accepted credential came from, so rotation can revoke it.

``api_credentials.source``
    Adoption used to be write-only: the store learned that a raw key was
    acceptable and never learned when it stopped being one. Rotating
    ``BRAINS_API_KEY`` or deleting ``~/.brains/operator-keys/<slug>.key``
    therefore left the superseded credential authenticating forever, because
    nothing in the row said it had been adopted from that file in the first
    place.

    The column names the provenance of every row - ``local:admin_key``,
    ``local:api_keys``, ``local:operator_key``, ``enrolment`` or ``manual`` -
    so :func:`brains.authz.credentials.sync_local_credentials` can revoke
    exactly the credentials it adopted itself, and nothing else. A Runtime
    credential minted by enrollment and a manually registered credential are
    outside the reconciled set by construction, so a key rotation can never
    take a live daemon down.

Backfill is by kind, which is the only evidence a pre-existing row carries:
``runtime`` rows came from enrollment redemption; every other row was adopted
from a local key file by the only code path that existed. A row whose raw
value is still on disk is re-affirmed on the next sync; one whose value is not
is revoked, which is precisely the fix.

Idempotent on both backends.
"""

from __future__ import annotations

import sqlite3


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def upgrade(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "api_credentials", "source"):
        conn.execute("ALTER TABLE api_credentials ADD COLUMN source VARCHAR(32)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_api_credentials_source ON api_credentials (source)")
    conn.execute(
        "UPDATE api_credentials SET source = 'enrolment' WHERE source IS NULL AND kind = 'runtime'"
    )
    conn.execute(
        "UPDATE api_credentials SET source = 'local:operator_key' "
        "WHERE source IS NULL AND kind = 'operator'"
    )
    conn.execute(
        "UPDATE api_credentials SET source = 'local:admin_key' "
        "WHERE source IS NULL AND kind = 'admin'"
    )

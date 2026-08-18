"""BL-P0-04 follow-up - adoption marker for signed heads, per-attempt lease.

Two columns, both closing a gap the 126 delta left open:

``audit_chain_head.adopted_version`` / ``adopted_at``
    A NULL ``head_mac`` used to be indistinguishable from "adopted at upgrade",
    so anyone who could write the database could truncate ``audit_log``, move
    the head and clear its signature to make the result verify. The marker is
    the store's persisted commitment to signed heads: once it is set a missing
    signature is tamper, not legacy state. A store whose head is already signed
    is marked here, since a signature is exactly the commitment the marker
    records; a genuinely unsigned head is left unmarked so
    ``brains-ai audit-adopt`` can adopt it once, after verifying it.

``governed_actions.attempt_started_at``
    The abandoned-attempt lease was keyed on ``created_at``, which does not
    move when a retry resets an attempt: the fresh attempt looked abandoned
    immediately and concurrent retries could each start their own. Existing
    rows are backfilled from the timestamp that best describes the attempt they
    are on, newest transition first.

Idempotent on both backends: the columns are added only when absent.
"""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def upgrade(conn: sqlite3.Connection) -> None:
    head_columns = _columns(conn, "audit_chain_head")
    if head_columns and "adopted_version" not in head_columns:
        conn.execute("ALTER TABLE audit_chain_head ADD COLUMN adopted_version INTEGER")
    if head_columns and "adopted_at" not in head_columns:
        conn.execute("ALTER TABLE audit_chain_head ADD COLUMN adopted_at DATETIME")
    if head_columns:
        conn.execute(
            "UPDATE audit_chain_head SET adopted_version = 1, adopted_at = CURRENT_TIMESTAMP "
            "WHERE head_mac IS NOT NULL AND adopted_version IS NULL"
        )

    action_columns = _columns(conn, "governed_actions")
    if action_columns and "attempt_started_at" not in action_columns:
        conn.execute("ALTER TABLE governed_actions ADD COLUMN attempt_started_at DATETIME")
        conn.execute(
            "UPDATE governed_actions "
            "SET attempt_started_at = COALESCE(executed_at, authorized_at, created_at) "
            "WHERE attempt_started_at IS NULL"
        )

"""BL-P0-04 follow-up - execution heartbeat for long-running governed actions.

``governed_actions.heartbeat_at``
    The abandoned-attempt lease is a fixed budget measured from the start of
    the attempt, so an execution that legitimately runs longer than the budget
    - an agent session, a deploy, a Windows child the gate waits on - was
    settled ``failed`` with "abandoned while executing" while it was still
    running, and its idempotency key was burned. The owner of an executing
    attempt now renews this column while the effect runs, and the sweep judges
    an executing row by the silence since its last heartbeat instead of by how
    long it has been running. A crashed owner stops renewing, so it still
    settles once the heartbeat lease expires.

Existing executing rows are backfilled from the timestamp that best describes
their liveness, so an upgrade neither resurrects nor immediately sweeps them.

Idempotent on both backends: the column is added only when absent.
"""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def upgrade(conn: sqlite3.Connection) -> None:
    action_columns = _columns(conn, "governed_actions")
    if not action_columns:
        return
    if "heartbeat_at" not in action_columns:
        conn.execute("ALTER TABLE governed_actions ADD COLUMN heartbeat_at DATETIME")
    conn.execute(
        "UPDATE governed_actions "
        "SET heartbeat_at = COALESCE(executed_at, attempt_started_at, created_at) "
        "WHERE status = 'executing' AND heartbeat_at IS NULL"
    )

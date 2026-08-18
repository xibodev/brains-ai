"""090 — Usage ledger (cost savings).

Adds the ``usage_ledger`` table consumed by the savings dashboard.
One row per successful gateway call, with the actual model + token
counts + the USD cost computed against
:mod:`brains.router.prices`, plus the cost the same call would have
incurred at the operator-configured baseline model. The dashboard
displays the running ``SUM(savings_usd)`` and a per-day series.

Idempotent and sqlite-safe; ``Base.metadata.create_all`` provisions
the table on fresh installs, so this migration only matters for
databases that pre-date the savings ledger.
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
    if _table_exists(conn, "usage_ledger"):
        return
    conn.execute(
        """
        CREATE TABLE usage_ledger (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                DATETIME NOT NULL,
            endpoint          VARCHAR(64) NOT NULL,
            requested_model   VARCHAR(128) NOT NULL DEFAULT '',
            routed_model      VARCHAR(128) NOT NULL,
            provider          VARCHAR(64) NOT NULL,
            task_type         VARCHAR(64) NULL,
            input_tokens      INTEGER NOT NULL DEFAULT 0,
            output_tokens     INTEGER NOT NULL DEFAULT 0,
            cost_actual_usd   REAL NULL,
            cost_baseline_usd REAL NULL,
            savings_usd       REAL NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_usage_ledger_ts ON usage_ledger(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_usage_ledger_endpoint ON usage_ledger(endpoint)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_usage_ledger_routed_model ON usage_ledger(routed_model)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_usage_ledger_provider ON usage_ledger(provider)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_usage_ledger_task_type ON usage_ledger(task_type)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_usage_ledger_savings_usd ON usage_ledger(savings_usd)"
    )

"""BL-P0-04 - durable governed-action ledger and audit chain head.

Adds two tables:

``governed_actions``
    One row per governed action, carrying the two uniqueness rules the
    approval contract rests on: ``idempotency_key`` (a retry reuses the row
    instead of re-executing) and ``approval_code`` (an approval is spendable
    exactly once, across processes).

``audit_chain_head``
    The single-row head pointer every audit append claims before it reads its
    predecessor, which is what serialises appends across processes instead of
    across threads only. Seeded from the existing ``audit_log`` so a store that
    already has entries keeps a continuous chain: ``seq`` is the current row
    count and ``head_hash``/``head_entry_id`` name the newest entry. A chain
    that was already forked by the old process-local append is *not* rewritten
    here - it is left exactly as it is so ``brains-ai audit-verify`` still
    reports the divergence.

The frozen baseline DDL predates both tables, so this delta - never a
regenerated baseline - provisions them on every backend. The DDL is byte-equal
to what the models render, so a store built by an older ``create_all`` and a
store built by this delta converge on the same schema. Idempotent.
"""

from __future__ import annotations

import sqlite3

_GOVERNED_ACTIONS = """
CREATE TABLE IF NOT EXISTS governed_actions (
	id INTEGER NOT NULL,
	action_id VARCHAR(40) NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	actor VARCHAR(128) NOT NULL,
	action VARCHAR(64) NOT NULL,
	tool VARCHAR(128) NOT NULL,
	args_hash VARCHAR(64) NOT NULL,
	tier VARCHAR(16) NOT NULL,
	status VARCHAR(16) NOT NULL,
	decision VARCHAR(24),
	approval_code VARCHAR(32),
	approval_expires_at DATETIME,
	org_id INTEGER,
	workspace_id INTEGER,
	issue_code VARCHAR(32),
	session_id VARCHAR(64),
	attempt INTEGER NOT NULL,
	result VARCHAR(16),
	error TEXT,
	summary TEXT,
	audit_request_id INTEGER,
	audit_decision_id INTEGER,
	audit_result_id INTEGER,
	created_at DATETIME NOT NULL,
	authorized_at DATETIME,
	executed_at DATETIME,
	completed_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
)
"""

_AUDIT_CHAIN_HEAD = """
CREATE TABLE IF NOT EXISTS audit_chain_head (
	id INTEGER NOT NULL,
	seq INTEGER NOT NULL,
	head_hash VARCHAR(64) NOT NULL,
	head_entry_id INTEGER,
	head_mac VARCHAR(64),
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_governed_actions_action ON governed_actions (action)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_governed_actions_action_id "
    "ON governed_actions (action_id)",
    "CREATE INDEX IF NOT EXISTS ix_governed_actions_actor ON governed_actions (actor)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_governed_actions_approval_code "
    "ON governed_actions (approval_code)",
    "CREATE INDEX IF NOT EXISTS ix_governed_actions_created_at ON governed_actions (created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_governed_actions_idempotency_key "
    "ON governed_actions (idempotency_key)",
    "CREATE INDEX IF NOT EXISTS ix_governed_actions_org_id ON governed_actions (org_id)",
    "CREATE INDEX IF NOT EXISTS ix_governed_actions_status ON governed_actions (status)",
    "CREATE INDEX IF NOT EXISTS ix_governed_actions_status_created "
    "ON governed_actions (status, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_governed_actions_workspace_id "
    "ON governed_actions (workspace_id)",
)


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_GOVERNED_ACTIONS)
    conn.execute(_AUDIT_CHAIN_HEAD)
    for statement in _INDEXES:
        conn.execute(statement)

    seeded = conn.execute("SELECT COUNT(*) FROM audit_chain_head WHERE id = 1").fetchone()
    if seeded and seeded[0]:
        return
    newest = conn.execute(
        "SELECT id, entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.execute(
        "INSERT INTO audit_chain_head (id, seq, head_hash, head_entry_id, updated_at) "
        "VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)",
        (total, newest[1] if newest else "GENESIS", newest[0] if newest else None),
    )

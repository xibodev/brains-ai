"""BL-P0-05 - the durable Session command queue.

``session_commands``
    One row per operator command addressed to a Session: a console message, a
    stop. The row commits before anything is announced and long before
    anything is delivered, so a reload shows what was asked for and what
    became of it instead of an optimistic bubble that no longer exists.

    ``operation_key`` is unique, which is what makes a retried mutation one
    logical command: the second writer loses the insert and reads the first
    row rather than queueing a second delivery.

    ``(session_id, sequence)`` is unique, so commands carry a stable order a
    consumer and the console both read, and two concurrent enqueues cannot
    share a position.

    ``claimed_by``/``lease_expires_at`` make the claim a conditional update
    rather than a read-then-write: exactly one consumer can move a command out
    of ``requested``, and a consumer that dies mid-flight releases it when the
    lease expires instead of stranding it.

The frozen baseline DDL predates this table, so this delta - never a
regenerated baseline - provisions it on every backend. The DDL is byte-equal
to what the model renders. Idempotent.
"""

from __future__ import annotations

import sqlite3

_SESSION_COMMANDS = """
CREATE TABLE IF NOT EXISTS session_commands (
	id INTEGER NOT NULL,
	command_id VARCHAR(40) NOT NULL,
	operation_key VARCHAR(160) NOT NULL,
	session_id VARCHAR(32) NOT NULL,
	sequence INTEGER NOT NULL,
	kind VARCHAR(16) NOT NULL,
	status VARCHAR(16) NOT NULL,
	payload_json TEXT NOT NULL,
	org_id INTEGER,
	workspace_id INTEGER,
	runtime_id INTEGER,
	machine_id VARCHAR(64),
	requested_by VARCHAR(128),
	attempt INTEGER NOT NULL,
	claimed_by VARCHAR(64),
	claimed_at DATETIME,
	lease_expires_at DATETIME,
	delivered_at DATETIME,
	completed_at DATETIME,
	result VARCHAR(32),
	error TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id),
	FOREIGN KEY(runtime_id) REFERENCES runtimes (id)
)
"""

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_session_commands_command_id "
    "ON session_commands (command_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_created_at ON session_commands (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_kind ON session_commands (kind)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_lease_expires_at "
    "ON session_commands (lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_machine_id ON session_commands (machine_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_session_commands_operation_key "
    "ON session_commands (operation_key)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_org_id ON session_commands (org_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_runtime_id ON session_commands (runtime_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_session_id ON session_commands (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_status ON session_commands (status)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_status_created "
    "ON session_commands (status, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_session_commands_workspace_id "
    "ON session_commands (workspace_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_session_commands_session_sequence "
    "ON session_commands (session_id, sequence)",
)


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_SESSION_COMMANDS)
    for statement in _INDEXES:
        conn.execute(statement)

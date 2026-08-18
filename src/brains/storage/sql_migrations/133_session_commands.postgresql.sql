-- BL-P0-05 - the durable Session command queue (postgresql).
--
-- The Postgres implementation of 133_session_commands.py. Both provision the
-- same table and the same indexes, so SQLite and Postgres converge through an
-- executed delta rather than through a recorded sentinel.

CREATE TABLE IF NOT EXISTS session_commands (
	id SERIAL NOT NULL,
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
	claimed_at TIMESTAMP WITH TIME ZONE,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	delivered_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	result VARCHAR(32),
	error TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id),
	FOREIGN KEY(runtime_id) REFERENCES runtimes (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_session_commands_command_id ON session_commands (command_id);

CREATE INDEX IF NOT EXISTS ix_session_commands_created_at ON session_commands (created_at);

CREATE INDEX IF NOT EXISTS ix_session_commands_kind ON session_commands (kind);

CREATE INDEX IF NOT EXISTS ix_session_commands_lease_expires_at ON session_commands (lease_expires_at);

CREATE INDEX IF NOT EXISTS ix_session_commands_machine_id ON session_commands (machine_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_session_commands_operation_key ON session_commands (operation_key);

CREATE INDEX IF NOT EXISTS ix_session_commands_org_id ON session_commands (org_id);

CREATE INDEX IF NOT EXISTS ix_session_commands_runtime_id ON session_commands (runtime_id);

CREATE INDEX IF NOT EXISTS ix_session_commands_session_id ON session_commands (session_id);

CREATE INDEX IF NOT EXISTS ix_session_commands_status ON session_commands (status);

CREATE INDEX IF NOT EXISTS ix_session_commands_status_created ON session_commands (status, created_at);

CREATE INDEX IF NOT EXISTS ix_session_commands_workspace_id ON session_commands (workspace_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_session_commands_session_sequence ON session_commands (session_id, sequence);

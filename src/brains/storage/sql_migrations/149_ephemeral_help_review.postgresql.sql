-- Fenced on-demand ephemeral help review execution (PostgreSQL).

CREATE TABLE IF NOT EXISTS help_request_executions (
	request_code VARCHAR(32) PRIMARY KEY REFERENCES help_requests(code),
	mode VARCHAR(16) NOT NULL,
	source_workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
	required_tool VARCHAR(64) NOT NULL,
	status VARCHAR(16) NOT NULL DEFAULT 'queued',
	runtime_id INTEGER REFERENCES runtimes(id),
	review_session_id VARCHAR(32) REFERENCES agent_sessions(id),
	attempt INTEGER NOT NULL DEFAULT 0,
	launch_after TIMESTAMP WITH TIME ZONE NOT NULL,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	started_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	error_code VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_help_request_executions_mode ON help_request_executions(mode);
CREATE INDEX IF NOT EXISTS ix_help_request_executions_source_workspace_id ON help_request_executions(source_workspace_id);
CREATE INDEX IF NOT EXISTS ix_help_request_executions_required_tool ON help_request_executions(required_tool);
CREATE INDEX IF NOT EXISTS ix_help_request_executions_status ON help_request_executions(status);
CREATE INDEX IF NOT EXISTS ix_help_request_executions_runtime_id ON help_request_executions(runtime_id);
CREATE INDEX IF NOT EXISTS ix_help_request_executions_review_session_id ON help_request_executions(review_session_id);
CREATE INDEX IF NOT EXISTS ix_help_request_executions_launch_after ON help_request_executions(launch_after);
CREATE INDEX IF NOT EXISTS ix_help_request_executions_lease_expires_at ON help_request_executions(lease_expires_at);

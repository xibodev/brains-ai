-- Agent-to-agent comms slice 1 (PostgreSQL).
--
-- Equivalent of 139_agent_comms.py. Idempotent.

CREATE TABLE IF NOT EXISTS help_request_constraints (
	request_code VARCHAR(32) NOT NULL,
	required_tool VARCHAR(64) NOT NULL,
	PRIMARY KEY (request_code),
	FOREIGN KEY(request_code) REFERENCES help_requests (code)
);

CREATE TABLE IF NOT EXISTS topic_posts (
	id INTEGER NOT NULL,
	topic VARCHAR(64) NOT NULL,
	from_session_id VARCHAR(32),
	from_workspace_id INTEGER,
	reply_to_id INTEGER,
	subject VARCHAR(256) NOT NULL,
	body TEXT,
	required_tool VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(from_session_id) REFERENCES agent_sessions (id),
	FOREIGN KEY(from_workspace_id) REFERENCES workspaces (id),
	FOREIGN KEY(reply_to_id) REFERENCES topic_posts (id)
);

CREATE INDEX IF NOT EXISTS ix_topic_posts_topic ON topic_posts (topic);
CREATE INDEX IF NOT EXISTS ix_topic_posts_created_at ON topic_posts (created_at);
CREATE INDEX IF NOT EXISTS ix_topic_posts_from_session_id ON topic_posts (from_session_id);
CREATE INDEX IF NOT EXISTS ix_topic_posts_from_workspace_id ON topic_posts (from_workspace_id);

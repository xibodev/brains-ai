-- BL-P0-04 - durable governed-action ledger and audit chain head (postgresql).
--
-- The Postgres implementation of 126_governed_actions.py. Both provision the
-- same objects, so SQLite and Postgres converge through an executed delta
-- rather than through a recorded sentinel.

CREATE TABLE IF NOT EXISTS governed_actions (
	id SERIAL NOT NULL,
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
	approval_expires_at TIMESTAMP WITH TIME ZONE,
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
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	authorized_at TIMESTAMP WITH TIME ZONE,
	executed_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE TABLE IF NOT EXISTS audit_chain_head (
	id SERIAL NOT NULL,
	seq INTEGER NOT NULL,
	head_hash VARCHAR(64) NOT NULL,
	head_entry_id INTEGER,
	head_mac VARCHAR(64),
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_governed_actions_action ON governed_actions (action);

CREATE UNIQUE INDEX IF NOT EXISTS ix_governed_actions_action_id ON governed_actions (action_id);

CREATE INDEX IF NOT EXISTS ix_governed_actions_actor ON governed_actions (actor);

CREATE UNIQUE INDEX IF NOT EXISTS ix_governed_actions_approval_code ON governed_actions (approval_code);

CREATE INDEX IF NOT EXISTS ix_governed_actions_created_at ON governed_actions (created_at);

CREATE UNIQUE INDEX IF NOT EXISTS ix_governed_actions_idempotency_key ON governed_actions (idempotency_key);

CREATE INDEX IF NOT EXISTS ix_governed_actions_org_id ON governed_actions (org_id);

CREATE INDEX IF NOT EXISTS ix_governed_actions_status ON governed_actions (status);

CREATE INDEX IF NOT EXISTS ix_governed_actions_status_created ON governed_actions (status, created_at);

CREATE INDEX IF NOT EXISTS ix_governed_actions_workspace_id ON governed_actions (workspace_id);

INSERT INTO audit_chain_head (id, seq, head_hash, head_entry_id, updated_at)
SELECT
    1,
    (SELECT COUNT(*) FROM audit_log),
    COALESCE((SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1), 'GENESIS'),
    (SELECT id FROM audit_log ORDER BY id DESC LIMIT 1),
    NOW()
WHERE NOT EXISTS (SELECT 1 FROM audit_chain_head WHERE id = 1);

-- Durable Workspace path aliases (PostgreSQL).

CREATE TABLE IF NOT EXISTS workspace_aliases (
	id SERIAL PRIMARY KEY,
	workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
	path VARCHAR(1024) NOT NULL UNIQUE,
	identity_key VARCHAR(1100) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_workspace_aliases_workspace_id ON workspace_aliases(workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_workspace_aliases_path ON workspace_aliases(path);
CREATE INDEX IF NOT EXISTS ix_workspace_aliases_identity_key ON workspace_aliases(identity_key);

INSERT INTO workspace_aliases (workspace_id, path, identity_key, created_at)
SELECT id, path, 'path:' || path, CURRENT_TIMESTAMP FROM workspaces
ON CONFLICT (path) DO NOTHING;

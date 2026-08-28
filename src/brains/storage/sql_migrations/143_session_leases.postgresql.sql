-- Renewable liveness leases for PID-less Sessions (PostgreSQL).

CREATE TABLE IF NOT EXISTS session_leases (
	session_id VARCHAR(32) NOT NULL,
	lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	renewed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (session_id),
	FOREIGN KEY(session_id) REFERENCES agent_sessions(id)
);

CREATE INDEX IF NOT EXISTS ix_session_leases_lease_expires_at
	ON session_leases (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_session_leases_renewed_at
	ON session_leases (renewed_at);

INSERT INTO session_leases (session_id, lease_expires_at, renewed_at)
SELECT id,
	COALESCE(last_activity_at, started_at) + INTERVAL '1 hour',
	COALESCE(last_activity_at, started_at)
FROM agent_sessions
WHERE ended_at IS NULL
	AND pid IS NULL
	AND runtime_id IS NULL
	AND issue_id IS NULL
	AND persona_id IS NULL
ON CONFLICT (session_id) DO NOTHING;

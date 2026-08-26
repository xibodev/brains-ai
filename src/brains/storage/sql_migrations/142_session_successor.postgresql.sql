-- Explicit session-handle successor links (PostgreSQL).

CREATE TABLE IF NOT EXISTS session_successors (
	predecessor_session_id VARCHAR(32) NOT NULL,
	successor_session_id VARCHAR(32) NOT NULL,
	linked_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (predecessor_session_id),
	FOREIGN KEY(predecessor_session_id) REFERENCES agent_sessions(id),
	FOREIGN KEY(successor_session_id) REFERENCES agent_sessions(id)
);

CREATE INDEX IF NOT EXISTS ix_session_successors_successor_session_id
	ON session_successors (successor_session_id);
CREATE INDEX IF NOT EXISTS ix_session_successors_linked_at
	ON session_successors (linked_at);

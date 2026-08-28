-- Human-owned approval assignment and escalation metadata (PostgreSQL).

CREATE TABLE IF NOT EXISTS approval_routing (
	approval_request_id INTEGER NOT NULL,
	assigned_operator_id INTEGER,
	priority VARCHAR(16) NOT NULL DEFAULT 'p2',
	due_at TIMESTAMP WITH TIME ZONE,
	escalation_level INTEGER NOT NULL DEFAULT 0,
	escalation_reason TEXT,
	updated_by_operator_id INTEGER,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (approval_request_id),
	FOREIGN KEY(approval_request_id) REFERENCES approval_requests(id),
	FOREIGN KEY(assigned_operator_id) REFERENCES operators(id),
	FOREIGN KEY(updated_by_operator_id) REFERENCES operators(id)
);

CREATE INDEX IF NOT EXISTS ix_approval_routing_assigned_operator_id
	ON approval_routing (assigned_operator_id);
CREATE INDEX IF NOT EXISTS ix_approval_routing_priority
	ON approval_routing (priority);
CREATE INDEX IF NOT EXISTS ix_approval_routing_due_at
	ON approval_routing (due_at);
CREATE INDEX IF NOT EXISTS ix_approval_routing_updated_at
	ON approval_routing (updated_at);

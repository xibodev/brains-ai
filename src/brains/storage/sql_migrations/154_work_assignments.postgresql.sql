-- Retained backend compatibility; SQLite is the supported local store.
CREATE TABLE IF NOT EXISTS work_assignments (
    code VARCHAR(39) NOT NULL PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    creator_operator_id INTEGER NOT NULL REFERENCES operators(id),
    creator_session_id VARCHAR(32) NOT NULL REFERENCES agent_sessions(id),
    idempotency_key VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    title VARCHAR(256) NOT NULL,
    spec_version INTEGER NOT NULL,
    specification_json TEXT NOT NULL,
    specification_hash VARCHAR(64) NOT NULL,
    status VARCHAR(24) NOT NULL,
    revision INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    cancel_requested_at TIMESTAMP WITH TIME ZONE,
    cancel_requested_by_session_id VARCHAR(32) REFERENCES agent_sessions(id),
    CONSTRAINT uq_work_assignments_creation
        UNIQUE (workspace_id, creator_operator_id, idempotency_key),
    CONSTRAINT ck_work_assignments_revision CHECK (revision >= 1),
    CONSTRAINT ck_work_assignments_generation CHECK (generation >= 0),
    CONSTRAINT ck_work_assignments_spec_version CHECK (spec_version = 1),
    CONSTRAINT ck_work_assignments_status CHECK (status IN
        ('ready','accepted','cancel_requested','completed','failed','cancelled','uncertain'))
);
CREATE INDEX IF NOT EXISTS ix_work_assignments_workspace_created
ON work_assignments (workspace_id, created_at);
CREATE TABLE IF NOT EXISTS work_assignment_attempts (
    attempt_id VARCHAR(36) NOT NULL PRIMARY KEY,
    assignment_code VARCHAR(39) NOT NULL REFERENCES work_assignments(code),
    generation INTEGER NOT NULL,
    source_session_id VARCHAR(32) NOT NULL REFERENCES agent_sessions(id),
    tool VARCHAR(64) NOT NULL,
    status VARCHAR(24) NOT NULL,
    accepted_at TIMESTAMP WITH TIME ZONE NOT NULL,
    deadline_at TIMESTAMP WITH TIME ZONE NOT NULL,
    max_runtime_seconds INTEGER NOT NULL,
    cancel_requested_at TIMESTAMP WITH TIME ZONE,
    reported_at TIMESTAMP WITH TIME ZONE,
    settled_at TIMESTAMP WITH TIME ZONE,
    evidence TEXT,
    result TEXT,
    usage_json TEXT,
    CONSTRAINT uq_work_assignment_generation UNIQUE (assignment_code, generation),
    CONSTRAINT ck_work_attempt_generation CHECK (generation >= 1),
    CONSTRAINT ck_work_attempt_runtime CHECK (max_runtime_seconds BETWEEN 1 AND 604800),
    CONSTRAINT ck_work_attempt_status CHECK (status IN
        ('accepted','cancel_requested','completed','failed','cancelled','uncertain')),
    CONSTRAINT ck_work_attempt_settlement CHECK (
        (status IN ('completed','failed','cancelled') AND settled_at IS NOT NULL) OR
        (status IN ('accepted','cancel_requested','uncertain') AND settled_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_work_assignment_live_attempt
ON work_assignment_attempts (assignment_code)
WHERE status IN ('accepted','cancel_requested','uncertain');

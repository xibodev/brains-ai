-- Retained compatibility only; SQLite remains the supported local store.
CREATE TABLE IF NOT EXISTS coordination_proposals (
    code VARCHAR(39) NOT NULL,
    version INTEGER NOT NULL,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    creator_operator_id INTEGER NOT NULL REFERENCES operators(id),
    creator_session_id VARCHAR(32) NOT NULL REFERENCES agent_sessions(id),
    result_owner_session_id VARCHAR(32) NOT NULL REFERENCES agent_sessions(id),
    idempotency_key VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    title VARCHAR(256) NOT NULL,
    specification_json TEXT NOT NULL,
    specification_hash VARCHAR(64) NOT NULL,
    status VARCHAR(24) NOT NULL,
    revision INTEGER NOT NULL,
    round INTEGER NOT NULL,
    initial_closed BOOLEAN NOT NULL,
    deadline_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    cancellation_reason TEXT,
    PRIMARY KEY (code, version),
    CONSTRAINT uq_coordination_creation
        UNIQUE (workspace_id, creator_operator_id, idempotency_key),
    CONSTRAINT ck_coordination_version CHECK (version >= 1 AND revision >= 0),
    CONSTRAINT ck_coordination_round CHECK (round BETWEEN 0 AND 4),
    CONSTRAINT ck_coordination_status CHECK (status IN
        ('planned','accepted','collecting','discussing','completed','cancelled'))
);
CREATE INDEX IF NOT EXISTS ix_coordination_workspace_created
ON coordination_proposals (workspace_id, created_at);
CREATE TABLE IF NOT EXISTS coordination_contributions (
    contribution_id VARCHAR(36) NOT NULL PRIMARY KEY,
    proposal_code VARCHAR(39) NOT NULL,
    version INTEGER NOT NULL,
    author_session_id VARCHAR(32) NOT NULL REFERENCES agent_sessions(id),
    kind VARCHAR(16) NOT NULL,
    round INTEGER NOT NULL,
    slot VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128),
    payload_json TEXT NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    FOREIGN KEY (proposal_code, version) REFERENCES coordination_proposals(code, version),
    CONSTRAINT uq_coordination_slot UNIQUE (proposal_code, version, slot),
    CONSTRAINT uq_coordination_contribution_key
        UNIQUE (proposal_code, version, author_session_id, idempotency_key),
    CONSTRAINT ck_coordination_contribution_kind CHECK (
        (kind IN ('accept','initial','final') AND round = 0) OR
        (kind = 'discussion' AND round BETWEEN 1 AND 3)),
    CONSTRAINT ck_coordination_contribution_slot CHECK (
        (kind = 'final' AND slot = 'final') OR
        (kind != 'final' AND slot = kind || ':' || CAST(round AS TEXT) || ':' || author_session_id))
);

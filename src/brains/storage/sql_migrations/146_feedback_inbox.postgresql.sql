-- Governed agent-experience feedback inbox (PostgreSQL).

CREATE TABLE IF NOT EXISTS feedback_reports (
	id SERIAL PRIMARY KEY,
	code VARCHAR(32) NOT NULL UNIQUE,
	workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
	reporter_session_id VARCHAR(32) REFERENCES agent_sessions(id),
	category VARCHAR(32) NOT NULL,
	severity VARCHAR(16) NOT NULL,
	summary VARCHAR(500) NOT NULL,
	evidence TEXT NOT NULL DEFAULT '',
	reproduction TEXT NOT NULL DEFAULT '',
	affected_version VARCHAR(64),
	surface VARCHAR(128),
	metadata_json TEXT NOT NULL DEFAULT '{}',
	fingerprint VARCHAR(64) NOT NULL,
	status VARCHAR(16) NOT NULL DEFAULT 'open',
	triage_note TEXT,
	triaged_by_operator_id INTEGER REFERENCES operators(id),
	triaged_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT uq_feedback_workspace_fingerprint UNIQUE (workspace_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS feedback_enrichments (
	id SERIAL PRIMARY KEY,
	feedback_report_id INTEGER NOT NULL REFERENCES feedback_reports(id),
	reporter_session_id VARCHAR(32) REFERENCES agent_sessions(id),
	kind VARCHAR(32) NOT NULL DEFAULT 'enrichment',
	note TEXT NOT NULL DEFAULT '',
	evidence TEXT NOT NULL DEFAULT '',
	reproduction TEXT NOT NULL DEFAULT '',
	metadata_json TEXT NOT NULL DEFAULT '{}',
	fingerprint VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT uq_feedback_enrichment UNIQUE (feedback_report_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS feedback_promotions (
	feedback_report_id INTEGER PRIMARY KEY REFERENCES feedback_reports(id),
	target_kind VARCHAR(16) NOT NULL,
	target_ref VARCHAR(128) NOT NULL,
	promoted_by_operator_id INTEGER REFERENCES operators(id),
	audit_entry_id INTEGER NOT NULL UNIQUE REFERENCES audit_log(id),
	promoted_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_feedback_reports_code ON feedback_reports(code);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_workspace_id ON feedback_reports(workspace_id);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_reporter_session_id ON feedback_reports(reporter_session_id);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_category ON feedback_reports(category);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_severity ON feedback_reports(severity);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_affected_version ON feedback_reports(affected_version);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_surface ON feedback_reports(surface);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_status ON feedback_reports(status);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_fingerprint ON feedback_reports(fingerprint);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_triaged_by_operator_id ON feedback_reports(triaged_by_operator_id);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_created_at ON feedback_reports(created_at);
CREATE INDEX IF NOT EXISTS ix_feedback_reports_updated_at ON feedback_reports(updated_at);
CREATE INDEX IF NOT EXISTS ix_feedback_enrichments_feedback_report_id ON feedback_enrichments(feedback_report_id);
CREATE INDEX IF NOT EXISTS ix_feedback_enrichments_reporter_session_id ON feedback_enrichments(reporter_session_id);
CREATE INDEX IF NOT EXISTS ix_feedback_enrichments_kind ON feedback_enrichments(kind);
CREATE INDEX IF NOT EXISTS ix_feedback_enrichments_fingerprint ON feedback_enrichments(fingerprint);
CREATE INDEX IF NOT EXISTS ix_feedback_enrichments_created_at ON feedback_enrichments(created_at);
CREATE INDEX IF NOT EXISTS ix_feedback_promotions_target_kind ON feedback_promotions(target_kind);
CREATE INDEX IF NOT EXISTS ix_feedback_promotions_target_ref ON feedback_promotions(target_ref);
CREATE INDEX IF NOT EXISTS ix_feedback_promotions_promoted_at ON feedback_promotions(promoted_at);

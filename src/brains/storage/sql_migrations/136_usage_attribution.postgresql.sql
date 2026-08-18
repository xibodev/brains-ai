-- BL-P1-02 - product attribution for gateway usage (PostgreSQL).
--
-- Equivalent of 136_usage_attribution.py. Idempotent.

CREATE TABLE IF NOT EXISTS usage_attributions (
	id SERIAL NOT NULL,
	usage_entry_id INTEGER NOT NULL,
	session_id VARCHAR(32),
	issue_id INTEGER,
	persona_id INTEGER,
	org_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(usage_entry_id) REFERENCES usage_ledger (id),
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id),
	FOREIGN KEY(issue_id) REFERENCES issues (id),
	FOREIGN KEY(persona_id) REFERENCES personas (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_usage_attributions_usage_entry_id ON usage_attributions (usage_entry_id);
CREATE INDEX IF NOT EXISTS ix_usage_attributions_session_id ON usage_attributions (session_id);
CREATE INDEX IF NOT EXISTS ix_usage_attributions_issue_id ON usage_attributions (issue_id);
CREATE INDEX IF NOT EXISTS ix_usage_attributions_persona_id ON usage_attributions (persona_id);
CREATE INDEX IF NOT EXISTS ix_usage_attributions_org_id ON usage_attributions (org_id);
CREATE INDEX IF NOT EXISTS ix_usage_attributions_created_at ON usage_attributions (created_at);

-- Typed event taxonomy and scope provenance (PostgreSQL).

CREATE TABLE IF NOT EXISTS event_contexts (
	event_id INTEGER PRIMARY KEY REFERENCES events(id),
	category VARCHAR(32) NOT NULL,
	scope VARCHAR(16) NOT NULL,
	scope_source VARCHAR(64) NOT NULL,
	taxonomy_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_event_contexts_category ON event_contexts(category);
CREATE INDEX IF NOT EXISTS ix_event_contexts_scope ON event_contexts(scope);
CREATE INDEX IF NOT EXISTS ix_event_contexts_scope_source ON event_contexts(scope_source);

UPDATE events
SET workspace_id = agent_sessions.workspace_id
FROM agent_sessions
WHERE events.workspace_id IS NULL
	AND events.session_id = agent_sessions.id;

INSERT INTO event_contexts (event_id, category, scope, scope_source, taxonomy_version)
SELECT id,
	'legacy',
	CASE WHEN workspace_id IS NULL THEN 'unresolved' ELSE 'workspace' END,
	CASE
		WHEN workspace_id IS NULL THEN 'legacy_unresolved'
		WHEN session_id IS NOT NULL THEN 'legacy_session'
		ELSE 'legacy_explicit'
	END,
	1
FROM events
ON CONFLICT (event_id) DO NOTHING;

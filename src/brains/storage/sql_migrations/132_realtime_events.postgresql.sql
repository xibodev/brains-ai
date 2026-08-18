-- BL-P0-02 - the durable realtime event log (postgresql).
--
-- The Postgres implementation of 132_realtime_events.py. Both provision the
-- same table and the same indexes, so SQLite and Postgres converge through an
-- executed delta rather than through a recorded sentinel.

CREATE TABLE IF NOT EXISTS realtime_events (
	id SERIAL NOT NULL,
	topic VARCHAR(160) NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	entity VARCHAR(32),
	entity_id VARCHAR(64),
	org_id INTEGER,
	workspace_id INTEGER,
	dedupe_key VARCHAR(128),
	payload_json TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX IF NOT EXISTS ix_realtime_events_topic ON realtime_events (topic);

CREATE INDEX IF NOT EXISTS ix_realtime_events_topic_id ON realtime_events (topic, id);

CREATE INDEX IF NOT EXISTS ix_realtime_events_event_type ON realtime_events (event_type);

CREATE INDEX IF NOT EXISTS ix_realtime_events_org_id ON realtime_events (org_id);

CREATE INDEX IF NOT EXISTS ix_realtime_events_workspace_id ON realtime_events (workspace_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_realtime_events_dedupe_key ON realtime_events (dedupe_key);

CREATE INDEX IF NOT EXISTS ix_realtime_events_created_at ON realtime_events (created_at);

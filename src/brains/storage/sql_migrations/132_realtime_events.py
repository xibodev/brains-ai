"""BL-P0-02 - the durable realtime event log.

``realtime_events``
    One row per announced event whose loss a user would notice: Session
    lifecycle, Issue change, approval/ASK movement, Runtime state. The row
    commits before the in-process bus announces anything, so a client that
    reconnects - to the same process or to another one - catches up by cursor
    (``id``) instead of by luck.

    ``dedupe_key`` is unique, which is what makes a re-published event
    idempotent across processes: the second writer loses the insert and reuses
    the first row's id rather than minting a second event.

    ``org_id``/``workspace_id`` record the scope the topic resolved to at
    publish time, so delivery and replay can be filtered on the event's own
    scope as well as on its topic string.

The frozen baseline DDL predates this table, so this delta - never a
regenerated baseline - provisions it on every backend. The DDL is byte-equal
to what the model renders. Idempotent.
"""

from __future__ import annotations

import sqlite3

_REALTIME_EVENTS = """
CREATE TABLE IF NOT EXISTS realtime_events (
	id INTEGER NOT NULL,
	topic VARCHAR(160) NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	entity VARCHAR(32),
	entity_id VARCHAR(64),
	org_id INTEGER,
	workspace_id INTEGER,
	dedupe_key VARCHAR(128),
	payload_json TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_realtime_events_topic ON realtime_events (topic)",
    "CREATE INDEX IF NOT EXISTS ix_realtime_events_topic_id ON realtime_events (topic, id)",
    "CREATE INDEX IF NOT EXISTS ix_realtime_events_event_type ON realtime_events (event_type)",
    "CREATE INDEX IF NOT EXISTS ix_realtime_events_org_id ON realtime_events (org_id)",
    "CREATE INDEX IF NOT EXISTS ix_realtime_events_workspace_id ON realtime_events (workspace_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_realtime_events_dedupe_key "
    "ON realtime_events (dedupe_key)",
    "CREATE INDEX IF NOT EXISTS ix_realtime_events_created_at ON realtime_events (created_at)",
)


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_REALTIME_EVENTS)
    for statement in _INDEXES:
        conn.execute(statement)

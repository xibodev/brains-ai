"""BL-P1-06 - durable integration delivery outcomes and deduplication."""

from __future__ import annotations

import sqlite3

_TABLE = """
CREATE TABLE IF NOT EXISTS integration_deliveries (
	id INTEGER NOT NULL,
	channel VARCHAR(32) NOT NULL,
	direction VARCHAR(16) NOT NULL,
	delivery_key VARCHAR(128) NOT NULL,
	status VARCHAR(24) NOT NULL,
	subject VARCHAR(256),
	detail TEXT,
	result_json TEXT,
	attempts INTEGER NOT NULL,
	lease_expires_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_integration_delivery UNIQUE (channel, direction, delivery_key)
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_integration_deliveries_channel "
    "ON integration_deliveries (channel)",
    "CREATE INDEX IF NOT EXISTS ix_integration_deliveries_direction "
    "ON integration_deliveries (direction)",
    "CREATE INDEX IF NOT EXISTS ix_integration_deliveries_status "
    "ON integration_deliveries (status)",
    "CREATE INDEX IF NOT EXISTS ix_integration_deliveries_lease_expires_at "
    "ON integration_deliveries (lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_integration_deliveries_created_at "
    "ON integration_deliveries (created_at)",
)


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE)
    for statement in _INDEXES:
        conn.execute(statement)

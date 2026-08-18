-- BL-P1-06 - durable integration delivery outcomes and deduplication.

CREATE TABLE IF NOT EXISTS integration_deliveries (
	id SERIAL NOT NULL,
	channel VARCHAR(32) NOT NULL,
	direction VARCHAR(16) NOT NULL,
	delivery_key VARCHAR(128) NOT NULL,
	status VARCHAR(24) NOT NULL,
	subject VARCHAR(256),
	detail TEXT,
	result_json TEXT,
	attempts INTEGER NOT NULL,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_integration_delivery UNIQUE (channel, direction, delivery_key)
);

CREATE INDEX IF NOT EXISTS ix_integration_deliveries_channel ON integration_deliveries (channel);
CREATE INDEX IF NOT EXISTS ix_integration_deliveries_direction ON integration_deliveries (direction);
CREATE INDEX IF NOT EXISTS ix_integration_deliveries_status ON integration_deliveries (status);
CREATE INDEX IF NOT EXISTS ix_integration_deliveries_lease_expires_at ON integration_deliveries (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_integration_deliveries_created_at ON integration_deliveries (created_at);

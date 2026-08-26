-- Encrypted local configuration store (PostgreSQL parity).

CREATE TABLE IF NOT EXISTS secure_settings (
	name VARCHAR(128) NOT NULL,
	ciphertext BYTEA NOT NULL,
	nonce BYTEA NOT NULL,
	salt BYTEA NOT NULL,
	version INTEGER NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (name)
);

CREATE INDEX IF NOT EXISTS ix_secure_settings_updated_at
	ON secure_settings (updated_at);

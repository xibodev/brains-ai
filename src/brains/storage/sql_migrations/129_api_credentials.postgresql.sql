-- BL-P0-01 - one row per accepted HTTP credential, bound to one principal
-- (postgresql).
--
-- The Postgres implementation of 129_api_credentials.py. Both provision the
-- same table and the same indexes, so SQLite and Postgres converge through an
-- executed delta rather than through a recorded sentinel.

CREATE TABLE IF NOT EXISTS api_credentials (
	id SERIAL NOT NULL,
	credential_id VARCHAR(40) NOT NULL,
	kind VARCHAR(16) NOT NULL,
	secret_hash VARCHAR(64) NOT NULL,
	fingerprint VARCHAR(64),
	operator_id INTEGER,
	org_id INTEGER,
	runtime_id INTEGER,
	machine_id VARCHAR(64),
	label VARCHAR(128),
	created_by_operator_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	last_used_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(operator_id) REFERENCES operators (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(runtime_id) REFERENCES runtimes (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_api_credentials_credential_id ON api_credentials (credential_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_api_credentials_secret_hash ON api_credentials (secret_hash);

CREATE INDEX IF NOT EXISTS ix_api_credentials_fingerprint ON api_credentials (fingerprint);

CREATE INDEX IF NOT EXISTS ix_api_credentials_kind ON api_credentials (kind);

CREATE INDEX IF NOT EXISTS ix_api_credentials_machine_id ON api_credentials (machine_id);

CREATE INDEX IF NOT EXISTS ix_api_credentials_operator_id ON api_credentials (operator_id);

CREATE INDEX IF NOT EXISTS ix_api_credentials_org_id ON api_credentials (org_id);

CREATE INDEX IF NOT EXISTS ix_api_credentials_runtime_id ON api_credentials (runtime_id);

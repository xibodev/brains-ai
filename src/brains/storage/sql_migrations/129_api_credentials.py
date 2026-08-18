"""BL-P0-01 - one row per accepted HTTP credential, bound to one principal.

``api_credentials``
    Authentication used to be membership in a broad key set: any key in
    ``settings.api_key`` / ``settings.api_keys`` / ``~/.brains/operator-keys``
    was accepted, and nothing downstream knew *which* one had been presented.
    This table gives every accepted credential an explicit identity - kind,
    operator, Org, Runtime, machine, expiry and revocation - so a request
    resolves to one principal instead of to "some valid key".

The raw secret is never stored. ``secret_hash`` is the sha256 hex of the raw
secret and is also the lookup key, so verification is a hash lookup rather
than a comparison against every accepted key.

Backfill is deliberately narrow and fail-closed:

* Rows are provisioned only for credentials whose *raw* value this process can
  actually read, because a hash cannot be derived from a fingerprint. The
  admin key and the per-operator key files are readable at runtime, so
  :func:`brains.authz.credentials.sync_local_credentials` adopts them on the
  first resolution rather than here, where the settings object is not
  available.
* An ``operators`` row whose key file is missing keeps working exactly as
  before *if* its key is presented, and otherwise simply never resolves. It is
  never promoted to a Runtime credential, because nothing in the store says
  which Runtime it belonged to; ``brains-ai credentials doctor`` reports those
  rows as ambiguous so an operator can re-enrol the machine deliberately.

The frozen baseline DDL predates this table, so this delta - never a
regenerated baseline - provisions it on every backend. Idempotent.
"""

from __future__ import annotations

import sqlite3

_API_CREDENTIALS = """
CREATE TABLE IF NOT EXISTS api_credentials (
	id INTEGER NOT NULL,
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
	created_at DATETIME NOT NULL,
	expires_at DATETIME,
	revoked_at DATETIME,
	last_used_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(operator_id) REFERENCES operators (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(runtime_id) REFERENCES runtimes (id)
)
"""

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_api_credentials_credential_id "
    "ON api_credentials (credential_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_api_credentials_secret_hash "
    "ON api_credentials (secret_hash)",
    "CREATE INDEX IF NOT EXISTS ix_api_credentials_fingerprint ON api_credentials (fingerprint)",
    "CREATE INDEX IF NOT EXISTS ix_api_credentials_kind ON api_credentials (kind)",
    "CREATE INDEX IF NOT EXISTS ix_api_credentials_machine_id ON api_credentials (machine_id)",
    "CREATE INDEX IF NOT EXISTS ix_api_credentials_operator_id ON api_credentials (operator_id)",
    "CREATE INDEX IF NOT EXISTS ix_api_credentials_org_id ON api_credentials (org_id)",
    "CREATE INDEX IF NOT EXISTS ix_api_credentials_runtime_id ON api_credentials (runtime_id)",
)


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_API_CREDENTIALS)
    for statement in _INDEXES:
        conn.execute(statement)

-- Record where an accepted credential came from, so rotation can revoke it
-- (postgresql).
--
-- The Postgres implementation of 131_api_credential_source.py. Both add
-- ``api_credentials.source`` and backfill it by kind, so
-- ``brains.authz.credentials.sync_local_credentials`` can revoke exactly the
-- credentials it adopted from local key files - a rotated admin key, a deleted
-- operator key file - and never a Runtime credential minted by enrollment.
--
-- Idempotent.

ALTER TABLE api_credentials ADD COLUMN IF NOT EXISTS source VARCHAR(32);

CREATE INDEX IF NOT EXISTS ix_api_credentials_source ON api_credentials (source);

UPDATE api_credentials SET source = 'enrolment'
WHERE source IS NULL AND kind = 'runtime';

UPDATE api_credentials SET source = 'local:operator_key'
WHERE source IS NULL AND kind = 'operator';

UPDATE api_credentials SET source = 'local:admin_key'
WHERE source IS NULL AND kind = 'admin';

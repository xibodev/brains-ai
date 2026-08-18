-- BL-P0-04 follow-up - adoption marker for signed heads, per-attempt lease
-- (postgresql).
--
-- The Postgres implementation of 127_signed_head_lease.py. Both
-- provision the same columns and the same backfill, so SQLite and Postgres
-- converge through an executed delta rather than through a recorded sentinel.

ALTER TABLE audit_chain_head ADD COLUMN IF NOT EXISTS adopted_version INTEGER;

ALTER TABLE audit_chain_head ADD COLUMN IF NOT EXISTS adopted_at TIMESTAMP WITH TIME ZONE;

UPDATE audit_chain_head
SET adopted_version = 1, adopted_at = NOW()
WHERE head_mac IS NOT NULL AND adopted_version IS NULL;

ALTER TABLE governed_actions ADD COLUMN IF NOT EXISTS attempt_started_at TIMESTAMP WITH TIME ZONE;

UPDATE governed_actions
SET attempt_started_at = COALESCE(executed_at, authorized_at, created_at)
WHERE attempt_started_at IS NULL;

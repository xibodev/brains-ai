-- BL-P0-04 follow-up - execution heartbeat for long-running governed actions
-- (postgresql).
--
-- The Postgres implementation of 128_execution_heartbeat.py. Both provision
-- the same column and the same backfill, so SQLite and Postgres converge
-- through an executed delta rather than through a recorded sentinel.

ALTER TABLE governed_actions ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITH TIME ZONE;

UPDATE governed_actions
SET heartbeat_at = COALESCE(executed_at, attempt_started_at, created_at)
WHERE status = 'executing' AND heartbeat_at IS NULL;

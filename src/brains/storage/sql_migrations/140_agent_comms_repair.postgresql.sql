-- Converge the one pre-release agent-comms schema draft (PostgreSQL parity).
--
-- No released Postgres store carried the leaked draft, but every post-baseline
-- migration ships equivalent backend implementations. Idempotent for either
-- shape.

CREATE TABLE IF NOT EXISTS help_request_constraints (
	request_code VARCHAR(32) NOT NULL,
	required_tool VARCHAR(64) NOT NULL,
	PRIMARY KEY (request_code),
	FOREIGN KEY(request_code) REFERENCES help_requests (code)
);

DO $$
BEGIN
	IF EXISTS (
		SELECT 1
		FROM information_schema.columns
		WHERE table_schema = current_schema()
		  AND table_name = 'help_requests'
		  AND column_name = 'required_tool'
	) THEN
		INSERT INTO help_request_constraints (request_code, required_tool)
		SELECT code, required_tool
		FROM help_requests
		WHERE required_tool IS NOT NULL AND BTRIM(required_tool) <> ''
		ON CONFLICT (request_code) DO NOTHING;

		ALTER TABLE help_requests DROP COLUMN required_tool;
	END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_topic_posts_from_workspace_id
	ON topic_posts (from_workspace_id);

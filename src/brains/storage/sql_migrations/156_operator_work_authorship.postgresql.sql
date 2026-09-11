-- Retained compatibility only; SQLite remains the supported local store.
ALTER TABLE work_assignments
    ALTER COLUMN creator_session_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS creator_kind VARCHAR(16) NOT NULL DEFAULT 'session';
ALTER TABLE coordination_proposals
    ALTER COLUMN creator_session_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS creator_kind VARCHAR(16) NOT NULL DEFAULT 'session';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'work_assignments'::regclass
          AND conname = 'ck_work_assignments_creator'
    ) THEN
        ALTER TABLE work_assignments ADD CONSTRAINT ck_work_assignments_creator CHECK (
            (creator_kind = 'session' AND creator_session_id IS NOT NULL) OR
            (creator_kind = 'operator' AND creator_session_id IS NULL)
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'coordination_proposals'::regclass
          AND conname = 'ck_coordination_creator'
    ) THEN
        ALTER TABLE coordination_proposals ADD CONSTRAINT ck_coordination_creator CHECK (
            (creator_kind = 'session' AND creator_session_id IS NOT NULL) OR
            (creator_kind = 'operator' AND creator_session_id IS NULL)
        );
    END IF;
END $$;

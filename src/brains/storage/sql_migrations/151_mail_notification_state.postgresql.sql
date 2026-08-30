DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'mailbox_attachments'::regclass
           AND conname = 'ck_mailbox_attachment_notification_mode'
    ) THEN
        ALTER TABLE mailbox_attachments
            ADD CONSTRAINT ck_mailbox_attachment_notification_mode
            CHECK (notification_mode IN ('pull', 'turn_boundary', 'immediate'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'mail_notification_attempts'::regclass
           AND conname = 'ck_mail_notification_status'
    ) THEN
        ALTER TABLE mail_notification_attempts
            ADD CONSTRAINT ck_mail_notification_status
            CHECK (status IN ('queued', 'claimed', 'delivered', 'failed'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'mail_notification_attempts'::regclass
           AND conname = 'ck_mail_notification_attempt'
    ) THEN
        ALTER TABLE mail_notification_attempts
            ADD CONSTRAINT ck_mail_notification_attempt CHECK (attempt >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'mail_notification_attempts'::regclass
           AND conname = 'ck_mail_notification_state'
    ) THEN
        ALTER TABLE mail_notification_attempts
            ADD CONSTRAINT ck_mail_notification_state CHECK (
                (status = 'queued' AND attempt = 0 AND error_code IS NULL
                 AND started_at IS NULL AND completed_at IS NULL)
                OR
                (status = 'claimed' AND attempt > 0 AND error_code IS NULL
                 AND started_at IS NOT NULL AND completed_at IS NULL)
                OR
                (status = 'delivered' AND attempt > 0 AND error_code IS NULL
                 AND started_at IS NOT NULL AND completed_at IS NOT NULL)
                OR
                (status = 'failed' AND attempt > 0 AND error_code IS NOT NULL
                 AND started_at IS NOT NULL AND completed_at IS NOT NULL)
            );
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM operator_mailbox_settings
         WHERE NOT (
            (smtp_destination_ref IS NULL AND smtp_destination_verified_at IS NULL
             AND smtp_copy_mode = 'disabled' AND smtp_consented_at IS NULL
             AND smtp_consented_by_operator_id IS NULL)
            OR
            (smtp_destination_ref IS NOT NULL AND smtp_destination_verified_at IS NULL
             AND smtp_copy_mode = 'disabled' AND smtp_consented_at IS NULL
             AND smtp_consented_by_operator_id IS NULL)
            OR
            (smtp_destination_ref IS NOT NULL AND smtp_destination_verified_at IS NOT NULL
             AND smtp_copy_mode IN ('disabled', 'notification')
             AND smtp_consented_at IS NULL AND smtp_consented_by_operator_id IS NULL)
            OR
            (smtp_destination_ref IS NOT NULL AND smtp_destination_verified_at IS NOT NULL
             AND smtp_copy_mode = 'full_body' AND smtp_consented_at IS NOT NULL
             AND smtp_consented_by_operator_id IS NOT NULL)
         )
    ) THEN
        RAISE EXCEPTION 'existing operator mailbox SMTP state is invalid';
    END IF;
    IF EXISTS (
        SELECT 1 FROM mail_smtp_outbox
         WHERE NOT (
            copy_mode IN ('notification', 'full_body') AND attempt >= 0 AND (
                (status = 'queued' AND attempt = 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NULL
                 AND error_code IS NULL AND sent_at IS NULL)
                OR
                (status = 'sending' AND attempt > 0 AND lease_owner IS NOT NULL
                 AND lease_expires_at IS NOT NULL AND next_attempt_at IS NULL
                 AND error_code IS NULL AND sent_at IS NULL)
                OR
                (status = 'retry' AND attempt > 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NOT NULL
                 AND error_code IS NOT NULL AND sent_at IS NULL)
                OR
                (status = 'sent' AND attempt > 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NULL
                 AND error_code IS NULL AND sent_at IS NOT NULL)
                OR
                (status = 'failed' AND attempt > 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NULL
                 AND error_code IS NOT NULL AND sent_at IS NULL)
                OR
                (status = 'uncertain' AND attempt >= 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NULL
                 AND error_code IS NOT NULL AND sent_at IS NULL)
                OR
                (status = 'cancelled' AND attempt >= 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NULL
                 AND error_code IS NOT NULL AND sent_at IS NULL)
            )
         )
    ) THEN
        RAISE EXCEPTION 'existing mail SMTP outbox state is invalid';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'operator_mailbox_settings'::regclass
           AND conname = 'ck_operator_mailbox_smtp_copy_mode'
    ) THEN
        ALTER TABLE operator_mailbox_settings
            ADD CONSTRAINT ck_operator_mailbox_smtp_copy_mode
            CHECK (smtp_copy_mode IN ('disabled', 'notification', 'full_body'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'operator_mailbox_settings'::regclass
           AND conname = 'ck_operator_mailbox_smtp_state'
    ) THEN
        ALTER TABLE operator_mailbox_settings
            ADD CONSTRAINT ck_operator_mailbox_smtp_state CHECK (
                (smtp_destination_ref IS NULL AND smtp_destination_verified_at IS NULL
                 AND smtp_copy_mode = 'disabled' AND smtp_consented_at IS NULL
                 AND smtp_consented_by_operator_id IS NULL)
                OR
                (smtp_destination_ref IS NOT NULL AND smtp_destination_verified_at IS NULL
                 AND smtp_copy_mode = 'disabled' AND smtp_consented_at IS NULL
                 AND smtp_consented_by_operator_id IS NULL)
                OR
                (smtp_destination_ref IS NOT NULL AND smtp_destination_verified_at IS NOT NULL
                 AND smtp_copy_mode IN ('disabled', 'notification')
                 AND smtp_consented_at IS NULL AND smtp_consented_by_operator_id IS NULL)
                OR
                (smtp_destination_ref IS NOT NULL AND smtp_destination_verified_at IS NOT NULL
                 AND smtp_copy_mode = 'full_body' AND smtp_consented_at IS NOT NULL
                 AND smtp_consented_by_operator_id IS NOT NULL)
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'mail_smtp_outbox'::regclass
           AND conname = 'ck_mail_smtp_outbox_copy_mode'
    ) THEN
        ALTER TABLE mail_smtp_outbox
            ADD CONSTRAINT ck_mail_smtp_outbox_copy_mode
            CHECK (copy_mode IN ('notification', 'full_body'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'mail_smtp_outbox'::regclass
           AND conname = 'ck_mail_smtp_outbox_status'
    ) THEN
        ALTER TABLE mail_smtp_outbox
            ADD CONSTRAINT ck_mail_smtp_outbox_status
            CHECK (status IN ('queued', 'sending', 'retry', 'sent', 'failed',
                              'uncertain', 'cancelled'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'mail_smtp_outbox'::regclass
           AND conname = 'ck_mail_smtp_outbox_attempt'
    ) THEN
        ALTER TABLE mail_smtp_outbox
            ADD CONSTRAINT ck_mail_smtp_outbox_attempt CHECK (attempt >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'mail_smtp_outbox'::regclass
           AND conname = 'ck_mail_smtp_outbox_state'
    ) THEN
        ALTER TABLE mail_smtp_outbox
            ADD CONSTRAINT ck_mail_smtp_outbox_state CHECK (
                (status = 'queued' AND attempt = 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NULL
                 AND error_code IS NULL AND sent_at IS NULL)
                OR
                (status = 'sending' AND attempt > 0 AND lease_owner IS NOT NULL
                 AND lease_expires_at IS NOT NULL AND next_attempt_at IS NULL
                 AND error_code IS NULL AND sent_at IS NULL)
                OR
                (status = 'retry' AND attempt > 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NOT NULL
                 AND error_code IS NOT NULL AND sent_at IS NULL)
                OR
                (status = 'sent' AND attempt > 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NULL
                 AND error_code IS NULL AND sent_at IS NOT NULL)
                OR
                (status = 'failed' AND attempt > 0
                 AND lease_owner IS NULL AND lease_expires_at IS NULL
                 AND next_attempt_at IS NULL AND error_code IS NOT NULL
                 AND sent_at IS NULL)
                OR
                (status = 'uncertain' AND attempt >= 0
                 AND lease_owner IS NULL AND lease_expires_at IS NULL
                 AND next_attempt_at IS NULL AND error_code IS NOT NULL
                 AND sent_at IS NULL)
                OR
                (status = 'cancelled' AND attempt >= 0 AND lease_owner IS NULL
                 AND lease_expires_at IS NULL AND next_attempt_at IS NULL
                 AND error_code IS NOT NULL AND sent_at IS NULL)
            );
    END IF;
END $$;

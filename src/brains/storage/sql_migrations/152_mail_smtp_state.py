"""Constrain operator SMTP consent and outbox state (SQLite)."""

import sqlite3

SETTING_STATE = """
(
    (NEW.smtp_destination_ref IS NULL AND NEW.smtp_destination_verified_at IS NULL
     AND NEW.smtp_copy_mode = 'disabled' AND NEW.smtp_consented_at IS NULL
     AND NEW.smtp_consented_by_operator_id IS NULL)
    OR
    (NEW.smtp_destination_ref IS NOT NULL AND NEW.smtp_destination_verified_at IS NULL
     AND NEW.smtp_copy_mode = 'disabled' AND NEW.smtp_consented_at IS NULL
     AND NEW.smtp_consented_by_operator_id IS NULL)
    OR
    (NEW.smtp_destination_ref IS NOT NULL AND NEW.smtp_destination_verified_at IS NOT NULL
     AND NEW.smtp_copy_mode IN ('disabled', 'notification')
     AND NEW.smtp_consented_at IS NULL AND NEW.smtp_consented_by_operator_id IS NULL)
    OR
    (NEW.smtp_destination_ref IS NOT NULL AND NEW.smtp_destination_verified_at IS NOT NULL
     AND NEW.smtp_copy_mode = 'full_body' AND NEW.smtp_consented_at IS NOT NULL
     AND NEW.smtp_consented_by_operator_id IS NOT NULL)
)
"""

OUTBOX_STATE = """
(
    NEW.copy_mode IN ('notification', 'full_body') AND NEW.attempt >= 0 AND (
        (NEW.status = 'queued' AND NEW.attempt = 0 AND NEW.lease_owner IS NULL
         AND NEW.lease_expires_at IS NULL AND NEW.next_attempt_at IS NULL
         AND NEW.error_code IS NULL AND NEW.sent_at IS NULL)
        OR
        (NEW.status = 'sending' AND NEW.attempt > 0 AND NEW.lease_owner IS NOT NULL
         AND NEW.lease_expires_at IS NOT NULL AND NEW.next_attempt_at IS NULL
         AND NEW.error_code IS NULL AND NEW.sent_at IS NULL)
        OR
        (NEW.status = 'retry' AND NEW.attempt > 0 AND NEW.lease_owner IS NULL
         AND NEW.lease_expires_at IS NULL AND NEW.next_attempt_at IS NOT NULL
         AND NEW.error_code IS NOT NULL AND NEW.sent_at IS NULL)
        OR
        (NEW.status = 'sent' AND NEW.attempt > 0 AND NEW.lease_owner IS NULL
         AND NEW.lease_expires_at IS NULL AND NEW.next_attempt_at IS NULL
         AND NEW.error_code IS NULL AND NEW.sent_at IS NOT NULL)
        OR
        (NEW.status = 'failed' AND NEW.attempt > 0
         AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
         AND NEW.next_attempt_at IS NULL AND NEW.error_code IS NOT NULL
         AND NEW.sent_at IS NULL)
        OR
        (NEW.status = 'uncertain' AND NEW.attempt >= 0
         AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
         AND NEW.next_attempt_at IS NULL AND NEW.error_code IS NOT NULL
         AND NEW.sent_at IS NULL)
        OR
        (NEW.status = 'cancelled' AND NEW.attempt >= 0 AND NEW.lease_owner IS NULL
         AND NEW.lease_expires_at IS NULL AND NEW.next_attempt_at IS NULL
         AND NEW.error_code IS NOT NULL AND NEW.sent_at IS NULL)
    )
)
"""


def upgrade(conn: sqlite3.Connection) -> None:
    invalid_settings = conn.execute(
        f"SELECT COUNT(*) FROM operator_mailbox_settings AS NEW WHERE NOT {SETTING_STATE}"
    ).fetchone()
    invalid_outbox = conn.execute(
        f"SELECT COUNT(*) FROM mail_smtp_outbox AS NEW WHERE NOT {OUTBOX_STATE}"
    ).fetchone()
    if invalid_settings and invalid_settings[0]:
        raise sqlite3.IntegrityError("existing operator mailbox SMTP state is invalid")
    if invalid_outbox and invalid_outbox[0]:
        raise sqlite3.IntegrityError("existing mail SMTP outbox state is invalid")
    statements = (
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_operator_mailbox_smtp_state_insert
        BEFORE INSERT ON operator_mailbox_settings
        FOR EACH ROW WHEN NOT {SETTING_STATE}
        BEGIN SELECT RAISE(ABORT, 'invalid operator mailbox SMTP state'); END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_operator_mailbox_smtp_state_update
        BEFORE UPDATE ON operator_mailbox_settings
        FOR EACH ROW WHEN NOT {SETTING_STATE}
        BEGIN SELECT RAISE(ABORT, 'invalid operator mailbox SMTP state'); END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_mail_smtp_outbox_state_insert
        BEFORE INSERT ON mail_smtp_outbox
        FOR EACH ROW WHEN NOT {OUTBOX_STATE}
        BEGIN SELECT RAISE(ABORT, 'invalid mail SMTP outbox state'); END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_mail_smtp_outbox_state_update
        BEFORE UPDATE ON mail_smtp_outbox
        FOR EACH ROW WHEN NOT {OUTBOX_STATE}
        BEGIN SELECT RAISE(ABORT, 'invalid mail SMTP outbox state'); END
        """,
    )
    for statement in statements:
        conn.execute(statement)

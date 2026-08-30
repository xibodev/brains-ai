"""Constrain durable mailbox notification attempt state (SQLite)."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TRIGGER IF NOT EXISTS trg_mailbox_attachment_notification_mode_insert
        BEFORE INSERT ON mailbox_attachments
        FOR EACH ROW
        WHEN NEW.notification_mode NOT IN ('pull', 'turn_boundary', 'immediate')
        BEGIN
            SELECT RAISE(ABORT, 'invalid mailbox notification mode');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_mailbox_attachment_notification_mode_update
        BEFORE UPDATE OF notification_mode ON mailbox_attachments
        FOR EACH ROW
        WHEN NEW.notification_mode NOT IN ('pull', 'turn_boundary', 'immediate')
        BEGIN
            SELECT RAISE(ABORT, 'invalid mailbox notification mode');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_mail_notification_state_insert
        BEFORE INSERT ON mail_notification_attempts
        FOR EACH ROW
        WHEN NOT (
            NEW.attempt >= 0 AND (
                (NEW.status = 'queued' AND NEW.attempt = 0
                 AND NEW.error_code IS NULL AND NEW.started_at IS NULL
                 AND NEW.completed_at IS NULL)
                OR
                (NEW.status = 'claimed' AND NEW.attempt > 0
                 AND NEW.error_code IS NULL AND NEW.started_at IS NOT NULL
                 AND NEW.completed_at IS NULL)
                OR
                (NEW.status = 'delivered' AND NEW.attempt > 0
                 AND NEW.error_code IS NULL AND NEW.started_at IS NOT NULL
                 AND NEW.completed_at IS NOT NULL)
                OR
                (NEW.status = 'failed' AND NEW.attempt > 0
                 AND NEW.error_code IS NOT NULL AND NEW.started_at IS NOT NULL
                 AND NEW.completed_at IS NOT NULL)
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid mail notification state');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_mail_notification_state_update
        BEFORE UPDATE OF status, attempt, error_code, started_at, completed_at
        ON mail_notification_attempts
        FOR EACH ROW
        WHEN NOT (
            NEW.attempt >= 0 AND (
                (NEW.status = 'queued' AND NEW.attempt = 0
                 AND NEW.error_code IS NULL AND NEW.started_at IS NULL
                 AND NEW.completed_at IS NULL)
                OR
                (NEW.status = 'claimed' AND NEW.attempt > 0
                 AND NEW.error_code IS NULL AND NEW.started_at IS NOT NULL
                 AND NEW.completed_at IS NULL)
                OR
                (NEW.status = 'delivered' AND NEW.attempt > 0
                 AND NEW.error_code IS NULL AND NEW.started_at IS NOT NULL
                 AND NEW.completed_at IS NOT NULL)
                OR
                (NEW.status = 'failed' AND NEW.attempt > 0
                 AND NEW.error_code IS NOT NULL AND NEW.started_at IS NOT NULL
                 AND NEW.completed_at IS NOT NULL)
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid mail notification state');
        END
        """,
    )
    for statement in statements:
        conn.execute(statement)

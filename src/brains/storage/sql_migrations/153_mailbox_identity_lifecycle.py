"""Persist adapter provenance and hash-only managed-binding transition intents."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mailboxes)")}
    if "adapter_provenance" not in columns:
        conn.execute("ALTER TABLE mailboxes ADD COLUMN adapter_provenance VARCHAR(64)")
    conn.execute(
        "UPDATE mailboxes SET adapter_provenance = tool "
        "WHERE kind = 'agent' AND adapter_provenance IS NULL"
    )
    attachment_columns = {row[1] for row in conn.execute("PRAGMA table_info(mailbox_attachments)")}
    if "adapter_provenance" not in attachment_columns:
        conn.execute("ALTER TABLE mailbox_attachments ADD COLUMN adapter_provenance VARCHAR(64)")
    conn.execute(
        "UPDATE mailbox_attachments SET adapter_provenance = ("
        "SELECT adapter_provenance FROM mailboxes "
        "WHERE mailboxes.id = mailbox_attachments.mailbox_id) "
        "WHERE adapter_provenance IS NULL"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mailbox_binding_transitions (
            mailbox_id INTEGER NOT NULL PRIMARY KEY,
            operation VARCHAR(16) NOT NULL,
            from_binding_hash VARCHAR(64),
            to_binding_hash VARCHAR(64),
            to_binding_version INTEGER,
            binding_file VARCHAR(1024) NOT NULL,
            session_id VARCHAR(32) NOT NULL,
            owner_pid INTEGER NOT NULL,
            owner_process_instance VARCHAR(128) NOT NULL,
            notification_mode VARCHAR(24) NOT NULL DEFAULT 'pull',
            created_at DATETIME NOT NULL,
            FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id),
            FOREIGN KEY(session_id) REFERENCES agent_sessions (id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_mailbox_binding_transitions_created_at "
        "ON mailbox_binding_transitions (created_at)"
    )

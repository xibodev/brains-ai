"""Reserve durable mailbox, delivery, notification, and SMTP state (SQLite)."""

from __future__ import annotations

import sqlite3

DDL = """
CREATE TABLE IF NOT EXISTS mailboxes (
    id INTEGER NOT NULL PRIMARY KEY,
    address VARCHAR(512) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    workspace_id INTEGER,
    tool VARCHAR(64),
    native_tool_session_id VARCHAR(256),
    owner_operator_id INTEGER NOT NULL,
    operator_slot INTEGER,
    binding_key_hash VARCHAR(64),
    binding_key_version INTEGER,
    binding_rotated_at DATETIME,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    retired_at DATETIME,
    CONSTRAINT uq_mailbox_address UNIQUE (address),
    CONSTRAINT uq_mailbox_agent_address UNIQUE (
        workspace_id, tool, native_tool_session_id
    ),
    CONSTRAINT uq_mailbox_operator_slot UNIQUE (owner_operator_id, operator_slot),
    CONSTRAINT uq_mailbox_binding_key_hash UNIQUE (binding_key_hash),
    CONSTRAINT ck_mailbox_operator_slot CHECK (operator_slot IS NULL OR operator_slot = 1),
    CONSTRAINT ck_mailbox_binding_version CHECK (
        binding_key_version IS NULL OR binding_key_version > 0
    ),
    CONSTRAINT ck_mailbox_identity_shape CHECK (
        (kind = 'agent' AND workspace_id IS NOT NULL AND tool IS NOT NULL
         AND native_tool_session_id IS NOT NULL AND binding_key_hash IS NOT NULL
         AND binding_key_version IS NOT NULL AND operator_slot IS NULL)
        OR
        (kind = 'operator' AND workspace_id IS NULL AND tool IS NULL
         AND native_tool_session_id IS NULL AND binding_key_hash IS NULL
         AND binding_key_version IS NULL AND binding_rotated_at IS NULL
         AND operator_slot = 1)
    ),
    FOREIGN KEY(workspace_id) REFERENCES workspaces (id),
    FOREIGN KEY(owner_operator_id) REFERENCES operators (id)
);
CREATE INDEX IF NOT EXISTS ix_mailboxes_address ON mailboxes (address);
CREATE INDEX IF NOT EXISTS ix_mailboxes_kind ON mailboxes (kind);
CREATE INDEX IF NOT EXISTS ix_mailboxes_workspace_id ON mailboxes (workspace_id);
CREATE INDEX IF NOT EXISTS ix_mailboxes_tool ON mailboxes (tool);
CREATE INDEX IF NOT EXISTS ix_mailboxes_native_tool_session_id
    ON mailboxes (native_tool_session_id);
CREATE INDEX IF NOT EXISTS ix_mailboxes_owner_operator_id ON mailboxes (owner_operator_id);
CREATE INDEX IF NOT EXISTS ix_mailboxes_status ON mailboxes (status);
CREATE INDEX IF NOT EXISTS ix_mailboxes_created_at ON mailboxes (created_at);

CREATE TABLE IF NOT EXISTS mailbox_attachments (
    id INTEGER NOT NULL PRIMARY KEY,
    mailbox_id INTEGER NOT NULL,
    session_id VARCHAR(32) NOT NULL,
    active_slot INTEGER,
    notification_mode VARCHAR(24) NOT NULL DEFAULT 'pull',
    last_seen_delivery_id INTEGER NOT NULL DEFAULT 0,
    attached_at DATETIME NOT NULL,
    detached_at DATETIME,
    detach_reason VARCHAR(64),
    CONSTRAINT uq_mailbox_attachment_session UNIQUE (session_id),
    CONSTRAINT uq_mailbox_attachment_current UNIQUE (mailbox_id, active_slot),
    CONSTRAINT ck_mailbox_attachment_active_slot CHECK (
        active_slot IS NULL OR active_slot = 1
    ),
    CONSTRAINT ck_mailbox_attachment_state CHECK (
        (active_slot IS NOT NULL AND active_slot = 1
         AND detached_at IS NULL AND detach_reason IS NULL)
        OR
        (active_slot IS NULL AND detached_at IS NOT NULL AND detach_reason IS NOT NULL)
    ),
    CONSTRAINT ck_mailbox_attachment_cursor CHECK (last_seen_delivery_id >= 0),
    FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id),
    FOREIGN KEY(session_id) REFERENCES agent_sessions (id)
);
CREATE INDEX IF NOT EXISTS ix_mailbox_attachments_mailbox_id
    ON mailbox_attachments (mailbox_id);
CREATE INDEX IF NOT EXISTS ix_mailbox_attachments_session_id
    ON mailbox_attachments (session_id);
CREATE INDEX IF NOT EXISTS ix_mailbox_attachments_notification_mode
    ON mailbox_attachments (notification_mode);
CREATE INDEX IF NOT EXISTS ix_mailbox_attachments_attached_at
    ON mailbox_attachments (attached_at);
CREATE INDEX IF NOT EXISTS ix_mailbox_attachments_detached_at
    ON mailbox_attachments (detached_at);

CREATE TABLE IF NOT EXISTS mail_threads (
    id INTEGER NOT NULL PRIMARY KEY,
    thread_id VARCHAR(40) NOT NULL,
    origin_workspace_id INTEGER NOT NULL,
    started_by_mailbox_id INTEGER NOT NULL,
    subject VARCHAR(256) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_mail_thread_id UNIQUE (thread_id),
    FOREIGN KEY(origin_workspace_id) REFERENCES workspaces (id),
    FOREIGN KEY(started_by_mailbox_id) REFERENCES mailboxes (id)
);
CREATE INDEX IF NOT EXISTS ix_mail_threads_thread_id ON mail_threads (thread_id);
CREATE INDEX IF NOT EXISTS ix_mail_threads_origin_workspace_id
    ON mail_threads (origin_workspace_id);
CREATE INDEX IF NOT EXISTS ix_mail_threads_started_by_mailbox_id
    ON mail_threads (started_by_mailbox_id);
CREATE INDEX IF NOT EXISTS ix_mail_threads_created_at ON mail_threads (created_at);
CREATE INDEX IF NOT EXISTS ix_mail_threads_updated_at ON mail_threads (updated_at);

CREATE TABLE IF NOT EXISTS mail_messages (
    id INTEGER NOT NULL PRIMARY KEY,
    message_id VARCHAR(40) NOT NULL,
    operation_key VARCHAR(160) NOT NULL,
    thread_id INTEGER NOT NULL,
    sender_mailbox_id INTEGER NOT NULL,
    sender_session_id VARCHAR(32),
    origin_workspace_id INTEGER NOT NULL,
    audience VARCHAR(16) NOT NULL,
    in_reply_to_id INTEGER,
    forwarded_from_id INTEGER,
    kind VARCHAR(32) NOT NULL DEFAULT 'info',
    subject VARCHAR(256) NOT NULL,
    body TEXT,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_mail_message_id UNIQUE (message_id),
    CONSTRAINT uq_mail_message_operation_key UNIQUE (operation_key),
    CONSTRAINT ck_mail_message_audience CHECK (audience IN ('direct', 'broadcast')),
    FOREIGN KEY(thread_id) REFERENCES mail_threads (id),
    FOREIGN KEY(sender_mailbox_id) REFERENCES mailboxes (id),
    FOREIGN KEY(sender_session_id) REFERENCES agent_sessions (id),
    FOREIGN KEY(origin_workspace_id) REFERENCES workspaces (id),
    FOREIGN KEY(in_reply_to_id) REFERENCES mail_messages (id),
    FOREIGN KEY(forwarded_from_id) REFERENCES mail_messages (id)
);
CREATE INDEX IF NOT EXISTS ix_mail_messages_message_id ON mail_messages (message_id);
CREATE INDEX IF NOT EXISTS ix_mail_messages_operation_key ON mail_messages (operation_key);
CREATE INDEX IF NOT EXISTS ix_mail_messages_thread_id ON mail_messages (thread_id);
CREATE INDEX IF NOT EXISTS ix_mail_messages_sender_mailbox_id
    ON mail_messages (sender_mailbox_id);
CREATE INDEX IF NOT EXISTS ix_mail_messages_sender_session_id
    ON mail_messages (sender_session_id);
CREATE INDEX IF NOT EXISTS ix_mail_messages_origin_workspace_id
    ON mail_messages (origin_workspace_id);
CREATE INDEX IF NOT EXISTS ix_mail_messages_audience ON mail_messages (audience);
CREATE INDEX IF NOT EXISTS ix_mail_messages_in_reply_to_id
    ON mail_messages (in_reply_to_id);
CREATE INDEX IF NOT EXISTS ix_mail_messages_forwarded_from_id
    ON mail_messages (forwarded_from_id);
CREATE INDEX IF NOT EXISTS ix_mail_messages_kind ON mail_messages (kind);
CREATE INDEX IF NOT EXISTS ix_mail_messages_created_at ON mail_messages (created_at);

CREATE TABLE IF NOT EXISTS mail_deliveries (
    id INTEGER NOT NULL PRIMARY KEY,
    delivery_id VARCHAR(40) NOT NULL,
    message_id INTEGER NOT NULL,
    recipient_mailbox_id INTEGER NOT NULL,
    recipient_workspace_id INTEGER,
    accepted_at DATETIME NOT NULL,
    read_at DATETIME,
    read_by_session_id VARCHAR(32),
    read_by_operator_id INTEGER,
    read_channel VARCHAR(16),
    CONSTRAINT uq_mail_delivery_id UNIQUE (delivery_id),
    CONSTRAINT uq_mail_delivery_recipient UNIQUE (message_id, recipient_mailbox_id),
    CONSTRAINT ck_mail_delivery_read_attribution CHECK (
        (read_at IS NULL AND read_by_session_id IS NULL
         AND read_by_operator_id IS NULL AND read_channel IS NULL)
        OR
        (read_at IS NOT NULL AND read_channel IS NOT NULL AND
         ((read_by_session_id IS NOT NULL AND read_by_operator_id IS NULL) OR
          (read_by_session_id IS NULL AND read_by_operator_id IS NOT NULL)))
    ),
    FOREIGN KEY(message_id) REFERENCES mail_messages (id),
    FOREIGN KEY(recipient_mailbox_id) REFERENCES mailboxes (id),
    FOREIGN KEY(recipient_workspace_id) REFERENCES workspaces (id),
    FOREIGN KEY(read_by_session_id) REFERENCES agent_sessions (id),
    FOREIGN KEY(read_by_operator_id) REFERENCES operators (id)
);
CREATE INDEX IF NOT EXISTS ix_mail_deliveries_delivery_id
    ON mail_deliveries (delivery_id);
CREATE INDEX IF NOT EXISTS ix_mail_deliveries_message_id ON mail_deliveries (message_id);
CREATE INDEX IF NOT EXISTS ix_mail_deliveries_recipient_mailbox_id
    ON mail_deliveries (recipient_mailbox_id);
CREATE INDEX IF NOT EXISTS ix_mail_deliveries_recipient_workspace_id
    ON mail_deliveries (recipient_workspace_id);
CREATE INDEX IF NOT EXISTS ix_mail_deliveries_accepted_at ON mail_deliveries (accepted_at);
CREATE INDEX IF NOT EXISTS ix_mail_deliveries_read_at ON mail_deliveries (read_at);
CREATE INDEX IF NOT EXISTS ix_mail_deliveries_read_by_session_id
    ON mail_deliveries (read_by_session_id);
CREATE INDEX IF NOT EXISTS ix_mail_deliveries_read_by_operator_id
    ON mail_deliveries (read_by_operator_id);

CREATE TABLE IF NOT EXISTS mail_notification_attempts (
    id INTEGER NOT NULL PRIMARY KEY,
    notification_id VARCHAR(40) NOT NULL,
    idempotency_key VARCHAR(160) NOT NULL,
    delivery_id INTEGER NOT NULL,
    attachment_id INTEGER,
    adapter VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    attempt INTEGER NOT NULL DEFAULT 0,
    error_code VARCHAR(64),
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    completed_at DATETIME,
    CONSTRAINT uq_mail_notification_id UNIQUE (notification_id),
    CONSTRAINT uq_mail_notification_idempotency_key UNIQUE (idempotency_key),
    FOREIGN KEY(delivery_id) REFERENCES mail_deliveries (id),
    FOREIGN KEY(attachment_id) REFERENCES mailbox_attachments (id)
);
CREATE INDEX IF NOT EXISTS ix_mail_notification_attempts_notification_id
    ON mail_notification_attempts (notification_id);
CREATE INDEX IF NOT EXISTS ix_mail_notification_attempts_idempotency_key
    ON mail_notification_attempts (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_mail_notification_attempts_delivery_id
    ON mail_notification_attempts (delivery_id);
CREATE INDEX IF NOT EXISTS ix_mail_notification_attempts_attachment_id
    ON mail_notification_attempts (attachment_id);
CREATE INDEX IF NOT EXISTS ix_mail_notification_attempts_adapter
    ON mail_notification_attempts (adapter);
CREATE INDEX IF NOT EXISTS ix_mail_notification_attempts_status
    ON mail_notification_attempts (status);
CREATE INDEX IF NOT EXISTS ix_mail_notification_attempts_created_at
    ON mail_notification_attempts (created_at);

CREATE TABLE IF NOT EXISTS operator_mailbox_settings (
    mailbox_id INTEGER NOT NULL PRIMARY KEY,
    smtp_destination_ref VARCHAR(160),
    smtp_destination_verified_at DATETIME,
    smtp_copy_mode VARCHAR(16) NOT NULL DEFAULT 'disabled',
    smtp_consented_at DATETIME,
    smtp_consented_by_operator_id INTEGER,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id),
    FOREIGN KEY(smtp_consented_by_operator_id) REFERENCES operators (id)
);
CREATE INDEX IF NOT EXISTS ix_operator_mailbox_settings_smtp_copy_mode
    ON operator_mailbox_settings (smtp_copy_mode);

CREATE TABLE IF NOT EXISTS mail_smtp_outbox (
    id INTEGER NOT NULL PRIMARY KEY,
    outbox_id VARCHAR(40) NOT NULL,
    idempotency_key VARCHAR(160) NOT NULL,
    delivery_id INTEGER NOT NULL,
    recipient_mailbox_id INTEGER NOT NULL,
    smtp_destination_ref VARCHAR(160) NOT NULL,
    copy_mode VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner VARCHAR(64),
    lease_expires_at DATETIME,
    next_attempt_at DATETIME,
    error_code VARCHAR(64),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    sent_at DATETIME,
    CONSTRAINT uq_mail_smtp_outbox_id UNIQUE (outbox_id),
    CONSTRAINT uq_mail_smtp_outbox_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT uq_mail_smtp_outbox_delivery UNIQUE (delivery_id),
    FOREIGN KEY(delivery_id) REFERENCES mail_deliveries (id),
    FOREIGN KEY(recipient_mailbox_id) REFERENCES mailboxes (id)
);
CREATE INDEX IF NOT EXISTS ix_mail_smtp_outbox_outbox_id
    ON mail_smtp_outbox (outbox_id);
CREATE INDEX IF NOT EXISTS ix_mail_smtp_outbox_idempotency_key
    ON mail_smtp_outbox (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_mail_smtp_outbox_delivery_id
    ON mail_smtp_outbox (delivery_id);
CREATE INDEX IF NOT EXISTS ix_mail_smtp_outbox_recipient_mailbox_id
    ON mail_smtp_outbox (recipient_mailbox_id);
CREATE INDEX IF NOT EXISTS ix_mail_smtp_outbox_status
    ON mail_smtp_outbox (status);
CREATE INDEX IF NOT EXISTS ix_mail_smtp_outbox_lease_expires_at
    ON mail_smtp_outbox (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_mail_smtp_outbox_next_attempt_at
    ON mail_smtp_outbox (next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_mail_smtp_outbox_created_at
    ON mail_smtp_outbox (created_at);

CREATE TABLE IF NOT EXISTS mail_legacy_records (
    id INTEGER NOT NULL PRIMARY KEY,
    source_table VARCHAR(64) NOT NULL,
    source_pk VARCHAR(64) NOT NULL,
    disposition VARCHAR(24) NOT NULL DEFAULT 'unverified',
    reason_code VARCHAR(64) NOT NULL,
    target_ref VARCHAR(64),
    classified_at DATETIME NOT NULL,
    CONSTRAINT uq_mail_legacy_source UNIQUE (source_table, source_pk)
);
CREATE INDEX IF NOT EXISTS ix_mail_legacy_records_source_table
    ON mail_legacy_records (source_table);
CREATE INDEX IF NOT EXISTS ix_mail_legacy_records_disposition
    ON mail_legacy_records (disposition);
CREATE INDEX IF NOT EXISTS ix_mail_legacy_records_reason_code
    ON mail_legacy_records (reason_code);
CREATE INDEX IF NOT EXISTS ix_mail_legacy_records_classified_at
    ON mail_legacy_records (classified_at);
"""


def upgrade(conn: sqlite3.Connection) -> None:
    buffer: list[str] = []
    for line in DDL.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer)
        if not sqlite3.complete_statement(candidate):
            continue
        statement = candidate.strip()
        buffer = []
        if statement:
            conn.execute(statement)
    if "".join(buffer).strip():
        raise ValueError("incomplete SQL in durable mailbox migration")
    # The migration cannot prove a durable address from an ephemeral recipient
    # or free-form tool link. Inventory the row only; never copy message bodies
    # or fabricate mailbox ownership.
    conn.execute(
        """
        INSERT OR IGNORE INTO mail_legacy_records (
            source_table, source_pk, disposition, reason_code, classified_at
        )
        SELECT 'mailbox_messages', CAST(id AS TEXT), 'unverified',
               'durable_recipient_unproven', CURRENT_TIMESTAMP
          FROM mailbox_messages
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO mail_legacy_records (
            source_table, source_pk, disposition, reason_code, classified_at
        )
        SELECT 'tool_session_links', CAST(id AS TEXT), 'unverified',
               'native_identity_unproven', CURRENT_TIMESTAMP
          FROM tool_session_links
        """
    )

ALTER TABLE mailboxes
    ADD COLUMN IF NOT EXISTS adapter_provenance VARCHAR(64);

UPDATE mailboxes
SET adapter_provenance = tool
WHERE kind = 'agent' AND adapter_provenance IS NULL;

ALTER TABLE mailbox_attachments
    ADD COLUMN IF NOT EXISTS adapter_provenance VARCHAR(64);

UPDATE mailbox_attachments AS attachment
SET adapter_provenance = mailbox.adapter_provenance
FROM mailboxes AS mailbox
WHERE mailbox.id = attachment.mailbox_id
  AND attachment.adapter_provenance IS NULL;

CREATE TABLE IF NOT EXISTS mailbox_binding_transitions (
    mailbox_id INTEGER PRIMARY KEY REFERENCES mailboxes (id),
    operation VARCHAR(16) NOT NULL,
    from_binding_hash VARCHAR(64),
    to_binding_hash VARCHAR(64),
    to_binding_version INTEGER,
    binding_file VARCHAR(1024) NOT NULL,
    session_id VARCHAR(32) NOT NULL REFERENCES agent_sessions (id),
    owner_pid INTEGER NOT NULL,
    owner_process_instance VARCHAR(128) NOT NULL,
    notification_mode VARCHAR(24) NOT NULL DEFAULT 'pull',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_mailbox_binding_transitions_created_at
    ON mailbox_binding_transitions (created_at);

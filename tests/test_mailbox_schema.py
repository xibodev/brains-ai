from __future__ import annotations

import importlib
import sqlite3

import pytest
from sqlalchemy import create_engine

from brains.storage.migrations import _run_sqlite_transaction


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY);
        CREATE TABLE operators (id INTEGER PRIMARY KEY);
        CREATE TABLE agent_sessions (id VARCHAR(32) PRIMARY KEY);
        CREATE TABLE mailbox_messages (
            id INTEGER PRIMARY KEY,
            subject VARCHAR(256),
            body TEXT
        );
        CREATE TABLE tool_session_links (
            id INTEGER PRIMARY KEY,
            tool VARCHAR(64),
            tool_session_id VARCHAR(256)
        );
        INSERT INTO workspaces (id) VALUES (1), (2);
        INSERT INTO operators (id) VALUES (1), (2);
        INSERT INTO agent_sessions (id) VALUES ('ses_one'), ('ses_two');
        """
    )
    migration = importlib.import_module("brains.storage.sql_migrations.150_durable_mailboxes")
    migration.upgrade(conn)
    return conn


def _agent_mailbox(conn: sqlite3.Connection, *, address: str = "codex:native@alpha") -> int:
    cursor = conn.execute(
        """
        INSERT INTO mailboxes (
            address, kind, workspace_id, tool, native_session_id,
            owner_operator_id, binding_key_hash, binding_key_version,
            status, created_at, updated_at
        ) VALUES (?, 'agent', 1, 'codex', 'native', 1, ?, 1, 'active',
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (address, "a" * 64),
    )
    return int(cursor.lastrowid)


def test_migration_inventories_legacy_rows_without_copying_content() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY);
        CREATE TABLE operators (id INTEGER PRIMARY KEY);
        CREATE TABLE agent_sessions (id VARCHAR(32) PRIMARY KEY);
        CREATE TABLE mailbox_messages (
            id INTEGER PRIMARY KEY,
            subject VARCHAR(256),
            body TEXT
        );
        CREATE TABLE tool_session_links (
            id INTEGER PRIMARY KEY,
            tool VARCHAR(64),
            tool_session_id VARCHAR(256)
        );
        INSERT INTO mailbox_messages (id, subject, body)
        VALUES (7, 'legacy subject', 'legacy private body');
        INSERT INTO tool_session_links (id, tool, tool_session_id)
        VALUES (9, 'opencode', 'current');
        """
    )
    migration = importlib.import_module("brains.storage.sql_migrations.150_durable_mailboxes")

    migration.upgrade(conn)
    migration.upgrade(conn)

    assert conn.execute("SELECT subject, body FROM mailbox_messages WHERE id = 7").fetchone() == (
        "legacy subject",
        "legacy private body",
    )
    assert conn.execute("SELECT COUNT(*) FROM mailboxes").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM mail_messages").fetchone() == (0,)
    assert conn.execute(
        """
        SELECT source_table, source_pk, disposition, reason_code, target_ref
          FROM mail_legacy_records
         ORDER BY source_table
        """
    ).fetchall() == [
        (
            "mailbox_messages",
            "7",
            "unverified",
            "durable_recipient_unproven",
            None,
        ),
        (
            "tool_session_links",
            "9",
            "unverified",
            "native_identity_unproven",
            None,
        ),
    ]


def test_migration_rolls_back_all_tables_when_legacy_inventory_fails(tmp_path) -> None:
    path = tmp_path / "rollback.sqlite"
    seed = sqlite3.connect(path)
    seed.executescript(
        """
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY);
        CREATE TABLE operators (id INTEGER PRIMARY KEY);
        CREATE TABLE agent_sessions (id VARCHAR(32) PRIMARY KEY);
        CREATE TABLE mailbox_messages (id INTEGER PRIMARY KEY);
        """
    )
    seed.close()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    migration = importlib.import_module("brains.storage.sql_migrations.150_durable_mailboxes")

    with pytest.raises(sqlite3.OperationalError, match="tool_session_links"):
        _run_sqlite_transaction(engine, migration.upgrade)

    inspect = sqlite3.connect(path)
    try:
        names = {
            row[0]
            for row in inspect.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        inspect.close()
        engine.dispose()
    assert (
        not {
            "mailboxes",
            "mailbox_attachments",
            "mail_threads",
            "mail_messages",
            "mail_deliveries",
            "mail_notification_attempts",
            "operator_mailbox_settings",
            "mail_smtp_outbox",
            "mail_legacy_records",
        }
        & names
    )


def test_agent_and_operator_identity_shapes_fail_closed() -> None:
    conn = _connection()
    _agent_mailbox(conn)

    with pytest.raises(sqlite3.IntegrityError):
        _agent_mailbox(conn, address="codex:native@other-spelling")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO mailboxes (
                address, kind, workspace_id, tool, native_session_id,
                owner_operator_id, status, created_at, updated_at
            ) VALUES ('codex:no-binding@alpha', 'agent', 1, 'codex',
                      'no-binding', 1, 'active', CURRENT_TIMESTAMP,
                      CURRENT_TIMESTAMP)
            """
        )

    conn.execute(
        """
        INSERT INTO mailboxes (
            address, kind, owner_operator_id, operator_slot, status,
            created_at, updated_at
        ) VALUES ('operator:one@brains', 'operator', 1, 1, 'active',
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO mailboxes (
                address, kind, owner_operator_id, operator_slot, status,
                created_at, updated_at
            ) VALUES ('operator:duplicate@brains', 'operator', 1, 1, 'active',
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )


def test_only_one_current_attachment_exists_per_mailbox() -> None:
    conn = _connection()
    mailbox_id = _agent_mailbox(conn)
    conn.execute(
        """
        INSERT INTO mailbox_attachments (
            mailbox_id, session_id, active_slot, notification_mode, attached_at
        ) VALUES (?, 'ses_one', 1, 'pull', CURRENT_TIMESTAMP)
        """,
        (mailbox_id,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO mailbox_attachments (
                mailbox_id, session_id, active_slot, notification_mode, attached_at
            ) VALUES (?, 'ses_two', 1, 'pull', CURRENT_TIMESTAMP)
            """,
            (mailbox_id,),
        )

    conn.execute(
        """
        UPDATE mailbox_attachments
           SET active_slot = NULL, detached_at = CURRENT_TIMESTAMP,
               detach_reason = 'replaced'
         WHERE session_id = 'ses_one'
        """
    )
    conn.execute(
        """
        INSERT INTO mailbox_attachments (
            mailbox_id, session_id, active_slot, notification_mode, attached_at
        ) VALUES (?, 'ses_two', 1, 'pull', CURRENT_TIMESTAMP)
        """,
        (mailbox_id,),
    )
    assert conn.execute(
        "SELECT session_id FROM mailbox_attachments WHERE active_slot = 1"
    ).fetchone() == ("ses_two",)


def test_message_audience_and_read_attribution_are_constrained() -> None:
    conn = _connection()
    mailbox_id = _agent_mailbox(conn)
    thread_id = conn.execute(
        """
        INSERT INTO mail_threads (
            thread_id, origin_workspace_id, started_by_mailbox_id, subject,
            created_at, updated_at
        ) VALUES ('thr_one', 1, ?, 'subject', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (mailbox_id,),
    ).lastrowid

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO mail_messages (
                message_id, operation_key, thread_id, sender_mailbox_id,
                origin_workspace_id, audience, subject, created_at
            ) VALUES ('msg_invalid', 'op_invalid', ?, ?, 1, 'implicit-null',
                      'subject', CURRENT_TIMESTAMP)
            """,
            (thread_id, mailbox_id),
        )

    message_id = conn.execute(
        """
        INSERT INTO mail_messages (
            message_id, operation_key, thread_id, sender_mailbox_id,
            origin_workspace_id, audience, subject, created_at
        ) VALUES ('msg_one', 'op_one', ?, ?, 1, 'direct', 'subject',
                  CURRENT_TIMESTAMP)
        """,
        (thread_id, mailbox_id),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO mail_deliveries (
                delivery_id, message_id, recipient_mailbox_id,
                recipient_workspace_id, accepted_at, read_at
            ) VALUES ('del_invalid', ?, ?, 1, CURRENT_TIMESTAMP,
                      CURRENT_TIMESTAMP)
            """,
            (message_id, mailbox_id),
        )


def test_notification_and_smtp_idempotency_are_reserved() -> None:
    conn = _connection()
    mailbox_id = _agent_mailbox(conn)
    thread_id = conn.execute(
        """
        INSERT INTO mail_threads (
            thread_id, origin_workspace_id, started_by_mailbox_id, subject,
            created_at, updated_at
        ) VALUES ('thr_one', 1, ?, 'subject', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (mailbox_id,),
    ).lastrowid
    message_id = conn.execute(
        """
        INSERT INTO mail_messages (
            message_id, operation_key, thread_id, sender_mailbox_id,
            origin_workspace_id, audience, subject, created_at
        ) VALUES ('msg_one', 'op_one', ?, ?, 1, 'direct', 'subject',
                  CURRENT_TIMESTAMP)
        """,
        (thread_id, mailbox_id),
    ).lastrowid
    delivery_id = conn.execute(
        """
        INSERT INTO mail_deliveries (
            delivery_id, message_id, recipient_mailbox_id,
            recipient_workspace_id, accepted_at
        ) VALUES ('del_one', ?, ?, 1, CURRENT_TIMESTAMP)
        """,
        (message_id, mailbox_id),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO mail_notification_attempts (
            notification_id, idempotency_key, delivery_id, adapter, status,
            created_at
        ) VALUES ('note_one', 'note-key', ?, 'pull', 'queued', CURRENT_TIMESTAMP)
        """,
        (delivery_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO mail_notification_attempts (
                notification_id, idempotency_key, delivery_id, adapter, status,
                created_at
            ) VALUES ('note_two', 'note-key', ?, 'pull', 'queued',
                      CURRENT_TIMESTAMP)
            """,
            (delivery_id,),
        )

    conn.execute(
        """
        INSERT INTO mail_smtp_outbox (
            outbox_id, idempotency_key, delivery_id, recipient_mailbox_id,
            smtp_destination_ref, copy_mode, status, created_at, updated_at
        ) VALUES ('smtp_one', 'smtp-key', ?, ?, 'secure:operator:1',
                  'notification', 'queued', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (delivery_id, mailbox_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO mail_smtp_outbox (
                outbox_id, idempotency_key, delivery_id, recipient_mailbox_id,
                smtp_destination_ref, copy_mode, status, created_at, updated_at
            ) VALUES ('smtp_two', 'smtp-other', ?, ?, 'secure:operator:1',
                      'notification', 'queued', CURRENT_TIMESTAMP,
                      CURRENT_TIMESTAMP)
            """,
            (delivery_id, mailbox_id),
        )

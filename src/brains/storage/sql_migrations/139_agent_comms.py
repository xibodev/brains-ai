"""Agent-to-agent comms slice 1 (B2).

``help_request_constraints``
    Optional harness constraint on a peer-help request, keyed by the
    request's unique ``code``. Absent row keeps the prior any-harness
    semantics. Grammar: an exact tool name (``claude``) or ``not:<tool>``
    (``not:copilot``), case-insensitive. Enforced by ``wait_for_request``
    against the claiming session's ``tool``, so a Copilot session can
    deliberately route validation to a different harness (cross-CLI review,
    adversarial passes) without either side sharing context.

    A separate table — not a column on ``help_requests`` — because that
    table is part of the frozen baseline and its rendering must stay
    byte-identical; post-freeze changes ship as additive deltas.

``topic_posts``
    The message-board half of the comms slice: flat, install-wide named
    topics agents post to and read. Delivery to busy agents is via the
    existing mailbox — posting blasts one notification per *other*
    workspace that has live sessions, so an agent only ever polls its own
    inbox (scenario 5 of the comms design).

Additive only. Fresh databases get both from the models; this delta patches
existing stores idempotently.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS help_request_constraints (
            request_code VARCHAR(32) NOT NULL,
            required_tool VARCHAR(64) NOT NULL,
            PRIMARY KEY (request_code),
            FOREIGN KEY(request_code) REFERENCES help_requests (code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS topic_posts (
            id INTEGER NOT NULL,
            topic VARCHAR(64) NOT NULL,
            from_session_id VARCHAR(32),
            from_workspace_id INTEGER,
            reply_to_id INTEGER,
            subject VARCHAR(256) NOT NULL,
            body TEXT,
            required_tool VARCHAR(64),
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY(from_session_id) REFERENCES agent_sessions (id),
            FOREIGN KEY(from_workspace_id) REFERENCES workspaces (id),
            FOREIGN KEY(reply_to_id) REFERENCES topic_posts (id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_topic_posts_topic ON topic_posts (topic)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_topic_posts_created_at ON topic_posts (created_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_topic_posts_from_session_id ON topic_posts (from_session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_topic_posts_from_workspace_id "
        "ON topic_posts (from_workspace_id)"
    )

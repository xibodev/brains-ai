"""BL-P1-02 - product attribution for gateway usage.

``usage_attributions``
    The link between one ``usage_ledger`` row and the Session, Issue, Persona
    and Org the call was spent on. ``usage_entry_id`` is unique, so a ledger
    row is attributed exactly once and an Issue rollup can never double-count
    a retried write.

The ledger itself is a frozen baseline table and is deliberately not altered:
attribution is additive, so a call that does not identify its Session is
simply absent here and the rollup reports it as unattributed rather than
implying that the Issue cost nothing.
"""

from __future__ import annotations

import sqlite3

_USAGE_ATTRIBUTIONS = """
CREATE TABLE IF NOT EXISTS usage_attributions (
	id INTEGER NOT NULL,
	usage_entry_id INTEGER NOT NULL,
	session_id VARCHAR(32),
	issue_id INTEGER,
	persona_id INTEGER,
	org_id INTEGER,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(usage_entry_id) REFERENCES usage_ledger (id),
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id),
	FOREIGN KEY(issue_id) REFERENCES issues (id),
	FOREIGN KEY(persona_id) REFERENCES personas (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id)
)
"""

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_usage_attributions_usage_entry_id "
    "ON usage_attributions (usage_entry_id)",
    "CREATE INDEX IF NOT EXISTS ix_usage_attributions_session_id "
    "ON usage_attributions (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_usage_attributions_issue_id ON usage_attributions (issue_id)",
    "CREATE INDEX IF NOT EXISTS ix_usage_attributions_persona_id "
    "ON usage_attributions (persona_id)",
    "CREATE INDEX IF NOT EXISTS ix_usage_attributions_org_id ON usage_attributions (org_id)",
    "CREATE INDEX IF NOT EXISTS ix_usage_attributions_created_at "
    "ON usage_attributions (created_at)",
)


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_USAGE_ATTRIBUTIONS)
    for statement in _INDEXES:
        conn.execute(statement)

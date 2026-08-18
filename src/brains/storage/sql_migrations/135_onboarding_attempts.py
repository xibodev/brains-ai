"""BL-P1-04 - durable fresh-state onboarding.

``onboarding_attempts``
    One row per operator run through onboarding. It carries the entities the
    run actually created, the step it is on, and either a completion stamped
    only when a real Session exists or an explicit blocked reason. There is no
    representation of "finished" that a Session does not back.

``onboarding_steps``
    One row per step of an attempt. ``(attempt_id, step)`` is unique, so a
    retry updates the same row and increments ``attempts`` instead of
    appending a second history, and a browser reload replays exactly what the
    server holds.

Both tables postdate the frozen baseline, so this delta provisions them on
every backend. Idempotent.
"""

from __future__ import annotations

import sqlite3

_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS onboarding_attempts (
	id INTEGER NOT NULL,
	attempt_id VARCHAR(40) NOT NULL,
	operator_id INTEGER,
	org_id INTEGER,
	runtime_id INTEGER,
	persona_id INTEGER,
	project_id INTEGER,
	issue_id INTEGER,
	session_id VARCHAR(32),
	status VARCHAR(16) NOT NULL,
	current_step VARCHAR(24) NOT NULL,
	blocked_reason VARCHAR(64),
	blocked_detail TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	completed_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(operator_id) REFERENCES operators (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(runtime_id) REFERENCES runtimes (id),
	FOREIGN KEY(persona_id) REFERENCES personas (id),
	FOREIGN KEY(project_id) REFERENCES projects (id),
	FOREIGN KEY(issue_id) REFERENCES issues (id),
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id)
)
"""

_STEPS = """
CREATE TABLE IF NOT EXISTS onboarding_steps (
	id INTEGER NOT NULL,
	attempt_id VARCHAR(40) NOT NULL,
	step VARCHAR(24) NOT NULL,
	status VARCHAR(16) NOT NULL,
	entity_ref VARCHAR(128),
	detail TEXT,
	error TEXT,
	attempts INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_onboarding_attempt_step UNIQUE (attempt_id, step),
	FOREIGN KEY(attempt_id) REFERENCES onboarding_attempts (attempt_id)
)
"""

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_onboarding_attempts_attempt_id "
    "ON onboarding_attempts (attempt_id)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_operator_id "
    "ON onboarding_attempts (operator_id)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_org_id ON onboarding_attempts (org_id)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_runtime_id "
    "ON onboarding_attempts (runtime_id)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_persona_id "
    "ON onboarding_attempts (persona_id)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_project_id "
    "ON onboarding_attempts (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_issue_id ON onboarding_attempts (issue_id)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_session_id "
    "ON onboarding_attempts (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_status ON onboarding_attempts (status)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_created_at "
    "ON onboarding_attempts (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_attempts_operator_status "
    "ON onboarding_attempts (operator_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_steps_attempt_id ON onboarding_steps (attempt_id)",
    "CREATE INDEX IF NOT EXISTS ix_onboarding_steps_step ON onboarding_steps (step)",
)


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_ATTEMPTS)
    conn.execute(_STEPS)
    for statement in _INDEXES:
        conn.execute(statement)

"""BL-P1-08 - Skill attachments to Personas and Projects (F10).

``persona_skills`` / ``project_skills``
    Durable, provenance-carrying attachment rows so a Skill's SKILL.md content
    enters a Persona's or Project's spawned Session context deterministically
    instead of only existing as a listing nobody reads. ``(persona_id,
    skill_id)`` and ``(project_id, skill_id)`` are each unique, so attaching the
    same Skill twice updates nothing rather than duplicating context.
    ``attached_by_operator_id`` and ``attached_at`` record who attached it and
    when; ``position`` orders multiple attachments deterministically for
    context assembly (see ``brains.control.skills.resolve_context_for_session``).

Additive only - ``skills`` itself (125_skills) is untouched. ``Base.metadata.
create_all`` provisions both tables on fresh DBs; this disk migration creates
them on existing SQLite installs. Idempotent (``CREATE TABLE IF NOT EXISTS`` +
``CREATE INDEX IF NOT EXISTS``).
"""

from __future__ import annotations

import sqlite3

_PERSONA_SKILLS = """
CREATE TABLE IF NOT EXISTS persona_skills (
	id INTEGER NOT NULL,
	persona_id INTEGER NOT NULL,
	skill_id INTEGER NOT NULL,
	position INTEGER NOT NULL,
	attached_by_operator_id INTEGER,
	attached_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_persona_skill UNIQUE (persona_id, skill_id),
	FOREIGN KEY(persona_id) REFERENCES personas (id),
	FOREIGN KEY(skill_id) REFERENCES skills (id),
	FOREIGN KEY(attached_by_operator_id) REFERENCES operators (id)
)
"""

_PROJECT_SKILLS = """
CREATE TABLE IF NOT EXISTS project_skills (
	id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	skill_id INTEGER NOT NULL,
	position INTEGER NOT NULL,
	attached_by_operator_id INTEGER,
	attached_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_project_skill UNIQUE (project_id, skill_id),
	FOREIGN KEY(project_id) REFERENCES projects (id),
	FOREIGN KEY(skill_id) REFERENCES skills (id),
	FOREIGN KEY(attached_by_operator_id) REFERENCES operators (id)
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_persona_skills_persona_id ON persona_skills (persona_id)",
    "CREATE INDEX IF NOT EXISTS ix_persona_skills_skill_id ON persona_skills (skill_id)",
    "CREATE INDEX IF NOT EXISTS ix_project_skills_project_id ON project_skills (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_project_skills_skill_id ON project_skills (skill_id)",
)


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_PERSONA_SKILLS)
    conn.execute(_PROJECT_SKILLS)
    for statement in _INDEXES:
        conn.execute(statement)

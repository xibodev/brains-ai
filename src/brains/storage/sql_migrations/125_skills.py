"""F10 native-battalion — skills (SKILL.md packs).

Adds the ``skills`` table: named, org-scoped context packs (SKILL.md content)
attachable to personas/projects. ``Base.metadata.create_all`` provisions it on
fresh DBs; this disk migration creates it on existing SQLite installs.
Idempotent (``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS``).
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY,
            org_id INTEGER REFERENCES orgs(id),
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            content TEXT,
            created_at TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_skills_org_slug ON skills (org_id, slug)")

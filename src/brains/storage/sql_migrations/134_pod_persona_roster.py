"""BL-P1-03 - Persona-oriented Pod membership.

``pod_profiles``
    The product record for a Pod: its Org and its one leader **Persona**. The
    legacy ``squads`` row keeps the Pod's identity, because
    ``issues.assignee_pod_id`` and ``projects.assignee_pod_id`` reference it
    and the legacy workspace task routing still reads its operator columns.

``pod_members``
    One row per Persona in a Pod. ``(pod_id, persona_id)`` is unique, so
    re-adding a member updates its role rather than duplicating the roster.

The backfill is conservative on purpose. A Pod's Org is taken from its
Workspace, which is the only Org fact a legacy squad carries. A legacy
``squad_members`` operator row becomes a Persona membership **only** when that
operator resolves to exactly one active Persona and that Persona is in the
Pod's Org; anything ambiguous is left in ``squad_members`` and reported by the
roster as a legacy operator member with the reason it could not be resolved.
Nothing is invented, and rerunning the migration inserts nothing twice.
"""

from __future__ import annotations

import sqlite3

_POD_PROFILES = """
CREATE TABLE IF NOT EXISTS pod_profiles (
	id INTEGER NOT NULL,
	pod_id INTEGER NOT NULL,
	org_id INTEGER,
	leader_persona_id INTEGER,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(pod_id) REFERENCES squads (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(leader_persona_id) REFERENCES personas (id)
)
"""

_POD_MEMBERS = """
CREATE TABLE IF NOT EXISTS pod_members (
	id INTEGER NOT NULL,
	pod_id INTEGER NOT NULL,
	persona_id INTEGER NOT NULL,
	role VARCHAR(64) NOT NULL,
	source VARCHAR(24) NOT NULL,
	added_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_pod_member_persona UNIQUE (pod_id, persona_id),
	FOREIGN KEY(pod_id) REFERENCES squads (id),
	FOREIGN KEY(persona_id) REFERENCES personas (id)
)
"""

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_pod_profiles_pod_id ON pod_profiles (pod_id)",
    "CREATE INDEX IF NOT EXISTS ix_pod_profiles_org_id ON pod_profiles (org_id)",
    "CREATE INDEX IF NOT EXISTS ix_pod_profiles_leader_persona_id "
    "ON pod_profiles (leader_persona_id)",
    "CREATE INDEX IF NOT EXISTS ix_pod_members_pod_id ON pod_members (pod_id)",
    "CREATE INDEX IF NOT EXISTS ix_pod_members_persona_id ON pod_members (persona_id)",
)

#: One profile per existing squad, carrying the Org its Workspace declares.
_BACKFILL_PROFILES = """
INSERT INTO pod_profiles (pod_id, org_id, leader_persona_id, created_at, updated_at)
SELECT s.id,
       (SELECT w.org_id FROM workspaces w WHERE w.id = s.workspace_id),
       NULL,
       CURRENT_TIMESTAMP,
       CURRENT_TIMESTAMP
FROM squads s
WHERE NOT EXISTS (SELECT 1 FROM pod_profiles p WHERE p.pod_id = s.id)
"""

#: The legacy leader operator becomes the leader Persona only when it resolves
#: to exactly one active Persona in the Pod's Org.
_BACKFILL_LEADER = """
UPDATE pod_profiles
SET leader_persona_id = (
        SELECT p.id
        FROM personas p, squads s
        WHERE s.id = pod_profiles.pod_id
          AND p.operator_id = s.leader_operator_id
          AND p.status = 'active'
          AND (pod_profiles.org_id IS NULL OR p.org_id = pod_profiles.org_id)
    ),
    updated_at = CURRENT_TIMESTAMP
WHERE leader_persona_id IS NULL
  AND (
        SELECT COUNT(*)
        FROM personas p, squads s
        WHERE s.id = pod_profiles.pod_id
          AND p.operator_id = s.leader_operator_id
          AND p.status = 'active'
          AND (pod_profiles.org_id IS NULL OR p.org_id = pod_profiles.org_id)
      ) = 1
"""

#: A legacy operator membership becomes a Persona membership only when that
#: operator resolves to exactly one active Persona in the Pod's Org.
_BACKFILL_MEMBERS = """
INSERT INTO pod_members (pod_id, persona_id, role, source, added_at)
SELECT sm.squad_id,
       (SELECT p.id
        FROM personas p
        WHERE p.operator_id = sm.operator_id
          AND p.status = 'active'
          AND (pp.org_id IS NULL OR p.org_id = pp.org_id)),
       sm.role,
       'legacy_backfill',
       CURRENT_TIMESTAMP
FROM squad_members sm
JOIN pod_profiles pp ON pp.pod_id = sm.squad_id
WHERE (
        SELECT COUNT(*)
        FROM personas p
        WHERE p.operator_id = sm.operator_id
          AND p.status = 'active'
          AND (pp.org_id IS NULL OR p.org_id = pp.org_id)
      ) = 1
  AND NOT EXISTS (
        SELECT 1
        FROM pod_members m
        WHERE m.pod_id = sm.squad_id
          AND m.persona_id = (
                SELECT p.id
                FROM personas p
                WHERE p.operator_id = sm.operator_id
                  AND p.status = 'active'
                  AND (pp.org_id IS NULL OR p.org_id = pp.org_id))
      )
"""

#: A backfilled leader Persona is rostered, so the leader is always a member.
_BACKFILL_LEADER_MEMBERSHIP = """
INSERT INTO pod_members (pod_id, persona_id, role, source, added_at)
SELECT pp.pod_id, pp.leader_persona_id, 'leader', 'legacy_backfill', CURRENT_TIMESTAMP
FROM pod_profiles pp
WHERE pp.leader_persona_id IS NOT NULL
  AND NOT EXISTS (
        SELECT 1 FROM pod_members m
        WHERE m.pod_id = pp.pod_id AND m.persona_id = pp.leader_persona_id
      )
"""

_BACKFILL_LEADER_ROLE = """
UPDATE pod_members
SET role = 'leader'
WHERE role <> 'leader'
  AND persona_id = (
        SELECT pp.leader_persona_id FROM pod_profiles pp WHERE pp.pod_id = pod_members.pod_id
      )
"""


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(_POD_PROFILES)
    conn.execute(_POD_MEMBERS)
    for statement in _INDEXES:
        conn.execute(statement)
    for statement in (
        _BACKFILL_PROFILES,
        _BACKFILL_LEADER,
        _BACKFILL_MEMBERS,
        _BACKFILL_LEADER_MEMBERSHIP,
        _BACKFILL_LEADER_ROLE,
    ):
        conn.execute(statement)

-- BL-P1-03 - Persona-oriented Pod membership (PostgreSQL).
--
-- Equivalent of 134_pod_persona_roster.py. The Pod's identity stays on the
-- legacy squads row; the product record and the Persona roster live here.
-- Idempotent: every create is guarded and every backfill is a NOT EXISTS
-- insert or a conditional update.

CREATE TABLE IF NOT EXISTS pod_profiles (
	id SERIAL NOT NULL,
	pod_id INTEGER NOT NULL,
	org_id INTEGER,
	leader_persona_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(pod_id) REFERENCES squads (id),
	FOREIGN KEY(org_id) REFERENCES orgs (id),
	FOREIGN KEY(leader_persona_id) REFERENCES personas (id)
);

CREATE TABLE IF NOT EXISTS pod_members (
	id SERIAL NOT NULL,
	pod_id INTEGER NOT NULL,
	persona_id INTEGER NOT NULL,
	role VARCHAR(64) NOT NULL,
	source VARCHAR(24) NOT NULL,
	added_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_pod_member_persona UNIQUE (pod_id, persona_id),
	FOREIGN KEY(pod_id) REFERENCES squads (id),
	FOREIGN KEY(persona_id) REFERENCES personas (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_pod_profiles_pod_id ON pod_profiles (pod_id);
CREATE INDEX IF NOT EXISTS ix_pod_profiles_org_id ON pod_profiles (org_id);
CREATE INDEX IF NOT EXISTS ix_pod_profiles_leader_persona_id ON pod_profiles (leader_persona_id);
CREATE INDEX IF NOT EXISTS ix_pod_members_pod_id ON pod_members (pod_id);
CREATE INDEX IF NOT EXISTS ix_pod_members_persona_id ON pod_members (persona_id);

INSERT INTO pod_profiles (pod_id, org_id, leader_persona_id, created_at, updated_at)
SELECT s.id,
       (SELECT w.org_id FROM workspaces w WHERE w.id = s.workspace_id),
       NULL,
       CURRENT_TIMESTAMP,
       CURRENT_TIMESTAMP
FROM squads s
WHERE NOT EXISTS (SELECT 1 FROM pod_profiles p WHERE p.pod_id = s.id);

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
      ) = 1;

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
      );

INSERT INTO pod_members (pod_id, persona_id, role, source, added_at)
SELECT pp.pod_id, pp.leader_persona_id, 'leader', 'legacy_backfill', CURRENT_TIMESTAMP
FROM pod_profiles pp
WHERE pp.leader_persona_id IS NOT NULL
  AND NOT EXISTS (
        SELECT 1 FROM pod_members m
        WHERE m.pod_id = pp.pod_id AND m.persona_id = pp.leader_persona_id
      );

UPDATE pod_members
SET role = 'leader'
WHERE role <> 'leader'
  AND persona_id = (
        SELECT pp.leader_persona_id FROM pod_profiles pp WHERE pp.pod_id = pod_members.pod_id
      );

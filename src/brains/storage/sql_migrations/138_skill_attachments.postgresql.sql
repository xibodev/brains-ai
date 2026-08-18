-- BL-P1-08 - Skill attachments to Personas and Projects (PostgreSQL).
--
-- Equivalent of 138_skill_attachments.py. Idempotent.

CREATE TABLE IF NOT EXISTS persona_skills (
	id SERIAL NOT NULL,
	persona_id INTEGER NOT NULL,
	skill_id INTEGER NOT NULL,
	position INTEGER NOT NULL,
	attached_by_operator_id INTEGER,
	attached_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_persona_skill UNIQUE (persona_id, skill_id),
	FOREIGN KEY(persona_id) REFERENCES personas (id),
	FOREIGN KEY(skill_id) REFERENCES skills (id),
	FOREIGN KEY(attached_by_operator_id) REFERENCES operators (id)
);

CREATE TABLE IF NOT EXISTS project_skills (
	id SERIAL NOT NULL,
	project_id INTEGER NOT NULL,
	skill_id INTEGER NOT NULL,
	position INTEGER NOT NULL,
	attached_by_operator_id INTEGER,
	attached_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_project_skill UNIQUE (project_id, skill_id),
	FOREIGN KEY(project_id) REFERENCES projects (id),
	FOREIGN KEY(skill_id) REFERENCES skills (id),
	FOREIGN KEY(attached_by_operator_id) REFERENCES operators (id)
);

CREATE INDEX IF NOT EXISTS ix_persona_skills_persona_id ON persona_skills (persona_id);
CREATE INDEX IF NOT EXISTS ix_persona_skills_skill_id ON persona_skills (skill_id);
CREATE INDEX IF NOT EXISTS ix_project_skills_project_id ON project_skills (project_id);
CREATE INDEX IF NOT EXISTS ix_project_skills_skill_id ON project_skills (skill_id);

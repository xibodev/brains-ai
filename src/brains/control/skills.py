"""Skills (F10) — named SKILL.md context packs, org-scoped.

A *skill* is a reusable context pack (SKILL.md content) attachable to personas/
projects to compose agent context (BL-P1-08). Pure control logic — the CRUD
routes live in :mod:`brains.api.orgs`, and the attach/detach routes live in
:mod:`brains.api.personas` and :mod:`brains.api.projects`.

Attachment (``persona_skills`` / ``project_skills``, migration 138) is
additive and idempotent: attaching an already-attached Skill returns the
existing row rather than duplicating context, and :func:`resolve_context_for_session`
deduplicates a Skill attached through both a Persona and its Session's Project,
reporting every source it was attached through. :func:`render_skill_context_block`
turns that resolved context into a markdown block a launcher can prepend to an
agent's prompt, mirroring :func:`brains.context.semantic.build_orientation_block`'s
"empty block is a comment, never fabricated content" contract.
"""

from __future__ import annotations

import re

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    Issue,
    Persona,
    PersonaSkill,
    Project,
    ProjectSkill,
    Skill,
)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class SkillAttachmentError(ValueError):
    """An invalid Skill attach/detach: unknown ref, or a cross-Org attempt."""


def _to_dict(s: Skill) -> dict:
    return {
        "id": s.id,
        "org_id": s.org_id,
        "slug": s.slug,
        "name": s.name,
        "content": s.content,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def create_skill(org_id: int | None, slug: str, name: str, content: str = "") -> dict:
    if not _SLUG.match(slug or ""):
        raise ValueError("skill slug must be lowercase alphanumeric with - or _ (max 63 chars)")
    init_db()
    with SessionLocal() as session:
        existing = (
            session.query(Skill).filter(Skill.org_id == org_id, Skill.slug == slug).one_or_none()
        )
        if existing is not None:
            raise ValueError(f"skill {slug!r} already exists in this org")
        row = Skill(org_id=org_id, slug=slug, name=name, content=content or None)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _to_dict(row)


def list_skills(org_id: int | None, limit: int = 200) -> list[dict]:
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(Skill)
            .filter(Skill.org_id == org_id)
            .order_by(Skill.slug.asc())
            .limit(limit)
            .all()
        )
        return [_to_dict(s) for s in rows]


def get_skill(skill_id: int) -> dict | None:
    init_db()
    with SessionLocal() as session:
        row = session.get(Skill, skill_id)
        return _to_dict(row) if row is not None else None


# --------------------------------------------------------------------------- #
# Attachment (BL-P1-08 / AC-F10-05)
# --------------------------------------------------------------------------- #


def _attachment_dict(row: PersonaSkill | ProjectSkill, skill: Skill, *, source: str) -> dict:
    entity_id = row.persona_id if isinstance(row, PersonaSkill) else row.project_id
    return {
        "id": row.id,
        "skill_id": row.skill_id,
        "slug": skill.slug,
        "name": skill.name,
        "source": source,
        "entity_id": entity_id,
        "position": row.position,
        "attached_by_operator_id": row.attached_by_operator_id,
        "attached_at": row.attached_at.isoformat() if row.attached_at else None,
    }


def attach_to_persona(
    persona_id: int, skill_id: int, *, attached_by_operator_id: int | None = None
) -> dict:
    """Attach a Skill to a Persona. Idempotent on ``(persona_id, skill_id)``:
    attaching an already-attached Skill returns the existing row unchanged
    rather than duplicating context. Refuses a Skill from another Org."""
    init_db()
    with SessionLocal() as session:
        persona = session.get(Persona, persona_id)
        if persona is None:
            raise SkillAttachmentError(f"unknown persona id: {persona_id!r}")
        skill = session.get(Skill, skill_id)
        if skill is None:
            raise SkillAttachmentError(f"unknown skill id: {skill_id!r}")
        if skill.org_id != persona.org_id:
            raise SkillAttachmentError(
                f"skill {skill_id} belongs to another Org and cannot attach to persona {persona_id}"
            )
        existing = (
            session.query(PersonaSkill)
            .filter(PersonaSkill.persona_id == persona_id, PersonaSkill.skill_id == skill_id)
            .one_or_none()
        )
        if existing is None:
            next_position = (
                session.query(func.coalesce(func.max(PersonaSkill.position), -1))
                .filter(PersonaSkill.persona_id == persona_id)
                .scalar()
                or -1
            ) + 1
            existing = PersonaSkill(
                persona_id=persona_id,
                skill_id=skill_id,
                position=next_position,
                attached_by_operator_id=attached_by_operator_id,
            )
            session.add(existing)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = (
                    session.query(PersonaSkill)
                    .filter(
                        PersonaSkill.persona_id == persona_id,
                        PersonaSkill.skill_id == skill_id,
                    )
                    .one_or_none()
                )
                if existing is None:
                    raise
            else:
                session.refresh(existing)
        return _attachment_dict(existing, skill, source="persona")


def detach_from_persona(persona_id: int, skill_id: int) -> None:
    init_db()
    with SessionLocal() as session:
        existing = (
            session.query(PersonaSkill)
            .filter(PersonaSkill.persona_id == persona_id, PersonaSkill.skill_id == skill_id)
            .one_or_none()
        )
        if existing is None:
            raise SkillAttachmentError(f"skill {skill_id} is not attached to persona {persona_id}")
        session.delete(existing)
        session.commit()


def list_persona_skills(persona_id: int) -> list[dict]:
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(PersonaSkill, Skill)
            .join(Skill, Skill.id == PersonaSkill.skill_id)
            .filter(PersonaSkill.persona_id == persona_id)
            .order_by(PersonaSkill.position.asc(), PersonaSkill.id.asc())
            .all()
        )
        return [_attachment_dict(row, skill, source="persona") for row, skill in rows]


def attach_to_project(
    project_id: int, skill_id: int, *, attached_by_operator_id: int | None = None
) -> dict:
    """Attach a Skill to a Project. Same idempotent/cross-Org contract as
    :func:`attach_to_persona`."""
    init_db()
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise SkillAttachmentError(f"unknown project id: {project_id!r}")
        skill = session.get(Skill, skill_id)
        if skill is None:
            raise SkillAttachmentError(f"unknown skill id: {skill_id!r}")
        if skill.org_id != project.org_id:
            raise SkillAttachmentError(
                f"skill {skill_id} belongs to another Org and cannot attach to project {project_id}"
            )
        existing = (
            session.query(ProjectSkill)
            .filter(ProjectSkill.project_id == project_id, ProjectSkill.skill_id == skill_id)
            .one_or_none()
        )
        if existing is None:
            next_position = (
                session.query(func.coalesce(func.max(ProjectSkill.position), -1))
                .filter(ProjectSkill.project_id == project_id)
                .scalar()
                or -1
            ) + 1
            existing = ProjectSkill(
                project_id=project_id,
                skill_id=skill_id,
                position=next_position,
                attached_by_operator_id=attached_by_operator_id,
            )
            session.add(existing)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = (
                    session.query(ProjectSkill)
                    .filter(
                        ProjectSkill.project_id == project_id,
                        ProjectSkill.skill_id == skill_id,
                    )
                    .one_or_none()
                )
                if existing is None:
                    raise
            else:
                session.refresh(existing)
        return _attachment_dict(existing, skill, source="project")


def detach_from_project(project_id: int, skill_id: int) -> None:
    init_db()
    with SessionLocal() as session:
        existing = (
            session.query(ProjectSkill)
            .filter(ProjectSkill.project_id == project_id, ProjectSkill.skill_id == skill_id)
            .one_or_none()
        )
        if existing is None:
            raise SkillAttachmentError(f"skill {skill_id} is not attached to project {project_id}")
        session.delete(existing)
        session.commit()


def list_project_skills(project_id: int) -> list[dict]:
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(ProjectSkill, Skill)
            .join(Skill, Skill.id == ProjectSkill.skill_id)
            .filter(ProjectSkill.project_id == project_id)
            .order_by(ProjectSkill.position.asc(), ProjectSkill.id.asc())
            .all()
        )
        return [_attachment_dict(row, skill, source="project") for row, skill in rows]


# --------------------------------------------------------------------------- #
# Session context injection (BL-P1-08 / AC-F10-05)
# --------------------------------------------------------------------------- #


def resolve_context_for_session(session_id: str) -> dict | None:
    """The deduplicated, ordered Skill context for one Session's Persona and
    Project, with provenance.

    Persona attachments are listed before Project attachments in a fixed,
    deterministic order (``position`` then attachment id); a Skill attached
    through both is reported exactly once with both sources recorded in
    ``sources``. Returns ``None`` when the Session is unknown or nothing is
    attached, so a caller never injects an empty/misleading block.
    """
    init_db()
    with SessionLocal() as session:
        agent_session = session.get(AgentSession, session_id)
        if agent_session is None:
            return None
        persona_id = agent_session.persona_id
        project_id: int | None = None
        if agent_session.issue_id is not None:
            issue = session.get(Issue, agent_session.issue_id)
            if issue is not None:
                project_id = issue.project_id

        ordered: list[dict] = []
        seen: dict[int, dict] = {}

        def _merge(rows, source: str) -> None:
            for _row, skill in rows:
                entry = seen.get(skill.id)
                if entry is None:
                    entry = {
                        "skill_id": skill.id,
                        "slug": skill.slug,
                        "name": skill.name,
                        "content": skill.content,
                        "sources": [source],
                    }
                    seen[skill.id] = entry
                    ordered.append(entry)
                elif source not in entry["sources"]:
                    entry["sources"].append(source)

        if persona_id is not None:
            _merge(
                session.query(PersonaSkill, Skill)
                .join(Skill, Skill.id == PersonaSkill.skill_id)
                .filter(PersonaSkill.persona_id == persona_id)
                .order_by(PersonaSkill.position.asc(), PersonaSkill.id.asc())
                .all(),
                "persona",
            )
        if project_id is not None:
            _merge(
                session.query(ProjectSkill, Skill)
                .join(Skill, Skill.id == ProjectSkill.skill_id)
                .filter(ProjectSkill.project_id == project_id)
                .order_by(ProjectSkill.position.asc(), ProjectSkill.id.asc())
                .all(),
                "project",
            )

        if not ordered:
            return None
        return {
            "session_id": session_id,
            "persona_id": persona_id,
            "project_id": project_id,
            "skills": ordered,
        }


def render_skill_context_block(context: dict | None) -> str:
    """Render :func:`resolve_context_for_session`'s result as a ready-to-inject
    markdown block.

    Mirrors :func:`brains.context.semantic.build_orientation_block`'s contract:
    an unavailable/empty context renders a ``<!-- -->`` comment marker instead
    of fabricated content, so a launcher can check ``.startswith("<!--")``
    before prepending it to a prompt.
    """
    if not context or not context.get("skills"):
        return "<!-- brains: no Skills attached to this session's Persona/Project -->"
    lines = [
        "## Attached Skills (brains — provenance: Persona and/or Project attachment)",
        "",
    ]
    for entry in context["skills"]:
        sources = "+".join(entry["sources"])
        lines.append(f"### {entry['name']} (`{entry['slug']}`, via {sources})")
        if entry.get("content"):
            lines.append(entry["content"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

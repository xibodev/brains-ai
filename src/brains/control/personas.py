"""Personas — named AI identities bound to a runtime (native-battalion WS2).

A *persona* = identity (``name``, ``system_prompt``, ``color``) + brain
(``model``) + hands (``tool`` + ``default_runtime_id``) + principal
(``operator_id``). Binding an operator lets a persona reuse the whole existing
principal machinery (sessions, claims, squad membership, knowledge authorship).
``operator_id`` is nullable now and bound 1:1 at first spawn via
:func:`bind_operator` (WS2-RATIFIED fork #1).

Pure control logic — no FastAPI.
"""

from __future__ import annotations

import re

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import Operator, Org, Persona

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
PERSONA_STATUSES = {"active", "archived"}


def _persona_to_dict(p: Persona) -> dict:
    return {
        "id": p.id,
        "slug": p.slug,
        "org_id": p.org_id,
        "operator_id": p.operator_id,
        "name": p.name,
        "description": p.description,
        "system_prompt": p.system_prompt,
        "model": p.model,
        "tool": p.tool,
        "default_runtime_id": p.default_runtime_id,
        "color": p.color,
        "avatar": p.avatar,
        "status": p.status,
        "created_by_session_id": p.created_by_session_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _operator_by_slug(session, slug: str) -> Operator | None:
    return session.query(Operator).filter(Operator.slug == slug).one_or_none()


def create_persona(
    org_id: int,
    slug: str,
    name: str,
    *,
    description: str = "",
    system_prompt: str = "",
    model: str | None = None,
    tool: str | None = None,
    default_runtime_id: int | None = None,
    operator: str | None = None,
    color: str | None = None,
    avatar: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Create a persona in an org. Raises ``ValueError`` on a bad slug, an
    unknown org, a duplicate ``(org, slug)``, or an unknown bound operator."""
    if not SLUG_PATTERN.match(slug):
        raise ValueError("persona slug must be lowercase alphanumeric with - or _ (max 63 chars)")
    init_db()
    with SessionLocal() as session:
        if session.get(Org, org_id) is None:
            raise ValueError(f"unknown org id: {org_id!r}")
        existing = (
            session.query(Persona)
            .filter(Persona.org_id == org_id, Persona.slug == slug)
            .one_or_none()
        )
        if existing is not None:
            raise ValueError(f"persona {slug!r} already exists in org {org_id}")
        operator_id = None
        if operator is not None:
            op = _operator_by_slug(session, operator)
            if op is None:
                raise ValueError(f"unknown operator: {operator!r}")
            operator_id = op.id
        persona = Persona(
            org_id=org_id,
            slug=slug,
            name=name,
            description=description or None,
            system_prompt=system_prompt or None,
            model=model,
            tool=tool,
            default_runtime_id=default_runtime_id,
            operator_id=operator_id,
            color=color,
            avatar=avatar,
            created_by_session_id=session_id,
        )
        session.add(persona)
        session.commit()
        session.refresh(persona)
        result = _persona_to_dict(persona)
    append_event(
        "persona_created",
        f"{slug}: {name}",
        session_id=session_id,
        metadata={"slug": slug, "org_id": org_id},
    )
    return result


def get_persona(ref: str | int, *, org_id: int | None = None) -> dict | None:
    """Look up a persona by id (int) or slug (str). When looking up by slug,
    ``org_id`` is required (slugs are unique only within an org)."""
    init_db()
    with SessionLocal() as session:
        persona = _get_persona_row(session, ref, org_id=org_id)
        return _persona_to_dict(persona) if persona is not None else None


def _get_persona_row(session, ref: str | int, *, org_id: int | None = None) -> Persona | None:
    if isinstance(ref, int):
        return session.get(Persona, ref)
    if isinstance(ref, str) and ref.isdigit():
        return session.get(Persona, int(ref))
    query = session.query(Persona).filter(Persona.slug == ref)
    if org_id is not None:
        query = query.filter(Persona.org_id == org_id)
    return query.one_or_none()


def list_personas(*, org_id: int | None = None, include_archived: bool = False) -> list[dict]:
    init_db()
    with SessionLocal() as session:
        query = session.query(Persona)
        if org_id is not None:
            query = query.filter(Persona.org_id == org_id)
        if not include_archived:
            query = query.filter(Persona.status == "active")
        return [_persona_to_dict(p) for p in query.order_by(Persona.slug).all()]


_UPDATABLE = {
    "name",
    "description",
    "system_prompt",
    "model",
    "tool",
    "default_runtime_id",
    "color",
    "avatar",
}


def update(
    persona_ref: str | int, *, org_id: int | None = None, session_id: str | None = None, **fields
) -> dict:
    """Update mutable persona fields. Unknown fields raise ``ValueError``."""
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")
    init_db()
    with SessionLocal() as session:
        persona = _get_persona_row(session, persona_ref, org_id=org_id)
        if persona is None:
            raise ValueError(f"unknown persona: {persona_ref!r}")
        for key, value in fields.items():
            setattr(persona, key, value)
        persona.updated_at = utc_now()
        session.commit()
        session.refresh(persona)
        return _persona_to_dict(persona)


def bind_operator(
    persona_ref: str | int,
    operator: str,
    *,
    org_id: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Bind a persona to an operator principal at first spawn (1:1).

    Idempotent if re-binding to the *same* operator; raises ``ValueError`` if
    the persona is already bound to a *different* operator.
    """
    init_db()
    with SessionLocal() as session:
        persona = _get_persona_row(session, persona_ref, org_id=org_id)
        if persona is None:
            raise ValueError(f"unknown persona: {persona_ref!r}")
        op = _operator_by_slug(session, operator)
        if op is None:
            raise ValueError(f"unknown operator: {operator!r}")
        if persona.operator_id is not None and persona.operator_id != op.id:
            raise ValueError(
                f"persona {persona.slug!r} is already bound to operator "
                f"{persona.operator_id}; cannot rebind to @{operator}"
            )
        persona.operator_id = op.id
        persona.updated_at = utc_now()
        session.commit()
        session.refresh(persona)
        result = _persona_to_dict(persona)
    append_event(
        "persona_bound",
        f"{result['slug']} bound to @{operator}",
        session_id=session_id,
        metadata={"slug": result["slug"], "operator": operator},
    )
    return result


def archive(
    persona_ref: str | int, *, org_id: int | None = None, session_id: str | None = None
) -> dict:
    init_db()
    with SessionLocal() as session:
        persona = _get_persona_row(session, persona_ref, org_id=org_id)
        if persona is None:
            raise ValueError(f"unknown persona: {persona_ref!r}")
        persona.status = "archived"
        persona.updated_at = utc_now()
        session.commit()
        session.refresh(persona)
        result = _persona_to_dict(persona)
    append_event(
        "persona_archived",
        f"{result['slug']} archived",
        session_id=session_id,
        metadata={"slug": result["slug"]},
    )
    return result

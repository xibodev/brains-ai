"""Orgs — top-level container (native-battalion WS2).

An *org* owns workspaces, personas, runtimes, projects and issues. Existing
single-machine installs are org-less; :func:`ensure_default_org` seeds a stable
``slug='default'`` org that the 120 disk migration and the app-level fallback
resolver both rely on.

Pure control logic — no FastAPI. WS3 layers HTTP on top. Mirrors the
``squads.py`` CRUD/lifecycle pattern: open a session via the storage layer,
validate, return plain dicts.
"""

from __future__ import annotations

import re
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import Operator, Org, OrgMember

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
DEFAULT_ORG_SLUG = "default"
ORG_STATUSES = {"active", "archived"}
ORG_MEMBER_ROLES = {"owner", "admin", "member"}


class LastOwnerError(ValueError):
    """The change would leave an Org with no ``owner``.

    An Org with no owner cannot grant ownership again - ``org.owner`` is the
    capability that does it - so it would be permanently unadministrable. Both
    the demotion path and the removal path refuse it, and both do so with a
    single conditional statement so two racing writers cannot each remove "the
    other" owner and leave none.

    The only way past it is explicit bootstrap recovery
    (``bootstrap_recovery=True``), which the HTTP API never sets.
    """


def _org_to_dict(org: Org) -> dict:
    return {
        "id": org.id,
        "slug": org.slug,
        "name": org.name,
        "description": org.description,
        "status": org.status,
        "created_at": org.created_at.isoformat() if org.created_at else None,
        "updated_at": org.updated_at.isoformat() if org.updated_at else None,
    }


def _member_to_dict(member: OrgMember, operator_slug: str) -> dict:
    return {
        "org_id": member.org_id,
        "operator_id": member.operator_id,
        "operator": operator_slug,
        "role": member.role,
        "created_at": member.created_at.isoformat() if member.created_at else None,
    }


def _operator_by_slug(session, slug: str) -> Operator | None:
    return session.query(Operator).filter(Operator.slug == slug).one_or_none()


def create_org(
    slug: str,
    name: str,
    description: str = "",
    session_id: str | None = None,
) -> dict:
    """Create an org. Raises ``ValueError`` on a bad slug or a duplicate slug."""
    if not SLUG_PATTERN.match(slug):
        raise ValueError("org slug must be lowercase alphanumeric with - or _ (max 63 chars)")
    init_db()
    with _db_module.SessionLocal() as session:
        existing = session.query(Org).filter(Org.slug == slug).one_or_none()
        if existing is not None:
            raise ValueError(f"org {slug!r} already exists")
        org = Org(slug=slug, name=name, description=description or None)
        session.add(org)
        session.commit()
        session.refresh(org)
        result = _org_to_dict(org)
    append_event(
        "org_created",
        f"{slug}: {name}",
        session_id=session_id,
        metadata={"slug": slug},
    )
    return result


def get_org(ref: str | int) -> dict | None:
    """Look up an org by id (int) or slug (str). Returns ``None`` if absent."""
    init_db()
    with _db_module.SessionLocal() as session:
        org = _get_org_row(session, ref)
        return _org_to_dict(org) if org is not None else None


def _get_org_row(session, ref: str | int) -> Org | None:
    if isinstance(ref, int):
        return session.get(Org, ref)
    # Numeric strings are treated as ids; otherwise as a slug.
    if isinstance(ref, str) and ref.isdigit():
        return session.get(Org, int(ref))
    return session.query(Org).filter(Org.slug == ref).one_or_none()


def list_orgs(include_archived: bool = False) -> list[dict]:
    init_db()
    with _db_module.SessionLocal() as session:
        query = session.query(Org)
        if not include_archived:
            query = query.filter(Org.status == "active")
        return [_org_to_dict(o) for o in query.order_by(Org.slug).all()]


def ensure_default_org() -> dict:
    """Return the default org, creating it idempotently if absent.

    Used by the 120 migration's app-side equivalent and as the runtime
    fallback resolver for org-less rows. Never raises on a re-run.
    """
    init_db()
    with _db_module.SessionLocal() as session:
        org = session.query(Org).filter(Org.slug == DEFAULT_ORG_SLUG).one_or_none()
        if org is None:
            org = Org(slug=DEFAULT_ORG_SLUG, name="Default Org")
            session.add(org)
            session.commit()
            session.refresh(org)
        return _org_to_dict(org)


def add_member(
    org_ref: str | int,
    operator: str,
    role: str = "member",
    session_id: str | None = None,
    *,
    bootstrap_recovery: bool = False,
) -> dict:
    """Add ``operator`` (a slug) to an org. Idempotent on (org, operator):
    re-adding updates the role.

    Demoting the *last* ``owner`` is refused: the resulting Org would have
    nobody who can grant ownership again. The check is a single conditional
    ``UPDATE`` whose ``WHERE`` counts the remaining owners, so two concurrent
    demotions cannot both pass a read-then-write check and empty the Org.
    """
    if role not in ORG_MEMBER_ROLES:
        raise ValueError(f"role must be one of {sorted(ORG_MEMBER_ROLES)}")
    init_db()
    with _db_module.SessionLocal() as session:
        org = _get_org_row(session, org_ref)
        if org is None:
            raise ValueError(f"unknown org: {org_ref!r}")
        op = _operator_by_slug(session, operator)
        if op is None:
            raise ValueError(f"unknown operator: {operator!r}")
        member = (
            session.query(OrgMember)
            .filter(OrgMember.org_id == org.id, OrgMember.operator_id == op.id)
            .one_or_none()
        )
        if member is None:
            member = OrgMember(org_id=org.id, operator_id=op.id, role=role)
            session.add(member)
            session.commit()
            session.refresh(member)
        elif member.role != role:
            if member.role == "owner" and role != "owner" and not bootstrap_recovery:
                _demote_last_owner_guarded(session, org.id, op.id, role)
            else:
                member.role = role
                session.commit()
            session.refresh(member)
        result = _member_to_dict(member, op.slug)
    append_event(
        "org_member_added",
        f"@{operator} joined {result['org_id']} as {role}",
        session_id=session_id,
        metadata={"org_id": result["org_id"], "operator": operator, "role": role},
    )
    return result


def _owner_count_subquery(org_id: int):
    return (
        select(func.count())
        .select_from(OrgMember.__table__)
        .where(OrgMember.org_id == org_id, OrgMember.role == "owner")
        .scalar_subquery()
    )


def _demote_last_owner_guarded(session, org_id: int, operator_id: int, role: str) -> None:
    """Demote one owner only while another owner remains. Atomic."""
    result = session.execute(
        update(OrgMember)
        .where(
            OrgMember.org_id == org_id,
            OrgMember.operator_id == operator_id,
            OrgMember.role == "owner",
            _owner_count_subquery(org_id) > 1,
        )
        .values(role=role)
    )
    if result.rowcount != 1:
        session.rollback()
        raise LastOwnerError(
            f"org {org_id} would be left with no owner; promote another owner first"
        )
    session.commit()


_ORG_UPDATABLE = {"name", "description", "status"}


def update_org(
    org_ref: str | int,
    *,
    session_id: str | None = None,
    **fields,
) -> dict:
    """Update mutable org fields (``name`` / ``description`` / ``status``)."""
    bad = set(fields) - _ORG_UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")
    if "status" in fields and fields["status"] not in ORG_STATUSES:
        raise ValueError(f"status must be one of {sorted(ORG_STATUSES)}")
    init_db()
    with _db_module.SessionLocal() as session:
        org = _get_org_row(session, org_ref)
        if org is None:
            raise ValueError(f"unknown org: {org_ref!r}")
        for key, value in fields.items():
            setattr(org, key, value)
        org.updated_at = utc_now()
        session.commit()
        session.refresh(org)
        return _org_to_dict(org)


def remove_member(
    org_ref: str | int,
    operator: str | int,
    session_id: str | None = None,
    *,
    bootstrap_recovery: bool = False,
) -> dict:
    """Remove a membership by operator slug or id. Raises if absent.

    Removing the *last* ``owner`` is refused for the same reason a demotion is:
    it would leave an Org nobody can administer. The delete is conditional on
    another owner still existing, so two concurrent removals cannot both
    succeed.
    """
    init_db()
    with _db_module.SessionLocal() as session:
        org = _get_org_row(session, org_ref)
        if org is None:
            raise ValueError(f"unknown org: {org_ref!r}")
        if isinstance(operator, int) or (isinstance(operator, str) and operator.isdigit()):
            operator_id = int(operator)
        else:
            op = _operator_by_slug(session, operator)
            if op is None:
                raise ValueError(f"unknown operator: {operator!r}")
            operator_id = op.id
        member = (
            session.query(OrgMember)
            .filter(OrgMember.org_id == org.id, OrgMember.operator_id == operator_id)
            .one_or_none()
        )
        if member is None:
            raise ValueError(f"operator {operator!r} is not a member of {org_ref!r}")
        if member.role == "owner" and not bootstrap_recovery:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    delete(OrgMember).where(
                        OrgMember.org_id == org.id,
                        OrgMember.operator_id == operator_id,
                        OrgMember.role == "owner",
                        _owner_count_subquery(org.id) > 1,
                    )
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                raise LastOwnerError(
                    f"org {org.id} would be left with no owner; promote another owner first"
                )
        else:
            session.delete(member)
        org_id = org.id
        session.commit()
    append_event(
        "org_member_removed",
        f"operator {operator} removed from org {org_ref}",
        session_id=session_id,
        metadata={"operator": str(operator)},
    )
    return {"removed": True, "org_id": org_id, "operator_id": operator_id}


def list_members(org_ref: str | int) -> list[dict]:
    init_db()
    with _db_module.SessionLocal() as session:
        org = _get_org_row(session, org_ref)
        if org is None:
            raise ValueError(f"unknown org: {org_ref!r}")
        rows = (
            session.query(OrgMember, Operator)
            .join(Operator, Operator.id == OrgMember.operator_id)
            .filter(OrgMember.org_id == org.id)
            .order_by(Operator.slug)
            .all()
        )
        return [_member_to_dict(member, op.slug) for member, op in rows]


def archive_org(org_ref: str | int, session_id: str | None = None) -> dict:
    init_db()
    with _db_module.SessionLocal() as session:
        org = _get_org_row(session, org_ref)
        if org is None:
            raise ValueError(f"unknown org: {org_ref!r}")
        org.status = "archived"
        org.updated_at = utc_now()
        session.commit()
        session.refresh(org)
        result = _org_to_dict(org)
    append_event(
        "org_archived",
        f"{result['slug']} archived",
        session_id=session_id,
        metadata={"slug": result["slug"]},
    )
    return result

"""Pods - teams of **Personas** with one leader (F5, BL-P1-03).

A Pod is the stable assignee for work a single Persona should not own alone.
Its roster is Personas, not operator labels: an operator is a human principal,
and routing an Issue to a human's login says nothing about which model, tool
and machine will actually run it.

What this module owns
---------------------

**One Org.** A Pod belongs to exactly one Org, recorded on ``pod_profiles``.
Every membership and leadership change is refused when the Persona belongs to
another Org, so a Pod can never become a cross-Org routing hole.

**One leader.** ``pod_profiles.leader_persona_id`` is the single leader.
Replacing it is deterministic: the new leader is rostered if it was not
already, the previous leader stays on the roster as a plain member, and a
leader that is archived, unknown or from another Org is refused with the
reason rather than silently ignored.

**A roster with a lifecycle.** Members are added, removed and the Pod is
archived. The leader cannot be removed - replace the leader first - and an
archived Pod refuses roster changes and dispatch instead of accepting writes
that would never execute.

**Deterministic dispatch.** :func:`resolve_dispatch` picks the member that
will run the work by a documented, stable order (leader first, then members by
Persona id) and returns the first one whose Persona is active and bound to an
online Runtime in the Pod's Org whose tool the Persona can drive. When none
qualifies, it returns an explicit blocked reason per candidate rather than a
silent ``None``.

Relationship to the legacy squad row
------------------------------------

The Pod's identity is still the ``squads`` row: ``issues.assignee_pod_id`` and
``projects.assignee_pod_id`` reference it, and the legacy workspace task
routing in :mod:`brains.control.squads` still reads its operator columns. So
``squads.leader_operator_id`` is retained as the operator principal that owns
that legacy row and is reported separately as ``legacy_leader_operator``; it
is not the Pod's leader. Legacy ``squad_members`` operator rows that resolve
to exactly one active Persona in the Pod's Org were migrated into
``pod_members``; the rest are reported as ``legacy_operator_members`` with the
reason they could not be resolved, and they are never dispatched.

Pure control logic - no FastAPI.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from brains.audit import record as audit_record
from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    Operator,
    Org,
    Persona,
    PodMember,
    PodProfile,
    Runtime,
    Squad,
    SquadMember,
    Workspace,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

#: Stable reasons a Pod (or one of its members) cannot take work. These are
#: part of the API contract: the console renders them and tests assert them.
POD_BLOCKED_REASONS = (
    "pod_archived",
    "pod_empty",
    "pod_no_leader",
    "pod_no_capable_member",
    "persona_archived",
    "persona_no_runtime",
    "runtime_unknown",
    "runtime_other_org",
    "runtime_offline",
    "runtime_tool_mismatch",
)


class PodError(ValueError):
    """A refused Pod operation. The message is the operator-facing reason."""


# --------------------------------------------------------------------------- #
# Row helpers
# --------------------------------------------------------------------------- #


def _squad(session, pod_id: int) -> Squad:
    row = session.get(Squad, pod_id)
    if row is None:
        raise PodError(f"unknown pod: {pod_id}")
    return row


def _profile(session, pod_id: int) -> PodProfile:
    """The Pod's product record, provisioned for a legacy squad on first read.

    The 134 migration writes one profile per squad that existed when it ran; a
    squad created afterwards by the legacy workspace API still needs one, and
    creating it here keeps that Pod readable instead of 500-ing on a missing
    row.
    """
    row = session.query(PodProfile).filter(PodProfile.pod_id == pod_id).one_or_none()
    if row is not None:
        return row
    squad = _squad(session, pod_id)
    workspace = session.get(Workspace, squad.workspace_id)
    row = PodProfile(
        pod_id=pod_id,
        org_id=workspace.org_id if workspace is not None else None,
        leader_persona_id=None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _persona_row(session, ref: str | int, *, org_id: int | None) -> Persona | None:
    if isinstance(ref, int):
        return session.get(Persona, ref)
    if isinstance(ref, str) and ref.isdigit():
        return session.get(Persona, int(ref))
    query = session.query(Persona).filter(Persona.slug == str(ref))
    if org_id is not None:
        query = query.filter(Persona.org_id == org_id)
    return query.order_by(Persona.id).first()


def _require_persona(session, ref: str | int, *, org_id: int | None) -> Persona:
    persona = _persona_row(session, ref, org_id=org_id)
    if persona is None:
        raise PodError(f"unknown persona: {ref!r}")
    if org_id is not None and persona.org_id != org_id:
        raise PodError(
            f"persona {persona.slug!r} belongs to org {persona.org_id}, not to this pod's "
            f"org {org_id}; a Pod's members must share its Org"
        )
    return persona


def _runtime_view(session, persona: Persona, org_id: int | None) -> dict[str, Any]:
    """Whether this Persona can run work now, and why not when it cannot."""
    if persona.status != "active":
        return {"dispatchable": False, "blocked_reason": "persona_archived", "runtime": None}
    if persona.default_runtime_id is None:
        return {"dispatchable": False, "blocked_reason": "persona_no_runtime", "runtime": None}
    runtime = session.get(Runtime, persona.default_runtime_id)
    if runtime is None:
        return {"dispatchable": False, "blocked_reason": "runtime_unknown", "runtime": None}
    view = {
        "runtime_id": runtime.id,
        "runtime_slug": runtime.slug,
        "runtime_status": runtime.status,
        "runtime_tool": runtime.tool,
        "runtime_working_root": runtime.working_root,
    }
    if org_id is not None and runtime.org_id is not None and runtime.org_id != org_id:
        return {"dispatchable": False, "blocked_reason": "runtime_other_org", **view}
    if runtime.status != "online":
        return {"dispatchable": False, "blocked_reason": "runtime_offline", **view}
    if persona.tool and runtime.tool and persona.tool != runtime.tool:
        return {"dispatchable": False, "blocked_reason": "runtime_tool_mismatch", **view}
    return {"dispatchable": True, "blocked_reason": None, **view}


def _member_dict(session, member: PodMember, persona: Persona, profile: PodProfile) -> dict:
    view = _runtime_view(session, persona, profile.org_id)
    is_leader = profile.leader_persona_id == persona.id
    return {
        "persona_id": persona.id,
        "persona_slug": persona.slug,
        "persona_name": persona.name,
        # The console renders ``name`` for any roster entry.
        "name": persona.name,
        "role": "leader" if is_leader else member.role,
        "is_leader": is_leader,
        "status": persona.status,
        "model": persona.model,
        "tool": persona.tool,
        "source": member.source,
        "runtime_id": view.get("runtime_id"),
        "runtime_slug": view.get("runtime_slug"),
        "runtime_status": view.get("runtime_status"),
        "dispatchable": view["dispatchable"],
        "blocked_reason": view["blocked_reason"],
    }


def _ordered_members(session, pod_id: int, profile: PodProfile) -> list[tuple[PodMember, Persona]]:
    """The roster in dispatch order: leader first, then Personas by id.

    The order is the contract, not an implementation detail: it is what makes
    Pod dispatch deterministic across processes and reruns.
    """
    rows = (
        session.query(PodMember, Persona)
        .join(Persona, Persona.id == PodMember.persona_id)
        .filter(PodMember.pod_id == pod_id)
        .order_by(PodMember.persona_id)
        .all()
    )
    leader_id = profile.leader_persona_id
    leaders = [pair for pair in rows if pair[1].id == leader_id]
    others = [pair for pair in rows if pair[1].id != leader_id]
    return leaders + others


def _legacy_members(session, pod_id: int, profile: PodProfile) -> list[dict]:
    """Legacy operator memberships that no Persona could be derived from."""
    migrated = {
        persona_id
        for (persona_id,) in session.query(PodMember.persona_id).filter(PodMember.pod_id == pod_id)
    }
    out: list[dict] = []
    rows = (
        session.query(SquadMember, Operator)
        .join(Operator, Operator.id == SquadMember.operator_id)
        .filter(SquadMember.squad_id == pod_id)
        .order_by(Operator.slug)
        .all()
    )
    for member, operator in rows:
        query = session.query(Persona).filter(
            Persona.operator_id == operator.id, Persona.status == "active"
        )
        if profile.org_id is not None:
            query = query.filter(Persona.org_id == profile.org_id)
        candidates = query.all()
        if len(candidates) == 1 and candidates[0].id in migrated:
            continue
        reason = (
            "operator has no active Persona in this Pod's Org"
            if not candidates
            else "operator maps to more than one active Persona; the Pod cannot pick one"
        )
        out.append(
            {
                "operator": operator.slug,
                "name": operator.display_name or operator.slug,
                "role": member.role,
                "reason": reason,
                "dispatchable": False,
            }
        )
    return out


def _pod_dict(session, pod_id: int) -> dict:
    squad = _squad(session, pod_id)
    profile = _profile(session, pod_id)
    workspace = session.get(Workspace, squad.workspace_id)
    leader = (
        session.get(Persona, profile.leader_persona_id)
        if profile.leader_persona_id is not None
        else None
    )
    legacy_leader = session.get(Operator, squad.leader_operator_id)
    members = [
        _member_dict(session, member, persona, profile)
        for member, persona in _ordered_members(session, pod_id, profile)
    ]
    return {
        "id": squad.id,
        "slug": squad.slug,
        "name": squad.name,
        "description": squad.description,
        "status": squad.status,
        "org_id": profile.org_id,
        "workspace": workspace.slug if workspace is not None else None,
        "leader_persona_id": profile.leader_persona_id,
        "leader_persona": leader.slug if leader is not None else None,
        # ``leader`` stays in the payload because the console and the CLI read
        # it, but it now names the leader **Persona**. A Pod with no leader
        # Persona reports ``None`` rather than falling back to the legacy
        # operator, which is not the leader.
        "leader": leader.slug if leader is not None else None,
        "legacy_leader_operator": legacy_leader.slug if legacy_leader is not None else None,
        "members": members,
        "legacy_operator_members": _legacy_members(session, pod_id, profile),
        "created_at": squad.created_at.isoformat() if squad.created_at else None,
        "archived_at": squad.archived_at.isoformat() if squad.archived_at else None,
    }


def _require_active(squad: Squad) -> None:
    if squad.status != "active":
        raise PodError(f"pod {squad.slug!r} is archived; restore it before changing its roster")


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def _org_workspace_path(session, org_id: int | None) -> str | None:
    query = session.query(Workspace).filter(Workspace.status == "active")
    query = (
        query.filter(Workspace.org_id.is_(None))
        if org_id is None
        else query.filter(Workspace.org_id == org_id)
    )
    workspace = query.order_by(Workspace.id).first()
    return workspace.path if workspace is not None else None


def _resolve_pod_workspace(org_id: int | None) -> Workspace:
    """The Workspace the legacy squad row lives on, auto-provisioned if absent."""
    init_db()
    with SessionLocal() as session:
        path = _org_workspace_path(session, org_id)
    if path is None:
        path = os.path.join(tempfile.gettempdir(), f"brains-org-{org_id or 'default'}-pods")
        workspace = register_workspace(path, name=f"org-{org_id or 'default'}-pods")
        if org_id is not None:
            with SessionLocal() as session:
                row = session.get(Workspace, workspace.id)
                if row is not None:
                    row.org_id = org_id
                    session.commit()
        return workspace
    return register_workspace(path)


def _legacy_owner_operator_id(session, leader: Persona | None, operator: str | None) -> int:
    """The operator principal that owns the legacy ``squads`` row.

    ``squads.leader_operator_id`` is NOT NULL in the frozen baseline and the
    legacy workspace task routing still reads it. It is the *owner* of that
    row - the leader Persona's bound principal when it has one, otherwise the
    principal that created the Pod - and it is reported separately from the
    Pod's leader so nothing here implies an operator leads the Pod.
    """
    from brains.control.operators import resolve_current_operator

    if leader is not None and leader.operator_id is not None:
        return int(leader.operator_id)
    return int(resolve_current_operator(operator=operator)["id"])


def create_pod(
    org_id: int,
    slug: str,
    name: str,
    *,
    leader_persona: str | int | None = None,
    description: str = "",
    operator: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Create a Pod in an Org, optionally with its leader Persona.

    Raises :class:`PodError` on a bad slug, an unknown Org, a duplicate Pod, or
    a leader Persona that is unknown, archived, or in another Org.
    """
    if not SLUG_PATTERN.match(slug):
        raise PodError("pod slug must be lowercase alphanumeric with - or _ (max 63 chars)")
    init_db()
    with SessionLocal() as session:
        if session.get(Org, org_id) is None:
            raise PodError(f"unknown org id: {org_id!r}")
    workspace = _resolve_pod_workspace(org_id)
    with SessionLocal() as session:
        existing = (
            session.query(Squad)
            .filter(Squad.workspace_id == workspace.id, Squad.slug == slug)
            .one_or_none()
        )
        if existing is not None:
            raise PodError(f"pod {slug!r} already exists in this org")
        leader = None
        if leader_persona is not None:
            leader = _require_persona(session, leader_persona, org_id=org_id)
            if leader.status != "active":
                raise PodError(f"persona {leader.slug!r} is archived and cannot lead a Pod")
        squad = Squad(
            workspace_id=workspace.id,
            slug=slug,
            name=name,
            description=description or None,
            leader_operator_id=_legacy_owner_operator_id(session, leader, operator),
            created_by_session_id=session_id,
        )
        session.add(squad)
        session.flush()
        session.add(
            PodProfile(
                pod_id=squad.id,
                org_id=org_id,
                leader_persona_id=leader.id if leader is not None else None,
            )
        )
        if leader is not None:
            session.add(
                PodMember(pod_id=squad.id, persona_id=leader.id, role="leader", source="api")
            )
        session.commit()
        pod_id = squad.id
        result = _pod_dict(session, pod_id)
    append_event(
        "squad_created",
        f"{slug}: {name}"
        + (f" (leader persona {result['leader_persona']})" if result["leader_persona"] else ""),
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={
            "pod_id": pod_id,
            "slug": slug,
            "org_id": org_id,
            "leader_persona_id": result["leader_persona_id"],
        },
    )
    audit_record(
        actor=f"session:{session_id}" if session_id else "system",
        action="pod.create",
        workspace_id=workspace.id,
        payload={"pod_id": pod_id, "slug": slug, "org_id": org_id},
    )
    return result


def get_pod(pod_id: int) -> dict | None:
    init_db()
    with SessionLocal() as session:
        if session.get(Squad, pod_id) is None:
            return None
        return _pod_dict(session, pod_id)


def list_pods(org_id: int | None, *, include_archived: bool = False) -> list[dict]:
    """Every Pod in an Org, with its Persona roster.

    Pods created through the legacy workspace API carry no product record yet,
    so they are resolved through their Workspace's Org as well and given one on
    read. A legacy Pod is therefore visible and classified rather than missing.
    Returns a clean empty list - never a refusal - for an Org with no Pods.
    """
    init_db()
    with SessionLocal() as session:
        by_profile = {
            row.pod_id
            for row in session.query(PodProfile).filter(PodProfile.org_id == org_id).all()
        }
        workspace_query = session.query(Workspace.id)
        workspace_query = (
            workspace_query.filter(Workspace.org_id.is_(None))
            if org_id is None
            else workspace_query.filter(Workspace.org_id == org_id)
        )
        workspace_ids = [row.id for row in workspace_query.all()]
        by_workspace: set[int] = set()
        if workspace_ids:
            by_workspace = {
                row.id
                for row in session.query(Squad.id)
                .filter(Squad.workspace_id.in_(workspace_ids))
                .all()
            }
        candidates = sorted(by_profile | by_workspace)
        pods: list[dict] = []
        for pod_id in candidates:
            squad = session.get(Squad, pod_id)
            if squad is None:
                continue
            if not include_archived and squad.status != "active":
                continue
            profile = _profile(session, pod_id)
            if profile.org_id is None and org_id is not None:
                profile.org_id = org_id
                profile.updated_at = utc_now()
                session.commit()
            pods.append(_pod_dict(session, pod_id))
        return sorted(pods, key=lambda row: row["slug"])


def add_member(
    pod_id: int,
    persona_ref: str | int,
    *,
    role: str = "member",
    session_id: str | None = None,
) -> dict:
    """Add (or re-role) a Persona in a Pod. Idempotent on ``(pod, persona)``."""
    init_db()
    with SessionLocal() as session:
        squad = _squad(session, pod_id)
        _require_active(squad)
        profile = _profile(session, pod_id)
        persona = _require_persona(session, persona_ref, org_id=profile.org_id)
        if persona.status != "active":
            raise PodError(f"persona {persona.slug!r} is archived and cannot join a Pod")
        member = (
            session.query(PodMember)
            .filter(PodMember.pod_id == pod_id, PodMember.persona_id == persona.id)
            .one_or_none()
        )
        if member is None:
            session.add(PodMember(pod_id=pod_id, persona_id=persona.id, role=role, source="api"))
        elif profile.leader_persona_id != persona.id:
            member.role = role
        session.commit()
        persona_slug = persona.slug
        workspace_id = squad.workspace_id
        result = _pod_dict(session, pod_id)
    append_event(
        "squad_member_added",
        f"persona {persona_slug} joined pod {result['slug']} as {role}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"pod_id": pod_id, "persona": persona_slug, "role": role},
    )
    return result


def remove_member(
    pod_id: int,
    persona_ref: str | int,
    *,
    session_id: str | None = None,
) -> dict:
    """Remove a Persona from a Pod. The leader is refused, not silently kept."""
    init_db()
    with SessionLocal() as session:
        squad = _squad(session, pod_id)
        _require_active(squad)
        profile = _profile(session, pod_id)
        persona = _require_persona(session, persona_ref, org_id=profile.org_id)
        if profile.leader_persona_id == persona.id:
            raise PodError(
                f"persona {persona.slug!r} leads this pod; assign a new leader before removing it"
            )
        deleted = (
            session.query(PodMember)
            .filter(PodMember.pod_id == pod_id, PodMember.persona_id == persona.id)
            .delete()
        )
        if not deleted:
            raise PodError(f"persona {persona.slug!r} is not a member of this pod")
        session.commit()
        persona_slug = persona.slug
        workspace_id = squad.workspace_id
        result = _pod_dict(session, pod_id)
    append_event(
        "squad_member_removed",
        f"persona {persona_slug} left pod {result['slug']}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"pod_id": pod_id, "persona": persona_slug},
    )
    return result


def set_leader(
    pod_id: int,
    persona_ref: str | int,
    *,
    session_id: str | None = None,
) -> dict:
    """Replace the Pod's leader.

    The new leader is rostered if it was not already and the previous leader
    stays on the roster as a plain member, so the one-leader rule holds without
    losing anyone. An unknown, archived or cross-Org Persona is refused.
    """
    init_db()
    with SessionLocal() as session:
        squad = _squad(session, pod_id)
        _require_active(squad)
        profile = _profile(session, pod_id)
        persona = _require_persona(session, persona_ref, org_id=profile.org_id)
        if persona.status != "active":
            raise PodError(f"persona {persona.slug!r} is archived and cannot lead a Pod")
        previous_id = profile.leader_persona_id
        if previous_id is not None and previous_id != persona.id:
            previous = (
                session.query(PodMember)
                .filter(PodMember.pod_id == pod_id, PodMember.persona_id == previous_id)
                .one_or_none()
            )
            if previous is not None:
                previous.role = "member"
        member = (
            session.query(PodMember)
            .filter(PodMember.pod_id == pod_id, PodMember.persona_id == persona.id)
            .one_or_none()
        )
        if member is None:
            session.add(
                PodMember(pod_id=pod_id, persona_id=persona.id, role="leader", source="api")
            )
        else:
            member.role = "leader"
        profile.leader_persona_id = persona.id
        profile.updated_at = utc_now()
        session.commit()
        persona_slug = persona.slug
        workspace_id = squad.workspace_id
        result = _pod_dict(session, pod_id)
    append_event(
        "squad_leader_changed",
        f"pod {result['slug']} is now led by persona {persona_slug}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"pod_id": pod_id, "leader_persona": persona_slug, "previous": previous_id},
    )
    return result


def archive_pod(pod_id: int, *, session_id: str | None = None) -> dict:
    """Archive a Pod. Idempotent; an archived Pod refuses roster changes."""
    init_db()
    with SessionLocal() as session:
        squad = _squad(session, pod_id)
        if squad.status == "active":
            squad.status = "archived"
            squad.archived_at = utc_now()
            session.commit()
        workspace_id = squad.workspace_id
        result = _pod_dict(session, pod_id)
    append_event(
        "squad_archived",
        f"pod {result['slug']} archived",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"pod_id": pod_id},
    )
    return result


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def resolve_dispatch(pod_id: int) -> dict:
    """Which member Persona runs this Pod's work, or why none can.

    Returns ``{"pod_id", "org_id", "leader_persona_id", "persona_id",
    "runtime_id", "tool", "blocked_reason", "candidates"}``. ``candidates``
    lists every Persona considered, in the order they were considered, with
    the reason each was skipped - so a refusal is inspectable rather than a
    bare "no".
    """
    init_db()
    with SessionLocal() as session:
        squad = _squad(session, pod_id)
        profile = _profile(session, pod_id)
        base: dict[str, Any] = {
            "pod_id": pod_id,
            "pod_slug": squad.slug,
            "org_id": profile.org_id,
            "leader_persona_id": profile.leader_persona_id,
            "persona_id": None,
            "runtime_id": None,
            "tool": None,
            "candidates": [],
        }
        if squad.status != "active":
            return {**base, "blocked_reason": "pod_archived"}
        pairs = _ordered_members(session, pod_id, profile)
        if not pairs:
            return {**base, "blocked_reason": "pod_empty"}
        if profile.leader_persona_id is None:
            return {**base, "blocked_reason": "pod_no_leader"}
        candidates: list[dict] = []
        for _member, persona in pairs:
            view = _runtime_view(session, persona, profile.org_id)
            candidates.append(
                {
                    "persona_id": persona.id,
                    "persona_slug": persona.slug,
                    "is_leader": persona.id == profile.leader_persona_id,
                    "runtime_id": view.get("runtime_id"),
                    "dispatchable": view["dispatchable"],
                    "blocked_reason": view["blocked_reason"],
                }
            )
            if view["dispatchable"]:
                return {
                    **base,
                    "persona_id": persona.id,
                    "persona_slug": persona.slug,
                    "runtime_id": view["runtime_id"],
                    "runtime_working_root": view.get("runtime_working_root"),
                    "tool": persona.tool or view.get("runtime_tool"),
                    "blocked_reason": None,
                    "candidates": candidates,
                }
        return {**base, "candidates": candidates, "blocked_reason": "pod_no_capable_member"}


def member_persona_ids(pod_id: int) -> list[int]:
    """The Pod's member Persona ids in dispatch order (leader first)."""
    init_db()
    with SessionLocal() as session:
        if session.get(Squad, pod_id) is None:
            return []
        profile = _profile(session, pod_id)
        return [persona.id for _member, persona in _ordered_members(session, pod_id, profile)]

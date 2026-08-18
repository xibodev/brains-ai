"""Squads — leader-routed team assignment.

A *squad* is a named group of operators with a designated **leader**, scoped to
a workspace. Work routed to a squad is handed to its leader, who decides which
member should pick it up and delegates to them. This layers stable team routing
on top of brains' pull-based task model: an assigner addresses ``@squad`` rather
than guessing which individual operator is free, and routing survives
membership changes.

Design (clean-room, inspired by the "leader delegates from a roster" idea):

* The squad is the stable assignee; the **leader is the router**.
* Assigning work to a squad enqueues a single task tagged to the squad and
  produces a **leader brief** — the roster (each member's role + demonstrated
  skills) plus the task — for the leader *agent* to reason over. brains does not
  pick the member with a hardcoded algorithm; the leader does.
* The leader delegates with :func:`delegate_task`, which records the routing
  decision (event + audit) and tags the task with the chosen operator. Dedup /
  anti-loop guards keep a leader from re-delegating an already-delegated task.

Everything here reuses existing primitives: operators, workspaces, tasks,
events, audit, and (as the "skills" signal) the per-operator knowledge ledger.
"""

from __future__ import annotations

import re

from brains.audit import record as audit_record
from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentTask,
    KnowledgeEntry,
    Operator,
    Squad,
    SquadMember,
    Workspace,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SQUAD_TAG_PREFIX = "squad:"
DELEGATED_TAG_PREFIX = "delegated-to:"


def _audit_actor(session_id: str | None) -> str:
    return f"session:{session_id}" if session_id else "system"


def _operator_by_slug(session, slug: str) -> Operator | None:
    return session.query(Operator).filter(Operator.slug == slug).one_or_none()


def _squad_to_dict(squad: Squad, workspace_slug: str, leader_slug: str) -> dict:
    return {
        "slug": squad.slug,
        "name": squad.name,
        "workspace": workspace_slug,
        "description": squad.description,
        "leader": leader_slug,
        "status": squad.status,
        "created_at": squad.created_at.isoformat(),
        "archived_at": squad.archived_at.isoformat() if squad.archived_at else None,
    }


def create_squad(
    workspace_path: str,
    slug: str,
    name: str,
    leader: str,
    description: str = "",
    session_id: str | None = None,
) -> dict:
    """Create a squad in a workspace with ``leader`` (an operator slug).

    The leader is auto-added to the roster with role ``leader`` so the roster is
    a single source of truth. Raises ``ValueError`` on a bad slug, a duplicate,
    or an unknown leader operator.
    """
    if not SLUG_PATTERN.match(slug):
        raise ValueError("squad slug must be lowercase alphanumeric with - or _ (max 63 chars)")
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        leader_op = _operator_by_slug(session, leader)
        if leader_op is None:
            raise ValueError(f"unknown leader operator: {leader!r}")
        existing = (
            session.query(Squad)
            .filter(Squad.workspace_id == workspace.id, Squad.slug == slug)
            .one_or_none()
        )
        if existing is not None:
            raise ValueError(f"squad {slug!r} already exists in this workspace")
        squad = Squad(
            workspace_id=workspace.id,
            slug=slug,
            name=name,
            description=description or None,
            leader_operator_id=leader_op.id,
            created_by_session_id=session_id,
        )
        session.add(squad)
        session.flush()
        session.add(SquadMember(squad_id=squad.id, operator_id=leader_op.id, role="leader"))
        session.commit()
        result = _squad_to_dict(squad, workspace.slug, leader_op.slug)
    append_event(
        "squad_created",
        f"{slug}: {name} (leader @{leader})",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"slug": slug, "leader": leader},
    )
    audit_record(
        actor=_audit_actor(session_id),
        action="squad.create",
        workspace_id=workspace.id,
        payload={"slug": slug, "leader": leader},
    )
    return result


def add_member(
    workspace_path: str,
    squad_slug: str,
    operator: str,
    role: str = "member",
    session_id: str | None = None,
) -> dict:
    """Add ``operator`` (a slug) to a squad with an informational ``role``.

    Idempotent on (squad, operator): re-adding updates the role.
    """
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        squad = _get_squad_row(session, workspace.id, squad_slug)
        op = _operator_by_slug(session, operator)
        if op is None:
            raise ValueError(f"unknown operator: {operator!r}")
        member = (
            session.query(SquadMember)
            .filter(SquadMember.squad_id == squad.id, SquadMember.operator_id == op.id)
            .one_or_none()
        )
        if member is None:
            session.add(SquadMember(squad_id=squad.id, operator_id=op.id, role=role))
        else:
            member.role = role
        session.commit()
    append_event(
        "squad_member_added",
        f"@{operator} joined {squad_slug} as {role}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"squad": squad_slug, "operator": operator, "role": role},
    )
    return roster(workspace_path, squad_slug)


def remove_member(
    workspace_path: str,
    squad_slug: str,
    operator: str,
    session_id: str | None = None,
) -> dict:
    """Remove ``operator`` from a squad. The leader cannot be removed (reassign
    the leader first by recreating/transferring)."""
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        squad = _get_squad_row(session, workspace.id, squad_slug)
        op = _operator_by_slug(session, operator)
        if op is None:
            raise ValueError(f"unknown operator: {operator!r}")
        if op.id == squad.leader_operator_id:
            raise ValueError("cannot remove the squad leader; transfer leadership first")
        session.query(SquadMember).filter(
            SquadMember.squad_id == squad.id, SquadMember.operator_id == op.id
        ).delete()
        session.commit()
    append_event(
        "squad_member_removed",
        f"@{operator} left {squad_slug}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"squad": squad_slug, "operator": operator},
    )
    return roster(workspace_path, squad_slug)


def _get_squad_row(session, workspace_id: int, slug: str) -> Squad:
    squad = (
        session.query(Squad)
        .filter(Squad.workspace_id == workspace_id, Squad.slug == slug)
        .one_or_none()
    )
    if squad is None:
        raise ValueError(f"unknown squad: {slug!r}")
    return squad


def list_squads(workspace_path: str, include_archived: bool = False) -> list[dict]:
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        query = session.query(Squad).filter(Squad.workspace_id == workspace.id)
        if not include_archived:
            query = query.filter(Squad.status == "active")
        out: list[dict] = []
        for squad in query.order_by(Squad.slug).all():
            leader = session.get(Operator, squad.leader_operator_id)
            out.append(_squad_to_dict(squad, workspace.slug, leader.slug if leader else "?"))
    return out


def list_squads_for_org(org_id: int | None, include_archived: bool = False) -> list[dict]:
    """List squads ("pods") across every workspace owned by an org.

    Pods are workspace-scoped (a ``squads`` row), but the console surfaces them
    org-scoped. We resolve the org's workspaces (treating a NULL ``org_id`` as
    the default org) and gather their squads. Returns an empty list when the org
    has no workspaces or no squads — a clean empty state, never a 404.
    """
    init_db()
    with SessionLocal() as session:
        ws_query = session.query(Workspace)
        if org_id is None:
            ws_query = ws_query.filter(Workspace.org_id.is_(None))
        else:
            ws_query = ws_query.filter(Workspace.org_id == org_id)
        workspace_ids = {w.id: w.slug for w in ws_query.all()}
        if not workspace_ids:
            return []
        query = session.query(Squad).filter(Squad.workspace_id.in_(workspace_ids.keys()))
        if not include_archived:
            query = query.filter(Squad.status == "active")
        out: list[dict] = []
        for squad in query.order_by(Squad.slug).all():
            leader = session.get(Operator, squad.leader_operator_id)
            member_rows = (
                session.query(SquadMember, Operator)
                .join(Operator, Operator.id == SquadMember.operator_id)
                .filter(SquadMember.squad_id == squad.id)
                .all()
            )
            members = [
                {
                    "persona_id": op.id,
                    "persona_name": op.display_name or op.slug,
                    "role": sm.role,
                }
                for sm, op in member_rows
            ]
            row = _squad_to_dict(
                squad, workspace_ids.get(squad.workspace_id, "?"), leader.slug if leader else "?"
            )
            row["id"] = squad.id
            row["members"] = members
            out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Org-scoped pod helpers (F5) — the console surfaces squads as org-scoped "pods".
# --------------------------------------------------------------------------- #


def _org_workspace_path(session, org_id: int | None) -> str | None:
    q = session.query(Workspace).filter(Workspace.status == "active")
    q = (
        q.filter(Workspace.org_id.is_(None))
        if org_id is None
        else q.filter(Workspace.org_id == org_id)
    )
    ws = q.order_by(Workspace.id).first()
    return ws.path if ws else None


def create_pod(
    org_id: int | None,
    slug: str,
    name: str,
    leader: str,
    description: str = "",
) -> dict:
    """Create a pod (squad) for an org. Pods are workspace-scoped; if the org has
    no workspace yet, one is auto-provisioned + stamped with the org so the pod
    surfaces org-scoped (no operator setup required)."""
    import os
    import tempfile

    init_db()
    with SessionLocal() as session:
        ws_path = _org_workspace_path(session, org_id)
    if ws_path is None:
        # Auto-provision an org-scoped workspace for pods.
        path = os.path.join(tempfile.gettempdir(), f"brains-org-{org_id or 'default'}-pods")
        ws = register_workspace(path, name=f"org-{org_id or 'default'}-pods")
        if org_id is not None:
            with SessionLocal() as session:
                row = session.get(Workspace, ws.id)
                if row is not None:
                    row.org_id = org_id
                    session.commit()
        ws_path = ws.path
    result = create_squad(ws_path, slug, name, leader, description)
    with SessionLocal() as session:
        squad = session.query(Squad).filter(Squad.slug == slug).order_by(Squad.id.desc()).first()
        if squad is not None:
            result["id"] = squad.id
    return result


def get_pod(pod_id: int) -> dict | None:
    init_db()
    with SessionLocal() as session:
        squad = session.get(Squad, pod_id)
        if squad is None:
            return None
        ws = session.get(Workspace, squad.workspace_id)
        leader = session.get(Operator, squad.leader_operator_id)
        row = _squad_to_dict(squad, ws.slug if ws else "?", leader.slug if leader else "?")
        row["id"] = squad.id
        member_rows = (
            session.query(SquadMember, Operator)
            .join(Operator, Operator.id == SquadMember.operator_id)
            .filter(SquadMember.squad_id == squad.id)
            .all()
        )
        row["members"] = [
            {"operator": op.slug, "name": op.display_name or op.slug, "role": sm.role}
            for sm, op in member_rows
        ]
        return row


def _require_pod(pod_id: int) -> dict:
    """The pod as a dict, or a refusal when it disappeared mid-operation."""
    row = get_pod(pod_id)
    if row is None:  # pragma: no cover - the pod was verified in the same call
        raise ValueError(f"unknown pod: {pod_id}")
    return row


def add_pod_member(pod_id: int, operator: str, role: str = "member") -> dict:
    """Add an operator to a pod by pod id (F5)."""
    init_db()
    with SessionLocal() as session:
        squad = session.get(Squad, pod_id)
        if squad is None:
            raise ValueError(f"unknown pod: {pod_id}")
        ws = session.get(Workspace, squad.workspace_id)
        ws_path, slug = (ws.path if ws else None), squad.slug
    if ws_path is None:
        raise ValueError(f"pod {pod_id} has no workspace")
    add_member(ws_path, slug, operator, role=role)
    return _require_pod(pod_id)


def set_pod_leader(pod_id: int, leader: str) -> dict:
    """Set the pod's leader (an operator slug); leader is also rostered (F5)."""
    init_db()
    with SessionLocal() as session:
        squad = session.get(Squad, pod_id)
        if squad is None:
            raise ValueError(f"unknown pod: {pod_id}")
        leader_op = _operator_by_slug(session, leader)
        if leader_op is None:
            raise ValueError(f"unknown leader operator: {leader!r}")
        squad.leader_operator_id = leader_op.id
        member = (
            session.query(SquadMember)
            .filter(SquadMember.squad_id == squad.id, SquadMember.operator_id == leader_op.id)
            .one_or_none()
        )
        if member is None:
            session.add(SquadMember(squad_id=squad.id, operator_id=leader_op.id, role="leader"))
        else:
            member.role = "leader"
        session.commit()
    return _require_pod(pod_id)


def _operator_skills(session, operator_id: int, workspace_id: int, limit: int = 5) -> list[str]:
    """Lightweight "skills" signal for the routing brief: subjects of the
    knowledge this operator has contributed in the workspace. This reuses the
    existing per-operator knowledge ledger rather than inventing a skills store.
    """
    rows = (
        session.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.created_by_operator_id == operator_id,
            KnowledgeEntry.workspace_id == workspace_id,
        )
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(limit)
        .all()
    )
    subjects: list[str] = []
    for row in rows:
        subject = getattr(row, "subject", None) or getattr(row, "title", None)
        if subject and subject not in subjects:
            subjects.append(str(subject))
    return subjects


def roster(workspace_path: str, squad_slug: str) -> dict:
    """Return the squad's full roster with each member's role + skill signal.

    This is the structured form the leader's routing brief is built from.
    """
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        squad = _get_squad_row(session, workspace.id, squad_slug)
        leader = session.get(Operator, squad.leader_operator_id)
        members = []
        rows = (
            session.query(SquadMember, Operator)
            .join(Operator, Operator.id == SquadMember.operator_id)
            .filter(SquadMember.squad_id == squad.id)
            .order_by(SquadMember.role.desc(), Operator.slug)
            .all()
        )
        for member, op in rows:
            members.append(
                {
                    "operator": op.slug,
                    "role": member.role,
                    "is_leader": op.id == squad.leader_operator_id,
                    "skills": _operator_skills(session, op.id, workspace.id),
                }
            )
        return {
            "slug": squad.slug,
            "name": squad.name,
            "workspace": workspace.slug,
            "leader": leader.slug if leader else "?",
            "description": squad.description,
            "members": members,
        }


def assign_task_to_squad(
    workspace_path: str,
    squad_slug: str,
    title: str,
    body: str = "",
    priority: str = "p2",
    session_id: str | None = None,
) -> dict:
    """Route a new task to a squad: create the task tagged to the squad and
    return a **leader brief** the leader agent uses to delegate.

    The task is created ``available`` and tagged ``squad:<slug>`` so it shows up
    as the squad's work; the returned brief instructs the leader to pick one
    member and delegate via :func:`delegate_task`.
    """
    from brains.control.tasks import create_task

    workspace = register_workspace(workspace_path)
    # Validate the squad exists up front (clear error before minting a task).
    squad_roster = roster(workspace_path, squad_slug)
    task = create_task(
        workspace_path,
        title,
        body=body,
        priority=priority,
        tags=f"{SQUAD_TAG_PREFIX}{squad_slug}",
        session_id=session_id,
    )
    append_event(
        "squad_task_assigned",
        f"{task['code']} -> @{squad_slug} (leader @{squad_roster['leader']})",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"task": task["code"], "squad": squad_slug, "leader": squad_roster["leader"]},
    )
    audit_record(
        actor=_audit_actor(session_id),
        action="squad.assign",
        workspace_id=workspace.id,
        payload={"task": task["code"], "squad": squad_slug},
    )
    return {
        "task": task,
        "squad": squad_slug,
        "leader": squad_roster["leader"],
        "brief": _leader_brief(squad_roster, task),
    }


def _leader_brief(squad_roster: dict, task: dict) -> str:
    """Render the routing brief the leader agent reasons over. Text, not an
    algorithm — the leader chooses the member."""
    lines = [
        f"You are @{squad_roster['leader']}, leader of squad @{squad_roster['slug']} "
        f"({squad_roster['name']}).",
        f"New work routed to your squad: {task['code']} — {task['title']}.",
    ]
    if task.get("body"):
        lines.append(f"Details: {task['body']}")
    lines.append("")
    lines.append("Roster (pick the best member, then delegate — do not do it yourself):")
    for m in squad_roster["members"]:
        if m["is_leader"]:
            continue
        skills = ", ".join(m["skills"]) if m["skills"] else "no recorded skills yet"
        lines.append(f"  - @{m['operator']} ({m['role']}) — skills: {skills}")
    lines.append("")
    lines.append(
        f"Delegate by calling squad_delegate(task_code={task['code']!r}, "
        "to_operator=<chosen member slug>, note=<why>). Delegate exactly once."
    )
    return "\n".join(lines)


def delegate_task(
    task_code: str,
    to_operator: str,
    note: str = "",
    session_id: str | None = None,
) -> dict:
    """Record a leader's delegation of a squad task to a member operator.

    Tags the task ``delegated-to:<operator>`` and records the decision. Anti-loop
    guard: a task already delegated cannot be re-delegated (raises ValueError) so
    a leader re-reading its inbox can't fan the same task out repeatedly.
    """
    init_db()
    with SessionLocal() as session:
        task = session.query(AgentTask).filter(AgentTask.code == task_code).one_or_none()
        if task is None:
            raise ValueError(f"unknown task: {task_code!r}")
        op = _operator_by_slug(session, to_operator)
        if op is None:
            raise ValueError(f"unknown operator: {to_operator!r}")
        tags = task.tags or ""
        if DELEGATED_TAG_PREFIX in tags:
            existing = next(
                (
                    t.split(":", 1)[1]
                    for t in tags.split(",")
                    if t.strip().startswith(DELEGATED_TAG_PREFIX)
                ),
                "?",
            )
            raise ValueError(f"{task_code} is already delegated to @{existing}; cannot re-delegate")
        new_tag = f"{DELEGATED_TAG_PREFIX}{to_operator}"
        task.tags = f"{tags},{new_tag}" if tags else new_tag
        workspace_id = task.workspace_id
        session.commit()
    append_event(
        "squad_task_delegated",
        f"{task_code} delegated to @{to_operator}" + (f": {note}" if note else ""),
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"task": task_code, "to": to_operator, "note": note},
    )
    audit_record(
        actor=_audit_actor(session_id),
        action="squad.delegate",
        workspace_id=workspace_id,
        payload={"task": task_code, "to": to_operator},
    )
    return {
        "task": task_code,
        "delegated_to": to_operator,
        "note": note,
        "at": utc_now().isoformat(),
    }

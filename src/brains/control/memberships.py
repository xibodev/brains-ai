"""Layer 2 of the multi-operator model — workspace memberships.

A workspace has a ``visibility`` (``shared`` or ``private``) and a set
of explicit ``WorkspaceMembership`` rows. The visibility rules are:

* ``admin`` operator sees everything (implicit membership, never needs
  a row, never appears in :func:`list_memberships`).
* ``shared`` workspaces are visible to every operator (back-compat
  default — single-operator and pre-Layer-2 installs see no change).
* ``private`` workspaces are visible only to operators with an explicit
  membership row.

The public entrypoints are:

* :func:`add_membership` / :func:`remove_membership` — CLI verbs.
* :func:`list_memberships` — dashboard + CLI introspection.
* :func:`set_workspace_visibility` — flip a workspace shared/private.
* :func:`operator_can_see_workspace` — pointwise check used in URL
  handlers and MCP tool surfaces that load a single workspace.
* :func:`visible_workspace_ids` — bulk filter used by every coordination
  ``list_*`` function. Returns ``None`` for admin (sentinel meaning
  "no filter") so existing query plans stay identical.

The module uses ``_db_module.SessionLocal()`` (not the captured
``SessionLocal`` symbol) so test fixtures that rebind the engine for an
isolated database pick up the new engine automatically — same pattern
as :mod:`brains.control.operators`.
"""

from __future__ import annotations

from typing import TypedDict

from brains.control.operators import (
    ADMIN_SLUG,
    SLUG_PATTERN,
    OperatorRecord,
    OperatorSlugError,
)
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import (
    Operator,
    Workspace,
    WorkspaceMembership,
)

ALLOWED_VISIBILITIES = frozenset({"shared", "private"})
ALLOWED_ROLES = frozenset({"member", "owner"})


class MembershipRecord(TypedDict):
    id: int
    operator_id: int
    operator_slug: str
    workspace_id: int
    workspace_slug: str
    role: str


class WorkspaceVisibilityError(ValueError):
    """Raised when an invalid visibility value is supplied."""


class MembershipRoleError(ValueError):
    """Raised when an invalid role is supplied to :func:`add_membership`."""


class MembershipNotFoundError(LookupError):
    """Raised when :func:`remove_membership` can't find the row."""


class WorkspaceLookupError(LookupError):
    """Raised when no workspace matches the slug or path supplied."""


class OperatorLookupError(LookupError):
    """Raised when no operator matches the slug supplied."""


def _resolve_workspace(session, identifier: str) -> Workspace:
    """Look up a workspace by slug first, falling back to absolute path.

    The CLI accepts either form (``brains workspace invite my-app …``
    or ``brains workspace invite /abs/path …``) so the resolver tries
    slug first (cheap, common) then path. We deliberately do **not**
    auto-register an unknown path here — Layer 2 verbs should fail
    loudly if the workspace doesn't exist yet, so operators never
    accidentally invite themselves to a brand-new empty workspace.
    """
    row = session.query(Workspace).filter(Workspace.slug == identifier).one_or_none()
    if row is not None:
        return row
    row = session.query(Workspace).filter(Workspace.path == identifier).one_or_none()
    if row is None:
        raise WorkspaceLookupError(
            f"workspace {identifier!r} not found (looked up by slug and by path)"
        )
    return row


def _resolve_operator(session, slug: str) -> Operator:
    raw = (slug or "").strip()
    if not SLUG_PATTERN.match(raw):
        raise OperatorSlugError(f"operator slug {slug!r} must match {SLUG_PATTERN.pattern}")
    row = session.query(Operator).filter(Operator.slug == raw).one_or_none()
    if row is None:
        raise OperatorLookupError(f"operator {raw!r} not found")
    return row


def _record(
    membership: WorkspaceMembership, operator: Operator, workspace: Workspace
) -> MembershipRecord:
    return {
        "id": membership.id,
        "operator_id": operator.id,
        "operator_slug": operator.slug,
        "workspace_id": workspace.id,
        "workspace_slug": workspace.slug,
        "role": membership.role,
    }


def add_membership(
    workspace: str,
    operator: str,
    role: str = "member",
) -> MembershipRecord:
    """Grant ``operator`` access to ``workspace``.

    Idempotent — re-running with the same (operator, workspace) just
    returns the existing row, optionally updating ``role`` if it
    changed. Admin invites are accepted but no-op: the admin operator
    already has implicit access to every workspace and we don't want a
    stray row that future code might treat as authoritative.
    """
    if role not in ALLOWED_ROLES:
        raise MembershipRoleError(f"role must be one of {sorted(ALLOWED_ROLES)} (got {role!r})")
    init_db()
    with _db_module.SessionLocal() as session:
        workspace_row = _resolve_workspace(session, workspace)
        operator_row = _resolve_operator(session, operator)
        if operator_row.slug == ADMIN_SLUG:
            # No stored row for admin — implicit membership everywhere.
            return {
                "id": 0,
                "operator_id": operator_row.id,
                "operator_slug": operator_row.slug,
                "workspace_id": workspace_row.id,
                "workspace_slug": workspace_row.slug,
                "role": role,
            }
        existing = (
            session.query(WorkspaceMembership)
            .filter(
                WorkspaceMembership.operator_id == operator_row.id,
                WorkspaceMembership.workspace_id == workspace_row.id,
            )
            .one_or_none()
        )
        if existing is None:
            membership = WorkspaceMembership(
                operator_id=operator_row.id,
                workspace_id=workspace_row.id,
                role=role,
            )
            session.add(membership)
            session.commit()
            session.refresh(membership)
        else:
            if existing.role != role:
                existing.role = role
                session.commit()
                session.refresh(existing)
            membership = existing
        return _record(membership, operator_row, workspace_row)


def remove_membership(workspace: str, operator: str) -> MembershipRecord:
    """Revoke ``operator``'s access to ``workspace``.

    Raises :class:`MembershipNotFoundError` when no row exists so the
    CLI can give a useful exit code. Admin can't be removed (no row to
    remove); attempting to do so raises the same error rather than
    silently succeeding.
    """
    init_db()
    with _db_module.SessionLocal() as session:
        workspace_row = _resolve_workspace(session, workspace)
        operator_row = _resolve_operator(session, operator)
        existing = (
            session.query(WorkspaceMembership)
            .filter(
                WorkspaceMembership.operator_id == operator_row.id,
                WorkspaceMembership.workspace_id == workspace_row.id,
            )
            .one_or_none()
        )
        if existing is None:
            raise MembershipNotFoundError(
                f"operator {operator_row.slug!r} is not a member of "
                f"workspace {workspace_row.slug!r}"
            )
        record = _record(existing, operator_row, workspace_row)
        session.delete(existing)
        session.commit()
        return record


def list_memberships(
    *,
    workspace: str | None = None,
    operator: str | None = None,
) -> list[MembershipRecord]:
    """List explicit membership rows.

    ``admin`` never appears here even though it has implicit access to
    every workspace — :func:`list_memberships` reports only what's in
    the table. Callers that want "who can see this workspace" should
    additionally treat admin as an implicit member of every result.
    """
    init_db()
    with _db_module.SessionLocal() as session:
        query = (
            session.query(WorkspaceMembership, Operator, Workspace)
            .join(Operator, Operator.id == WorkspaceMembership.operator_id)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        )
        if workspace is not None:
            workspace_row = _resolve_workspace(session, workspace)
            query = query.filter(WorkspaceMembership.workspace_id == workspace_row.id)
        if operator is not None:
            operator_row = _resolve_operator(session, operator)
            query = query.filter(WorkspaceMembership.operator_id == operator_row.id)
        rows = query.order_by(
            Workspace.slug.asc(), Operator.slug.asc(), WorkspaceMembership.id.asc()
        ).all()
        return [_record(m, op, ws) for (m, op, ws) in rows]


def set_workspace_visibility(workspace: str, visibility: str) -> dict:
    """Flip a workspace between ``shared`` and ``private``."""
    if visibility not in ALLOWED_VISIBILITIES:
        raise WorkspaceVisibilityError(
            f"visibility must be one of {sorted(ALLOWED_VISIBILITIES)} (got {visibility!r})"
        )
    init_db()
    with _db_module.SessionLocal() as session:
        workspace_row = _resolve_workspace(session, workspace)
        workspace_row.visibility = visibility
        session.commit()
        session.refresh(workspace_row)
        return {
            "workspace_id": workspace_row.id,
            "workspace_slug": workspace_row.slug,
            "visibility": workspace_row.visibility,
        }


def _operator_is_admin(session, operator_id: int | None) -> bool:
    if operator_id is None:
        # Defensive: treat "no operator" as admin so back-compat callers
        # that never went through the Layer 1 resolver keep seeing
        # everything. The resolver itself always returns *some* operator
        # so this branch only fires in tests that bypass the resolver.
        return True
    row = session.query(Operator).filter(Operator.id == operator_id).one_or_none()
    if row is None:
        return True
    return row.slug == ADMIN_SLUG


def visible_workspace_ids(operator_id: int | None) -> set[int] | None:
    """Return the set of workspace IDs ``operator_id`` can see.

    Returns ``None`` as a sentinel meaning "no filter" when the
    operator is admin (the default and the only case in single-
    operator installs). Callers can short-circuit on ``None`` and skip
    adding a ``WHERE workspace_id IN (…)`` clause entirely, which keeps
    the query plan identical to pre-Layer-2.

    Non-admin operators get the union of:

    * every workspace with ``visibility = 'shared'`` (the default), and
    * every workspace with an explicit membership row for this operator.
    """
    init_db()
    with _db_module.SessionLocal() as session:
        if _operator_is_admin(session, operator_id):
            return None
        shared_ids = {
            row.id
            for row in session.query(Workspace.id).filter(Workspace.visibility == "shared").all()
        }
        member_ids = {
            row.workspace_id
            for row in session.query(WorkspaceMembership.workspace_id)
            .filter(WorkspaceMembership.operator_id == operator_id)
            .all()
        }
        return shared_ids | member_ids


def operator_can_see_workspace(operator_id: int | None, workspace_id: int) -> bool:
    """Pointwise check for URL handlers that load a single workspace."""
    init_db()
    with _db_module.SessionLocal() as session:
        if _operator_is_admin(session, operator_id):
            return True
        workspace = session.query(Workspace).filter(Workspace.id == workspace_id).one_or_none()
        if workspace is None:
            return False
        if workspace.visibility == "shared":
            return True
        return (
            session.query(WorkspaceMembership)
            .filter(
                WorkspaceMembership.operator_id == operator_id,
                WorkspaceMembership.workspace_id == workspace_id,
            )
            .one_or_none()
            is not None
        )


def visible_workspace_ids_for_current() -> set[int] | None:
    """Convenience wrapper that resolves the current principal first.

    Used by every coordination ``list_*`` function so the call site is a
    single line. Returns ``None`` (no filter) only for the bootstrap ``admin``
    principal - the single-operator install - so its query plans stay
    identical. Every other principal gets the Org-bounded set from
    :func:`brains.authz.policy.visible_workspace_ids`, so a ``shared``
    Workspace in an Org the caller is not a member of is not visible either.

    A resolution failure is **not** "see everything": an actor that cannot be
    resolved sees nothing, which is the deny-by-default rule BL-P0-01 requires.
    """
    from brains.authz.policy import visible_workspace_ids as _visible_for_principal
    from brains.authz.resolver import resolve_local_principal

    try:
        principal = resolve_local_principal()
    except Exception:
        return set()
    return _visible_for_principal(principal)


__all__ = [
    "ALLOWED_VISIBILITIES",
    "ALLOWED_ROLES",
    "MembershipRecord",
    "MembershipNotFoundError",
    "MembershipRoleError",
    "OperatorLookupError",
    "WorkspaceLookupError",
    "WorkspaceVisibilityError",
    "add_membership",
    "remove_membership",
    "list_memberships",
    "set_workspace_visibility",
    "visible_workspace_ids",
    "visible_workspace_ids_for_current",
    "operator_can_see_workspace",
    "OperatorRecord",
]

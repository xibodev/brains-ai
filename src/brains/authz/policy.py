"""The authorization policy: one deny-by-default gate for every scoped surface.

The rules this module enforces are deliberately small and uniform:

* **Deny by default.** Nothing is authorized unless a capability check passes
  for an explicitly resolved Org.
* **401 means "who are you?"** No credential, an unknown credential, a revoked
  or expired credential.
* **403 means "not allowed"**, and is only ever returned to a principal that
  already knows the target exists - i.e. one that holds at least read access
  to the Org.
* **404 means "not for you to know"**. A principal with no read access to an
  Org gets the same answer for a real entity as for one that never existed, so
  entity IDs cannot be enumerated across Org boundaries.

Entity scope resolution lives here too, because "what Org does this Issue
belong to?" is an authorization question, not a routing question.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from brains.authz.principal import (
    CAP_ORG_ADMIN,
    CAP_ORG_OWNER,
    CAP_ORG_READ,
    CAP_ORG_WRITE,
    Principal,
)
from brains.events import topics as topic_grammar
from brains.events.bus import SubscriptionScope
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    ApprovalRequest,
    Issue,
    Org,
    Persona,
    PodProfile,
    Project,
    Runtime,
    Squad,
    Workspace,
    WorkspaceMembership,
)

__all__ = [
    "CAP_ORG_ADMIN",
    "CAP_ORG_OWNER",
    "CAP_ORG_READ",
    "CAP_ORG_WRITE",
    "TopicGrant",
    "approval_org_id",
    "approval_workspace_id",
    "authorize_runtime_operation",
    "authorize_topic",
    "authorize_topics",
    "can_see_workspace",
    "default_org_id",
    "forbidden",
    "issue_org_id",
    "issue_org_id_for_code",
    "machine_declared_org_id",
    "machine_declared_org_ids",
    "not_found",
    "persona_org_id",
    "pod_org_id",
    "project_org_id",
    "require_capability",
    "require_install_admin",
    "require_operator",
    "require_scoped_principal",
    "require_workspace_capability",
    "resolve_topic",
    "runtime_declared_org_id",
    "runtime_org_id",
    "scope_sessions",
    "session_org_id",
    "session_workspace_id",
    "subscription_scope",
    "unauthenticated",
    "visible_org_ids",
    "visible_workspace_ids",
    "workspace_declared_org_id",
    "workspace_org_id",
]


# --------------------------------------------------------------------------- #
# Error shapes
# --------------------------------------------------------------------------- #


def unauthenticated(detail: str = "Invalid API key") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


def not_found(kind: str, ref: object) -> HTTPException:
    """The non-disclosing answer: identical for absent and unauthorized."""
    return HTTPException(status_code=404, detail=f"unknown {kind}: {ref!r}")


# --------------------------------------------------------------------------- #
# Capability checks
# --------------------------------------------------------------------------- #


def require_operator(principal: Principal, *, operation: str) -> None:
    """Refuse a Runtime credential on an operator/admin surface."""
    if principal.is_runtime:
        raise forbidden(
            f"Runtime credentials authorize Runtime operations only; {operation} is refused"
        )


def require_scoped_principal(principal: Principal, *, operation: str) -> None:
    """Refuse a principal that holds no Org role at all.

    An authenticated credential with zero memberships can read nothing through
    the scoped APIs, so letting it render an install-wide console would be a
    privilege it does not otherwise have. Deny by default applies to HTML too.
    """
    if not principal.has_any_org_role:
        raise forbidden(f"{operation} requires membership of at least one Org")


def require_install_admin(principal: Principal, *, operation: str) -> None:
    """Refuse anything but the install's own bootstrap admin.

    Install-level configuration - provider credentials, environment overrides,
    router policy - is not Org-attributed, so no Org role can confer it.
    """
    require_operator(principal, operation=operation)
    if not principal.is_bootstrap_admin:
        raise forbidden(f"{operation} is restricted to the install administrator")


def require_capability(
    principal: Principal,
    capability: str,
    org_id: int | None,
    *,
    entity: str = "org",
    ref: object = None,
) -> None:
    """Deny-by-default gate for one capability against one Org.

    Raises 404 when the principal may not know the Org exists, and 403 when it
    may but lacks the capability.
    """
    if org_id is None or not principal.can_see_org(org_id):
        raise not_found(entity, ref if ref is not None else org_id)
    if not principal.has_capability(capability, org_id):
        role = principal.role_in_org(org_id) or "none"
        raise forbidden(f"{capability} requires a higher role in this Org (current role: {role})")


def authorize_runtime_operation(
    principal: Principal,
    operation: str,
    *,
    machine_id: str | None = None,
    org_id: int | None = None,
) -> None:
    """Authorize a Runtime credential for one Runtime operation.

    A Runtime credential may only act on the machine it was minted for, inside
    the Org it was bound to. Anything else - another machine, another Org, or
    an operation outside :data:`RUNTIME_OPERATIONS` - is refused.
    """
    if not principal.allows_runtime_operation(operation):
        raise forbidden(f"Runtime credential is not authorized for {operation}")
    if machine_id is not None and not principal.owns_machine(machine_id):
        raise not_found("runtime", machine_id)
    if org_id is not None and principal.runtime_org_id != org_id:
        raise not_found("runtime", machine_id or org_id)


# --------------------------------------------------------------------------- #
# Scope resolution
# --------------------------------------------------------------------------- #


def default_org_id() -> int | None:
    """The ``default`` Org's id, or ``None`` when the install has none.

    Legacy rows carry ``org_id IS NULL``; the 120 migration seeds a ``default``
    Org and the app treats a NULL as belonging to it. Authorization resolves
    the same way so a pre-Org row is scoped rather than unscoped.
    """
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(Org).filter(Org.slug == "default").one_or_none()
        return row.id if row is not None else None


def workspace_org_id(workspace_id: int | None) -> int | None:
    if workspace_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Workspace, workspace_id)
        if row is None:
            return None
        return row.org_id if row.org_id is not None else default_org_id()


def workspace_declared_org_id(workspace_id: int | None) -> int | None:
    """The Org a Workspace *declares*, without the default-Org fallback.

    Used where a missing Org must mean "claims nothing" rather than "belongs to
    the default Org" - an org-less Workspace created on the fly by a spawn is
    not a claim that another Org's Runtime may not touch it.
    """
    if workspace_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Workspace, workspace_id)
        return row.org_id if row is not None else None


def persona_org_id(persona_id: int | None) -> int | None:
    if persona_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Persona, persona_id)
        return row.org_id if row is not None else None


def project_org_id(project_id: int | None) -> int | None:
    if project_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Project, project_id)
        return row.org_id if row is not None else None


def issue_org_id(issue_id: int | None) -> int | None:
    if issue_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Issue, issue_id)
        if row is None:
            return None
        project = session.get(Project, row.project_id)
        return project.org_id if project is not None else None


def runtime_org_id(runtime_id: int | None) -> int | None:
    if runtime_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Runtime, runtime_id)
        if row is None:
            return None
        return row.org_id if row.org_id is not None else default_org_id()


def runtime_declared_org_id(runtime_id: int | None) -> int | None:
    """The Org a Runtime *declares*, without the default-Org fallback.

    Used where a missing Org must mean "claims nothing" rather than "belongs to
    the default Org": a pre-Org Runtime row is unclaimed, so it neither grants
    nor blocks a cross-entity comparison.
    """
    if runtime_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Runtime, runtime_id)
        return row.org_id if row is not None else None


def pod_org_id(pod_id: int | None) -> int | None:
    """The Org a Pod belongs to.

    The Pod's own product record (``pod_profiles.org_id``) is the authority.
    A legacy squad row that predates it, or one created by the legacy
    workspace API, falls back to its Workspace's Org so authorization keeps a
    definite answer instead of failing open.
    """
    if pod_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Squad, pod_id)
        if row is None:
            return None
        profile = session.query(PodProfile).filter(PodProfile.pod_id == pod_id).one_or_none()
        if profile is not None and profile.org_id is not None:
            return profile.org_id
        workspace = session.get(Workspace, row.workspace_id)
        if workspace is None:
            return None
        return workspace.org_id if workspace.org_id is not None else default_org_id()


def session_org_id(session_id: str | None) -> int | None:
    if not session_id:
        return None
    return workspace_org_id(session_workspace_id(session_id))


def session_workspace_id(session_id: str | None) -> int | None:
    """The Workspace a Session belongs to, or ``None`` when it is unknown."""
    if not session_id:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(AgentSession, session_id)
        return row.workspace_id if row is not None else None


def approval_org_id(code: str | None) -> int | None:
    if not code:
        return None
    return workspace_org_id(approval_workspace_id(code))


def approval_workspace_id(code: str | None) -> int | None:
    """The Workspace an approval request belongs to, or ``None``."""
    if not code:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(ApprovalRequest).filter(ApprovalRequest.code == code).one_or_none()
        return row.workspace_id if row is not None else None


def visible_org_ids(principal: Principal) -> set[int] | None:
    """Org IDs the principal may read; ``None`` means "no filter"."""
    return principal.visible_org_ids()


def visible_workspace_ids(principal: Principal | None) -> set[int] | None:
    """Workspace IDs the principal may read; ``None`` means "no filter".

    A Workspace is visible when the principal can read its Org *and* the
    Workspace's own visibility allows it: ``shared`` Workspaces are visible to
    every member of the owning Org, ``private`` ones only to operators with an
    explicit ``workspace_memberships`` row. Org scope is the outer boundary, so
    a ``shared`` Workspace in another Org is still invisible.
    """
    if principal is None:
        return set()
    if principal.is_bootstrap_admin:
        return None
    org_ids = principal.visible_org_ids()
    if org_ids is None:
        return None
    init_db()
    fallback_org = default_org_id()
    with _db_module.SessionLocal() as session:
        rows = session.query(Workspace.id, Workspace.org_id, Workspace.visibility).all()
        member_ids: set[int] = set()
        if principal.operator_id is not None:
            member_ids = {
                row.workspace_id
                for row in session.query(WorkspaceMembership.workspace_id)
                .filter(WorkspaceMembership.operator_id == principal.operator_id)
                .all()
            }
    visible: set[int] = set()
    for workspace_id, org_id, visibility in rows:
        effective_org = org_id if org_id is not None else fallback_org
        if effective_org is None or effective_org not in org_ids:
            continue
        if visibility == "private" and workspace_id not in member_ids:
            continue
        visible.add(workspace_id)
    return visible


def can_see_workspace(principal: Principal | None, workspace_id: int | None) -> bool:
    """Pointwise counterpart of :func:`visible_workspace_ids`.

    Every per-ID helper must ask this, not only the Org question: a ``private``
    Workspace is filtered out of a listing, so a detail, event, state or
    approval route that checked the Org alone would answer for a row the same
    principal cannot see in the list it came from.
    """
    if principal is None or workspace_id is None:
        return False
    visible = visible_workspace_ids(principal)
    return visible is None or workspace_id in visible


def scope_sessions(principal: Principal | None, rows: list[dict]) -> list[dict]:
    """Filter a Session listing to the Workspaces ``principal`` may see.

    Every listing of Sessions goes through here, whichever entity it is reached
    from. ``GET /v1/sessions`` is not the only door: a Session is also listed
    under the Issue it works and under the Persona running it, and those
    listings take the same rows from the same table. Authorizing only the Issue
    or the Persona answers the Org question and leaves the Workspace question
    unasked, so a Session in a ``private`` Workspace - absent from
    ``/v1/sessions`` and refused by id - would still be enumerable, with its
    summary, through its Issue.
    """
    visible = visible_workspace_ids(principal)
    if visible is None:
        return rows
    return [row for row in rows if row.get("workspace_id") in visible]


def require_workspace_capability(
    principal: Principal,
    capability: str,
    workspace_id: int | None,
    *,
    entity: str,
    ref: object,
) -> int | None:
    """Authorize one capability against the Org *and* visibility of a Workspace.

    Returns the resolved Org id. Raises the same non-disclosing ``404`` for a
    Workspace the principal may not see as for one that does not exist, so
    list and detail agree: an entity absent from a listing is absent from every
    per-ID surface too.
    """
    org_id = workspace_org_id(workspace_id)
    require_capability(principal, capability, org_id, entity=entity, ref=ref)
    if not can_see_workspace(principal, workspace_id):
        raise not_found(entity, ref)
    return org_id


# --------------------------------------------------------------------------- #
# Realtime topics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TopicGrant:
    """One authorized subscription: the canonical topic and the scope it carries.

    ``topic`` is the *server-derived* name - the client's ``org/acme/issues``
    or ``org/default/issues`` resolves to ``org/7/issues`` here - so a
    subscriber never decides what a topic means. ``reference`` is the resolved
    entity the topic names, and ``org_id``/``workspace_id`` are the scope the
    server resolved, which is what delivery and replay filter on afterwards.
    """

    topic: str
    requested: str
    family: str
    channel: str
    entity: str
    reference: str
    org_id: int | None
    workspace_id: int | None
    capability: str = CAP_ORG_READ


def _org_id_for_reference(reference: str) -> int | None:
    """Resolve an Org reference (id, slug, or ``default``) to an existing id."""
    init_db()
    if reference == topic_grammar.DEFAULT_ORG_ALIAS:
        return default_org_id()
    with _db_module.SessionLocal() as session:
        as_int = _as_int(reference)
        if as_int is not None:
            row = session.get(Org, as_int)
            return row.id if row is not None else None
        row = session.query(Org).filter(Org.slug == reference).one_or_none()
        return row.id if row is not None else None


def machine_declared_org_ids(machine_id: str | None) -> set[int]:
    """Every Org the machine's Runtimes declare.

    A machine is not a table of its own: it is the identity a Runtime row (and
    the credential minted for it) is bound to, so the Orgs it belongs to are
    the Orgs its Runtimes carry. Empty means it declares none.
    """
    if not machine_id:
        return set()
    init_db()
    with _db_module.SessionLocal() as session:
        rows = (
            session.query(Runtime.org_id).filter(Runtime.machine_id == machine_id).distinct().all()
        )
    return {row[0] for row in rows if row[0] is not None}


def machine_declared_org_id(machine_id: str | None) -> int | None:
    """The single Org a machine's Runtimes declare, or ``None``.

    ``None`` covers both "declares no Org" and "declares more than one": the
    machine claims nothing definite either way, so a caller that needs one
    answer has to decide what an ambiguous machine means rather than have this
    helper pick.
    """
    return _single(machine_declared_org_ids(machine_id))


def _single(org_ids: set[int]) -> int | None:
    return next(iter(org_ids)) if len(org_ids) == 1 else None


def _machine_is_known(machine_id: str | None) -> bool:
    if not machine_id:
        return False
    init_db()
    with _db_module.SessionLocal() as session:
        return (
            session.query(Runtime.id).filter(Runtime.machine_id == machine_id).first() is not None
        )


def _runtime_binding(runtime_id: int | None) -> tuple[str | None, int | None, bool]:
    """``(machine_id, declared org, exists)`` for one Runtime row."""
    if runtime_id is None:
        return None, None, False
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Runtime, runtime_id)
        if row is None:
            return None, None, False
        return row.machine_id, row.org_id, True


def _session_binding(session_id: str) -> tuple[int | None, str | None, int | None, bool]:
    """``(workspace_id, machine_id, runtime_id, exists)`` for one Session row."""
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(AgentSession, session_id)
        if row is None:
            return None, None, None, False
        return row.workspace_id, row.machine_id, row.runtime_id, True


def _runtime_may_own_org(principal: Principal, declared_org_id: int | None) -> bool:
    """True when a Runtime credential's Org matches ``declared_org_id``.

    A row that declares no Org - or that sits in the ``default`` bucket a
    Workspace created on the fly lands in - claims nothing, so it neither
    grants nor blocks: the machine binding decides. Any other Org must be the
    credential's own.
    """
    if declared_org_id is None:
        return True
    if declared_org_id == default_org_id():
        return True
    return declared_org_id == principal.runtime_org_id


def resolve_topic(principal: Principal | None, requested: object) -> TopicGrant | None:
    """Authorize ``requested`` and return the derived topic, or ``None``.

    Deny by default and **non-disclosing**: a malformed topic, a wildcard, an
    unknown family or channel, an entity that does not exist, and an entity in
    another Org all return the same ``None``, so a subscription attempt can
    never be used to test whether an Issue code, Session id, machine or Org
    exists. Scope is resolved from the server's own state before the decision
    is taken, never from the string the client sent.

    The grammar and the scope each topic family carries are defined in
    :mod:`brains.events.topics`:

    * ``org/{org_id|slug|default}/{channel}`` - the Org must be readable;
    * ``issue/{code}`` - the Issue's Project Org must be readable;
    * ``session/{session_id}/{stream}`` - the Session's Workspace must be
      visible, or the Session must be running on the credential's own machine;
    * ``machine/{machine_id}/{stream}`` and ``runtime/{runtime_id}/{stream}`` -
      the Runtime's Org must be readable, or the machine must be the
      credential's own.
    """
    if principal is None:
        return None
    parsed = topic_grammar.parse_topic(requested)
    if parsed is None:
        return None
    assert isinstance(requested, str)  # noqa: S101 - parse_topic rejects everything else

    if principal.is_runtime and not parsed.is_runtime_topic:
        return None
    if not principal.is_runtime and not parsed.is_operator_topic:
        return None

    if parsed.family == "org":
        org_id = _org_id_for_reference(parsed.reference)
        if org_id is None or principal.is_runtime or not principal.can_see_org(org_id):
            return None
        return TopicGrant(
            topic=topic_grammar.org_topic(org_id, parsed.channel),
            requested=requested,
            family=parsed.family,
            channel=parsed.channel,
            entity=parsed.entity,
            reference=str(org_id),
            org_id=org_id,
            workspace_id=None,
        )

    if parsed.family == "issue":
        org_id = issue_org_id_for_code(parsed.reference)
        if org_id is None or principal.is_runtime or not principal.can_see_org(org_id):
            return None
        return TopicGrant(
            topic=topic_grammar.issue_topic(parsed.reference),
            requested=requested,
            family=parsed.family,
            channel=parsed.channel,
            entity=parsed.entity,
            reference=parsed.reference,
            org_id=org_id,
            workspace_id=None,
        )

    if parsed.family == "session":
        workspace_id, machine_id, runtime_id, exists = _session_binding(parsed.reference)
        if not exists:
            return None
        org_id = workspace_org_id(workspace_id)
        if principal.is_runtime:
            if not machine_id or not principal.owns_machine(machine_id):
                return None
            if not _runtime_may_own_org(principal, workspace_declared_org_id(workspace_id)):
                return None
            if runtime_id is not None and not _runtime_may_own_org(
                principal, runtime_declared_org_id(runtime_id)
            ):
                return None
        elif not (principal.can_see_org(org_id) and can_see_workspace(principal, workspace_id)):
            return None
        return TopicGrant(
            topic=topic_grammar.session_topic(parsed.reference, parsed.channel),
            requested=requested,
            family=parsed.family,
            channel=parsed.channel,
            entity=parsed.entity,
            reference=parsed.reference,
            org_id=org_id,
            workspace_id=workspace_id,
        )

    if parsed.family == "machine":
        declared_ids = machine_declared_org_ids(parsed.reference)
        declared = _single(declared_ids)
        if principal.is_runtime:
            if not principal.owns_machine(parsed.reference):
                return None
            if not _runtime_may_own_org(principal, declared):
                return None
            org_id = declared if declared is not None else principal.runtime_org_id
        else:
            if not _machine_is_known(parsed.reference):
                return None
            if declared_ids:
                # A machine whose Runtimes straddle two Orgs claims both. It
                # must not collapse to the default Org, or a member of that Org
                # would learn the machine exists - the same disclosure an
                # unknown machine id is refused for.
                if not all(principal.can_see_org(candidate) for candidate in declared_ids):
                    return None
                org_id = declared
            else:
                org_id = default_org_id()
                if not principal.can_see_org(org_id):
                    return None
        return TopicGrant(
            topic=topic_grammar.machine_topic(parsed.reference, parsed.channel),
            requested=requested,
            family=parsed.family,
            channel=parsed.channel,
            entity=parsed.entity,
            reference=parsed.reference,
            org_id=org_id,
            workspace_id=None,
        )

    if parsed.family == "runtime":
        runtime_id = _as_int(parsed.reference)
        machine_id, declared, exists = _runtime_binding(runtime_id)
        if not exists:
            return None
        if principal.is_runtime:
            if not machine_id or not principal.owns_machine(machine_id):
                return None
            if not _runtime_may_own_org(principal, declared):
                return None
            org_id = declared if declared is not None else principal.runtime_org_id
        else:
            org_id = runtime_org_id(runtime_id)
            if not principal.can_see_org(org_id):
                return None
        return TopicGrant(
            topic=topic_grammar.runtime_topic(runtime_id, parsed.channel),
            requested=requested,
            family=parsed.family,
            channel=parsed.channel,
            entity=parsed.entity,
            reference=str(runtime_id),
            org_id=org_id,
            workspace_id=None,
        )

    return None  # pragma: no cover - the grammar has no other family


def authorize_topic(principal: Principal | None, topic: str) -> bool:
    """True when ``principal`` may subscribe to ``topic``.

    Thin boolean wrapper over :func:`resolve_topic`; callers that need the
    derived name or its scope use the grant itself.
    """
    return resolve_topic(principal, topic) is not None


def authorize_topics(
    principal: Principal | None, requested: object
) -> tuple[list[TopicGrant], list[str]]:
    """Split requested topics into ``(grants, denied)``.

    ``denied`` echoes what the client asked for and nothing else: no reason
    code distinguishes "malformed", "unknown" and "not yours", because telling
    them apart is exactly how a subscription becomes an existence oracle. The
    request is bounded (:data:`~brains.events.topics.MAX_TOPICS_PER_REQUEST`)
    because every resolution is a database read, and the echo is truncated to
    a legal topic length so a refusal cannot be used to reflect a large string.
    """
    grants: list[TopicGrant] = []
    denied: list[str] = []
    seen: set[str] = set()
    items = requested if isinstance(requested, list | tuple | set | frozenset) else []
    for item in list(items)[: topic_grammar.MAX_TOPICS_PER_REQUEST]:
        if not isinstance(item, str):
            continue
        grant = resolve_topic(principal, item)
        if grant is None:
            denied.append(item[: topic_grammar.MAX_TOPIC_LENGTH])
            continue
        if grant.topic in seen:
            continue
        seen.add(grant.topic)
        grants.append(grant)
    return grants, denied


def subscription_scope(principal: Principal | None) -> SubscriptionScope:
    """The Org/Workspace filter applied to every frame this principal receives.

    Defence in depth behind topic authorization: a publisher that puts one
    Org's payload on another Org's topic still cannot reach a subscriber whose
    scope excludes it. ``None`` inside the scope means "no filter" and is only
    produced for a principal that may read every Org.
    """
    if principal is None:
        return SubscriptionScope(org_ids=frozenset(), workspace_ids=frozenset())
    if principal.is_runtime:
        org_ids = {principal.runtime_org_id} if principal.runtime_org_id is not None else set()
        # Workspace visibility is an operator concept; a Runtime credential is
        # bound by its machine, which topic authorization already enforced.
        return SubscriptionScope(org_ids=frozenset(org_ids), workspace_ids=None)
    org_ids_or_none = visible_org_ids(principal)
    workspace_ids_or_none = visible_workspace_ids(principal)
    return SubscriptionScope(
        org_ids=None if org_ids_or_none is None else frozenset(org_ids_or_none),
        workspace_ids=None if workspace_ids_or_none is None else frozenset(workspace_ids_or_none),
    )


def _as_int(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def issue_org_id_for_code(code: str | None) -> int | None:
    """The Org an Issue code belongs to, or ``None`` when it is unknown."""
    if not code:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(Issue).filter(Issue.code == code).one_or_none()
        if row is None:
            return None
        project = session.get(Project, row.project_id)
        return project.org_id if project is not None else None

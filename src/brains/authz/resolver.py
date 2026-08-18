"""Resolve a credential into a :class:`~brains.authz.principal.Principal`.

This is the one place that turns "a secret was presented" into "this actor,
with this scope, is acting". Every HTTP surface (gateway, console, admin,
realtime), the MCP transports and the CLI resolve through here, so an actor is
attributed the same way whichever door it came through.

The resolved principal is also published on a :class:`contextvars.ContextVar`
so control-layer reads can scope themselves without every call site growing a
parameter. The ContextVar is *not* an authorization decision - it carries the
same principal the route already authenticated, and it is empty by default, so
nothing is granted when it is unset.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging

from brains.authz import credentials as creds
from brains.authz.principal import (
    ACTOR_OPERATOR,
    ACTOR_RUNTIME,
    CHANNEL_API,
    CHANNEL_LOCAL,
    CREDENTIAL_BOOTSTRAP,
    Principal,
)
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, Operator, OrgMember, Runtime

log = logging.getLogger(__name__)

#: The principal for the current request / CLI invocation. ``None`` means "no
#: authenticated context", which every scoped read treats as "see nothing"
#: unless it is running as the bootstrap admin.
#:
#: HTTP requests bind a :class:`_PrincipalSlot` here at the ASGI boundary
#: instead of a principal, because a FastAPI dependency does not run in the
#: same ``contextvars`` context as the endpoint it guards (sync dependencies
#: and sync endpoints each get their own copy). Binding one mutable slot up
#: front and filling it in the dependency makes the resolved principal visible
#: to every child context - the endpoint, the threadpool worker, and any
#: control-layer read they make.
current_principal: contextvars.ContextVar = contextvars.ContextVar(
    "brains_current_principal", default=None
)

ADMIN_SLUG = "admin"


class _PrincipalSlot:
    """One mutable carrier per request, shared by every child context."""

    __slots__ = ("principal",)

    def __init__(self) -> None:
        self.principal: Principal | None = None


def _org_roles_for_operator(operator_id: int | None) -> dict[int, str]:
    if operator_id is None:
        return {}
    init_db()
    with _db_module.SessionLocal() as session:
        rows = session.query(OrgMember).filter(OrgMember.operator_id == operator_id).all()
        return {row.org_id: row.role for row in rows}


def _operator_slug(operator_id: int | None) -> str | None:
    if operator_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Operator, operator_id)
        return row.slug if row is not None else None


def _runtime_org_id(runtime_id: int | None) -> int | None:
    if runtime_id is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.get(Runtime, runtime_id)
        return row.org_id if row is not None else None


def sessions_bound_to_machine(machine_id: str | None) -> frozenset[str]:
    """Live Session ids running on ``machine_id``.

    This is the *server's* view of what a credential is currently running, and
    it is the only Session binding an approval decision trusts: a caller-declared
    ``session_id`` can be omitted or invented, so it may add a denial but can
    never be the thing that establishes separation of duty.
    """
    if not machine_id:
        return frozenset()
    init_db()
    with _db_module.SessionLocal() as session:
        rows = (
            session.query(AgentSession.id)
            .filter(AgentSession.machine_id == machine_id, AgentSession.ended_at.is_(None))
            .all()
        )
        return frozenset(row.id for row in rows)


def principal_from_credential(record: creds.CredentialRecord) -> Principal:
    """Build the principal a credential row authorizes."""
    if record["kind"] == creds.KIND_RUNTIME:
        org_id = record["org_id"]
        if org_id is None:
            org_id = _runtime_org_id(record["runtime_id"])
        machine_id = record["machine_id"]
        return Principal(
            actor_kind=ACTOR_RUNTIME,
            actor_id=f"runtime:{machine_id or record['runtime_id'] or '?'}",
            credential_kind=creds.KIND_RUNTIME,
            credential_id=record["credential_id"],
            runtime_org_id=org_id,
            runtime_machine_id=machine_id,
            runtime_id=record["runtime_id"],
            channel=CHANNEL_API,
            bound_session_ids=sessions_bound_to_machine(machine_id),
        )

    operator_id = record["operator_id"]
    slug = record["operator_slug"] or _operator_slug(operator_id)
    return Principal(
        actor_kind=ACTOR_OPERATOR,
        actor_id=f"operator:{slug}" if slug else f"credential:{record['credential_id']}",
        credential_kind=record["kind"],
        credential_id=record["credential_id"],
        operator_id=operator_id,
        operator_slug=slug,
        org_roles=_org_roles_for_operator(operator_id),
        is_bootstrap_admin=slug == ADMIN_SLUG,
        channel=CHANNEL_API,
    )


def principal_for_secret(raw_secret: str | None) -> Principal | None:
    """Resolve a raw API key / Runtime secret into a principal, or ``None``."""
    record = creds.resolve_secret(raw_secret)
    if record is None:
        return None
    return principal_from_credential(record)


def bootstrap_principal(*, channel: str = CHANNEL_LOCAL) -> Principal:
    """The principal used when authentication is explicitly disabled.

    ``BRAINS_ALLOW_UNAUTHENTICATED_API`` is an opt-in for a sealed single-user
    network. It still resolves to the ``admin`` operator so attribution, audit
    and foreign keys stay valid - it does not create an anonymous actor. The
    channel defaults to :data:`CHANNEL_LOCAL` because the trust boundary in
    that mode *is* the process/network boundary.
    """
    operator_id: int | None = None
    try:
        from brains.control.operators import ensure_admin_operator

        operator_id = ensure_admin_operator()["id"]
    except Exception:
        log.debug("admin operator unavailable for bootstrap principal", exc_info=True)
    return Principal(
        actor_kind=ACTOR_OPERATOR,
        actor_id=f"operator:{ADMIN_SLUG}",
        credential_kind=CREDENTIAL_BOOTSTRAP,
        operator_id=operator_id,
        operator_slug=ADMIN_SLUG,
        org_roles=_org_roles_for_operator(operator_id),
        is_bootstrap_admin=True,
        channel=channel,
    )


def principal_for_operator_slug(slug: str | None) -> Principal | None:
    """Resolve a *local* actor (CLI, stdio MCP) by operator slug.

    Used only where the trust boundary is the operating-system process itself:
    a local CLI invocation and stdio MCP inherit the launching user's
    authority. It never resolves an HTTP request - those must present a
    credential.
    """
    if not slug:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(Operator).filter(Operator.slug == slug.strip().lower()).one_or_none()
        if row is None:
            return None
        operator_id = row.id
        operator_slug = row.slug
    return Principal(
        actor_kind=ACTOR_OPERATOR,
        actor_id=f"operator:{operator_slug}",
        credential_kind=creds.KIND_OPERATOR,
        operator_id=operator_id,
        operator_slug=operator_slug,
        org_roles=_org_roles_for_operator(operator_id),
        is_bootstrap_admin=operator_slug == ADMIN_SLUG,
        channel=CHANNEL_LOCAL,
    )


def set_current_principal(principal: Principal | None):
    """Publish ``principal`` for the current context.

    When a request-scoped slot is bound (see :func:`principal_slot`) the slot
    is filled instead of rebinding the ContextVar, so the value is visible in
    the sibling contexts FastAPI creates for the endpoint and its threadpool
    worker. Returns the reset token when a real ``set`` happened, else ``None``.
    """
    existing = current_principal.get()
    if isinstance(existing, _PrincipalSlot):
        existing.principal = principal
        return None
    return current_principal.set(principal)


def get_current_principal() -> Principal | None:
    value = current_principal.get()
    if isinstance(value, _PrincipalSlot):
        return value.principal
    return value


@contextlib.contextmanager
def principal_slot():
    """Bind a fresh per-request principal slot for the duration of the block."""
    token = current_principal.set(_PrincipalSlot())
    try:
        yield
    finally:
        current_principal.reset(token)


def anonymous_principal() -> Principal:
    """A named-but-scopeless principal for an unauthenticated request in flight.

    When an ASGI request has bound a principal slot and nothing has filled it,
    the caller is authenticated by *no* credential. Falling back to the
    bootstrap admin there would silently hand an unauthenticated HTTP request
    the install's full authority, so this principal is returned instead: it has
    no operator, no Org role, and every scoped read resolves to "nothing".
    """
    return Principal(
        actor_kind=ACTOR_OPERATOR,
        actor_id="anonymous",
        credential_kind="none",
        channel=CHANNEL_API,
    )


def resolve_local_principal(*, operator: str | None = None) -> Principal:
    """The principal a *local* invocation acts as, resolved explicitly.

    Order, first hit wins:

    1. the explicit ``operator`` argument (``--operator`` on the CLI),
    2. the principal already published on the ContextVar (HTTP/MCP request),
    3. ``BRAINS_OPERATOR`` in the environment,
    4. ``BRAINS_API_KEY`` in the environment, resolved through the store,
    5. the bootstrap ``admin`` operator.

    Step 5 is the documented single-operator compatibility path: a local
    process with no stated actor acts as the install's admin, exactly as it
    did before. It is not reachable over HTTP: when a request slot is bound but
    empty, the request authenticated with no credential and
    :func:`anonymous_principal` is returned instead.
    """
    import os

    if operator:
        resolved = principal_for_operator_slug(operator)
        if resolved is not None:
            return resolved
    existing = current_principal.get()
    if isinstance(existing, _PrincipalSlot):
        if existing.principal is not None:
            return existing.principal
        return anonymous_principal()
    if existing is not None:
        return existing
    env_slug = (os.environ.get("BRAINS_OPERATOR") or "").strip().lower()
    if env_slug:
        resolved = principal_for_operator_slug(env_slug)
        if resolved is not None:
            return resolved
    env_key = os.environ.get("BRAINS_API_KEY")
    if env_key:
        resolved = principal_for_secret(env_key)
        if resolved is not None:
            # A key read from this process's own environment is the local
            # operating-system boundary, not an HTTP credential.
            return resolved.with_channel(CHANNEL_LOCAL)
    return bootstrap_principal()


__all__ = [
    "ADMIN_SLUG",
    "anonymous_principal",
    "bootstrap_principal",
    "current_principal",
    "get_current_principal",
    "principal_for_operator_slug",
    "principal_for_secret",
    "principal_from_credential",
    "principal_slot",
    "resolve_local_principal",
    "sessions_bound_to_machine",
    "set_current_principal",
]

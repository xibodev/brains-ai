"""Roles, capabilities, and the resolved principal (BL-P0-01).

One authenticated request resolves to exactly one :class:`Principal`. The
principal names *who* is acting (an operator, a Runtime, or the bootstrap
admin), *what credential* said so, and *what scope* that credential carries
(Org memberships and roles, plus an optional Runtime/machine binding).

The role vocabulary is the one the product contract already uses and no more:
``owner``, ``admin`` and ``member`` on an Org (``brains.control.orgs``), and
``owner``/``member`` on a Workspace (``brains.control.memberships``). No new
role is invented here.

Capabilities are the enforceable verbs those roles unlock. Every check is
deny-by-default: a capability is granted only when the principal holds a role
in the target Org that ranks at or above the capability's minimum.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

#: Org roles, weakest first. Index in this tuple is the rank.
ORG_ROLES: tuple[str, ...] = ("member", "admin", "owner")

_ORG_ROLE_RANK: Mapping[str, int] = MappingProxyType(
    {role: index for index, role in enumerate(ORG_ROLES)}
)

# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #

#: Read any Org-scoped entity (Orgs, Personas, Projects, Issues, Pods, Skills,
#: Sessions, Runtimes, approvals, automation).
CAP_ORG_READ = "org.read"
#: Create/update Org *content*: Personas, Projects, Issues, comments, Pods,
#: Skills, Session spawn, approval resolution.
CAP_ORG_WRITE = "org.write"
#: Administer the Org itself: rename/archive, membership, automation
#: enable/fire, Runtime lifecycle, enrollment minting.
CAP_ORG_ADMIN = "org.admin"
#: Change ownership: grant or revoke the ``owner`` role.
CAP_ORG_OWNER = "org.owner"

#: capability -> minimum Org role that grants it.
CAPABILITY_MIN_ROLE: Mapping[str, str] = MappingProxyType(
    {
        CAP_ORG_READ: "member",
        CAP_ORG_WRITE: "member",
        CAP_ORG_ADMIN: "admin",
        CAP_ORG_OWNER: "owner",
    }
)

# --------------------------------------------------------------------------- #
# Actor and credential kinds
# --------------------------------------------------------------------------- #

ACTOR_OPERATOR = "operator"
ACTOR_RUNTIME = "runtime"

CREDENTIAL_ADMIN = "admin"
CREDENTIAL_OPERATOR = "operator"
CREDENTIAL_RUNTIME = "runtime"
#: Only produced when ``settings.allow_unauthenticated_api`` is explicitly on.
CREDENTIAL_BOOTSTRAP = "bootstrap"

# --------------------------------------------------------------------------- #
# Channels - *how* the credential reached us
# --------------------------------------------------------------------------- #

#: A raw secret presented on an HTTP request (``Authorization``, ``x-api-key``
#: or the legacy ``?key=``). A shared operator key looks exactly like this
#: whether a human or an agent process holds it, so it is **not** bindable to a
#: human being.
CHANNEL_API = "api"
#: The signed console cookie, which is only ever minted by a browser sign-in at
#: ``/admin/login``. Holding it means someone typed the key into a browser.
CHANNEL_BROWSER = "browser"
#: A local process boundary: the CLI, stdio MCP, or an install that has
#: explicitly disabled authentication. The operating-system user is the actor.
CHANNEL_LOCAL = "local"

#: Channels that can be attributed to a human at a keyboard rather than to
#: "whoever holds this secret". Used by approval separation of duty, which must
#: not depend on a caller-declared Session id.
HUMAN_CHANNELS: frozenset[str] = frozenset({CHANNEL_BROWSER, CHANNEL_LOCAL})

#: The Runtime operations a Runtime-narrow credential may perform. Anything not
#: named here is refused for a Runtime principal, including every operator and
#: admin API.
RUNTIME_OPERATIONS: frozenset[str] = frozenset(
    {
        "runtime.register",
        "runtime.heartbeat",
        "runtime.status",
        "runtime.claim",
        "runtime.execute",
    }
)


def role_rank(role: str | None) -> int:
    """Rank of ``role``; ``-1`` for absent or unknown roles (deny by default)."""
    if not role:
        return -1
    return _ORG_ROLE_RANK.get(role, -1)


def role_satisfies(role: str | None, minimum: str) -> bool:
    """True when ``role`` ranks at or above ``minimum``."""
    minimum_rank = role_rank(minimum)
    if minimum_rank < 0:
        return False
    return role_rank(role) >= minimum_rank


@dataclass(frozen=True)
class Principal:
    """The single identity an authenticated request acts as.

    ``org_roles`` maps ``org_id -> role`` for explicit memberships only. The
    bootstrap admin carries ``is_bootstrap_admin`` instead of a row per Org:
    it is the auto-provisioned single-operator install, and treating it as
    ``owner`` everywhere is what keeps a pre-existing install working without
    a migration. That compatibility is deliberate, explicit and testable - it
    is not an implicit "unknown key sees everything" fallback, because an
    unknown key never authenticates at all.
    """

    actor_kind: str
    actor_id: str
    credential_kind: str
    credential_id: str | None = None
    operator_id: int | None = None
    operator_slug: str | None = None
    org_roles: Mapping[int, str] = field(default_factory=dict)
    runtime_org_id: int | None = None
    runtime_machine_id: str | None = None
    runtime_id: int | None = None
    is_bootstrap_admin: bool = False
    #: How the credential reached us. See :data:`CHANNEL_API` and friends.
    channel: str = CHANNEL_API
    #: Session ids the *server* knows this principal is currently running,
    #: derived from the credential rather than declared by the caller.
    bound_session_ids: frozenset[str] = frozenset()

    # -- identity ---------------------------------------------------------- #

    @property
    def is_operator(self) -> bool:
        return self.actor_kind == ACTOR_OPERATOR

    @property
    def is_runtime(self) -> bool:
        return self.actor_kind == ACTOR_RUNTIME

    @property
    def is_human_channel(self) -> bool:
        """True when the credential arrived through a human-bindable channel.

        A raw API key on an HTTP request is not: an agent process holding a
        shared operator key presents exactly the same bytes as its owner.
        """
        return self.channel in HUMAN_CHANNELS

    @property
    def has_any_org_role(self) -> bool:
        """True when the principal holds at least one explicit Org role."""
        return self.is_bootstrap_admin or bool(self.org_roles)

    def with_channel(self, channel: str) -> Principal:
        """Return a copy of this principal tagged with ``channel``."""
        return replace(self, channel=channel)

    def describe(self) -> str:
        """Short, non-secret attribution string for audit and logs."""
        return self.actor_id

    # -- Org scope --------------------------------------------------------- #

    def role_in_org(self, org_id: int | None) -> str | None:
        """The principal's role in ``org_id``, or ``None`` when it has none."""
        if org_id is None:
            return None
        if self.is_runtime:
            # A Runtime credential is Org-bound but holds no operator role: it
            # can act on its own Runtimes, never on Org content.
            return None
        if self.is_bootstrap_admin:
            return "owner"
        return self.org_roles.get(org_id)

    def has_capability(self, capability: str, org_id: int | None) -> bool:
        """Deny-by-default capability check for ``org_id``."""
        minimum = CAPABILITY_MIN_ROLE.get(capability)
        if minimum is None:
            return False
        if self.is_runtime:
            return False
        return role_satisfies(self.role_in_org(org_id), minimum)

    def can_see_org(self, org_id: int | None) -> bool:
        """True when the principal may know that ``org_id`` exists."""
        if org_id is None:
            return False
        if self.is_runtime:
            return self.runtime_org_id == org_id
        return self.has_capability(CAP_ORG_READ, org_id)

    def visible_org_ids(self) -> set[int] | None:
        """Org IDs this principal may read; ``None`` means "no filter".

        ``None`` is returned only for the bootstrap admin, so an existing
        single-operator install keeps identical query plans.
        """
        if self.is_bootstrap_admin:
            return None
        if self.is_runtime:
            return {self.runtime_org_id} if self.runtime_org_id is not None else set()
        return set(self.org_roles)

    # -- Runtime scope ----------------------------------------------------- #

    def allows_runtime_operation(self, operation: str) -> bool:
        """True when a Runtime credential may perform ``operation``."""
        return self.is_runtime and operation in RUNTIME_OPERATIONS

    def owns_machine(self, machine_id: str | None) -> bool:
        """True when a Runtime credential is bound to ``machine_id``."""
        if not self.is_runtime or not machine_id:
            return False
        return self.runtime_machine_id == machine_id


__all__ = [
    "ACTOR_OPERATOR",
    "ACTOR_RUNTIME",
    "CAPABILITY_MIN_ROLE",
    "CAP_ORG_ADMIN",
    "CAP_ORG_OWNER",
    "CAP_ORG_READ",
    "CAP_ORG_WRITE",
    "CHANNEL_API",
    "CHANNEL_BROWSER",
    "CHANNEL_LOCAL",
    "CREDENTIAL_ADMIN",
    "CREDENTIAL_BOOTSTRAP",
    "CREDENTIAL_OPERATOR",
    "CREDENTIAL_RUNTIME",
    "HUMAN_CHANNELS",
    "ORG_ROLES",
    "RUNTIME_OPERATIONS",
    "Principal",
    "role_rank",
    "role_satisfies",
]

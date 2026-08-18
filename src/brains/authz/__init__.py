"""Authenticated identity, roles, and Org/Workspace scope (BL-P0-01).

The package answers three questions in one place:

* **Who is acting?** :mod:`brains.authz.credentials` stores one row per
  accepted secret; :mod:`brains.authz.resolver` turns it into a
  :class:`~brains.authz.principal.Principal`.
* **What may they do?** :mod:`brains.authz.principal` defines the roles the
  product contract already uses (``owner``/``admin``/``member``) and the
  capabilities they unlock.
* **What may they see?** :mod:`brains.authz.policy` resolves an entity to its
  Org, applies the deny-by-default capability check, and returns consistent
  401 / 403 / 404 answers.

:mod:`brains.authz.deps` wires the same resolution into FastAPI.
"""

from brains.authz.principal import (
    ACTOR_OPERATOR,
    ACTOR_RUNTIME,
    CAP_ORG_ADMIN,
    CAP_ORG_OWNER,
    CAP_ORG_READ,
    CAP_ORG_WRITE,
    ORG_ROLES,
    RUNTIME_OPERATIONS,
    Principal,
    role_satisfies,
)
from brains.authz.resolver import (
    bootstrap_principal,
    current_principal,
    get_current_principal,
    principal_for_operator_slug,
    principal_for_secret,
    resolve_local_principal,
    set_current_principal,
)

__all__ = [
    "ACTOR_OPERATOR",
    "ACTOR_RUNTIME",
    "CAP_ORG_ADMIN",
    "CAP_ORG_OWNER",
    "CAP_ORG_READ",
    "CAP_ORG_WRITE",
    "ORG_ROLES",
    "RUNTIME_OPERATIONS",
    "Principal",
    "bootstrap_principal",
    "current_principal",
    "get_current_principal",
    "principal_for_operator_slug",
    "principal_for_secret",
    "resolve_local_principal",
    "role_satisfies",
    "set_current_principal",
]

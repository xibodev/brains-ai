"""BL-P0-01 - make the Org membership an existing install already relied on explicit.

Before this migration, an operator's access was not derived from Org
membership at all: every authenticated key saw every ``shared`` Workspace,
whichever Org owned it. Enforcing Org scope without a backfill would therefore
lock existing operators out of the store they have been using.

So the *implicit* grant becomes an explicit row: every operator that exists at
upgrade time joins the ``default`` Org - the one migration 120 seeds and the
one an org-less install resolves to - as ``member``, and ``admin`` joins as
``owner``. Behaviour is unchanged for those operators, and it is now a row an
operator can see and revoke rather than a rule buried in a resolver.

Two deliberate exclusions, both fail-closed:

* ``daemon-*`` operators, minted by pre-BL-P0-01 enrollment, are **not**
  granted anything. Nothing in the store says which Runtime they belonged to,
  so promoting them to Org members would hand a machine credential the
  operator API. They keep authenticating and see nothing; re-enrolling the
  machine mints a proper Runtime-narrow credential.
  ``brains-ai credentials doctor`` lists them.
* Operators created *after* the upgrade get no implicit grant. A new operator
  is invited to an Org deliberately (``brains-ai operator add --org`` or
  ``POST /v1/orgs/{org}/members``).

A store with no ``default`` Org (a fresh install, which has no operators to
carry over either) is a no-op. Idempotent: a membership that already exists is
left exactly as it is, including its role.
"""

from __future__ import annotations

import sqlite3

_LEGACY_DAEMON_PREFIX = "daemon-"


def upgrade(conn: sqlite3.Connection) -> None:
    default_org = conn.execute("SELECT id FROM orgs WHERE slug = 'default'").fetchone()
    if default_org is None:
        return
    org_id = default_org[0]
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT operator_id FROM org_members WHERE org_id = ?", (org_id,)
        ).fetchall()
    }
    for operator_id, slug in conn.execute("SELECT id, slug FROM operators").fetchall():
        if operator_id in existing:
            continue
        if (slug or "").startswith(_LEGACY_DAEMON_PREFIX):
            continue
        role = "owner" if slug == "admin" else "member"
        conn.execute(
            "INSERT INTO org_members (org_id, operator_id, role, created_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (org_id, operator_id, role),
        )

"""Synthetic Workspace Work seed/agent driver, executable only in a disposable container.

Run ``python tests/e2e/fixtures/workspace_work.py seed`` before the main app;
pass the returned key as BRAINS_E2E_KEY and JSON as BRAINS_E2E_WORK_MANIFEST.
Playwright and the app share BRAINS_DB_URL in that container. No test HTTP routes.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4


def guard() -> None:
    if not Path("/.dockerenv").exists() or os.environ.get("BRAINS_E2E_WORK_DRIVER") != "1":
        raise RuntimeError("Workspace Work fixture requires explicit disposable Docker execution")
    state = Path(os.environ["BRAINS_STATE_DIR"]).resolve()
    if state == Path.home() / ".brains" or not str(state).startswith("/tmp/"):
        raise RuntimeError("Use a synthetic /tmp state directory, never the operator home")
    if os.environ.get("BRAINS_DB_URL") != f"sqlite:///{state}/brains.db":
        raise RuntimeError("Driver and server must use the same synthetic SQLite database")


def seed() -> dict:
    from brains.authz import credentials
    from brains.control.common import utc_now
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.orgs import add_member, create_org
    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import AgentSession, SessionLease, Workspace, WorkspaceMembership

    init_db()
    ensure_admin_operator()
    suffix = uuid4().hex[:8]
    org = create_org(f"work-{suffix}", "Synthetic browser work")
    owner, key = add_operator(f"work-owner-{suffix}")
    foreign, _ = add_operator(f"work-foreign-{suffix}")
    add_member(org["id"], owner["slug"], role="owner")
    add_member(org["id"], foreign["slug"], role="member")
    now = utc_now()
    with SessionLocal() as session:
        workspaces = [
            Workspace(
                slug=f"work-{name}-{suffix}",
                name=f"Synthetic {name}",
                path=f"{os.environ['BRAINS_STATE_DIR']}/{name}",
                org_id=org["id"],
                visibility="private" if name == "hidden" else "shared",
            )
            for name in ("alpha", "beta", "hidden")
        ]
        session.add_all(workspaces)
        session.flush()
        session.add(WorkspaceMembership(workspace_id=workspaces[2].id, operator_id=foreign["id"]))
        peers = []
        for index, tool in enumerate(("codex", "opencode", "claude-code")):
            ident = f"work-peer-{index}-{suffix}"
            if index < 2:
                peers.append(ident)
            session.add(
                AgentSession(
                    id=ident,
                    workspace_id=workspaces[0].id,
                    tool=tool,
                    state="running",
                    created_by_operator_id=owner["id"] if index < 2 else foreign["id"],
                    started_at=now,
                    summary="Synthetic private Session summary",
                )
            )
            session.flush()
            session.add(
                SessionLease(
                    session_id=ident, renewed_at=now, lease_expires_at=now + timedelta(hours=4)
                )
            )
        session.commit()
        result = {
            "org": org["slug"],
            "key": key,
            "operator_id": owner["id"],
            "peers": peers,
            "foreign_peer": ident,
            **{
                name: {"slug": row.slug, "id": row.id, "path": row.path}
                for name, row in zip(("alpha", "beta", "hidden"), workspaces, strict=True)
            },
        }
    credentials.sync_local_credentials()
    return result


def drive(data: dict) -> dict:
    from brains.authz import resolver
    from brains.control import coordination as peer
    from brains.control import work_assignments as work
    from brains.control.sessions import end_session, start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import AgentSession, SessionLease, Workspace

    if data["action"] == "sessions":
        with SessionLocal() as session:
            return {
                "sessions": [
                    {
                        "id": row.id,
                        "ended_at": str(row.ended_at),
                        "lease": str(session.get(SessionLease, row.id).lease_expires_at),
                    }
                    for row in session.query(AgentSession).order_by(AgentSession.id)
                ]
            }
    principal = resolver.principal_for_secret(os.environ["BRAINS_E2E_KEY"])
    assert principal is not None
    ident = data["session_id"]
    bound = replace(principal, channel="local", bound_session_ids=frozenset({ident}))
    with resolver.principal_slot():
        resolver.set_current_principal(bound)
        if data["action"] in ("start_retry_owner", "end_session"):
            with SessionLocal() as session:
                actor = session.get(AgentSession, ident)
                assert actor is not None and actor.created_by_operator_id == principal.operator_id
                workspace = session.get(Workspace, actor.workspace_id)
                assert workspace is not None
                path = workspace.path
            if data["action"] == "end_session":
                return end_session(ident, summary="Synthetic retry owner ended")
            return start_session(path, tool="codex", operator=principal.operator_slug)
        if data["action"] == "accept_assignment":
            return work.accept_work_assignment(
                data["code"], session_id=ident, expected_revision=data["revision"]
            )
        row = peer.get_coordination(data["code"], session_id=ident)
        fence = {
            "session_id": ident,
            "version": row["version"],
            "expected_revision": row["revision"],
        }
        if data["action"] == "accept_proposal":
            return peer.accept_coordination(row["code"], spec_hash=row["spec_hash"], **fence)
        if data["action"] in ("initial", "final"):
            return peer.submit_coordination(
                row["code"], data["action"], data["payload"], idempotency_key=uuid4().hex, **fence
            )
        raise ValueError("Unknown synthetic driver action")


if __name__ == "__main__":
    guard()
    result = seed() if sys.argv[1] == "seed" else drive(json.loads(sys.argv[1]))
    print(json.dumps(result))

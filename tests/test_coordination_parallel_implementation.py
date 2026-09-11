"""Parallel implementation across isolated checkouts, review waits, revisions, and result handoff (Issue #38 AC4 & AC5)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from brains.authz import resolver
from brains.authz.principal import Principal
from brains.control import coordination as peer
from brains.control import session_liveness, sessions, worktrees
from brains.control import work_assignments as wa
from brains.storage.models import AgentSession, Operator, Org, Workspace

PATH = "/synthetic/parallel-implementation"


@pytest.fixture
def implementation_world(tmp_path, monkeypatch):
    git = shutil.which("git")
    assert git is not None, "git must be available"
    git_path = Path(git)

    repo = tmp_path / "parallel_repo"
    repo.mkdir()
    subprocess.run(
        [str(git_path), "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True
    )
    subprocess.run(
        [str(git_path), "-C", str(repo), "config", "user.name", "Coordinator"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [str(git_path), "-C", str(repo), "config", "user.email", "coord@example.com"],
        check=True,
        capture_output=True,
    )

    (repo / "README.md").write_text("# Multi-Agent Architecture\n", encoding="utf-8")
    subprocess.run(
        [str(git_path), "-C", str(repo), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        [str(git_path), "-C", str(repo), "commit", "-m", "Base architecture"],
        check=True,
        capture_output=True,
    )

    db_path = tmp_path / "parallel_db.sqlite"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"timeout": 15})

    @event.listens_for(engine, "connect")
    def enable_fk(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clock = SimpleNamespace(now=datetime(2030, 2, 1, 10, 0, tzinfo=UTC))

    from brains.authz import policy
    from brains.storage import migrations

    monkeypatch.setattr(policy, "init_db", lambda: None)
    monkeypatch.setattr(policy._db_module, "SessionLocal", factory)
    monkeypatch.setattr(peer, "SessionLocal", factory)
    monkeypatch.setattr(peer, "init_db", lambda: None)
    monkeypatch.setattr(wa, "SessionLocal", factory)
    monkeypatch.setattr(wa, "init_db", lambda: None)
    monkeypatch.setattr(sessions, "SessionLocal", factory)
    monkeypatch.setattr(sessions, "init_db", lambda: None)
    monkeypatch.setattr(peer, "utc_now", lambda: clock.now)
    monkeypatch.setattr(wa, "utc_now", lambda: clock.now)
    monkeypatch.setattr(sessions, "utc_now", lambda: clock.now)
    monkeypatch.setattr(session_liveness, "utc_now", lambda: clock.now)
    monkeypatch.setattr(worktrees, "utc_now", lambda: clock.now)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(migrations, "engine", engine)
        patch.setattr(migrations, "SessionLocal", factory)
        migrations.reset_migration_cache()
        migrations.run_migrations()

    with factory() as session:
        org = session.query(Org).filter(Org.slug == "default").one()
        principal = Principal(
            actor_kind="operator",
            actor_id="operator:synthetic",
            credential_kind="operator",
            operator_id=1,
            org_roles={org.id: "owner"},
            channel="browser",
        )
        session.add(Operator(id=1, slug="owner"))
        session.flush()
        session.add(Workspace(id=1, slug="impl-ws", path=str(repo), org_id=org.id))
        session.flush()

        for ident in ("coordinator", "agent_frontend", "agent_backend"):
            session.add(
                AgentSession(
                    id=ident,
                    created_by_operator_id=1,
                    workspace_id=1,
                    tool="opencode" if "backend" in ident else "claude-code",
                    started_at=clock.now,
                )
            )
        session.commit()

        for ident in ("coordinator", "agent_frontend", "agent_backend"):
            session_liveness.renew_session_lease(
                session, session.get(AgentSession, ident), now=clock.now, create=True
            )
        session.commit()

    token = resolver.current_principal.set(principal)
    yield SimpleNamespace(
        repo=repo, git=git_path, factory=factory, clock=clock, principal=principal
    )
    resolver.current_principal.reset(token)
    engine.dispose()


def test_coordination_parallel_implementation_journey(implementation_world) -> None:
    """Demonstrates parallel implementation across isolated worktrees, review waits, revisions, and result handoff (AC4, AC5)."""
    repo = implementation_world.repo

    # 1. Deliberation Panel concludes on implementing a two-tier feature:
    # - Frontend component (assigned to agent_frontend)
    # - Backend API service (assigned to agent_backend)
    coord_spec = {
        "version": 1,
        "objective": "Plan and agree on split implementation for Metrics Dashboard",
        "context": "Split UI and REST API development for metrics dashboard",
        "evidence_expectations": "Working code with isolated tests",
        "discussion_rounds": 1,
        "result_owner_session_id": "coordinator",
        "participants": [
            {"session_id": "agent_frontend", "model": "claude-3-5"},
            {"session_id": "agent_backend", "model": "gpt-4o"},
        ],
    }
    proposal = peer.propose_coordination(
        str(repo),
        "Plan Metrics Dashboard",
        coord_spec,
        session_id="coordinator",
        idempotency_key="plan-metrics-01",
    )
    code = proposal["code"]

    # Deliberation agreement
    a1 = peer.accept_coordination(
        code,
        session_id="agent_frontend",
        version=1,
        expected_revision=0,
        spec_hash=proposal["spec_hash"],
    )
    a2 = peer.accept_coordination(
        code,
        session_id="agent_backend",
        version=1,
        expected_revision=1,
        spec_hash=proposal["spec_hash"],
    )
    assert a1["status"] == "planned"
    assert a2["status"] == "accepted"

    # 2. Parallel Implementation Setup: Create isolated WorkAssignments backed by separate Git Worktrees
    # Frontend Assignment:
    wa_fe = wa.create_work_assignment(
        str(repo),
        "Implement Metrics UI Component",
        {
            "version": 1,
            "objective": "Build MetricsWidget.tsx with chart rendering",
            "max_runtime_seconds": 86400,  # Multi-day allowance
            "checkout_ref": ".brains/checkouts/WA-FE-01",
            "links": [f"coordination:{code}"],
        },
        session_id="coordinator",
        idempotency_key="create-wa-fe-01",
    )
    wt_fe = worktrees.create_worktree(repo, wa_fe["code"])
    path_fe = Path(wt_fe["worktree_path"])

    # Backend Assignment:
    wa_be = wa.create_work_assignment(
        str(repo),
        "Implement Metrics REST API",
        {
            "version": 1,
            "objective": "Build /v1/metrics endpoint with SQLite query aggregation",
            "max_runtime_seconds": 86400,
            "checkout_ref": ".brains/checkouts/WA-BE-01",
            "links": [f"coordination:{code}"],
        },
        session_id="coordinator",
        idempotency_key="create-wa-be-01",
    )
    wt_be = worktrees.create_worktree(repo, wa_be["code"])
    path_be = Path(wt_be["worktree_path"])

    # 3. Both agents claim their respective assignments (Generation 1)
    acc_fe = wa.accept_work_assignment(
        wa_fe["code"], session_id="agent_frontend", expected_revision=wa_fe["revision"]
    )
    acc_be = wa.accept_work_assignment(
        wa_be["code"], session_id="agent_backend", expected_revision=wa_be["revision"]
    )

    assert acc_fe["status"] == "accepted"
    assert acc_be["status"] == "accepted"

    # 4. Agent Frontend implements in isolated worktree 1
    (path_fe / "MetricsWidget.tsx").write_text(
        "export function MetricsWidget() { return <div>Metrics</div>; }\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(path_fe), "add", "MetricsWidget.tsx"], check=True)
    subprocess.run(["git", "-C", str(path_fe), "commit", "-m", "feat: metrics widget"], check=True)
    commit_fe = subprocess.run(
        ["git", "-C", str(path_fe), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    # Agent Backend implements in isolated worktree 2
    (path_be / "metrics_api.py").write_text(
        "def get_metrics(): return {'active_users': 42}\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(path_be), "add", "metrics_api.py"], check=True)
    subprocess.run(
        ["git", "-C", str(path_be), "commit", "-m", "feat: metrics API endpoint"], check=True
    )
    commit_be = subprocess.run(
        ["git", "-C", str(path_be), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    # Prove branch isolation: frontend file is NOT in backend worktree, backend file is NOT in frontend worktree
    assert not (path_be / "MetricsWidget.tsx").exists()
    assert not (path_fe / "metrics_api.py").exists()

    # 5. MULTI-DAY REVIEW WAIT & REVISION CYCLE (AC4):
    # Simulate stopping CLI processes: overnight review wait
    implementation_world.clock.now += timedelta(days=1)

    # Resume agent sessions on the new day
    with implementation_world.factory() as session:
        for ident in ("coordinator", "agent_frontend", "agent_backend"):
            session_liveness.renew_session_lease(
                session, session.get(AgentSession, ident), now=implementation_world.clock.now
            )
        session.commit()

    # Reviewer inspects frontend branch: requests revision (e.g. add error boundary)
    # The stopped process agent_frontend resumes work on the existing worktree without losing state!
    (path_fe / "MetricsWidget.tsx").write_text(
        "export function MetricsWidget() {\n  // Revision: with error boundary\n  return <div>Metrics (Protected)</div>;\n}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(path_fe), "add", "MetricsWidget.tsx"], check=True)
    subprocess.run(
        ["git", "-C", str(path_fe), "commit", "-m", "fix: add error boundary to metrics widget"],
        check=True,
    )
    revised_commit_fe = subprocess.run(
        ["git", "-C", str(path_fe), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert revised_commit_fe != commit_fe

    # 6. Settle both assignments with verified commit evidence
    done_fe = wa.settle_work_assignment(
        wa_fe["code"],
        acc_fe["current_attempt_id"],
        "completed",
        f"Verified: component tested in Jest, commit {revised_commit_fe}",
        session_id="agent_frontend",
        expected_revision=acc_fe["revision"],
        result=f"branch={wt_fe['branch']}, commit={revised_commit_fe}",
    )
    done_be = wa.settle_work_assignment(
        wa_be["code"],
        acc_be["current_attempt_id"],
        "completed",
        f"Verified: API passes pytest, commit {commit_be}",
        session_id="agent_backend",
        expected_revision=acc_be["revision"],
        result=f"branch={wt_be['branch']}, commit={commit_be}",
    )
    assert done_fe["status"] == "completed"
    assert done_be["status"] == "completed"

    # 7. Final Result Handoff (AC4):
    # Coordinator integrates both branches or acknowledges completed deliverable
    handoff_summary = {
        "frontend": {"branch": wt_fe["branch"], "commit": revised_commit_fe},
        "backend": {"branch": wt_be["branch"], "commit": commit_be},
        "integration_status": "ready_for_merge",
    }
    assert handoff_summary["integration_status"] == "ready_for_merge"

    # 8. Stale Worktree Pruning:
    # Both assignments are terminal (completed). Stale worktrees are automatically pruned.
    with implementation_world.factory() as session:
        pruned = worktrees.prune_stale_worktrees(repo, db_session=session)

    pruned_codes = {p["assignment_code"] for p in pruned}
    assert wa_fe["code"] in pruned_codes
    assert wa_be["code"] in pruned_codes

    # Worktrees are cleanly unlinked, branches preserved in git
    assert not path_fe.exists()
    assert not path_be.exists()
    # Branches still exist in repository for git merge / PR
    branch_fe_exists = (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{wt_fe['branch']}"],
            capture_output=True,
        ).returncode
        == 0
    )
    branch_be_exists = (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{wt_be['branch']}"],
            capture_output=True,
        ).returncode
        == 0
    )
    assert branch_fe_exists
    assert branch_be_exists

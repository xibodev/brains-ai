"""Contract tests for isolated git worktrees, fenced attempts, and stale cleanup (Issue #36 AC2 & AC3)."""

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
from brains.control import session_liveness, sessions, worktrees
from brains.control import work_assignments as wa
from brains.storage.models import AgentSession, Operator, Org, Workspace


@pytest.fixture
def repo_world(tmp_path):
    git = shutil.which("git")
    assert git is not None, "git must be available"
    git_path = Path(git)

    repo = tmp_path / "target_repo"
    repo.mkdir()
    subprocess.run(
        [str(git_path), "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True
    )
    subprocess.run(
        [str(git_path), "-C", str(repo), "config", "user.name", "Test Agent"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [str(git_path), "-C", str(repo), "config", "user.email", "agent@example.com"],
        check=True,
        capture_output=True,
    )

    initial_file = repo / "README.md"
    initial_file.write_text("# Core Project\n", encoding="utf-8")
    subprocess.run(
        [str(git_path), "-C", str(repo), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        [str(git_path), "-C", str(repo), "commit", "-m", "Initial commit"],
        check=True,
        capture_output=True,
    )

    # SQLite DB for work assignments
    db_path = tmp_path / "test_wa.sqlite"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"timeout": 15})

    @event.listens_for(engine, "connect")
    def enable_fk(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clock = SimpleNamespace(now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC))

    from brains.storage import migrations

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(migrations, "engine", engine)
        patch.setattr(migrations, "SessionLocal", factory)
        patch.setattr(wa, "SessionLocal", factory)
        patch.setattr(wa, "init_db", lambda: None)
        patch.setattr(wa, "utc_now", lambda: clock.now)
        patch.setattr(sessions, "utc_now", lambda: clock.now)
        patch.setattr(session_liveness, "utc_now", lambda: clock.now)
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
        session.add(Workspace(id=1, slug="wa-ws", path=str(repo), org_id=org.id))
        session.flush()

        # Agents
        for ident in ("agent_a", "agent_b", "requester"):
            session.add(
                AgentSession(
                    id=ident,
                    created_by_operator_id=1,
                    workspace_id=1,
                    tool="opencode",
                    started_at=clock.now,
                )
            )
        session.commit()

        for ident in ("agent_a", "agent_b", "requester"):
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


def test_isolated_git_worktrees_parallel_branches(repo_world) -> None:
    """Test AC3: Worktree creation, branch isolation, and cleanup without collision."""
    repo = repo_world.repo

    # 1. Create two parallel worktrees
    wt1 = worktrees.create_worktree(repo, "WA-101")
    wt2 = worktrees.create_worktree(repo, "WA-102")

    path1 = Path(wt1["worktree_path"])
    path2 = Path(wt2["worktree_path"])

    assert path1.is_dir()
    assert path2.is_dir()
    assert wt1["branch"] == "brains/work/WA-101"
    assert wt2["branch"] == "brains/work/WA-102"

    # Verify agent instructions are generated
    instr1 = worktrees.worktree_instructions("WA-101", str(path1), wt1["branch"])
    assert "All code edits and testing must take place inside" in instr1

    # 2. Agent 1 makes changes in worktree 1
    file1 = path1 / "feature1.py"
    file1.write_text("# Feature 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path1), "add", "feature1.py"], check=True)
    subprocess.run(["git", "-C", str(path1), "commit", "-m", "Add feature 1"], check=True)

    # 3. Agent 2 makes different changes in worktree 2
    file2 = path2 / "feature2.py"
    file2.write_text("# Feature 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path2), "add", "feature2.py"], check=True)
    subprocess.run(["git", "-C", str(path2), "commit", "-m", "Add feature 2"], check=True)

    # Assert branch isolation: file1 is not in worktree 2, file2 is not in worktree 1, neither is in main repo
    assert not (path2 / "feature1.py").exists()
    assert not (path1 / "feature2.py").exists()
    assert not (repo / "feature1.py").exists()
    assert not (repo / "feature2.py").exists()

    # 4. List managed worktrees
    managed = worktrees.list_managed_worktrees(repo)
    codes = {m["assignment_code"] for m in managed}
    assert "WA-101" in codes
    assert "WA-102" in codes

    # 5. Clean up worktree 1
    cleaned = worktrees.cleanup_worktree(repo, "WA-101", delete_branch=False)
    assert cleaned["cleaned"] is True
    assert not path1.exists()

    # Worktree 2 remains active
    assert path2.exists()
    remaining = worktrees.list_managed_worktrees(repo)
    assert "WA-101" not in {m["assignment_code"] for m in remaining}
    assert "WA-102" in {m["assignment_code"] for m in remaining}

    # Clean up worktree 2
    worktrees.cleanup_worktree(repo, "WA-102", delete_branch=True)
    assert not path2.exists()


def test_fenced_attempt_ownership_timeout_and_late_completion_rejection(
    repo_world, monkeypatch
) -> None:
    """Test AC2: Fencing against late completion, budget enforcement, and attempt recovery."""
    repo = repo_world.repo
    factory = repo_world.factory

    monkeypatch.setattr(wa, "SessionLocal", factory)
    monkeypatch.setattr(wa, "utc_now", lambda: repo_world.clock.now)

    # 1. Create a work assignment with 300 second runtime budget
    spec = {
        "version": 1,
        "objective": "Implement bounded retry parser",
        "max_runtime_seconds": 300,
        "checkout_ref": ".brains/checkouts/WA-201",
    }
    assignment = wa.create_work_assignment(
        str(repo),
        "WA-201 Implement Parser",
        spec,
        session_id="requester",
        idempotency_key="create-wa-201",
    )
    code = assignment["code"]
    assert assignment["status"] == "ready"
    assert assignment["generation"] == 0

    # Provision isolated worktree for the assignment
    wt = worktrees.create_worktree(repo, code)
    assert Path(wt["worktree_path"]).is_dir()

    # 2. Agent A claims assignment (Generation 1)
    acc_a = wa.accept_work_assignment(
        code, session_id="agent_a", expected_revision=assignment["revision"]
    )
    assert acc_a["status"] == "accepted"
    assert acc_a["generation"] == 1
    attempt_a_id = acc_a["current_attempt_id"]
    assert attempt_a_id is not None

    # Agent A starts working, creates file in worktree
    wt_path = Path(wt["worktree_path"])
    (wt_path / "parser.py").write_text("# in progress by Agent A\n", encoding="utf-8")

    # 3. Time advances past the 300s budget deadline (e.g. Agent A crashed or hung)
    repo_world.clock.now += timedelta(seconds=400)

    # Inspection before reconciliation shows deadline exceeded and observed status uncertain
    snap = wa.get_work_assignment(code, session_id="requester")
    assert snap["deadline_exceeded"] is True
    assert snap["observed_status"] == "uncertain"

    # 4. Reconcile assignment attempts (AC2 budget enforcement)
    reconciled = wa.reconcile_assignment_attempts(code)
    assert reconciled["status"] == "failed"
    failed_attempt = reconciled["attempts"][-1]
    assert failed_attempt["status"] == "failed"
    assert "runtime budget exceeded" in failed_attempt["evidence"]

    # 5. Retry assignment: reset to ready for a new generation
    retried = wa.retry_work_assignment(
        code, session_id="requester", expected_revision=reconciled["revision"]
    )
    assert retried["status"] == "ready"

    # 6. Agent B claims assignment (Generation 2)
    acc_b = wa.accept_work_assignment(
        code, session_id="agent_b", expected_revision=retried["revision"]
    )
    assert acc_b["status"] == "accepted"
    assert acc_b["generation"] == 2
    attempt_b_id = acc_b["current_attempt_id"]
    assert attempt_b_id != attempt_a_id

    # 7. CRITICAL FENCING TEST (AC2):
    # Agent A (old process) wakes up late and attempts to submit completion for Generation 1!
    # Must be REJECTED to prevent stale overwriting of the current owner.
    with pytest.raises(ValueError, match="unknown or unavailable work assignment"):
        wa.settle_work_assignment(
            code,
            attempt_a_id,  # Stale generation 1 attempt ID
            "completed",
            "Late completed evidence from A",
            session_id="agent_a",
            expected_revision=acc_b["revision"],
            result="overwritten result",
        )

    # 8. Agent B submits legitimate completion for Generation 2
    done = wa.settle_work_assignment(
        code,
        attempt_b_id,
        "completed",
        "Parser implemented and verified with unit tests",
        session_id="agent_b",
        expected_revision=acc_b["revision"],
        result="Success: parser.py complete",
    )
    assert done["status"] == "completed"
    assert done["generation"] == 2

    # 9. Stale Worktree Pruning (AC3 cleanup tracking)
    # The assignment is now completed; worktree is stale and must be automatically pruned
    with factory() as session:
        pruned = worktrees.prune_stale_worktrees(repo, db_session=session)
    assert len(pruned) == 1
    assert pruned[0]["assignment_code"] == code
    assert pruned[0]["reason"] == "assignment_completed"
    assert not Path(wt["worktree_path"]).exists()

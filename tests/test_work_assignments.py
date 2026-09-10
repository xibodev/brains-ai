"""Local work state: real schema upgrades, authorization, races and lost replies.

Run in disposable test state. The module builds the real migration corpus once;
individual tests clone that SQLite database rather than rerunning the corpus.
"""

from __future__ import annotations

import importlib
import json
import socket
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from brains.authz import resolver
from brains.authz.principal import Principal
from brains.control import session_liveness, sessions
from brains.control import work_assignments as work
from brains.storage import migration_registry, migrations
from brains.storage.models import (
    AgentSession,
    Base,
    Event,
    EventContext,
    Operator,
    Org,
    SessionLease,
    WorkAssignment,
    WorkAssignmentAttempt,
    Workspace,
    WorkspaceAlias,
)

MIGRATION = "154_work_assignments"
OWN_TABLES = {"work_assignments", "work_assignment_attempts"}
PATH = "/synthetic/work"


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("work-template") / "template.sqlite"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    corpus = migration_registry.build_corpus()
    previous = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name not in OWN_TABLES:
            table.to_metadata(previous)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(migrations, "engine", engine)
        patch.setattr(migrations, "SessionLocal", factory)
        with patch.context() as old:
            old.setattr(
                migrations,
                "corpus",
                lambda: tuple(spec for spec in corpus if spec.migration_id != MIGRATION),
            )
            old.setattr(migrations, "Base", SimpleNamespace(metadata=previous))
            migrations.reset_migration_cache()
            report = migrations.run_migrations()
            assert report.healthy
            assert report.applied[-1] == "153_mailbox_identity_lifecycle"
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO operators (id, slug, created_at) VALUES (901, 'preserved', ?)",
                ("2030-01-01 00:00:00",),
            )
        with factory() as session:
            session.add(Workspace(id=901, slug="preserved", path="/synthetic/preserved"))
            session.flush()
            session.add(
                AgentSession(
                    id="preserved-session",
                    workspace_id=901,
                    tool="codex",
                    created_by_operator_id=901,
                    summary="Historical coordination evidence",
                )
            )
            session.commit()
        with sqlite3.connect(path) as conn:
            before = conn.execute("SELECT * FROM schema_versions ORDER BY id").fetchall()
            old_session = conn.execute("SELECT * FROM agent_sessions").fetchall()

        # Fail after both new tables exist: the runner must roll back their DDL.
        load_upgrade = migrations._load_python_upgrade

        def interrupted_upgrade(path):
            upgrade = load_upgrade(path)
            if path.stem != MIGRATION:
                return upgrade

            def fail_after_ddl(conn):
                upgrade(conn)
                raise RuntimeError("synthetic migration interruption")

            return fail_after_ddl

        with patch.context() as failure:
            failure.setattr(migrations, "_load_python_upgrade", interrupted_upgrade)
            with pytest.raises(migrations.MigrationExecutionError, match="synthetic migration"):
                migrations.run_migrations()
        with sqlite3.connect(path) as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not OWN_TABLES & tables
            assert conn.execute(
                "SELECT status FROM schema_versions WHERE version=?", (MIGRATION,)
            ).fetchone() == ("failed",)
        migrations.reset_migration_cache()
        upgrade = migrations.run_migrations()
        assert upgrade.executed == [MIGRATION]
        assert migrations.run_migrations().executed == []
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT slug FROM operators WHERE id=901").fetchone() == (
                "preserved",
            )
            assert conn.execute("SELECT * FROM agent_sessions").fetchall() == old_session
            assert (
                conn.execute(
                    "SELECT * FROM schema_versions WHERE version != ? ORDER BY id",
                    (MIGRATION,),
                ).fetchall()
                == before
            )
            assert conn.execute(
                "SELECT status, attempts FROM schema_versions WHERE version=?",
                (MIGRATION,),
            ).fetchone() == ("applied", 2)
            # An interrupted ledger replay may call upgrade again; DDL is idempotent.
            importlib.import_module(f"brains.storage.sql_migrations.{MIGRATION}").upgrade(conn)
        migrations.reset_migration_cache()
    engine.dispose()
    return path


@pytest.fixture
def world(migrated_template, tmp_path, monkeypatch):
    path = tmp_path / "work.sqlite"
    with sqlite3.connect(migrated_template) as source, sqlite3.connect(path) as target:
        source.backup(target)
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"timeout": 10})

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(work, "SessionLocal", factory)
    monkeypatch.setattr(work, "init_db", lambda: None)
    clock = SimpleNamespace(now=datetime(2030, 1, 1, tzinfo=UTC))
    for module in (work, sessions, session_liveness):
        monkeypatch.setattr(module, "utc_now", lambda: clock.now)
    principal = Principal(
        actor_kind="operator",
        actor_id="operator:synthetic",
        credential_kind="operator",
        operator_id=1,
        org_roles={1: "member"},
    )
    token = resolver.current_principal.set(principal)
    with factory() as session:
        # Migration 120 seeds the default Org, so use that durable row.
        org = session.query(Org).filter(Org.slug == "default").one()
        principal = replace(principal, org_roles={org.id: "member"})
        resolver.current_principal.set(principal)
        session.add_all([Operator(id=1, slug="owner"), Operator(id=2, slug="other")])
        session.flush()
        session.add_all(
            [
                Workspace(id=1, slug="work", path=PATH, org_id=org.id),
                Workspace(id=2, slug="other", path="/synthetic/other", org_id=org.id),
                Workspace(
                    id=3,
                    slug="hidden",
                    path="/synthetic/hidden",
                    org_id=org.id,
                    visibility="private",
                ),
            ]
        )
        session.flush()
        session.add(
            WorkspaceAlias(workspace_id=1, path="/synthetic/alias", identity_key="synthetic")
        )
        for ident, owner, workspace in (
            ("creator", 1, 1),
            ("worker", 1, 1),
            ("continuation", 1, 1),
            ("foreign", 2, 1),
            ("elsewhere", 1, 2),
            ("hidden", 1, 3),
            ("legacy", None, 1),
        ):
            session.add(
                AgentSession(
                    id=ident,
                    workspace_id=workspace,
                    tool="opencode" if ident == "worker" else "codex",
                    created_by_operator_id=owner,
                    started_at=clock.now,
                )
            )
        session.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("work state must not spawn, connect, or read checkout references")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    def create(spec=None, **kwargs):
        return work.create_work_assignment(
            PATH,
            "Synthetic assignment",
            {"objective": "Check synthetic evidence"} if spec is None else spec,
            **({"session_id": "creator", "idempotency_key": "request"} | kwargs),
        )

    def accept(row, actor="worker"):
        return work.accept_work_assignment(
            row["code"], session_id=actor, expected_revision=row["revision"]
        )

    def settle(row, outcome="completed", actor="worker", evidence="Synthetic probe", result=""):
        return work.settle_work_assignment(
            row["code"],
            row["current_attempt_id"],
            outcome,
            evidence,
            session_id=actor,
            expected_revision=row["revision"],
            result=result,
        )

    yield SimpleNamespace(
        db=factory,
        path=path,
        engine=engine,
        clock=clock,
        principal=principal,
        create=create,
        accept=accept,
        settle=settle,
        forbidden=forbidden,
    )
    resolver.current_principal.reset(token)
    engine.dispose()


def test_migration_from_153_rollback_rerun_and_model_shape(world):
    with world.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    reference = create_engine("sqlite://")
    try:
        Base.metadata.create_all(reference)
        for table in OWN_TABLES:
            with world.engine.connect() as actual, reference.connect() as expected:
                for pragma in ("table_info", "foreign_key_list", "index_list"):
                    left = actual.exec_driver_sql(f"PRAGMA {pragma}({table})").fetchall()
                    right = expected.exec_driver_sql(f"PRAGMA {pragma}({table})").fetchall()
                    # Index/FK enumeration order is not part of the schema contract.
                    if pragma == "table_info":
                        assert left == right
                    else:
                        assert sorted(tuple(r)[1:] for r in left) == sorted(
                            tuple(r)[1:] for r in right
                        )
    finally:
        reference.dispose()


def test_spec_is_canonical_immutable_and_references_are_inert(world, monkeypatch):
    spec = {
        "objective": "Inspect",
        "checkout_ref": "/never/read/checkout",
        "tool": "advisory",
        "links": ["https://invalid.example/evidence"],
        "context": "Data only",
    }
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", world.forbidden)
        patch.setattr(Path, "resolve", world.forbidden)
        row = world.create(spec)
        accepted = world.accept(row)
    assert row["specification"]["version"] == 1
    assert row["code"].startswith("WA-") and len(row["code"]) == 39
    assert accepted["attempts"][0]["tool"] == "opencode"
    assert accepted["attempts"][0]["usage"] is None
    spec["links"].append("changed")
    again = world.create(
        dict(reversed(list(row["specification"].items()))), session_id="continuation"
    )
    assert again == accepted
    with pytest.raises(ValueError, match="different request"):
        world.create(spec)
    with world.db() as session:
        assert session.query(WorkAssignment).count() == 1
        assert session.query(Event).count() == 2
        assert {r.category for r in session.query(EventContext)} == {"task"}
        assert {r.scope for r in session.query(EventContext)} == {"workspace"}


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"objective": " "},
        {"objective": 1},
        {"objective": "\ud800"},
        {"objective": "ok", "unknown": "value"},
        {"objective": "ok", "version": True},
        {"objective": "ok", "version": 2},
        {"objective": "ok", "context": {}},
        {"objective": "ok", "deadline": None},
        {"objective": "ok", "deadline": "2030-01-01"},
        {"objective": "ok", "max_runtime_seconds": 1.0},
        {"objective": "ok", "max_runtime_seconds": float("nan")},
        {"objective": "ok", "max_runtime_seconds": float("inf")},
        {"objective": "ok", "max_runtime_seconds": True},
        {"objective": "ok", "max_runtime_seconds": 0},
        {"objective": "ok", "max_runtime_seconds": 604801},
        {"objective": "ok", "links": ("a",)},
        {"objective": "ok", "links": [1]},
        {"objective": "ok", "links": ["x"] * 33},
        {"objective": "x" * 32768},
    ],
)
def test_spec_refusals_leave_no_rows(world, spec):
    with pytest.raises(ValueError):
        world.create(spec)
    with world.db() as session:
        assert session.query(WorkAssignment).count() == session.query(Event).count() == 0


def test_input_byte_bounds_and_strict_revision(world):
    for field, value in (("title", "é" * 129), ("title", ""), ("idempotency_key", "x" * 129)):
        args = dict(
            workspace_path=PATH,
            title="Title",
            specification={"objective": "ok"},
            session_id="creator",
            idempotency_key="key",
        )
        args[field] = value
        with pytest.raises(ValueError):
            work.create_work_assignment(**args)
    row = world.create()
    for revision in (True, 1.0, 0, -1):
        with pytest.raises(ValueError, match="expected_revision"):
            work.accept_work_assignment(
                row["code"], session_id="worker", expected_revision=revision
            )
    accepted = world.accept(row)
    for evidence, result in (("", ""), ("é" * 32769, ""), ("ok", "x" * 65537)):
        with pytest.raises(ValueError):
            world.settle(accepted, evidence=evidence, result=result)
    completed = world.settle(accepted, evidence="é" * 32768, result="x" * 65536)
    assert completed["status"] == "completed"


def test_lost_ack_requires_read_then_exact_noop(world):
    row = world.create()
    accepted = world.accept(row)
    with pytest.raises(ValueError, match="stale"):
        world.accept(row)
    assert world.accept(accepted) == accepted
    with pytest.raises(ValueError, match="not ready"):
        world.accept(accepted, actor="continuation")
    completed = world.settle(accepted, result="done")
    with pytest.raises(ValueError, match="stale"):
        world.settle(accepted, result="done")
    assert world.settle(completed, result="done") == completed
    with pytest.raises(ValueError, match="already reported"):
        world.settle(completed, result="different")
    with world.db() as session:
        assert session.query(Event).count() == 3
        assert session.query(WorkAssignmentAttempt).count() == 1


def test_delayed_cancel_requires_observed_confirmation_and_preserves_history(world):
    accepted = world.accept(world.create())
    requested = work.cancel_work_assignment(
        accepted["code"], session_id="continuation", expected_revision=2
    )
    assert requested["status"] == "cancel_requested"
    assert requested["attempts"][0]["settled_at"] is None
    with pytest.raises(ValueError, match="stale"):
        world.settle(accepted)
    with pytest.raises(ValueError, match="completion after"):
        world.settle(requested)
    with pytest.raises(ValueError, match="unavailable"):
        world.settle(requested, outcome="cancelled", actor="continuation")
    with pytest.raises(ValueError, match="conclusive"):
        work.retry_work_assignment(requested["code"], session_id="creator", expected_revision=3)
    cancelled = world.settle(requested, outcome="cancelled")
    old_attempt = cancelled["attempts"][0]
    ready = work.retry_work_assignment(
        cancelled["code"], session_id="continuation", expected_revision=4
    )
    next_attempt = world.accept(ready, actor="continuation")
    assert next_attempt["attempts"][0] == old_attempt
    assert next_attempt["generation"] == 2
    assert next_attempt["current_attempt_id"] != accepted["current_attempt_id"]
    with pytest.raises(ValueError, match="unavailable"):
        work.settle_work_assignment(
            next_attempt["code"],
            old_attempt["attempt_id"],
            "failed",
            "late",
            session_id="worker",
            expected_revision=6,
        )


@pytest.mark.parametrize("outcome", ["failed", "uncertain"])
def test_cancel_race_failure_or_uncertainty(world, outcome):
    accepted = world.accept(world.create())
    requested = work.cancel_work_assignment(
        accepted["code"], session_id="creator", expected_revision=2
    )
    reported = world.settle(requested, outcome=outcome)
    if outcome == "failed":
        assert (
            work.retry_work_assignment(reported["code"], session_id="creator", expected_revision=4)[
                "status"
            ]
            == "ready"
        )
    else:
        assert reported["attempts"][0]["settled_at"] is None
        with pytest.raises(ValueError, match="conclusive"):
            work.retry_work_assignment(reported["code"], session_id="creator", expected_revision=4)
        with pytest.raises(ValueError, match="reconciliation"):
            world.settle(reported, outcome="cancelled")


def test_cancel_ready_creates_no_attempt_and_explicit_retry_only(world):
    row = world.create()
    cancelled = work.cancel_work_assignment(row["code"], session_id="creator", expected_revision=1)
    assert cancelled["attempts"] == []
    assert (
        work.cancel_work_assignment(row["code"], session_id="creator", expected_revision=2)
        == cancelled
    )
    ready = work.retry_work_assignment(row["code"], session_id="continuation", expected_revision=2)
    assert ready["status"] == "ready" and ready["attempts"] == []
    assert world.accept(ready)["generation"] == 1


def test_deadline_is_durable_read_only_observation_over_days(world):
    row = world.create(
        {
            "objective": "wait",
            "deadline": "2030-01-03T02:00:00+02:00",
            "max_runtime_seconds": 604800,
        }
    )
    accepted = world.accept(row)
    assert accepted["attempts"][0]["deadline_at"] == "2030-01-03T00:00:00+00:00"
    world.clock.now += timedelta(days=3)
    observed = work.get_work_assignment(row["code"], session_id="continuation")
    assert observed["deadline_exceeded"] is True
    assert observed["observed_status"] == "uncertain"
    assert observed["status"] == "accepted" and observed["revision"] == 2
    with pytest.raises(ValueError, match="conclusive"):
        work.retry_work_assignment(row["code"], session_id="creator", expected_revision=2)
    assert world.accept(observed)["current_attempt_id"] == accepted["current_attempt_id"]
    with world.db() as session:
        assert session.query(Event).count() == 2
        assert session.get(WorkAssignment, row["code"]).status == "accepted"
    assert world.settle(observed, outcome="failed")["status"] == "failed"


def test_runtime_budget_default_and_expired_unaccepted_deadline(world):
    accepted = world.accept(world.create())
    assert accepted["attempts"][0]["max_runtime_seconds"] == 3600
    assert accepted["attempts"][0]["deadline_at"] == "2030-01-01T01:00:00+00:00"
    row = world.create(
        {"objective": "expired", "deadline": "2029-12-31T23:59:59Z"}, idempotency_key="old"
    )
    with pytest.raises(ValueError, match="deadline exceeded"):
        world.accept(row)
    assert work.get_work_assignment(row["code"], session_id="creator")["attempts"] == []
    budgeted = world.accept(
        world.create(
            {"objective": "bounded", "deadline": "2030-01-05T00:00:00Z", "max_runtime_seconds": 90},
            idempotency_key="budgeted",
        )
    )
    assert budgeted["attempts"][0]["deadline_at"] == "2030-01-01T00:01:30+00:00"


def test_session_end_does_not_transfer_or_delete_attempt(world):
    accepted = world.accept(world.create())
    with world.db() as session:
        worker = session.get(AgentSession, "worker")
        worker.ended_at = world.clock.now
        worker.state = "completed"
        session.commit()
    observed = work.get_work_assignment(accepted["code"], session_id="continuation")
    assert observed["observed_status"] == "uncertain"
    assert observed["attempts"][0]["source_session_id"] == "worker"
    with pytest.raises(ValueError, match="unavailable"):
        world.settle(observed, actor="continuation")
    with pytest.raises(ValueError, match="ended"):
        world.settle(observed)
    requested = work.cancel_work_assignment(
        accepted["code"], session_id="continuation", expected_revision=2
    )
    assert requested["observed_status"] == "uncertain"
    with pytest.raises(ValueError, match="conclusive"):
        work.retry_work_assignment(accepted["code"], session_id="creator", expected_revision=3)


@pytest.mark.parametrize("actor", ["foreign", "elsewhere", "hidden", "legacy", "missing"])
def test_every_api_fences_actor_before_lease_side_effects(world, actor):
    row = world.accept(world.create())
    with world.db() as session:
        for ident in ("creator", "worker", "foreign", "elsewhere", "hidden", "legacy"):
            session.add(
                SessionLease(
                    session_id=ident,
                    renewed_at=world.clock.now,
                    lease_expires_at=world.clock.now + timedelta(hours=1),
                )
            )
        session.commit()
    calls = [
        lambda: world.create(session_id=actor),
        lambda: work.get_work_assignment(row["code"], session_id=actor),
        lambda: work.list_work_assignments(PATH, session_id=actor),
        lambda: world.accept(row, actor=actor),
        lambda: world.settle(row, actor=actor),
        lambda: work.cancel_work_assignment(row["code"], session_id=actor, expected_revision=2),
        lambda: work.retry_work_assignment(row["code"], session_id=actor, expected_revision=2),
    ]
    world.clock.now += timedelta(minutes=5)
    for call in calls:
        with pytest.raises(ValueError, match="unknown or unavailable"):
            call()
    with world.db() as session:
        assert {work._iso(lease.renewed_at) for lease in session.query(SessionLease)} == {
            "2030-01-01T00:00:00+00:00"
        }
        assert session.query(Event).count() == 2


def test_context_slot_anonymous_runtime_and_other_operator_are_denied(world):
    row = world.create()
    for principal in (
        resolver._PrincipalSlot(),
        replace(world.principal, operator_id=2, is_bootstrap_admin=True),
        replace(world.principal, actor_kind="runtime"),
        replace(world.principal, operator_id=999),
        replace(world.principal, org_roles={}),
    ):
        token = resolver.current_principal.set(principal)
        try:
            with pytest.raises(ValueError, match="unavailable"):
                work.get_work_assignment(row["code"], session_id="creator")
        finally:
            resolver.current_principal.reset(token)
    token = resolver.current_principal.set(replace(world.principal, is_bootstrap_admin=True))
    try:
        assert world.create(session_id="legacy")["code"] == row["code"]
    finally:
        resolver.current_principal.reset(token)


def test_visibility_rechecked_under_writer_lock(world, monkeypatch):
    row = world.create()
    original = work._lock_session_lifecycle

    def revoke(session, session_id):
        original(session, session_id)
        session.query(Workspace).filter(Workspace.id == 1).update({"visibility": "private"})

    monkeypatch.setattr(work, "_lock_session_lifecycle", revoke)
    with pytest.raises(ValueError, match="unavailable"):
        world.accept(row)
    assert work.get_work_assignment(row["code"], session_id="creator")["revision"] == 1


def test_database_reopen_and_workspace_alias_list(world):
    accepted = world.accept(world.create())
    world.engine.dispose()
    assert work.get_work_assignment(accepted["code"], session_id="worker") == accepted
    assert work.list_work_assignments("/synthetic/alias", session_id="creator") == [accepted]
    for limit in (True, 0, 201, 1.0):
        with pytest.raises(ValueError, match="limit"):
            work.list_work_assignments(PATH, session_id="creator", limit=limit)


def test_same_creation_key_is_scoped_to_workspace_and_principal(world):
    first = world.create()
    elsewhere = work.create_work_assignment(
        "/synthetic/other",
        "Synthetic assignment",
        first["specification"],
        session_id="elsewhere",
        idempotency_key="request",
    )
    token = resolver.current_principal.set(replace(world.principal, operator_id=2))
    try:
        foreign = world.create(session_id="foreign")
        with pytest.raises(ValueError, match="unavailable"):
            work.get_work_assignment(first["code"], session_id="foreign")
        assert work.list_work_assignments(PATH, session_id="foreign") == [foreign]
    finally:
        resolver.current_principal.reset(token)
    assert len({first["code"], elsewhere["code"], foreign["code"]}) == 3


def test_duplicate_create_and_reads_do_not_renew_leases(world):
    row = world.create()
    with world.db() as session:
        session.add(
            SessionLease(
                session_id="creator",
                renewed_at=world.clock.now,
                lease_expires_at=world.clock.now + timedelta(hours=1),
            )
        )
        session.commit()
    world.clock.now += timedelta(minutes=5)
    assert world.create() == row
    assert work.get_work_assignment(row["code"], session_id="creator") == row
    assert work.list_work_assignments(PATH, session_id="creator") == [row]
    with world.db() as session:
        assert (
            work._iso(session.get(SessionLease, "creator").renewed_at)
            == "2030-01-01T00:00:00+00:00"
        )


def test_uncertain_report_keeps_the_sql_live_attempt_slot(world):
    row = world.settle(world.accept(world.create()), outcome="uncertain")
    with world.db() as session:
        attempt = session.get(WorkAssignmentAttempt, row["current_attempt_id"])
        values = {
            column.name: getattr(attempt, column.name) for column in attempt.__table__.columns
        }
        session.add(
            WorkAssignmentAttempt(
                **(
                    values
                    | {
                        "attempt_id": "second-live",
                        "generation": 2,
                        "status": "accepted",
                    }
                )
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    with pytest.raises(ValueError, match="conclusive"):
        work.retry_work_assignment(row["code"], session_id="creator", expected_revision=3)


def _race(world, calls):
    barrier = threading.Barrier(len(calls))

    def invoke(call):
        token = resolver.current_principal.set(world.principal)
        try:
            barrier.wait(timeout=10)
            try:
                return call()
            except ValueError as exc:
                return str(exc)
        finally:
            resolver.current_principal.reset(token)

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(invoke, calls))


def test_concurrent_creation_and_acceptance_are_single_winner(world):
    created = _race(
        world, [lambda: world.create(), lambda: world.create(session_id="continuation")]
    )
    assert created[0]["code"] == created[1]["code"]
    accepted = _race(
        world,
        [lambda: world.accept(created[0]), lambda: world.accept(created[0], actor="continuation")],
    )
    assert sum(isinstance(result, dict) for result in accepted) == 1
    assert any("stale" in result for result in accepted if isinstance(result, str))
    with world.db() as session:
        assert session.query(WorkAssignmentAttempt).count() == 1
        assert session.query(Event).count() == 2


def test_cancel_and_success_race_are_revision_fenced(world):
    row = world.accept(world.create())
    results = _race(
        world,
        [
            lambda: world.settle(row),
            lambda: work.cancel_work_assignment(
                row["code"],
                session_id="creator",
                expected_revision=2,
            ),
        ],
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert any("stale" in result for result in results if isinstance(result, str))
    assert work.get_work_assignment(row["code"], session_id="creator")["revision"] == 3


def test_event_failure_rolls_back_attempt_revision_and_lease(world, monkeypatch):
    row = world.create()
    with world.db() as session:
        session.add(
            SessionLease(
                session_id="worker",
                renewed_at=world.clock.now,
                lease_expires_at=world.clock.now + timedelta(hours=1),
            )
        )
        session.commit()
    world.clock.now += timedelta(minutes=5)
    monkeypatch.setattr(work, "classify_event_kind", world.forbidden)
    with pytest.raises(AssertionError):
        world.accept(row)
    with world.db() as session:
        assert session.get(WorkAssignment, row["code"]).revision == 1
        assert session.query(WorkAssignmentAttempt).count() == 0
        assert session.query(Event).count() == 1
        assert (
            work._iso(session.get(SessionLease, "worker").renewed_at) == "2030-01-01T00:00:00+00:00"
        )


def test_sql_constraints_block_parallel_live_generations_and_invalid_state(world):
    row = world.accept(world.create())
    for changes in (
        {"attempt_id": "duplicate-live", "generation": 2},
        {"attempt_id": "duplicate-generation", "status": "failed", "settled_at": world.clock.now},
        {"attempt_id": "invalid-state", "generation": 2, "status": "invented"},
        {"attempt_id": "false-terminal", "generation": 2, "status": "completed"},
    ):
        with world.db() as session:
            original = session.get(WorkAssignmentAttempt, row["current_attempt_id"])
            values = {
                column.name: getattr(original, column.name) for column in original.__table__.columns
            }
            session.add(WorkAssignmentAttempt(**(values | changes)))
            with pytest.raises(IntegrityError):
                session.commit()
    with world.db() as session:
        session.get(WorkAssignment, row["code"]).revision = 0
        with pytest.raises(IntegrityError):
            session.commit()
    with world.db() as session:
        assert session.query(WorkAssignmentAttempt).count() == 1
        assert (
            json.loads(session.get(WorkAssignment, row["code"]).specification_json)["version"] == 1
        )

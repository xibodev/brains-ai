"""Local peer protocol: synthetic SQLite, explicit boundaries and durable recovery."""

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
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from brains.authz import resolver
from brains.authz.principal import Principal
from brains.control import coordination as peer
from brains.control import session_liveness, sessions
from brains.storage import migration_registry, migrations
from brains.storage.models import (
    AgentSession,
    Base,
    CoordinationContribution,
    CoordinationProposal,
    Event,
    EventContext,
    Operator,
    Org,
    SessionLease,
    WorkAssignment,
    Workspace,
)

MIGRATION = "155_peer_coordination"
OWN_TABLES = {"coordination_proposals", "coordination_contributions"}
PATH = "/synthetic/peers"


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("peer-template") / "template.sqlite"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    corpus = migration_registry.build_corpus()
    through_155 = MetaData()
    previous = MetaData()
    for table in Base.metadata.sorted_tables:
        historical = table.to_metadata(through_155)
        if table.name in {"work_assignments", "coordination_proposals"}:
            historical._columns.remove(historical.c.creator_kind)
            historical.constraints = {
                constraint
                for constraint in historical.constraints
                if constraint.name not in {"ck_work_assignments_creator", "ck_coordination_creator"}
            }
            historical.c.creator_session_id.nullable = False
        if table.name not in OWN_TABLES:
            historical.to_metadata(previous)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(migrations, "engine", engine)
        patch.setattr(migrations, "SessionLocal", factory)
        patch.setattr(migrations, "Base", SimpleNamespace(metadata=through_155))
        # Prove real 155 interruption/replay before applying later model upgrades.
        patch.setattr(
            migrations, "corpus", lambda: tuple(s for s in corpus if s.migration_id <= MIGRATION)
        )
        with patch.context() as old:
            old.setattr(
                migrations,
                "corpus",
                lambda: tuple(s for s in corpus if s.migration_id < MIGRATION),
            )
            old.setattr(migrations, "Base", SimpleNamespace(metadata=previous))
            migrations.reset_migration_cache()
            report = migrations.run_migrations()
            assert report.healthy
            assert report.applied[-1] == "154_work_assignments"
        with factory() as session:
            session.add(Operator(id=901, slug="preserved"))
            session.flush()
            session.add(Workspace(id=901, slug="preserved", path="/synthetic/preserved"))
            session.flush()
            session.add(
                AgentSession(
                    id="preserved",
                    workspace_id=901,
                    tool="codex",
                    created_by_operator_id=901,
                    summary="preserved historical evidence",
                )
            )
            session.commit()
        with sqlite3.connect(path) as conn:
            ledger = conn.execute("SELECT * FROM schema_versions ORDER BY id").fetchall()
            history = conn.execute("SELECT * FROM agent_sessions").fetchall()
        load_upgrade = migrations._load_python_upgrade

        def interrupted(path):
            upgrade = load_upgrade(path)
            if path.stem != MIGRATION:
                return upgrade

            def fail(conn):
                upgrade(conn)
                raise RuntimeError("synthetic peer interruption")

            return fail

        with patch.context() as failure:
            failure.setattr(migrations, "_load_python_upgrade", interrupted)
            with pytest.raises(
                migrations.MigrationExecutionError, match="synthetic peer interruption"
            ):
                migrations.run_migrations()
        with sqlite3.connect(path) as conn:
            assert not OWN_TABLES & {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        migrations.reset_migration_cache()
        assert migrations.run_migrations().executed == [MIGRATION]
        assert migrations.run_migrations().executed == []
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT * FROM agent_sessions").fetchall() == history
            assert (
                conn.execute(
                    "SELECT * FROM schema_versions WHERE version != ? ORDER BY id", (MIGRATION,)
                ).fetchall()
                == ledger
            )
            assert conn.execute(
                "SELECT status, attempts FROM schema_versions WHERE version=?", (MIGRATION,)
            ).fetchone() == ("applied", 2)
            importlib.import_module(f"brains.storage.sql_migrations.{MIGRATION}").upgrade(conn)
        patch.setattr(migrations, "corpus", lambda: corpus)
        patch.setattr(migrations, "Base", Base)
        migrations.reset_migration_cache()
        assert migrations.run_migrations().healthy
        migrations.reset_migration_cache()
    engine.dispose()
    return path


@pytest.fixture
def world(migrated_template, tmp_path, monkeypatch):
    path = tmp_path / "peers.sqlite"
    with sqlite3.connect(migrated_template) as source, sqlite3.connect(path) as target:
        source.backup(target)
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"timeout": 15})

    @event.listens_for(engine, "connect")
    def enable_fk(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(peer, "SessionLocal", factory)
    monkeypatch.setattr(peer, "init_db", lambda: None)
    clock = SimpleNamespace(now=datetime(2030, 1, 1, tzinfo=UTC))
    for module in (peer, sessions, session_liveness):
        monkeypatch.setattr(module, "utc_now", lambda: clock.now)
    with factory() as session:
        org = session.query(Org).filter(Org.slug == "default").one()
        principal = Principal(
            actor_kind="operator",
            actor_id="operator:synthetic",
            credential_kind="operator",
            operator_id=1,
            org_roles={org.id: "member"},
        )
        session.add_all([Operator(id=1, slug="owner"), Operator(id=2, slug="foreign")])
        session.flush()
        session.add_all(
            [
                Workspace(id=1, slug="peers", path=PATH, org_id=org.id),
                Workspace(id=2, slug="other", path="/synthetic/other", org_id=org.id),
                Workspace(
                    id=3,
                    slug="private",
                    path="/synthetic/private",
                    org_id=org.id,
                    visibility="private",
                ),
            ]
        )
        session.flush()
        for ident, owner, ws in (
            ("requester", 1, 1),
            ("a", 1, 1),
            ("b", 1, 1),
            ("unrelated", 1, 1),
            ("foreign", 2, 1),
            ("elsewhere", 1, 2),
            ("private", 1, 3),
            ("legacy", None, 1),
        ):
            session.add(
                AgentSession(
                    id=ident,
                    created_by_operator_id=owner,
                    workspace_id=ws,
                    tool="opencode" if ident == "b" else "codex",
                    started_at=clock.now,
                )
            )
        session.commit()
    token = resolver.current_principal.set(principal)

    def forbidden(*args, **kwargs):
        raise AssertionError("peer coordination must not spawn or connect")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    def create(**changes):
        spec = {
            "objective": "Synthetic objective",
            "context": "Selected snapshot",
            "evidence_expectations": "Cite synthetic probe",
            "participants": [
                {"session_id": "a", "model": None},
                {"session_id": "b", "model": "declared"},
            ],
        }
        spec.update(changes)
        return peer.propose_coordination(
            PATH, "Synthetic proposal", spec, session_id="requester", idempotency_key="create"
        )

    yield SimpleNamespace(
        factory=factory, engine=engine, path=path, clock=clock, principal=principal, create=create
    )
    resolver.current_principal.reset(token)
    engine.dispose()


def accept(row, actor):
    return peer.accept_coordination(
        row["code"],
        session_id=actor,
        version=row["version"],
        expected_revision=row["revision"],
        spec_hash=row["spec_hash"],
    )


def advance(row):
    return peer.advance_coordination(
        row["code"],
        session_id="requester",
        version=row["version"],
        expected_revision=row["revision"],
    )


def submit(row, actor, kind="initial", key=None, **changes):
    payload = (
        {"summary": "All looks positive", "evidence": "synthetic final probe"}
        if kind == "final"
        else {
            "findings": f"secret-{actor}-{row['round']}",
            "evidence": "synthetic probe",
            "uncertainty": "Not verified externally",
            "dissent": f"original objection {actor}",
        }
    )
    payload.update(changes)
    return peer.submit_coordination(
        row["code"],
        kind,
        payload,
        session_id=actor,
        version=row["version"],
        expected_revision=row["revision"],
        idempotency_key=key or f"{kind}-{row['round']}",
    )


def collecting(world, **changes):
    row = world.create(**changes)
    row = accept(row, "a")
    row = accept(row, "b")
    assert row["status"] == "accepted"
    return advance(row)


def seed_leases(world):
    with world.factory() as session:
        for ident in ("requester", "a", "b"):
            session_liveness.renew_session_lease(
                session, session.get(AgentSession, ident), now=world.clock.now, create=True
            )
        session.commit()


def test_blinding_all_read_and_mutation_paths(world):
    row = collecting(world)
    row = submit(row, "a")
    assert [c["author_session_id"] for c in row["contributions"]] == ["a"]
    for actor in ("requester", "b"):
        for snapshot in (
            peer.get_coordination(row["code"], session_id=actor),
            *peer.list_coordinations(PATH, session_id=actor),
            accept(row, actor),
        ):
            assert snapshot["counts"]["initial"] == 1
            assert snapshot["contributions"] == []
            assert snapshot["unresolved_dissent"] == []
            assert "secret-a" not in json.dumps(snapshot)
    with pytest.raises(ValueError, match="all initial"):
        advance(row)
    row = submit(row, "b")
    assert row["initial_closed"] is False
    assert [c["author_session_id"] for c in row["contributions"]] == ["b"]
    assert "secret-a" not in json.dumps(row)
    assert peer.get_coordination(row["code"], session_id="requester")["contributions"] == []
    row = advance(row)
    assert row["initial_closed"] is True and row["round"] == 1
    assert len(row["contributions"]) == 2
    assert len(row["unresolved_dissent"]) == 2
    with world.factory() as session:
        events = session.query(Event).filter(Event.kind.like("coordination_%")).all()
        assert events
        for item in events:
            assert "secret-" not in item.message + item.metadata_json
            assert "Synthetic" not in item.message + item.metadata_json
            context = session.get(EventContext, item.id)
            assert context.category == "coordination" and context.scope == "workspace"


@pytest.mark.parametrize("rounds", [0, 1, 3])
def test_rounds_final_owner_and_dissent_cannot_be_omitted(world, rounds):
    row = collecting(world, discussion_rounds=rounds, result_owner_session_id="b")
    with pytest.raises(ValueError, match="not ready"):
        submit(row, "b", "final")
    row = submit(row, "a")
    row = submit(row, "b")
    row = advance(row)
    for round in range(1, rounds + 1):
        assert row["round"] == round and not row["final_ready"]
        with pytest.raises(ValueError, match="all discussion"):
            advance(row)
        row = submit(row, "a", "discussion")
        row = submit(row, "b", "discussion")
        row = advance(row)
    assert row["final_ready"]
    assert row["round"] == (rounds + 1 if rounds else 0)
    with pytest.raises(ValueError, match="unavailable"):
        submit(row, "requester", "final")
    row = submit(row, "b", "final")
    assert row["status"] == "completed" and not row["incomplete_flag"]
    assert row["counts"]["contributions"] == 2 * (1 + rounds) + 1
    assert len(row["unresolved_dissent"]) == 2 * (1 + rounds)
    assert all(not d["resolved"] for d in row["unresolved_dissent"])
    assert row["final"]["summary"] == "All looks positive"
    assert submit(row, "b", "final") == row
    with pytest.raises(ValueError, match="different contribution"):
        submit(row, "b", "final", summary="Changed")


def test_version_reaccept_stale_and_cancelled_history(world):
    row = collecting(world)
    row = submit(row, "a")
    original = peer.get_coordination(row["code"], session_id="a")
    spec = {k: v for k, v in row["specification"].items()}
    spec["participants"] = [
        {k: v for k, v in p.items() if k != "tool"} for p in spec["participants"]
    ]
    spec["context"] = "A replacement immutable context"
    new = peer.propose_coordination(
        PATH,
        "Replacement",
        spec,
        session_id="requester",
        code=row["code"],
        expected_revision=row["revision"],
        idempotency_key="replace",
    )
    assert new["version"] == 2 and new["revision"] == original["revision"] + 2
    assert new["accepted_session_ids"] == ["requester"]
    assert new["contributions"] == []
    old = peer.get_coordination(row["code"], session_id="a", version=1)
    assert old["status"] == "cancelled" and old["revision"] == original["revision"] + 1
    assert old["specification"] == original["specification"]
    assert old["contributions"] == original["contributions"]
    with pytest.raises(ValueError, match="stale"):
        accept(row, "a")
    with pytest.raises(ValueError, match="spec_hash"):
        peer.accept_coordination(
            new["code"],
            session_id="a",
            version=2,
            expected_revision=new["revision"],
            spec_hash=row["spec_hash"],
        )
    new = accept(new, "a")
    new = accept(new, "b")
    assert new["status"] == "accepted"


def replacement(row, *, actor="requester", key="replace", context="Replacement"):
    spec = dict(row["specification"])
    spec["participants"] = [
        {k: v for k, v in participant.items() if k != "tool"}
        for participant in spec["participants"]
    ]
    spec["context"] = context
    return peer.propose_coordination(
        PATH,
        "Replacement",
        spec,
        session_id=actor,
        code=row["code"],
        expected_revision=row["revision"],
        idempotency_key=key,
    )


def test_replacement_global_revision_rejects_old_zero_and_cancel_revision(world):
    seed_leases(world)
    original = world.create()
    assert original["revision"] == 0
    current = replacement(original)
    assert current["version"] == 2 and current["revision"] == 2
    cancelled = peer.get_coordination(original["code"], session_id="requester", version=1)
    assert cancelled["revision"] == 1 and cancelled["status"] == "cancelled"
    with world.factory() as session:
        events = session.query(Event).count()
        renewed = session.get(SessionLease, "requester").renewed_at
    world.clock.now += timedelta(seconds=10)
    # Both an identical lost response and a different stale payload fail. A reset
    # to zero would let the latter silently replace version 2 with version 3.
    for stale, key in ((original, "replace"), (original, "stale-other"), (cancelled, "old-cancel")):
        with pytest.raises(ValueError, match="stale"):
            replacement(stale, key=key)
    assert replacement(current) == current  # current-revision identical replay
    assert peer.get_coordination(original["code"], session_id="requester") == current
    with world.factory() as session:
        assert session.query(CoordinationProposal).count() == 2
        assert session.query(Event).count() == events
        assert session.get(SessionLease, "requester").renewed_at == renewed
    for actor in ("a", "unrelated"):
        with pytest.raises(ValueError, match="unavailable"):
            replacement(current, actor=actor, key=f"borrow-{actor}")
    third = replacement(current, key="third", context="Third immutable context")
    assert third["version"] == 3 and third["revision"] == 4
    assert peer.get_coordination(original["code"], session_id="requester", version=1) == cancelled
    assert (
        peer.get_coordination(original["code"], session_id="requester", version=2)["revision"] == 3
    )


def test_concurrent_replacements_have_one_winner_and_no_revision_reuse(world):
    original = world.create()
    barrier = threading.Barrier(2)

    def run(key):
        token = resolver.current_principal.set(world.principal)
        try:
            barrier.wait()
            return replacement(original, key=key, context=key)
        except ValueError as exc:
            return str(exc)
        finally:
            resolver.current_principal.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["client-one", "client-two"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert any(isinstance(result, str) and "stale" in result for result in results)
    current = peer.get_coordination(original["code"], session_id="requester")
    assert current["version"] == 2 and current["revision"] == 2
    with world.factory() as session:
        assert session.query(CoordinationProposal).count() == 2
        assert session.query(CoordinationContribution).count() == 2


def test_replacement_failure_rolls_back_old_version_and_revision(world):
    original = world.create()

    def fail(session, flush_context, instances):
        if any(isinstance(obj, EventContext) for obj in session.new):
            raise RuntimeError("synthetic replacement failure")

    event.listen(world.factory, "before_flush", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic replacement"):
            replacement(original)
    finally:
        event.remove(world.factory, "before_flush", fail)
    assert peer.get_coordination(original["code"], session_id="requester") == original
    with world.factory() as session:
        assert session.query(CoordinationProposal).count() == 1
    assert replacement(original)["revision"] == 2


def test_replacement_never_rewrites_completed_history(world):
    row = collecting(world, discussion_rounds=0)
    row = submit(row, "a")
    row = submit(row, "b")
    row = advance(row)
    completed = submit(row, "requester", "final")
    with pytest.raises(ValueError, match="not open"):
        replacement(completed)
    assert peer.get_coordination(row["code"], session_id="requester") == completed
    with world.factory() as session:
        assert session.query(CoordinationProposal).count() == 1


def test_idempotency_append_only_noop_and_stale(world):
    seed_leases(world)
    row = world.create()
    assert world.create() == row
    with pytest.raises(ValueError, match="different request"):
        world.create(objective="Changed")
    with world.factory() as session:
        before = session.query(Event).count()
        lease = session.get(SessionLease, "requester").renewed_at
    world.clock.now += timedelta(seconds=10)
    assert world.create() == row
    assert accept(row, "requester") == row
    with world.factory() as session:
        assert session.query(Event).count() == before
        assert session.get(SessionLease, "requester").renewed_at == lease
    row = accept(row, "a")
    row = accept(row, "b")
    row = advance(row)
    stale = row
    row = submit(row, "a")
    with pytest.raises(ValueError, match="stale"):
        submit(stale, "a")
    assert submit(row, "a") == row
    with pytest.raises(ValueError, match="different contribution"):
        submit(row, "a", findings="changed")
    with pytest.raises(ValueError, match="append-only"):
        submit(row, "a", key="new-key")
    with world.factory() as session:
        assert session.query(CoordinationContribution).filter_by(kind="initial").count() == 1


def test_concurrent_accept_and_initial_cas(world):
    row = world.create()

    def race(call):
        barrier = threading.Barrier(2)

        def run(actor):
            token = resolver.current_principal.set(world.principal)
            try:
                barrier.wait()
                return call(actor)
            except ValueError as exc:
                return str(exc)
            finally:
                resolver.current_principal.reset(token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ["a", "b"]))
        assert sum(isinstance(r, dict) for r in results) == 1
        assert any(isinstance(r, str) and "stale" in r for r in results)
        return next(r for r in results if isinstance(r, dict))

    row = race(lambda actor: accept(row, actor))
    row = accept(row, row["remaining_acceptance_session_ids"][0])
    row = advance(row)
    row = race(lambda actor: submit(row, actor))
    row = submit(row, row["remaining_initial_session_ids"][0])
    assert row["counts"]["initial"] == 2


@pytest.mark.parametrize("actor", ["foreign", "elsewhere", "private", "legacy", "unrelated"])
def test_membership_denial_before_lease_side_effects(world, actor):
    row = world.create()
    with world.factory() as session:
        leases = session.query(SessionLease).count()
        events = session.query(Event).count()
    for action in (
        lambda: peer.get_coordination(row["code"], session_id=actor),
        lambda: accept(row, actor),
        lambda: submit(row, actor),
    ):
        with pytest.raises(ValueError, match="unavailable"):
            action()
    if actor == "unrelated":
        assert peer.list_coordinations(PATH, session_id=actor) == []
    with world.factory() as session:
        assert session.query(SessionLease).count() == leases
        assert session.query(Event).count() == events


@pytest.mark.parametrize("kind", ["anonymous", "runtime", "other_operator"])
def test_actual_principal_cannot_borrow_owned_handle(world, kind):
    seed_leases(world)
    row = world.create()
    with world.factory() as session:
        renewed = session.get(SessionLease, "a").renewed_at
    world.clock.now += timedelta(seconds=10)
    principal = (
        replace(world.principal, operator_id=2)
        if kind == "other_operator"
        else Principal(actor_kind=kind, actor_id=kind, credential_kind=kind)
    )
    token = resolver.current_principal.set(principal)
    try:
        with pytest.raises(ValueError, match="unavailable"):
            accept(row, "a")
    finally:
        resolver.current_principal.reset(token)
    with world.factory() as session:
        assert session.get(SessionLease, "a").renewed_at == renewed


def test_dead_peer_and_same_operator_cooperation_boundary(world):
    row = world.create()
    with world.factory() as session:
        session.get(AgentSession, "a").ended_at = world.clock.now
        session.commit()
    with pytest.raises(ValueError, match="ended"):
        accept(row, "a")
    with world.factory() as session:
        assert session.get(SessionLease, "a") is None
    # No sandbox claim: the operator really can act as a different owned peer.
    assert accept(row, "b")["accepted_session_ids"] == ["b", "requester"]


def test_two_day_interruption_new_connection_same_session_and_expiry(world, monkeypatch):
    seed_leases(world)
    row = collecting(world, deadline=(world.clock.now + timedelta(days=3)).isoformat())
    row = submit(row, "a")
    world.clock.now += timedelta(days=2)
    # Simulate an explicit harness resume of the SAME durable handles; no takeover.
    with world.factory() as session:
        for ident in ("requester", "a", "b"):
            lease = session.get(SessionLease, ident)
            lease.lease_expires_at = world.clock.now + timedelta(days=3)
        session.commit()
    new_engine = create_engine(f"sqlite:///{world.path.as_posix()}")
    try:
        monkeypatch.setattr(
            peer, "SessionLocal", sessionmaker(bind=new_engine, expire_on_commit=False)
        )
        resumed = peer.get_coordination(row["code"], session_id="a")
        assert resumed["contributions"] == row["contributions"]
        assert resumed["incomplete_flag"] and not resumed["expired_flag"]
        row = submit(resumed, "b")
        row = advance(row)
        world.clock.now += timedelta(days=2)
        with world.factory() as session:
            for ident in ("requester", "a", "b"):
                session.get(SessionLease, ident).lease_expires_at = world.clock.now + timedelta(
                    days=1
                )
            session.commit()
        before = peer.get_coordination(row["code"], session_id="a")
        assert before["expired_flag"] and before["incomplete_flag"]
        for action in (
            lambda: accept(row, "a"),
            lambda: advance(row),
            lambda: submit(row, "a", "discussion"),
        ):
            with pytest.raises(ValueError, match="deadline"):
                action()
        after = peer.get_coordination(row["code"], session_id="a")
        assert before == after
        cancelled = peer.cancel_coordination(
            row["code"],
            "Expired incomplete",
            session_id="requester",
            version=row["version"],
            expected_revision=row["revision"],
        )
        assert cancelled["status"] == "cancelled"
    finally:
        new_engine.dispose()


@pytest.mark.parametrize(
    "changes",
    [
        {"discussion_rounds": True},
        {"discussion_rounds": 4},
        {"discussion_rounds": -1},
        {"objective": ""},
        {"context": "x" * (32 * 1024)},
        {"context": "\ud800"},
        {"objective": "bad\x00"},
        {"deadline": "2030-01-01"},
        {"deadline": "2031-01-01T00:00:00Z"},
        {"deadline": "2029-01-01T00:00:00Z"},
        {"links": ["ref"] * 33},
        {"extra": "no"},
        {"result_owner_session_id": "unrelated"},
        {"participants": [{"session_id": "a", "model": None}] * 2},
        {
            "participants": [
                {"session_id": "a", "model": None},
                {"session_id": "foreign", "model": None},
            ]
        },
        {
            "participants": [
                {"session_id": "a", "model": None},
                {"session_id": "missing", "model": None},
            ]
        },
        {
            "participants": [
                {"session_id": "a", "model": float("nan")},
                {"session_id": "b", "model": None},
            ]
        },
    ],
)
def test_spec_bounds_and_existing_parties(world, changes):
    with pytest.raises(ValueError):
        world.create(**changes)
    with world.factory() as session:
        assert session.query(CoordinationProposal).count() == 0
        assert session.query(SessionLease).count() == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence": ""},
        {"findings": "x" * (64 * 1024)},
        {"dissent": float("nan")},
        {"clarifications": ["ref"] * 33},
        {"unexpected": "no"},
    ],
)
def test_payload_boundaries(world, changes):
    row = collecting(world)
    with pytest.raises(ValueError):
        submit(row, "a", **changes)
    assert peer.get_coordination(row["code"], session_id="a")["revision"] == row["revision"]


def test_transactional_event_failure_rolls_back_contribution_and_lease(world, monkeypatch):
    seed_leases(world)
    row = collecting(world)
    with world.factory() as session:
        lease = session.get(SessionLease, "a").renewed_at
    world.clock.now += timedelta(seconds=10)

    def fail(session, flush_context, instances):
        if any(isinstance(obj, EventContext) for obj in session.new):
            raise RuntimeError("synthetic event failure")

    event.listen(world.factory, "before_flush", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic event"):
            submit(row, "a")
    finally:
        event.remove(world.factory, "before_flush", fail)
    with world.factory() as session:
        assert session.get(SessionLease, "a").renewed_at == lease
        assert session.query(CoordinationContribution).filter_by(kind="initial").count() == 0
        assert (
            session.get(CoordinationProposal, (row["code"], row["version"])).revision
            == row["revision"]
        )


def test_cancellation_inert_links_preserves_blinding_and_foreign_tables(world):
    with world.factory() as session:
        session.add(
            WorkAssignment(
                code="WA-synthetic",
                workspace_id=1,
                creator_operator_id=1,
                creator_session_id="requester",
                idempotency_key="linked-work",
                request_hash="a" * 64,
                title="Inert linked assignment",
                specification_json='{"version":1,"objective":"Preserved"}',
                specification_hash="b" * 64,
                status="ready",
                revision=1,
                generation=0,
                created_at=world.clock.now,
                updated_at=world.clock.now,
            )
        )
        session.commit()
    row = collecting(
        world,
        links=[
            "WA-synthetic",
            "mailbox:synthetic",
            "https://invalid.example/evidence",
            "file:///do-not-ingest",
        ],
    )
    row = submit(row, "a")
    with sqlite3.connect(world.path) as conn:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if r[0]
            not in OWN_TABLES | {"events", "event_contexts", "session_leases", "agent_sessions"}
        ]
        before = {t: conn.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}
    with pytest.raises(ValueError, match="unavailable"):
        peer.cancel_coordination(
            row["code"], "No", session_id="a", version=1, expected_revision=row["revision"]
        )
    row = peer.cancel_coordination(
        row["code"], "Stop", session_id="requester", version=1, expected_revision=row["revision"]
    )
    assert row["contributions"] == [] and row["blinded"]
    assert (
        peer.cancel_coordination(
            row["code"],
            "Stop",
            session_id="requester",
            version=1,
            expected_revision=row["revision"],
        )
        == row
    )
    with sqlite3.connect(world.path) as conn:
        assert before == {t: conn.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}


@pytest.mark.parametrize("author", ["a", "requester"])
def test_cancel_before_initial_close_preserves_only_authors_own_findings(world, author):
    changes = {}
    if author == "requester":
        changes["participants"] = [
            {"session_id": "requester", "model": None},
            {"session_id": "b", "model": None},
        ]
        row = accept(world.create(**changes), "b")
        row = advance(row)
    else:
        row = collecting(world)
    own = submit(row, author)
    row = peer.cancel_coordination(
        own["code"],
        "Stop before disclosure",
        session_id="requester",
        version=own["version"],
        expected_revision=own["revision"],
    )
    replay = peer.cancel_coordination(
        row["code"],
        "Stop before disclosure",
        session_id="requester",
        version=row["version"],
        expected_revision=row["revision"],
    )
    assert replay == row
    actors = {author, "b", "requester"}
    for actor in actors:
        snapshots = [
            peer.get_coordination(row["code"], session_id=actor),
            *peer.list_coordinations(PATH, session_id=actor),
        ]
        if actor == "requester":
            snapshots.extend([row, replay])
        for snapshot in snapshots:
            assert snapshot["status"] == "cancelled"
            assert snapshot["blinded"] and not snapshot["initial_closed"]
            assert snapshot["counts"]["initial"] == 1
            if actor == author:
                assert snapshot["contributions"] == own["contributions"]
                assert snapshot["unresolved_dissent"] == own["unresolved_dissent"]
            else:
                assert snapshot["contributions"] == []
                assert snapshot["unresolved_dissent"] == []
                assert f"secret-{author}" not in json.dumps(snapshot)
                assert f"original objection {author}" not in json.dumps(snapshot)


def test_cancel_after_initial_close_preserves_all_findings_and_dissent(world):
    row = collecting(world)
    row = submit(row, "a")
    row = submit(row, "b")
    row = advance(row)
    row = submit(row, "a", "discussion", dissent="Discussion objection remains")
    before = peer.get_coordination(row["code"], session_id="requester")
    assert len(before["unresolved_dissent"]) == 3
    cancelled = peer.cancel_coordination(
        row["code"],
        "Stop discussion",
        session_id="requester",
        version=row["version"],
        expected_revision=row["revision"],
    )
    replay = peer.cancel_coordination(
        row["code"],
        "Stop discussion",
        session_id="requester",
        version=cancelled["version"],
        expected_revision=cancelled["revision"],
    )
    assert replay == cancelled
    for actor in ("a", "b", "requester"):
        snapshots = [
            peer.get_coordination(row["code"], session_id=actor),
            *peer.list_coordinations(PATH, session_id=actor),
        ]
        if actor == "requester":
            snapshots.extend([cancelled, replay])
        for snapshot in snapshots:
            assert snapshot["status"] == "cancelled"
            assert snapshot["initial_closed"] and not snapshot["blinded"]
            assert snapshot["contributions"] == before["contributions"]
            assert snapshot["unresolved_dissent"] == before["unresolved_dissent"]
            assert all(not dissent["resolved"] for dissent in snapshot["unresolved_dissent"])
            assert snapshot["final"] is None and snapshot["incomplete_flag"]


def test_database_constraints_enforce_slots_and_foreign_keys(world):
    row = collecting(world)
    row = submit(row, "a")
    with world.factory() as session:
        entry = session.query(CoordinationContribution).filter_by(kind="initial").one()
        values = {
            c.name: getattr(entry, c.name) for c in CoordinationContribution.__table__.columns
        }
    for changes in (
        {"contribution_id": "duplicate", "idempotency_key": "different"},
        {"contribution_id": "missing", "proposal_code": "PC-missing"},
        {"contribution_id": "bad-slot", "slot": "arbitrary", "idempotency_key": "different"},
    ):
        with world.factory() as session:
            session.add(CoordinationContribution(**(values | changes)))
            with pytest.raises(IntegrityError):
                session.commit()


def test_requester_participant_autoack_and_stored_tool_not_declared_model(world):
    row = world.create(
        participants=[
            {"session_id": "requester", "model": "unverified-model"},
            {"session_id": "b", "model": None},
        ]
    )
    assert row["revision"] == 0
    assert row["counts"]["required_acceptances"] == 2
    assert row["accepted_session_ids"] == ["requester"]
    assert row["specification"]["participants"] == [
        {"session_id": "b", "model": None, "tool": "opencode"},
        {"session_id": "requester", "model": "unverified-model", "tool": "codex"},
    ]
    with world.factory() as session:
        acknowledgement = session.query(CoordinationContribution).filter_by(kind="accept").one()
        assert json.loads(acknowledgement.payload_json) == {"spec_hash": row["spec_hash"]}
    row = accept(row, "b")
    row = advance(row)
    row = submit(row, "requester")
    assert row["counts"]["initial"] == 1


def test_canonical_creation_replay_latest_and_current_membership(world):
    row = world.create()
    reversed_peers = [{"session_id": "b", "model": "declared"}, {"session_id": "a", "model": None}]
    assert world.create(participants=reversed_peers) == row
    spec = dict(row["specification"])
    spec["participants"] = [
        {"session_id": "a", "model": None},
        {"session_id": "unrelated", "model": None},
    ]
    new = peer.propose_coordination(
        PATH,
        "Replacement",
        spec,
        session_id="requester",
        idempotency_key="replacement",
        code=row["code"],
        expected_revision=0,
    )
    assert world.create() == new
    with pytest.raises(ValueError, match="unavailable"):
        peer.get_coordination(row["code"], session_id="b")
    assert peer.list_coordinations(PATH, session_id="b") == []
    assert peer.get_coordination(row["code"], session_id="b", version=1)["status"] == "cancelled"
    with pytest.raises(ValueError, match="unavailable"):
        peer.get_coordination(row["code"], session_id="unrelated", version=1)


def test_peers_live_at_accept_and_before_start(world):
    seed_leases(world)
    row = world.create()
    row = accept(row, "a")
    row = accept(row, "b")
    with world.factory() as session:
        session.get(SessionLease, "b").lease_expires_at = world.clock.now - timedelta(seconds=1)
        session.commit()
    with pytest.raises(ValueError, match="dormant or expired"):
        accept(row, "b")
    with pytest.raises(ValueError, match="dormant or expired"):
        advance(row)
    assert peer.get_coordination(row["code"], session_id="requester")["revision"] == row["revision"]


def test_no_late_final_success(world):
    seed_leases(world)
    row = collecting(
        world, discussion_rounds=0, deadline=(world.clock.now + timedelta(minutes=1)).isoformat()
    )
    row = submit(row, "a")
    row = submit(row, "b")
    row = advance(row)
    world.clock.now += timedelta(minutes=1)
    with pytest.raises(ValueError, match="deadline"):
        submit(row, "requester", "final")
    observed = peer.get_coordination(row["code"], session_id="requester")
    assert observed["expired_flag"] and observed["incomplete_flag"]
    assert not observed["final_ready"] and observed["final"] is None


def test_eight_peers_and_four_total_contribution_rounds_bounded(world):
    with world.factory() as session:
        for ident in ("c", "d", "e", "f", "g", "h", "i"):
            session.add(
                AgentSession(id=ident, workspace_id=1, created_by_operator_id=1, tool="codex")
            )
        session.commit()
    peers = [{"session_id": ident, "model": None} for ident in "abcdefghi"]
    with pytest.raises(ValueError, match="2..8"):
        world.create(participants=peers)
    row = world.create(participants=peers[:8], discussion_rounds=3)
    assert row["counts"]["participants"] == 8
    assert row["counts"]["required_acceptances"] == 9
    for ident in "abcdefgh":
        row = accept(row, ident)
    row = advance(row)
    for ident in "abcdefgh":
        row = submit(row, ident)
    row = advance(row)
    for _ in range(3):
        for ident in "abcdefgh":
            row = submit(row, ident, "discussion")
        row = advance(row)
    assert row["counts"]["contributions"] == 32 and row["final_ready"]
    with pytest.raises(ValueError, match="cannot advance"):
        advance(row)
    row = submit(row, "requester", "final")
    assert row["counts"]["contributions"] == 33

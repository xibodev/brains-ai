"""Human work authoring, attribution, ownership and cooperative cancellation."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
import test_work_assignments as assignment_tests
from fastapi import HTTPException
from sqlalchemy import event

from brains.authz import policy, resolver
from brains.control import work_assignments as work
from brains.storage.models import (
    AgentSession,
    Event,
    EventContext,
    SessionLease,
    WorkAssignment,
    WorkAssignmentAttempt,
    Workspace,
    WorkspaceMembership,
)

# Reuse the actual fixtures without imported names shadowed by fixture arguments.
migrated_template = assignment_tests.migrated_template
world = assignment_tests.world
_race = assignment_tests._race


@pytest.fixture
def operator(world, monkeypatch):
    # Use the real policy with the same disposable database as the core.
    monkeypatch.setattr(policy, "init_db", lambda: None)
    monkeypatch.setattr(policy._db_module, "SessionLocal", world.db)
    human = replace(world.principal, channel="browser")

    def create(**kwargs):
        return work.create_operator_assignment(
            **(
                {
                    "workspace_id": 1,
                    "title": "Synthetic assignment",
                    "specification": {"objective": "Check synthetic evidence"},
                    "principal": human,
                    "idempotency_key": "human-request",
                }
                | kwargs
            )
        )

    def get(row, **kwargs):
        return work.get_operator_assignment(1, row["code"], principal=human, **kwargs)

    def cancel(row, **kwargs):
        return work.cancel_operator_assignment(
            1,
            row["code"],
            **({"principal": human, "expected_revision": row["revision"]} | kwargs),
        )

    return SimpleNamespace(principal=human, create=create, get=get, cancel=cancel)


def test_human_authoring_and_ready_cancellation_have_no_session_side_effects(
    world, operator, monkeypatch
):
    with world.db() as session:
        before = session.query(AgentSession).count()
    monkeypatch.setattr(work, "resolve_local_principal", world.forbidden)
    monkeypatch.setattr(work, "require_live_session", world.forbidden)
    monkeypatch.setattr(work, "_lock_session_lifecycle", world.forbidden)
    row = operator.create()
    assert row["creator_kind"] == "operator"
    assert row["creator_session_id"] is None
    assert row["creator_operator_id"] == operator.principal.operator_id
    assert row["permissions"] == {"can_cancel": True, "reason": None}
    assert row["attempts"] == [] and row["status"] == "ready"
    assert operator.create() == row
    assert operator.get(row) == row
    assert work.list_operator_assignments(1, principal=operator.principal) == [row]
    cancelled = operator.cancel(row)
    assert cancelled["status"] == "cancelled"
    assert cancelled["attempts"] == []
    assert cancelled["cancel_requested_by_session_id"] is None
    assert cancelled["permissions"]["can_cancel"] is False
    with pytest.raises(ValueError, match="stale"):
        operator.cancel(row)
    assert operator.cancel(cancelled) == cancelled
    with world.db() as session:
        assert session.query(AgentSession).count() == before
        assert session.query(SessionLease).count() == 0
        assert session.query(WorkAssignmentAttempt).count() == 0
        events = session.query(Event).order_by(Event.id).all()
        assert [e.kind for e in events] == ["work_assignment_created", "work_assignment_cancelled"]
        for recorded in events:
            assert recorded.session_id is None
            metadata = json.loads(recorded.metadata_json)
            assert metadata["actor_kind"] == "operator"
            assert metadata["operator_id"] == 1
            assert metadata["channel"] == "browser"
        assert {ctx.scope_source for ctx in session.query(EventContext)} == {"explicit"}
        assert {ctx.category for ctx in session.query(EventContext)} == {"task"}


def test_actual_passed_principal_and_human_channel_are_required(world, operator):
    row = operator.create()
    token = resolver.current_principal.set(operator.principal)
    try:
        for principal in (
            world.principal,
            replace(world.principal, is_bootstrap_admin=True),
            replace(operator.principal, channel="session-cookie"),
        ):
            read = work.get_operator_assignment(1, row["code"], principal=principal)
            assert read["permissions"] == {
                "can_cancel": False,
                "reason": "human_channel_required",
            }
            with pytest.raises(ValueError, match="human channel"):
                operator.create(principal=principal)
            with pytest.raises(ValueError, match="human channel"):
                operator.cancel(row, principal=principal)
        for principal in (
            None,
            resolver._PrincipalSlot(),
            SimpleNamespace(is_operator=True, operator_id=1, is_human_channel=True),
            replace(operator.principal, actor_kind="runtime"),
            replace(operator.principal, operator_id=None),
            replace(operator.principal, operator_id=999),
        ):
            with pytest.raises(ValueError, match="unavailable"):
                work.get_operator_assignment(1, row["code"], principal=principal)
            with pytest.raises(ValueError, match="unavailable"):
                operator.create(principal=principal)
    finally:
        resolver.current_principal.reset(token)


def test_private_workspace_org_and_creator_fences_even_for_admin(world, operator):
    row = operator.create()
    other = replace(operator.principal, operator_id=2, is_bootstrap_admin=True)
    assert work.list_operator_assignments(1, principal=other) == []
    with pytest.raises(ValueError, match="unavailable"):
        work.get_operator_assignment(1, row["code"], principal=other)
    with pytest.raises(ValueError, match="unavailable"):
        operator.cancel(row, principal=other)
    with pytest.raises(ValueError, match="unavailable"):
        work.get_operator_assignment(2, row["code"], principal=operator.principal)
    for workspace_id, principal in (
        (3, operator.principal),
        (999, operator.principal),
        (1, replace(operator.principal, org_roles={})),
        (1, replace(operator.principal, org_roles={999: "owner"})),
        (1, replace(operator.principal, org_roles={next(iter(world.principal.org_roles)): "bad"})),
    ):
        with pytest.raises(HTTPException) as error:
            work.list_operator_assignments(workspace_id, principal=principal)
        assert error.value.status_code == 404
        with pytest.raises(HTTPException):
            operator.create(workspace_id=workspace_id, principal=principal)
    with world.db() as session:
        session.add(WorkspaceMembership(workspace_id=3, operator_id=1, role="member"))
        session.commit()
    assert operator.create(workspace_id=3)["workspace_id"] == 3


def test_visibility_rechecked_after_policy_before_mutation(world, operator, monkeypatch):
    row = operator.create()
    original = work.require_workspace_capability

    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        with world.db() as session:
            session.get(Workspace, 1).visibility = "private"
            session.commit()
        return result

    monkeypatch.setattr(work, "require_workspace_capability", revoke)
    with pytest.raises(ValueError, match="unavailable"):
        operator.cancel(row)
    with world.db() as session:
        assert session.get(WorkAssignment, row["code"]).revision == 1
        assert session.query(Event).count() == 1


def test_human_reads_ended_creator_and_expired_peer_are_read_only(world, operator, monkeypatch):
    row = world.accept(world.create())
    with world.db() as session:
        for ident in ("creator", "worker"):
            session.get(AgentSession, ident).ended_at = world.clock.now
            session.add(
                SessionLease(
                    session_id=ident,
                    renewed_at=world.clock.now,
                    lease_expires_at=world.clock.now + timedelta(minutes=1),
                )
            )
        session.commit()
    world.clock.now += timedelta(days=2)
    monkeypatch.setattr(work, "require_live_session", world.forbidden)
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper())

    event.listen(world.engine, "before_cursor_execute", observe)
    try:
        observed = operator.get(row)
        assert work.list_operator_assignments(1, principal=operator.principal) == [observed]
    finally:
        event.remove(world.engine, "before_cursor_execute", observe)
    assert statements.count("BEGIN") == 2
    assert not {"INSERT", "UPDATE", "DELETE"} & set(statements)
    assert observed["status"] == "accepted"
    assert observed["observed_status"] == "uncertain"
    assert observed["revision"] == row["revision"]
    cancelled = operator.cancel(observed)
    assert cancelled["status"] == "cancel_requested"
    assert cancelled["observed_status"] == "uncertain"
    assert cancelled["attempts"][0]["settled_at"] is None
    with world.db() as session:
        assert {work._iso(lease.renewed_at) for lease in session.query(SessionLease)} == {
            "2030-01-01T00:00:00+00:00"
        }
        recorded = session.query(Event).order_by(Event.id.desc()).first()
        assert recorded.session_id is None
        assert json.loads(recorded.metadata_json)["actor_kind"] == "operator"


@pytest.mark.parametrize("pid", [None, 43210])
def test_expired_lease_only_marks_pidless_source_unavailable(world, operator, monkeypatch, pid):
    monkeypatch.setattr(assignment_tests.sessions, "_pid_alive", world.forbidden)
    with world.db() as session:
        session.get(AgentSession, "worker").pid = pid
        session.add(
            SessionLease(
                session_id="worker",
                renewed_at=world.clock.now,
                lease_expires_at=world.clock.now + timedelta(minutes=1),
            )
        )
        session.commit()
    row = world.accept(operator.create())
    # Acceptance may renew a PID-less lease; give both sources the same expiry.
    with world.db() as session:
        lease = session.get(SessionLease, "worker")
        lease.lease_expires_at = world.clock.now + timedelta(minutes=1)
        session.commit()
    world.clock.now += timedelta(minutes=2)
    if pid is not None:
        # The canonical acceptance guard still permits this tracked Session.
        assert world.accept(row)["status"] == "accepted"
    monkeypatch.setattr(work, "require_live_session", world.forbidden)
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper())

    event.listen(world.engine, "before_cursor_execute", observe)
    try:
        observed = operator.get(row)
        assert work.list_operator_assignments(1, principal=operator.principal) == [observed]
        attempt = observed["attempts"][0]
        assert attempt["source_session_unavailable"] is (pid is None)
        assert observed["observed_status"] == ("uncertain" if pid is None else "accepted")
        assert observed["deadline_exceeded"] is False
        assert observed["revision"] == row["revision"]
        # Deadline expiry is independent of the source's PID/lease availability.
        world.clock.now += timedelta(hours=2)
        expired = operator.get(row)
        assert expired["observed_status"] == "uncertain"
        assert expired["deadline_exceeded"] is True
        assert expired["attempts"][0]["source_session_unavailable"] is (pid is None)
    finally:
        event.remove(world.engine, "before_cursor_execute", observe)
    assert not {"INSERT", "UPDATE", "DELETE"} & set(statements)


@pytest.mark.parametrize(
    "source_state", ["missing", "ended", "dormant", "completed", "failed", "cancelled"]
)
def test_tracked_source_gone_or_ended_is_still_unavailable(
    world, operator, monkeypatch, source_state
):
    monkeypatch.setattr(assignment_tests.sessions, "_pid_alive", world.forbidden)
    with world.db() as session:
        session.get(AgentSession, "worker").pid = 43210
        session.commit()
    row = world.accept(operator.create())
    if source_state == "missing":
        # A missing historical source cannot be manufactured by deleting an FK
        # target in a healthy DB; simulate that read without disabling constraints.
        get = world.db.class_.get

        def missing_source(session, entity, ident, **kwargs):
            if entity is AgentSession and ident == "worker":
                return None
            return get(session, entity, ident, **kwargs)

        monkeypatch.setattr(world.db.class_, "get", missing_source)
    else:
        with world.db() as session:
            source = session.get(AgentSession, "worker")
            if source_state == "ended":
                source.ended_at = world.clock.now
            else:
                source.state = source_state
            session.commit()
    monkeypatch.setattr(work, "require_live_session", world.forbidden)
    observed = operator.get(row)
    assert observed["attempts"][0]["source_session_unavailable"] is True
    assert observed["observed_status"] == "uncertain"
    assert observed["deadline_exceeded"] is False
    assert observed["revision"] == row["revision"]


def test_same_operator_live_peer_acceptance_settlement_and_cancellation(world, operator):
    row = operator.create()
    for actor in ("foreign", "elsewhere"):
        with pytest.raises(ValueError, match="unavailable"):
            world.accept(row, actor=actor)
    accepted = world.accept(row)
    requested = operator.cancel(accepted)
    assert requested["creator_kind"] == "operator"
    assert requested["creator_session_id"] is None
    assert requested["status"] == "cancel_requested"
    with pytest.raises(ValueError, match="conclusive"):
        work.retry_work_assignment(row["code"], session_id="creator", expected_revision=3)
    with pytest.raises(ValueError, match="completion after"):
        world.settle(requested)
    assert world.settle(requested, outcome="cancelled")["status"] == "cancelled"
    # Preserve existing same-operator Session requester semantics and attribution.
    second = operator.create(idempotency_key="second")
    cancelled = work.cancel_work_assignment(
        second["code"], session_id="continuation", expected_revision=1
    )
    assert cancelled["creator_kind"] == "operator"
    assert cancelled["cancel_requested_by_session_id"] == "continuation"
    with world.db() as session:
        recorded = session.query(Event).order_by(Event.id.desc()).first()
        assert recorded.session_id == "continuation"
        assert json.loads(recorded.metadata_json)["actor_kind"] == "session"


def test_operator_read_keeps_revision_and_attempts_in_one_snapshot(world, operator, monkeypatch):
    row = operator.create()
    with world.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    snapshot = work._snapshot
    accepted = False

    def accept_between_assignment_and_history(session, assignment):
        nonlocal accepted
        if not accepted:
            accepted = True
            world.accept(row)
        return snapshot(session, assignment)

    monkeypatch.setattr(work, "_snapshot", accept_between_assignment_and_history)
    observed = operator.get(row)
    assert observed["revision"] == 1
    assert observed["status"] == "ready"
    assert observed["attempts"] == []
    current = operator.get(row)
    assert current["revision"] == 2
    assert current["status"] == "accepted"
    assert len(current["attempts"]) == 1


def test_uncertain_and_terminal_permissions_preserve_no_retry_fence(world, operator):
    uncertain = world.settle(world.accept(operator.create()), outcome="uncertain")
    requested = operator.cancel(uncertain)
    assert requested["status"] == requested["observed_status"] == "uncertain"
    assert requested["permissions"]["can_cancel"] is False
    assert operator.cancel(requested) == requested
    with pytest.raises(ValueError, match="conclusive"):
        work.retry_work_assignment(requested["code"], session_id="creator", expected_revision=4)
    completed = world.settle(world.accept(operator.create(idempotency_key="completed")))
    assert operator.get(completed)["permissions"] == {
        "can_cancel": False,
        "reason": "assignment_not_cancellable",
    }
    with pytest.raises(ValueError, match="cannot be cancelled"):
        operator.cancel(completed)


def test_idempotency_is_canonical_and_author_kind_fenced(world, operator):
    row = operator.create(specification={"context": "inert", "objective": "Check"})
    assert operator.create(specification={"objective": "Check", "context": "inert"}) == row
    with pytest.raises(ValueError, match="different request"):
        operator.create(specification={"objective": "changed"})
    with pytest.raises(ValueError, match="different request"):
        world.create(row["specification"], idempotency_key="human-request")
    agent = world.create()
    with pytest.raises(ValueError, match="different request"):
        operator.create(idempotency_key=agent["idempotency_key"])
    assert operator.create(workspace_id=2)["code"] != row["code"]
    foreign = operator.create(principal=replace(operator.principal, operator_id=2))
    assert foreign["code"] != row["code"]


def test_default_org_compatibility_and_database_reopen(world, operator):
    with world.db() as session:
        session.get(Workspace, 1).org_id = None
        session.commit()
    row = operator.create()
    world.engine.dispose()
    assert operator.get(row) == row
    assert work.list_operator_assignments(1, principal=operator.principal) == [row]


@pytest.mark.parametrize(
    "arguments",
    [
        {"title": ""},
        {"title": "é" * 129},
        {"idempotency_key": ""},
        {"specification": {"objective": "ok", "operator_id": 2}},
        {"specification": {"objective": "ok", "version": True}},
        {"specification": {"objective": "ok", "max_runtime_seconds": float("nan")}},
        {"specification": {"objective": "ok", "deadline": "2030-01-01"}},
        {"workspace_id": True},
        {"workspace_id": 1.0},
    ],
)
def test_invalid_inputs_leave_no_state(world, operator, arguments):
    with pytest.raises(ValueError):
        operator.create(**arguments)
    with world.db() as session:
        assert session.query(WorkAssignment).count() == session.query(Event).count() == 0


def test_limits_and_revisions_are_strict(world, operator):
    row = operator.create()
    for limit in (True, 0, 201, 1.0):
        with pytest.raises(ValueError, match="limit"):
            work.list_operator_assignments(1, principal=operator.principal, limit=limit)
    for revision in (True, 0, -1, 1.0):
        with pytest.raises(ValueError, match="expected_revision"):
            operator.cancel(row, expected_revision=revision)


def test_concurrent_human_create_and_cancel_accept_are_revision_fenced(world, operator):
    created = _race(world, [operator.create, operator.create])
    assert created[0] == created[1]
    row = created[0]
    results = _race(world, [lambda: operator.cancel(row), lambda: world.accept(row)])
    assert sum(isinstance(result, dict) for result in results) == 1
    assert any("stale" in result for result in results if isinstance(result, str))
    assert operator.get(row)["revision"] == 2
    with world.db() as session:
        assert session.query(Event).count() == 2


def test_concurrent_human_cancel_and_peer_success_are_revision_fenced(world, operator):
    row = world.accept(operator.create())
    results = _race(world, [lambda: operator.cancel(row), lambda: world.settle(row)])
    assert sum(isinstance(result, dict) for result in results) == 1
    assert any("stale" in result for result in results if isinstance(result, str))
    assert operator.get(row)["revision"] == 3


def test_concurrent_agent_and_human_creation_cannot_share_a_key(world, operator):
    results = _race(
        world,
        [lambda: operator.create(idempotency_key="request"), world.create],
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert any("different request" in result for result in results if isinstance(result, str))
    with world.db() as session:
        assert session.query(WorkAssignment).count() == 1
        assert session.query(Event).count() == 1


def test_event_failure_rolls_back_human_create_and_attempt_cancellation(
    world, operator, monkeypatch
):
    with monkeypatch.context() as patch:
        patch.setattr(work, "classify_event_kind", world.forbidden)
        with pytest.raises(AssertionError):
            operator.create()
    with world.db() as session:
        assert session.query(WorkAssignment).count() == session.query(Event).count() == 0
    row = world.accept(operator.create())
    monkeypatch.setattr(work, "classify_event_kind", world.forbidden)
    with pytest.raises(AssertionError):
        operator.cancel(row)
    observed = operator.get(row)
    assert observed["revision"] == row["revision"]
    assert observed["attempts"] == row["attempts"]
    with world.db() as session:
        assert session.query(Event).count() == 2

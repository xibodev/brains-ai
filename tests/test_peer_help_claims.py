"""Exact acceptance, actor authorization and fenced SQLite peer-help transitions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from brains.authz import policy, resolver
from brains.authz.principal import Principal
from brains.control import help as peer_help
from brains.control import help_execution, memberships, operators, session_liveness, sessions
from brains.storage import db, migrations
from brains.storage.models import (
    AgentSession,
    HelpRequest,
    HelpRequestConstraint,
    HelpRequestExecution,
    MailboxAttachment,
    Operator,
    Org,
    SessionLease,
    SessionSuccessor,
    Workspace,
    WorkspaceMembership,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'help.db').as_posix()}", connect_args={"timeout": 1}
    )
    # These are lifecycle/authz tests, not migration tests. Avoid rebuilding
    # the entire application schema for every parametrized identity case.
    tables = (
        Operator,
        Org,
        Workspace,
        WorkspaceMembership,
        AgentSession,
        SessionLease,
        SessionSuccessor,
        MailboxAttachment,
        HelpRequest,
        HelpRequestConstraint,
        HelpRequestExecution,
    )
    db.Base.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    # Patch captured aliases as well as dynamic db-module lookups. Keep the
    # real resolver, ownership, membership and liveness functions in use.
    for module in (db, migrations, peer_help, sessions, help_execution):
        monkeypatch.setattr(module, "SessionLocal", factory)
    for module in (db, migrations):
        monkeypatch.setattr(module, "engine", engine)
    for module in (
        migrations,
        peer_help,
        policy,
        resolver,
        memberships,
        operators,
        sessions,
        help_execution,
    ):
        monkeypatch.setattr(module, "init_db", lambda: None)
    clock = SimpleNamespace(now=datetime(2030, 1, 1), monotonic=0.0)

    def advance(seconds):
        clock.now += timedelta(seconds=seconds)
        clock.monotonic += seconds

    sleeps = 0

    def sleep(seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 100:
            pytest.fail("peer-help polling did not terminate under the controlled clock")
        advance(seconds)

    monkeypatch.setattr(peer_help, "utc_now", lambda: clock.now)
    monkeypatch.setattr(session_liveness, "utc_now", lambda: clock.now)
    monkeypatch.setattr(sessions, "utc_now", lambda: clock.now)
    monkeypatch.setattr(
        peer_help, "time", SimpleNamespace(monotonic=lambda: clock.monotonic, sleep=sleep)
    )
    monkeypatch.setenv("BRAINS_HELP_CLAIM_GRACE_SECONDS", "600")
    events = []
    monkeypatch.setattr(peer_help, "append_event", lambda *args, **kwargs: events.append(args))
    principal = Principal(
        actor_kind="operator",
        actor_id="operator:peer",
        credential_kind="operator",
        operator_id=1,
        org_roles={1: "member"},
    )
    token = resolver.current_principal.set(principal)
    with factory() as session:
        session.add_all([Operator(id=1, slug="peer"), Operator(id=2, slug="other")])
        session.add(Org(id=1, slug="default", name="Synthetic Org"))
        session.add_all(
            [
                Workspace(id=1, slug="source", path="/synthetic/source", org_id=1),
                Workspace(id=2, slug="peers", path="/synthetic/peers", org_id=1),
                Workspace(
                    id=3,
                    slug="private",
                    path="/synthetic/private",
                    org_id=1,
                    visibility="private",
                ),
            ]
        )
        for ident, workspace_id, tool, owner in (
            ("asker", 1, "opencode", 1),
            ("peer", 2, "claude", 1),
            ("second", 2, "codex", 1),
            ("outside", 1, "claude", 1),
            ("foreign", 2, "claude", 2),
            ("hidden", 3, "claude", 1),
            ("legacy", 2, "claude", None),
        ):
            session.add(
                AgentSession(
                    id=ident,
                    workspace_id=workspace_id,
                    tool=tool,
                    created_by_operator_id=owner,
                    started_at=clock.now,
                )
            )
        session.commit()

    def file(**kwargs):
        return peer_help.file_help_request(
            "Synthetic review",
            "Check the synthetic change",
            from_session_id="asker",
            **({"to_workspace": "peers"} | kwargs),
        )["code"]

    yield SimpleNamespace(
        db=factory, clock=clock, advance=advance, events=events, principal=principal, file=file
    )
    resolver.current_principal.reset(token)
    engine.dispose()


def test_exact_newer_and_waiter_fifo_do_not_accept_on_read(world):
    older = world.file()
    world.advance(1)
    newer = world.file()
    assert peer_help.get_help_request(newer, session_id="peer")["status"] == "open"
    assert all(row[0] != "help_claimed" for row in world.events)
    assert peer_help.claim_help_request(newer, session_id="peer")["code"] == newer
    assert peer_help.get_help_request(older)["status"] == "open"
    assert peer_help.wait_for_request(session_id="second", timeout_ms=100)["code"] == older


@pytest.mark.parametrize(
    ("target", "actor", "allowed"),
    [
        ({"to_workspace": None, "to_session_id": "peer"}, "second", False),
        ({"to_workspace": None, "to_session_id": "peer"}, "peer", True),
        ({"to_workspace": "peers"}, "second", True),
        ({"to_workspace": "peers", "to_session_id": "outside"}, "peer", True),
        ({"to_workspace": "peers", "to_session_id": "outside"}, "outside", True),
        ({"required_tool": "not:claude"}, "peer", False),
        ({"required_tool": "not:claude"}, "second", True),
        ({"required_tool": "CLAUDE"}, "peer", True),
        ({"required_tool": "codex"}, "peer", False),
    ],
)
def test_exact_routing_and_harness(world, target, actor, allowed):
    code = world.file(**target)
    if allowed:
        assert peer_help.claim_help_request(code, session_id=actor)["status"] == "claimed"
    else:
        with pytest.raises(ValueError, match="unknown or unavailable help request"):
            peer_help.claim_help_request(code, session_id=actor)
        assert peer_help.get_help_request(code)["status"] == "open"


def test_waiter_workspace_override_is_retained(world):
    code = world.file(to_workspace="override")
    with pytest.raises(ValueError, match="unavailable"):
        peer_help.claim_help_request(code, session_id="peer")
    result = peer_help.wait_for_request(
        session_id="peer", workspace_slug="override", timeout_ms=100
    )
    assert result["code"] == code


def test_duplicate_preserves_claim_deadlines_and_event_count(world):
    code = world.file()
    first = peer_help.claim_help_request(code, session_id="peer")
    world.advance(31)
    retry = peer_help.claim_help_request(code, session_id="peer")
    assert retry == first
    assert sum(event[0] == "help_claimed" for event in world.events) == 1
    with pytest.raises(ValueError, match="unavailable"):
        peer_help.claim_help_request(code, session_id="second")
    world.advance(570)
    with pytest.raises(ValueError, match="unavailable"):
        peer_help.claim_help_request(code, session_id="peer")
    assert peer_help.get_help_request(code)["status"] == "expired"


@pytest.mark.parametrize("terminal", ["cancelled", "answered", "expired", "missing"])
def test_exact_terminal_or_missing_never_takes_another_code(world, terminal):
    code = world.file()
    with world.db() as session:
        row = session.query(HelpRequest).filter_by(code=code).one()
        if terminal == "missing":
            session.delete(row)
        else:
            row.status = terminal
        session.commit()
    other = world.file()
    with pytest.raises(ValueError, match=f"unknown or unavailable help request: {code}"):
        peer_help.claim_help_request(code, session_id="peer")
    assert peer_help.get_help_request(other)["status"] == "open"


def test_request_lifetime_wait_timeout_and_claim_grace_are_distinct(world):
    code = world.file()
    row = peer_help.get_help_request(code)
    assert datetime.fromisoformat(row["expires_at"]) - world.clock.now == timedelta(seconds=30)
    waited = peer_help.wait_help_request(code, session_id="asker", timeout_ms=100)
    assert waited["wait_timed_out"] and waited["status"] == "open"
    assert peer_help.wait_for_request(session_id="outside", timeout_ms=100) is None
    assert peer_help.get_help_request(code)["status"] == "open"
    world.advance(30)
    with pytest.raises(ValueError, match="unavailable"):
        peer_help.claim_help_request(code, session_id="peer")
    code = world.file(timeout_ms=100)
    peer_help.claim_help_request(code, session_id="peer")
    world.advance(600)
    assert peer_help.get_help_request(code)["status"] == "claimed"
    world.advance(0.001)
    with pytest.raises(peer_help.HelpExpiredError):
        peer_help.answer_request(code, "late", "synthetic evidence", session_id="peer")


def _act(operation, code, actor):
    if operation == "file":
        return peer_help.file_help_request(
            "test", "test", from_session_id=actor, to_workspace="peers"
        )
    if operation == "get":
        return peer_help.get_help_request(code, session_id=actor)
    if operation == "wait":
        return peer_help.wait_for_request(session_id=actor, timeout_ms=100)
    if operation == "answer":
        return peer_help.answer_request(code, "answer", "evidence", session_id=actor)
    function = getattr(peer_help, f"{operation}_help_request")
    return function(code, session_id=actor)


@pytest.mark.parametrize(
    "operation", ["claim", "wait", "release", "answer", "cancel", "file", "get"]
)
@pytest.mark.parametrize(
    "identity", ["wrong_owner", "anonymous", "runtime", "nonoperator", "hidden"]
)
def test_actor_guard_precedes_liveness_renewal(world, operation, identity):
    code = world.file()
    actor = "hidden" if identity == "hidden" else "peer"
    with world.db() as session:
        session.add(
            SessionLease(
                session_id=actor,
                renewed_at=world.clock.now,
                lease_expires_at=world.clock.now + timedelta(seconds=60),
            )
        )
        session.commit()
    principal = {
        "wrong_owner": replace(world.principal, operator_id=2),
        "anonymous": resolver.anonymous_principal(),
        "runtime": replace(world.principal, actor_kind="runtime"),
        "nonoperator": replace(world.principal, actor_kind="unknown"),
        "hidden": world.principal,
    }[identity]
    token = resolver.current_principal.set(principal)
    try:
        with pytest.raises(ValueError, match="help session unavailable"):
            _act(operation, code, actor)
    finally:
        resolver.current_principal.reset(token)
    with world.db() as session:
        assert session.get(SessionLease, actor).lease_expires_at == (
            world.clock.now + timedelta(seconds=60)
        )
        assert session.query(HelpRequest).filter_by(code=code).one().status == "open"


def test_empty_request_slot_does_not_fall_back_to_local_admin(world):
    code = world.file()
    with resolver.principal_slot(), pytest.raises(ValueError, match="unavailable"):
        peer_help.claim_help_request(code, session_id="peer")


def test_bootstrap_legacy_null_owner_only_and_known_owner_still_checked(world):
    code = world.file()
    with pytest.raises(ValueError, match="unavailable"):
        peer_help.claim_help_request(code, session_id="legacy")
    token = resolver.current_principal.set(replace(world.principal, is_bootstrap_admin=True))
    try:
        assert peer_help.claim_help_request(code, session_id="legacy")["status"] == "claimed"
        with pytest.raises(ValueError, match="unavailable"):
            peer_help.claim_help_request(world.file(), session_id="foreign")
    finally:
        resolver.current_principal.reset(token)


@pytest.mark.parametrize("state", ["completed", "failed", "dormant", "ended", "missing"])
def test_claim_refuses_nonlive_session(world, state):
    code = world.file()
    with world.db() as session:
        agent = session.get(AgentSession, "peer")
        if state == "missing":
            session.delete(agent)
        elif state == "ended":
            agent.ended_at = world.clock.now
        else:
            agent.state = state
        session.commit()
    with pytest.raises(ValueError):
        peer_help.claim_help_request(code, session_id="peer")


def test_source_visibility_shared_by_claim_wait_get_and_answer(world):
    code = world.file()
    with world.db() as session:
        session.query(HelpRequest).filter_by(code=code).update({"from_workspace_id": 3})
        session.commit()
    assert peer_help.get_help_request(code, session_id="peer") is None
    with pytest.raises(ValueError, match="unavailable"):
        peer_help.claim_help_request(code, session_id="peer")
    assert peer_help.wait_for_request(session_id="peer", timeout_ms=100) is None
    with world.db() as session:
        session.query(HelpRequest).filter_by(code=code).update(
            {"status": "claimed", "claimed_by_session_id": "peer", "claimed_at": world.clock.now}
        )
        session.commit()
    with pytest.raises(ValueError, match="unavailable"):
        peer_help.answer_request(code, "answer", "evidence", session_id="peer")


def _interleave_snapshot(monkeypatch, winner):
    original = peer_help._request_snapshot
    fired = False

    def snapshot(session, row):
        nonlocal fired
        query = original(session, row)
        if not fired:
            fired = True
            # Release the read transaction, retain its captured CAS predicates,
            # and commit the competitor through a separate real SQLite Session.
            session.rollback()
            winner()
        return query

    monkeypatch.setattr(peer_help, "_request_snapshot", snapshot)


@pytest.mark.parametrize("winner", ["other_peer", "same_peer", "cancel"])
def test_competing_exact_claims_and_cancel_have_one_winner(world, monkeypatch, winner):
    code = world.file()

    def compete():
        if winner == "cancel":
            peer_help.cancel_help_request(code, session_id="asker")
        else:
            peer_help.claim_help_request(
                code, session_id="peer" if winner == "same_peer" else "second"
            )

    _interleave_snapshot(monkeypatch, compete)
    if winner == "same_peer":
        assert peer_help.claim_help_request(code, session_id="peer")["status"] == "claimed"
    else:
        with pytest.raises(ValueError, match="unavailable"):
            peer_help.claim_help_request(code, session_id="peer")
    row = peer_help.get_help_request(code)
    assert row["status"] == ("cancelled" if winner == "cancel" else "claimed")
    assert sum(event[0] == "help_claimed" for event in world.events) == (winner != "cancel")


@pytest.mark.parametrize("lane", ["queue", "exact"])
@pytest.mark.parametrize("older_kind", ["eligible", "hidden", "harness", "expired"])
def test_locked_queue_reselects_oldest_current_eligible_request(
    world, monkeypatch, lane, older_kind
):
    older = world.file()
    peer_help.claim_help_request(older, session_id="second")
    world.advance(1)
    newer = world.file()

    def release_older():
        peer_help.release_help_request(older, session_id="second")
        with world.db() as session:
            row = session.query(HelpRequest).filter_by(code=older).one()
            if older_kind == "hidden":
                row.from_workspace_id = 3
            elif older_kind == "harness":
                session.add(HelpRequestConstraint(request_code=older, required_tool="codex"))
            elif older_kind == "expired":
                row.expires_at = world.clock.now - timedelta(seconds=1)
            session.commit()

    _interleave_snapshot(monkeypatch, release_older)
    result = (
        peer_help.wait_for_request(session_id="peer", timeout_ms=100)
        if lane == "queue"
        else peer_help.claim_help_request(newer, session_id="peer")
    )
    expected = older if lane == "queue" and older_kind == "eligible" else newer
    assert result["code"] == expected
    assert result["required_tool"] is None
    with world.db() as session:
        untouched = (
            session.query(HelpRequest).filter_by(code=newer if expected == older else older).one()
        )
        assert untouched.status == "open"
    assert sum(row[0] == "help_claimed" for row in world.events) == 2


def test_queue_reselection_preserves_original_candidate_snapshot(world, monkeypatch):
    code = world.file()

    def reclaim():
        peer_help.claim_help_request(code, session_id="second")
        peer_help.release_help_request(code, session_id="second", retry_timeout_ms=60_000)

    _interleave_snapshot(monkeypatch, reclaim)
    assert peer_help._claim_request(session_id="peer") is None
    assert peer_help.get_help_request(code)["status"] == "open"
    assert sum(row[0] == "help_claimed" for row in world.events) == 1


def test_queue_reselects_after_original_candidate_is_claimed(world, monkeypatch):
    older = world.file()
    peer_help.claim_help_request(older, session_id="second")
    world.advance(1)
    newer = world.file()

    def replace_candidate():
        peer_help.release_help_request(older, session_id="second")
        peer_help.claim_help_request(newer, session_id="second")

    _interleave_snapshot(monkeypatch, replace_candidate)
    assert peer_help.wait_for_request(session_id="peer", timeout_ms=100)["code"] == older
    assert peer_help.get_help_request(newer)["claimed_by_session_id"] == "second"


def test_queue_reselection_checks_current_private_source_membership(world, monkeypatch):
    older = world.file(required_tool="not:opencode")
    peer_help.claim_help_request(older, session_id="second")
    world.advance(1)
    newer = world.file()

    def release_and_grant():
        peer_help.release_help_request(older, session_id="second")
        with world.db() as session:
            session.query(HelpRequest).filter_by(code=older).update({"from_workspace_id": 3})
            session.add(WorkspaceMembership(workspace_id=3, operator_id=1, role="member"))
            session.commit()

    _interleave_snapshot(monkeypatch, release_and_grant)
    result = peer_help.wait_for_request(session_id="peer", timeout_ms=100)
    assert result["code"] == older
    assert result["required_tool"] == "not:opencode"
    assert result["from_workspace_id"] == 3
    assert peer_help.get_help_request(newer)["status"] == "open"


@pytest.mark.parametrize(
    "winner", ["cancel", "release", "expiry", "same_owner_reclaim", "other_owner_reclaim"]
)
def test_answer_snapshot_cannot_overwrite_winning_transition(world, monkeypatch, winner):
    code = world.file()
    initial = peer_help.claim_help_request(code, session_id="peer")

    def compete():
        if winner == "cancel":
            peer_help.cancel_help_request(code, session_id="asker")
        elif winner == "expiry":
            world.advance(601)
            assert peer_help.get_help_request(code)["status"] == "expired"
        else:
            peer_help.release_help_request(code, session_id="peer")
            if winner.endswith("reclaim"):
                world.advance(1)
                actor = "peer" if winner == "same_owner_reclaim" else "second"
                peer_help.claim_help_request(code, session_id=actor)

    _interleave_snapshot(monkeypatch, compete)
    with pytest.raises(ValueError):
        peer_help.answer_request(code, "stale", "synthetic evidence", session_id="peer")
    row = peer_help.get_help_request(code)
    assert row["answer"] is None
    assert (
        row["status"]
        == {
            "cancel": "cancelled",
            "release": "open",
            "expiry": "expired",
            "same_owner_reclaim": "claimed",
            "other_owner_reclaim": "claimed",
        }[winner]
    )
    if winner.endswith("reclaim"):
        assert row["claimed_at"] != initial["claimed_at"]
    assert all(event[0] != "help_answered" for event in world.events)


@pytest.mark.parametrize("loser", ["cancel", "release"])
def test_answer_winner_cannot_be_cancelled_or_released(world, monkeypatch, loser):
    code = world.file()
    peer_help.claim_help_request(code, session_id="peer")
    _interleave_snapshot(
        monkeypatch,
        lambda: peer_help.answer_request(code, "winner", "evidence", session_id="peer"),
    )
    with pytest.raises(ValueError, match="changed state"):
        _act(loser, code, "asker" if loser == "cancel" else "peer")
    assert peer_help.get_help_request(code)["answer"] == "winner"


def test_answer_checks_grace_again_at_update_without_expiry_worker(world, monkeypatch):
    code = world.file()
    peer_help.claim_help_request(code, session_id="peer")
    _interleave_snapshot(monkeypatch, lambda: world.advance(601))
    with pytest.raises(peer_help.HelpExpiredError):
        peer_help.answer_request(code, "late", "evidence", session_id="peer")
    assert peer_help.get_help_request(code)["status"] == "expired"


def test_retained_live_review_grace_does_not_launch_execution(world):
    code = world.file()
    peer_help.claim_help_request(code, session_id="peer")
    with world.db() as session:
        session.add(
            HelpRequestExecution(
                request_code=code,
                mode="ephemeral",
                source_workspace_id=2,
                required_tool="claude",
                status="running",
                review_session_id="peer",
                launch_after=world.clock.now,
                lease_expires_at=world.clock.now + timedelta(seconds=700),
            )
        )
        session.commit()
    world.advance(601)
    result = peer_help.answer_request(code, "review", "evidence", session_id="peer")
    assert result["status"] == "answered"


def test_new_submission_after_same_owner_reclaim_has_no_client_generation_token(world):
    # The schema/API fence an in-flight snapshot, not a newly submitted stale
    # payload after the same owner reclaims: no caller generation token exists.
    code = world.file()
    peer_help.claim_help_request(code, session_id="peer")
    peer_help.release_help_request(code, session_id="peer")
    world.advance(1)
    peer_help.claim_help_request(code, session_id="peer")
    result = peer_help.answer_request(code, "new call", "evidence", session_id="peer")
    assert result["status"] == "answered"


@pytest.mark.parametrize("loser", ["claim", "cancel", "release"])
def test_due_deadline_checked_again_at_transition(world, monkeypatch, loser):
    code = world.file()
    if loser == "release":
        peer_help.claim_help_request(code, session_id="peer")
    _interleave_snapshot(monkeypatch, lambda: world.advance(601))
    with pytest.raises(ValueError):
        _act(loser, code, "asker" if loser == "cancel" else "peer")
    assert peer_help.get_help_request(code)["status"] == "expired"


def test_release_snapshot_does_not_release_same_owner_reclaim(world, monkeypatch):
    code = world.file()
    first = peer_help.claim_help_request(code, session_id="peer")

    def reclaim():
        peer_help.release_help_request(code, session_id="peer")
        world.advance(1)
        peer_help.claim_help_request(code, session_id="peer")

    _interleave_snapshot(monkeypatch, reclaim)
    with pytest.raises(ValueError, match="changed state"):
        peer_help.release_help_request(code, session_id="peer")
    row = peer_help.get_help_request(code)
    assert row["status"] == "claimed" and row["claimed_at"] != first["claimed_at"]


def test_release_retry_deadline_starts_after_writer_lock(world, monkeypatch):
    code = world.file()
    peer_help.claim_help_request(code, session_id="peer")
    original = sessions._lock_session_lifecycle

    def delayed_lock(session, session_id):
        world.advance(5)
        return original(session, session_id)

    monkeypatch.setattr(sessions, "_lock_session_lifecycle", delayed_lock)
    result = peer_help.release_help_request(code, session_id="peer", retry_timeout_ms=100)
    assert result["status"] == "open"
    assert datetime.fromisoformat(result["expires_at"]) == (
        world.clock.now + timedelta(milliseconds=100)
    )
    assert peer_help.get_help_request(code)["status"] == "open"


@pytest.mark.parametrize("lease_seconds", [60, 120, 3600])
def test_idle_waiter_bounds_lease_writes_and_keeps_long_wait_live(
    world, monkeypatch, lease_seconds
):
    monkeypatch.setenv("BRAINS_SESSION_LEASE_SECONDS", str(lease_seconds))
    monkeypatch.setenv("BRAINS_HELP_POLL_INTERVAL_MS", "2000")
    with world.db() as session:
        session.add(
            SessionLease(
                session_id="peer",
                renewed_at=world.clock.now - timedelta(seconds=1),
                lease_expires_at=world.clock.now + timedelta(seconds=1),
            )
        )
        session.commit()
    renewals = []
    writes = []
    commits = []
    original = session_liveness.renew_session_lease

    def renew(session, agent, **kwargs):
        renewals.append(world.clock.monotonic)
        return original(session, agent, **kwargs)

    def statement(_conn, _cursor, sql, _parameters, _context, _executemany):
        if sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            writes.append(world.clock.monotonic)

    def committed(_session):
        commits.append(world.clock.monotonic)

    monkeypatch.setattr(session_liveness, "renew_session_lease", renew)
    engine = world.db.kw["bind"]
    event.listen(engine, "before_cursor_execute", statement)
    event.listen(world.db.class_, "after_commit", committed)
    try:
        assert peer_help.wait_for_request(session_id="peer", timeout_ms=130_000) is None
    finally:
        event.remove(engine, "before_cursor_execute", statement)
        event.remove(world.db.class_, "after_commit", committed)
    assert renewals == [0, 30, 60, 90, 120]
    assert commits == renewals
    assert writes and set(writes) == set(renewals)
    with world.db() as session:
        lease = session.get(SessionLease, "peer")
        assert session_liveness.lease_is_current(lease)
        assert lease.lease_expires_at == lease.renewed_at + timedelta(seconds=lease_seconds)


def test_repeated_idle_polls_are_read_only_between_renewals(world, monkeypatch):
    monkeypatch.setenv("BRAINS_HELP_POLL_INTERVAL_MS", "200")
    checks = []
    original = sessions.require_live_session

    def checked(session, session_id, **kwargs):
        checks.append(kwargs["renew_lease"])
        return original(session, session_id, **kwargs)

    monkeypatch.setattr(sessions, "require_live_session", checked)
    assert peer_help.wait_for_request(session_id="peer", timeout_ms=1000) is None
    assert checks == [True, False, False, False, False, False]


@pytest.mark.parametrize("change", ["ended", "expired", "mailbox_expired"])
def test_idle_waiter_checks_liveness_before_next_renewal(world, monkeypatch, change):
    monkeypatch.setenv("BRAINS_SESSION_LEASE_SECONDS", "60")
    monkeypatch.setenv("BRAINS_HELP_POLL_INTERVAL_MS", "200")
    with world.db() as session:
        session.add(
            SessionLease(
                session_id="peer",
                renewed_at=world.clock.now,
                lease_expires_at=world.clock.now + timedelta(milliseconds=100),
            )
        )
        if change == "mailbox_expired":
            session.add(
                MailboxAttachment(
                    session_id="peer", mailbox_id=1, active_slot=1, attached_at=world.clock.now
                )
            )
        session.commit()
    sleeps = []

    def invalidate(seconds):
        sleeps.append(seconds)
        assert len(sleeps) == 1
        world.advance(seconds)
        with world.db() as session:
            if change == "ended":
                session.get(AgentSession, "peer").ended_at = world.clock.now
            elif change == "expired":
                session.get(SessionLease, "peer").lease_expires_at = world.clock.now - timedelta(
                    seconds=1
                )
            session.commit()

    monkeypatch.setattr(peer_help.time, "sleep", invalidate)
    with pytest.raises(ValueError, match="ended" if change == "ended" else "expired"):
        peer_help.wait_for_request(session_id="peer", timeout_ms=30_000)
    assert len(sleeps) == 1


def test_nonrenewing_poll_retains_final_locked_actor_guard(world, monkeypatch):
    code = world.file(to_workspace="elsewhere")

    def arrive(seconds):
        world.advance(seconds)
        with world.db() as session:
            session.query(HelpRequest).filter_by(code=code).update({"to_workspace": "peers"})
            session.commit()

    def end_before_lock():
        with world.db() as session:
            session.get(AgentSession, "peer").ended_at = world.clock.now
            session.commit()

    monkeypatch.setattr(peer_help.time, "sleep", arrive)
    _interleave_snapshot(monkeypatch, end_before_lock)
    with pytest.raises(ValueError, match="ended"):
        peer_help.wait_for_request(session_id="peer", timeout_ms=1000)
    assert peer_help.get_help_request(code)["status"] == "open"
    assert all(row[0] != "help_claimed" for row in world.events)


def test_waiter_rechecks_liveness_between_polls_without_holding_writer_lock(world, monkeypatch):
    def end_while_waiting(_seconds):
        with world.db() as session:
            session.get(AgentSession, "peer").ended_at = world.clock.now
            session.commit()

    monkeypatch.setattr(peer_help.time, "sleep", end_while_waiting)
    with pytest.raises(ValueError, match="ended"):
        peer_help.wait_for_request(session_id="peer", timeout_ms=100)


def test_help_reads_and_claim_do_not_schedule_execution(world, monkeypatch):
    monkeypatch.setattr(
        "brains.control.help_execution.schedule_help_review",
        lambda _code: pytest.fail("help must not launch a worker"),
    )
    code = world.file()
    peer_help.list_open_help_requests(to_workspace="peers")
    peer_help.get_help_request(code, session_id="peer")
    peer_help.wait_help_request(code, session_id="peer", timeout_ms=100)
    assert peer_help.get_help_request(code)["status"] == "open"
    assert all(event[0] != "help_claimed" for event in world.events)
    assert peer_help.claim_help_request(code, session_id="peer")["status"] == "claimed"


def test_claim_winner_fences_cancel_open_snapshot(world, monkeypatch):
    code = world.file()
    _interleave_snapshot(monkeypatch, lambda: peer_help.claim_help_request(code, session_id="peer"))
    with pytest.raises(ValueError, match="changed state"):
        peer_help.cancel_help_request(code, session_id="asker")
    assert peer_help.get_help_request(code)["status"] == "claimed"
    assert all(event[0] != "help_cancelled" for event in world.events)


def test_peer_help_does_not_require_unrelated_org_write(world, monkeypatch):
    original = Principal.has_capability
    monkeypatch.setattr(
        Principal,
        "has_capability",
        lambda self, capability, org_id: (
            capability != "org.write" and original(self, capability, org_id)
        ),
    )
    code = world.file()
    peer_help.claim_help_request(code, session_id="peer")
    result = peer_help.answer_request(code, "answer", "evidence", session_id="peer")
    assert result["status"] == "answered"


def test_empty_harness_cannot_claim_a_constrained_request(world):
    with world.db() as session:
        session.get(AgentSession, "peer").tool = ""
        session.commit()
    constrained = world.file(required_tool="not:codex")
    with pytest.raises(ValueError, match="unavailable"):
        peer_help.claim_help_request(constrained, session_id="peer")
    assert peer_help.claim_help_request(world.file(), session_id="peer")["status"] == "claimed"


def test_blocking_ask_deadline_cannot_overwrite_answer(world, monkeypatch):
    original = peer_help._expire_due
    polls = 0

    def expire(session):
        nonlocal polls
        polls += 1
        if polls == 2:
            with world.db() as reader:
                code = reader.query(HelpRequest.code).scalar()
            # The peer arrives at the inclusive original deadline, just before
            # the blocking compatibility wrapper attempts its expiry update.
            peer_help.claim_help_request(code, session_id="peer")
            peer_help.answer_request(code, "winner", "evidence", session_id="peer")
        return original(session)

    monkeypatch.setenv("BRAINS_HELP_POLL_INTERVAL_MS", "200")
    monkeypatch.setattr(peer_help, "_expire_due", expire)
    result = peer_help.ask_peer(
        "test", "test", from_session_id="asker", to_workspace="peers", timeout_ms=200
    )
    assert result["status"] == "answered" and result["answer"] == "winner"


@pytest.mark.parametrize("operation", ["claim", "wait", "answer", "release", "cancel"])
@pytest.mark.parametrize("change", ["ended", "superseded", "lease_expired", "owner", "deleted"])
def test_actor_change_before_transition_refuses_stale_authority(
    world, monkeypatch, operation, change
):
    code = world.file()
    if operation in {"answer", "release"}:
        peer_help.claim_help_request(code, session_id="peer")
    actor = "asker" if operation == "cancel" else "peer"
    before = peer_help.get_help_request(code)
    events_before = list(world.events)

    def invalidate():
        with world.db() as session:
            agent = sessions._lock_session_lifecycle(session, actor)
            if change == "ended":
                agent.ended_at = world.clock.now
                agent.state = "completed"
            elif change == "superseded":
                agent.state = "dormant"
                session.add(
                    SessionSuccessor(
                        predecessor_session_id=actor,
                        successor_session_id="second",
                        linked_at=world.clock.now,
                    )
                )
            elif change == "lease_expired":
                session.add(
                    SessionLease(
                        session_id=actor,
                        renewed_at=world.clock.now - timedelta(seconds=60),
                        lease_expires_at=world.clock.now - timedelta(seconds=1),
                    )
                )
            elif change == "owner":
                agent.created_by_operator_id = 2
            else:
                session.delete(agent)
            session.commit()

    _interleave_snapshot(monkeypatch, invalidate)
    with pytest.raises(ValueError):
        _act(operation, code, actor)
    assert peer_help.get_help_request(code) == before
    assert world.events == events_before


@pytest.mark.parametrize("operation", ["claim", "wait", "answer", "release", "cancel"])
@pytest.mark.parametrize("scope", ["actor", "source", "membership"])
def test_visibility_revoked_before_transition_is_rechecked(world, monkeypatch, operation, scope):
    code = world.file()
    if operation in {"answer", "release"}:
        peer_help.claim_help_request(code, session_id="peer")
    actor = "asker" if operation == "cancel" else "peer"
    workspace_id = 1 if scope == "source" or actor == "asker" else 2
    if scope == "membership":
        with world.db() as session:
            session.get(Workspace, workspace_id).visibility = "private"
            session.add(
                WorkspaceMembership(workspace_id=workspace_id, operator_id=1, role="member")
            )
            session.commit()
    before = peer_help.get_help_request(code)
    events_before = list(world.events)

    def revoke():
        with world.db() as session:
            if scope == "membership":
                session.query(WorkspaceMembership).filter_by(
                    workspace_id=workspace_id, operator_id=1
                ).delete()
            else:
                session.get(Workspace, workspace_id).visibility = "private"
            session.commit()

    _interleave_snapshot(monkeypatch, revoke)
    with pytest.raises(ValueError, match="unavailable"):
        _act(operation, code, actor)
    with world.db() as session:
        row = session.query(HelpRequest).filter_by(code=code).one()
        assert row.status == before["status"]
        assert row.answer is None
    assert world.events == events_before


@pytest.mark.parametrize("change", ["workspace", "harness"])
def test_claim_eligibility_rechecked_under_lifecycle_lock(world, monkeypatch, change):
    code = world.file(required_tool="claude")

    def move():
        with world.db() as session:
            agent = sessions._lock_session_lifecycle(session, "peer")
            if change == "workspace":
                agent.workspace_id = 1
            else:
                agent.tool = "codex"
            session.commit()

    _interleave_snapshot(monkeypatch, move)
    with pytest.raises(ValueError, match="unavailable"):
        peer_help.claim_help_request(code, session_id="peer")
    assert peer_help.get_help_request(code)["status"] == "open"


@pytest.mark.parametrize("operation", ["claim", "answer", "release", "cancel"])
def test_actor_validation_and_transition_share_sqlite_writer_lock(world, monkeypatch, operation):
    code = world.file()
    if operation in {"answer", "release"}:
        peer_help.claim_help_request(code, session_id="peer")
    actor = "asker" if operation == "cancel" else "peer"
    original = sessions.require_live_session
    checks = 0

    def checked(session, session_id, **kwargs):
        nonlocal checks
        result = original(session, session_id, **kwargs)
        checks += 1
        if checks == 2:
            # No sleeps: the competing connection must be refused immediately
            # after final actor validation, while the help CAS is still pending.
            with world.db() as contender:
                contender.connection().exec_driver_sql("PRAGMA busy_timeout=0")
                with pytest.raises(OperationalError, match="locked"):
                    contender.query(AgentSession).filter_by(id=actor).update(
                        {"state": "completed"}, synchronize_session=False
                    )
                contender.rollback()
        return result

    monkeypatch.setattr(sessions, "require_live_session", checked)
    result = _act(operation, code, actor)
    assert checks == 2
    assert (
        result["status"]
        == {
            "claim": "claimed",
            "answer": "answered",
            "release": "open",
            "cancel": "cancelled",
        }[operation]
    )

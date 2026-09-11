"""Operator observers and human proposal controls, using real isolated migrations."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
import test_peer_coordination as peer_tests
from fastapi import HTTPException
from sqlalchemy import event
from test_peer_coordination import (
    PATH,
    accept,
    replacement,
    seed_leases,
    submit,
)

from brains.authz import policy, resolver
from brains.authz.principal import Principal
from brains.control import coordination as peer
from brains.storage.models import (
    AgentSession,
    CoordinationContribution,
    CoordinationProposal,
    Event,
    EventContext,
    SessionLease,
    Workspace,
    WorkspaceMembership,
)

world = peer_tests.world
migrated_template = peer_tests.migrated_template


@pytest.fixture
def operator(world, monkeypatch):
    monkeypatch.setattr(policy, "init_db", lambda: None)
    monkeypatch.setattr(policy._db_module, "SessionLocal", world.factory)
    human = replace(world.principal, channel="browser")
    specification = {
        "objective": "Synthetic objective",
        "context": "Selected snapshot",
        "evidence_expectations": "Cite synthetic probe",
        "participants": [
            {"session_id": "a", "model": None},
            {"session_id": "b", "model": "declared"},
        ],
        "result_owner_session_id": "requester",
        "discussion_rounds": 0,
    }

    def create(**kwargs):
        return peer.propose_operator_coordination(
            **(
                {
                    "workspace_id": 1,
                    "title": "Synthetic proposal",
                    "specification": specification,
                    "principal": human,
                    "idempotency_key": "create",
                }
                | kwargs
            )
        )

    def get(row, **kwargs):
        return peer.get_operator_coordination(1, row["code"], **({"principal": human} | kwargs))

    def advance(row, **kwargs):
        return peer.advance_operator_coordination(
            1,
            row["code"],
            **(
                {
                    "principal": human,
                    "version": row["version"],
                    "expected_revision": row["revision"],
                }
                | kwargs
            ),
        )

    def cancel(row, **kwargs):
        return peer.cancel_operator_coordination(
            1,
            row["code"],
            **(
                {
                    "reason": "Stop synthetic work",
                    "principal": human,
                    "version": row["version"],
                    "expected_revision": row["revision"],
                }
                | kwargs
            ),
        )

    def collecting(**kwargs):
        row = create(**kwargs)
        for ident in row["required_acceptance_session_ids"]:
            row = accept(row, ident)
        return advance(row)

    return SimpleNamespace(
        principal=human,
        specification=specification,
        create=create,
        get=get,
        advance=advance,
        cancel=cancel,
        collecting=collecting,
    )


def session_state(world):
    with sqlite3.connect(world.path) as conn:
        return {
            table: conn.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
            for table in ("agent_sessions", "session_leases", "work_assignments")
        }


def assert_blinded(row, initial):
    assert row["blinded"] and not row["initial_closed"]
    assert row["counts"]["initial"] == initial
    assert row["counts"]["visible_contributions"] == 0
    assert row["contributions"] == []
    assert row["unresolved_dissent"] == []
    assert row["final"] is None
    assert "secret-" not in json.dumps(row)
    assert "original objection" not in json.dumps(row)


def test_human_creation_is_agreement_not_peer_ack_and_has_no_session_side_effects(
    world, operator, monkeypatch
):
    seed_leases(world)
    before = session_state(world)

    def forbidden(*args, **kwargs):
        raise AssertionError("operator path must not resolve or borrow a Session identity")

    monkeypatch.setattr(peer, "resolve_local_principal", forbidden)
    monkeypatch.setattr(peer, "_lock_session_lifecycle", forbidden)
    original_live = peer.require_live_session

    def no_renew(*args, **kwargs):
        assert kwargs["renew_lease"] is False
        return original_live(*args, **kwargs)

    monkeypatch.setattr(peer, "require_live_session", no_renew)
    row = operator.create()
    assert row["creator_kind"] == "operator"
    assert row["requester_session_id"] is None and row["creator_session_id"] is None
    assert row["result_owner_session_id"] == "requester"
    assert row["creator_operator_id"] == operator.principal.operator_id
    assert row["required_acceptance_session_ids"] == ["a", "b", "requester"]
    assert row["accepted_session_ids"] == [] and row["counts"]["acceptances"] == 0
    assert row["status"] == "planned" and row["revision"] == 0
    assert row["permissions"] == {
        "can_advance": False,
        "advance_blocked_reason": "acceptances_required",
        "can_cancel": True,
        "cancel_blocked_reason": None,
    }
    assert row["specification"]["participants"] == [
        {"session_id": "a", "model": None, "tool": "codex"},
        {"session_id": "b", "model": "declared", "tool": "opencode"},
    ]
    world.clock.now += timedelta(seconds=10)
    assert operator.create() == row
    assert operator.get(row) == row
    assert peer.list_operator_coordinations(1, principal=operator.principal) == [row]
    assert operator.cancel(row)["status"] == "cancelled"
    assert session_state(world) == before
    with world.factory() as session:
        assert session.query(CoordinationContribution).count() == 0
        events = session.query(Event).filter(Event.kind.like("coordination_%")).all()
        assert [e.kind for e in events] == ["coordination_proposed", "coordination_cancelled"]
        for recorded in events:
            assert recorded.session_id is None
            metadata = json.loads(recorded.metadata_json)
            assert metadata["actor_kind"] == "operator"
            assert metadata["operator_id"] == 1 and metadata["channel"] == "browser"
            assert "Synthetic" not in recorded.message + recorded.metadata_json
            context = session.get(EventContext, recorded.id)
            assert context.category == "coordination" and context.scope_source == "explicit"


def test_human_origin_requires_exact_hash_from_all_agents_then_explicit_closure(world, operator):
    row = operator.create()
    with pytest.raises(ValueError, match="spec_hash"):
        peer.accept_coordination(
            row["code"], session_id="a", version=1, expected_revision=0, spec_hash="wrong"
        )
    row = accept(row, "a")
    row = accept(row, "b")
    assert row["status"] == "planned" and row["remaining_acceptance_session_ids"] == ["requester"]
    with pytest.raises(ValueError, match="all acceptances"):
        operator.advance(row)
    row = accept(row, "requester")
    assert row["status"] == "accepted"
    for actor in ("a", "requester"):
        with pytest.raises(ValueError, match="unavailable"):
            peer.advance_coordination(
                row["code"], session_id=actor, version=1, expected_revision=row["revision"]
            )
        with pytest.raises(ValueError, match="unavailable"):
            peer.cancel_coordination(
                row["code"], "No", session_id=actor, version=1, expected_revision=row["revision"]
            )
    before = session_state(world)
    row = operator.advance(row)
    assert session_state(world) == before
    row = submit(row, "a")
    assert_blinded(operator.get(row), 1)
    assert (
        operator.get(row)["permissions"]["advance_blocked_reason"]
        == "initial_contributions_required"
    )
    with pytest.raises(ValueError, match="all initial"):
        operator.advance(row)
    row = submit(row, "b")
    assert_blinded(operator.get(row), 2)
    assert operator.get(row)["permissions"]["can_advance"]
    before = session_state(world)
    row = operator.advance(row)
    assert session_state(world) == before
    assert row["initial_closed"] and len(row["contributions"]) == 2
    assert len(row["unresolved_dissent"]) == 2
    assert row["permissions"]["advance_blocked_reason"] == "final_synthesis_required"
    with pytest.raises(ValueError, match="unavailable"):
        submit(row, "a", "final")
    row = submit(row, "requester", "final")
    observed = operator.get(row)
    assert observed["status"] == "completed" and observed["final"] == row["final"]
    assert len(observed["unresolved_dissent"]) == 2
    assert not observed["permissions"]["can_cancel"]
    with pytest.raises(ValueError, match="cannot be cancelled"):
        operator.cancel(observed)


@pytest.mark.parametrize("closed", [False, True])
def test_operator_cancel_and_replays_all_read_paths_preserve_blinding(world, operator, closed):
    row = operator.collecting()
    row = submit(row, "a")
    if closed:
        row = submit(row, "b")
        row = operator.advance(row)
    before = operator.get(row)
    cancelled = operator.cancel(row)
    with pytest.raises(ValueError, match="stale"):
        operator.cancel(row)
    state = session_state(world)
    for snapshot in (
        cancelled,
        operator.cancel(cancelled),
        operator.create(),
        operator.get(cancelled),
        operator.get(cancelled, version=1),
        *peer.list_operator_coordinations(1, principal=operator.principal),
    ):
        assert snapshot["status"] == "cancelled"
        assert snapshot["contributions"] == before["contributions"]
        assert snapshot["unresolved_dissent"] == before["unresolved_dissent"]
        if closed:
            assert not snapshot["blinded"] and len(snapshot["unresolved_dissent"]) == 2
        else:
            assert_blinded(snapshot, 1)
    assert session_state(world) == state


def test_operator_expiry_is_observation_and_cancel_does_not_disclose(world, operator):
    row = submit(operator.collecting(), "a")
    world.clock.now += timedelta(hours=2)
    before = session_state(world)
    observed = operator.get(row)
    assert observed["status"] == "collecting" and observed["expired_flag"]
    assert observed["revision"] == row["revision"]
    assert observed["permissions"]["advance_blocked_reason"] == "deadline_exceeded"
    assert observed["permissions"]["can_cancel"]
    assert_blinded(observed, 1)
    with pytest.raises(ValueError, match="deadline"):
        operator.advance(row)
    assert_blinded(operator.cancel(row), 1)
    assert session_state(world) == before


@pytest.mark.parametrize("channel", ["api", "session-cookie", "unknown"])
def test_api_operator_reads_but_cannot_mutate_even_with_ambient_human(world, operator, channel):
    row = operator.create()
    principal = replace(operator.principal, channel=channel, is_bootstrap_admin=True)
    token = resolver.current_principal.set(operator.principal)
    try:
        read = operator.get(row, principal=principal)
        assert read["permissions"] == {
            "can_advance": False,
            "advance_blocked_reason": "human_channel_required",
            "can_cancel": False,
            "cancel_blocked_reason": "human_channel_required",
        }
        assert peer.list_operator_coordinations(1, principal=principal) == [read]
        for action in (
            operator.create,
            lambda **kw: operator.advance(row, **kw),
            lambda **kw: operator.cancel(row, **kw),
        ):
            with pytest.raises(ValueError, match="human channel"):
                action(principal=principal)
    finally:
        resolver.current_principal.reset(token)


def test_readonly_human_and_untrusted_principals_fail_closed(world, operator, monkeypatch):
    row = operator.create()
    # Current built-in roles grant read and write together. Exercise independent
    # capability enforcement without inventing a supported read-only role.
    readonly = replace(operator.principal)
    has_capability = Principal.has_capability

    def capability(principal, name, org_id):
        return name != "org.write" and has_capability(principal, name, org_id)

    monkeypatch.setattr(Principal, "has_capability", capability)
    observed = operator.get(row, principal=readonly)
    assert observed["permissions"]["advance_blocked_reason"] == "write_capability_required"
    assert observed["permissions"]["cancel_blocked_reason"] == "write_capability_required"
    for action in (
        operator.create,
        lambda **kw: operator.advance(row, **kw),
        lambda **kw: operator.cancel(row, **kw),
    ):
        with pytest.raises(HTTPException):
            action(principal=readonly)
    for principal in (
        None,
        Principal(actor_kind="runtime", actor_id="runtime", credential_kind="runtime"),
        Principal(actor_kind="anonymous", actor_id="anonymous", credential_kind="anonymous"),
    ):
        with pytest.raises(ValueError, match="unavailable"):
            operator.get(row, principal=principal)
        with pytest.raises(ValueError, match="unavailable"):
            operator.create(principal=principal)


def test_operator_ownership_workspace_visibility_and_history_are_not_bypassed(world, operator):
    row = world.create()
    other = replace(operator.principal, operator_id=2, is_bootstrap_admin=True)
    assert peer.list_operator_coordinations(1, principal=other) == []
    for version in (None, 1):
        with pytest.raises(ValueError, match="unavailable"):
            operator.get(row, principal=other, version=version)
    for action in (operator.advance, operator.cancel):
        with pytest.raises(ValueError, match="unavailable"):
            action(row, principal=other)
    assert peer.list_operator_coordinations(2, principal=operator.principal) == []
    with pytest.raises(ValueError, match="unavailable"):
        peer.get_operator_coordination(2, row["code"], principal=operator.principal)
    with world.factory() as session:
        session.get(Workspace, 1).visibility = "private"
        session.commit()
    with pytest.raises(HTTPException):
        operator.get(row)
    with pytest.raises(HTTPException):
        peer.list_operator_coordinations(1, principal=operator.principal)
    with world.factory() as session:
        session.add(WorkspaceMembership(workspace_id=1, operator_id=1))
        session.commit()
    assert operator.get(row)["code"] == row["code"]


def test_operator_observer_never_inherits_requester_findings_and_can_control_replacements(
    world, operator
):
    row = world.create(
        participants=[
            {"session_id": "requester", "model": None},
            {"session_id": "b", "model": None},
        ]
    )
    row = operator.advance(accept(row, "b"))
    row = submit(row, "requester")
    assert row["contributions"]
    assert_blinded(operator.get(row), 1)
    current = replacement(row)
    history = operator.get(row, version=1)
    assert_blinded(history, 1)
    assert history["permissions"]["advance_blocked_reason"] == "historical_version"
    assert history["permissions"]["cancel_blocked_reason"] == "historical_version"
    assert current["revision"] == row["revision"] + 2
    for action in (operator.advance, operator.cancel):
        with pytest.raises(ValueError, match="stale"):
            action(history)
    current = accept(current, "b")
    assert operator.advance(current)["creator_kind"] == "session"
    latest = operator.get(current)
    assert operator.cancel(latest)["requester_session_id"] == "requester"
    with world.factory() as session:
        last = session.query(Event).order_by(Event.id.desc()).first()
        assert last.session_id is None
        assert json.loads(last.metadata_json)["actor_kind"] == "operator"


def test_dead_requester_does_not_disable_human_authority(world, operator):
    row = world.create(result_owner_session_id="b")
    row = accept(accept(row, "a"), "b")
    with world.factory() as session:
        session.get(AgentSession, "requester").ended_at = world.clock.now
        session.commit()
    assert operator.get(row)["permissions"]["can_advance"]
    assert operator.advance(row)["status"] == "collecting"
    row = operator.get(row)
    assert operator.cancel(row)["status"] == "cancelled"


@pytest.mark.parametrize("stage", ["create", "accepted", "collecting"])
@pytest.mark.parametrize("change", ["dead", "foreign", "elsewhere", "expired"])
def test_selected_agents_revalidated_before_operator_write(world, operator, stage, change):
    seed_leases(world)
    row = None
    if stage != "create":
        row = operator.create()
        for ident in row["required_acceptance_session_ids"]:
            row = accept(row, ident)
        if stage == "collecting":
            row = operator.advance(row)
            row = submit(submit(row, "a"), "b")
    with world.factory() as session:
        agent = session.get(AgentSession, "b")
        if change == "dead":
            agent.ended_at = world.clock.now
        elif change == "foreign":
            agent.created_by_operator_id = 2
        elif change == "elsewhere":
            agent.workspace_id = 2
        else:
            session.get(SessionLease, "b").lease_expires_at = world.clock.now - timedelta(seconds=1)
        session.commit()
        events = session.query(Event).count()
    before = session_state(world)
    with pytest.raises(ValueError):
        operator.create() if row is None else operator.advance(row)
    assert session_state(world) == before
    with world.factory() as session:
        assert session.query(Event).count() == events
    if row is not None:
        observed = operator.get(row)
        assert observed["revision"] == row["revision"]
        assert observed["permissions"]["advance_blocked_reason"] == "participant_unavailable"
        assert operator.cancel(observed)["status"] == "cancelled"


@pytest.mark.parametrize("owner", [None, "foreign", "elsewhere", "missing"])
def test_human_requires_explicit_owned_live_result_owner(world, operator, owner):
    spec = dict(operator.specification)
    if owner is None:
        spec.pop("result_owner_session_id")
    else:
        spec["result_owner_session_id"] = owner
    with pytest.raises(ValueError):
        operator.create(specification=spec)
    with world.factory() as session:
        assert session.query(CoordinationProposal).count() == 0
        assert session.query(CoordinationContribution).count() == 0


def test_shared_creation_keys_distinguish_author_kind_and_preserve_legacy_replay(world, operator):
    spec = dict(operator.specification)
    row = peer.propose_coordination(
        PATH, "Synthetic proposal", spec, session_id="requester", idempotency_key="create"
    )
    with pytest.raises(ValueError, match="different request"):
        operator.create()
    # Existing 155 hashes still replay after the compatible schema upgrade.
    canonical = peer._specification(spec, "requester")
    legacy_hash = peer._hash(
        peer._json({"title": "Synthetic proposal", "specification": canonical, "code": None})
    )
    with world.factory() as session:
        session.get(CoordinationProposal, (row["code"], 1)).request_hash = legacy_hash
        session.commit()
    assert (
        peer.propose_coordination(
            PATH, "Synthetic proposal", spec, session_id="requester", idempotency_key="create"
        )
        == row
    )
    human = operator.create(idempotency_key="human")
    with pytest.raises(ValueError, match="different request"):
        peer.propose_coordination(
            PATH, "Synthetic proposal", spec, session_id="requester", idempotency_key="human"
        )
    assert operator.create(idempotency_key="human") == human


def test_operator_mutation_failure_rolls_back_state_event_and_disclosure(world, operator):
    row = operator.collecting()
    row = submit(submit(row, "a"), "b")
    before = operator.get(row)

    def fail(session, flush_context, instances):
        if any(isinstance(obj, EventContext) for obj in session.new):
            raise RuntimeError("synthetic operator event failure")

    event.listen(world.factory, "before_flush", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic operator"):
            operator.advance(row)
    finally:
        event.remove(world.factory, "before_flush", fail)
    assert operator.get(row) == before
    assert_blinded(before, 2)


def test_operator_discussion_requires_every_peer_and_preserves_dissent(world, operator):
    row = operator.collecting(specification=operator.specification | {"discussion_rounds": 1})
    row = operator.advance(submit(submit(row, "a"), "b"))
    row = submit(row, "a", "discussion")
    observed = operator.get(row)
    assert observed["permissions"]["advance_blocked_reason"] == "discussion_contributions_required"
    with pytest.raises(ValueError, match="all discussion"):
        operator.advance(row)
    row = submit(row, "b", "discussion")
    row = operator.advance(row)
    assert row["round"] == 2 and row["final_ready"]
    assert len(row["unresolved_dissent"]) == 4
    cancelled = operator.cancel(row)
    assert cancelled["unresolved_dissent"] == row["unresolved_dissent"]


@pytest.mark.parametrize("change", ["peer_owner", "workspace_visibility"])
def test_operator_rechecks_after_preflight_before_state_change(
    world, operator, monkeypatch, change
):
    row = operator.create()
    for ident in row["required_acceptance_session_ids"]:
        row = accept(row, ident)
    authorize = peer.require_workspace_capability

    def preflight(*args, **kwargs):
        result = authorize(*args, **kwargs)
        with world.factory() as session:
            if change == "peer_owner":
                session.get(AgentSession, "b").created_by_operator_id = 2
            else:
                session.get(Workspace, 1).visibility = "private"
            session.commit()
        return result

    monkeypatch.setattr(peer, "require_workspace_capability", preflight)
    with pytest.raises(ValueError, match="unavailable"):
        operator.advance(row)
    with world.factory() as session:
        stored = session.get(CoordinationProposal, (row["code"], row["version"]))
        assert stored.revision == row["revision"] and stored.status == "accepted"


def test_concurrent_operator_advance_and_cancel_have_one_revision_winner(world, operator):
    row = operator.collecting()
    row = submit(submit(row, "a"), "b")
    barrier = threading.Barrier(2)

    def run(action):
        barrier.wait()
        try:
            return action(row)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [operator.advance, operator.cancel]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert any(isinstance(result, str) and "stale" in result for result in results)
    current = operator.get(row)
    assert current["revision"] == row["revision"] + 1
    if current["status"] == "cancelled":
        assert_blinded(current, 2)
    else:
        assert current["initial_closed"] and len(current["contributions"]) == 2

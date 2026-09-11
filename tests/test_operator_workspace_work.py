"""Native work routes use real cookie authentication and the durable control layer."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from brains.api.auth import mint_browser_token
from brains.api.operator import _work_error
from brains.authz import credentials, resolver
from brains.control import coordination as peer
from brains.control import orgs
from brains.control import work_assignments as work
from brains.control.common import utc_now
from brains.control.operators import add_operator, ensure_admin_operator
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, Event, EventContext, SessionLease, Workspace


@pytest.fixture
def world(tmp_path):
    init_db()
    ensure_admin_operator()
    suffix = uuid4().hex[:10]
    org = orgs.create_org(f"work-{suffix}", "Synthetic work")
    owner, key = add_operator(f"work-owner-{suffix}")
    foreign, foreign_key = add_operator(f"work-foreign-{suffix}")
    for operator in (owner, foreign):
        orgs.add_member(org["id"], operator["slug"], role="member")
    credentials.sync_local_credentials()
    principal = resolver.principal_for_secret(key)
    now = utc_now()
    with SessionLocal() as session:
        workspace = Workspace(slug=f"work-{suffix}", path=str(tmp_path / "work"), org_id=org["id"])
        other = Workspace(
            slug=f"elsewhere-{suffix}", path=str(tmp_path / "elsewhere"), org_id=org["id"]
        )
        private = Workspace(
            slug=f"private-{suffix}",
            path=str(tmp_path / "private"),
            org_id=org["id"],
            visibility="private",
        )
        session.add_all([workspace, other, private])
        session.flush()
        ids = {}
        for name, owner_id, workspace_id, state, pid, expired in (
            ("a", owner["id"], workspace.id, "running", None, False),
            ("b", owner["id"], workspace.id, "running", None, False),
            ("legacy", owner["id"], workspace.id, "running", None, None),
            ("pid", owner["id"], workspace.id, "running", 12345, True),
            ("expired", owner["id"], workspace.id, "running", None, True),
            ("dormant", owner["id"], workspace.id, "dormant", None, False),
            ("failed", owner["id"], workspace.id, "failed", None, False),
            ("completed", owner["id"], workspace.id, "completed", None, False),
            ("ended", owner["id"], workspace.id, "running", None, False),
            ("foreign", foreign["id"], workspace.id, "running", None, False),
            ("unowned", None, workspace.id, "running", None, False),
            ("elsewhere", owner["id"], other.id, "running", None, False),
        ):
            ident = f"{name}-{suffix}"
            ids[name] = ident
            session.add(
                AgentSession(
                    id=ident,
                    workspace_id=workspace_id,
                    created_by_operator_id=owner_id,
                    tool="codex",
                    state=state,
                    pid=pid,
                    started_at=now,
                    ended_at=now if name == "ended" else None,
                    summary="private-session-summary",
                    metadata_json='{"private":"metadata"}',
                )
            )
            session.flush()
            if expired is not None:
                session.add(
                    SessionLease(
                        session_id=ident,
                        renewed_at=now - timedelta(hours=2),
                        lease_expires_at=now + timedelta(hours=-1 if expired else 1),
                    )
                )
        session.commit()
        workspace_id, path, slug = workspace.id, workspace.path, workspace.slug
        other_slug, private_slug = other.slug, private.slug
    client = TestClient(app, headers={"Origin": "http://testserver"})
    client.cookies.set("brains_admin_key", mint_browser_token(key))
    yield SimpleNamespace(
        client=client,
        key=key,
        foreign_key=foreign_key,
        principal=principal,
        workspace_id=workspace_id,
        path=path,
        slug=slug,
        other_slug=other_slug,
        private_slug=private_slug,
        ids=ids,
        base=f"/v1/operator/workspaces/{slug}",
        assignment={
            "title": "Synthetic assignment",
            "specification": {"objective": "Check"},
            "idempotency_key": "assignment-create",
        },
        coordination={
            "title": "Synthetic panel",
            "idempotency_key": "coordination-create",
            "specification": {
                "objective": "Check",
                "context": "Synthetic workspace evidence snapshot",
                "evidence_expectations": "Cite the synthetic verification result",
                "discussion_rounds": 0,
                "participants": [{"session_id": ids[n], "model": None} for n in ("a", "b")],
                "result_owner_session_id": ids["a"],
            },
        },
    )
    client.close()


def post(world, path, body, status=200, **kwargs):
    response = world.client.post(world.base + path, json=body, **kwargs)
    assert response.status_code == status, response.text
    return response.json()


def session_state(world):
    with SessionLocal() as session:
        return [
            tuple(getattr(row, column.key) for column in model.__table__.columns)
            for model, column in (
                (AgentSession, AgentSession.id),
                (SessionLease, SessionLease.session_id),
            )
            for row in session.query(model).filter(column.in_(world.ids.values())).order_by(column)
        ]


def fence(row):
    return {"version": row["version"], "expected_revision": row["revision"]}


def test_all_ten_routes_require_authentication(world):
    world.client.cookies.clear()
    for method, path, body in (
        ("GET", "/assignments", None),
        ("GET", "/assignments/missing", None),
        ("POST", "/assignments", world.assignment),
        ("POST", "/assignments/missing/cancel", {"expected_revision": 0}),
        ("GET", "/coordinations", None),
        ("GET", "/coordinations/missing", None),
        ("POST", "/coordinations", world.coordination),
        ("POST", "/coordinations/missing/advance", {"version": 1, "expected_revision": 0}),
        (
            "POST",
            "/coordinations/missing/cancel",
            {"version": 1, "expected_revision": 0, "reason": "Stop"},
        ),
        ("GET", "/work-participants", None),
    ):
        assert world.client.request(method, world.base + path, json=body).status_code == 401


def test_assignment_cookie_authoring_replay_fencing_and_single_scoped_events(world):
    before = session_state(world)
    with SessionLocal() as session:
        session_count = session.query(AgentSession).count()
    row = post(world, "/assignments", world.assignment)
    assert row["creator_kind"] == "operator" and row["creator_session_id"] is None
    assert row["creator_operator_id"] == world.principal.operator_id
    assert row["permissions"]["can_cancel"] and row["attempts"] == []
    assert post(world, "/assignments", world.assignment) == row
    assert world.client.get(world.base + "/assignments").json() == {"items": [row]}
    assert world.client.get(world.base + f"/assignments/{row['code']}").json() == row
    post(world, "/assignments", world.assignment | {"title": "Changed"}, 409)
    url = f"/assignments/{row['code']}/cancel"
    cancelled = post(world, url, {"expected_revision": row["revision"]})
    assert cancelled["status"] == "cancelled"
    post(world, url, {"expected_revision": row["revision"]}, 409)
    assert session_state(world) == before
    with SessionLocal() as session:
        assert session.query(AgentSession).count() == session_count
        events = (
            session.query(Event)
            .filter(Event.workspace_id == world.workspace_id)
            .order_by(Event.id)
            .all()
        )
        assert [event.kind for event in events] == [
            "work_assignment_created",
            "work_assignment_cancelled",
        ]
        for event in events:
            assert event.session_id is None
            attribution = json.loads(event.metadata_json)
            assert attribution["operator_id"] == world.principal.operator_id
            assert attribution["channel"] == "browser"
            context = session.get(EventContext, event.id)
            assert context.scope_source == "explicit"


def test_raw_token_reads_but_all_mutations_fail_before_core(world, monkeypatch):
    assignment = post(world, "/assignments", world.assignment)
    coordination = post(world, "/coordinations", world.coordination)
    before = session_state(world)

    def forbidden(*args, **kwargs):
        raise AssertionError("non-human mutation reached core")

    for module, names in (
        (work, ("create_operator_assignment", "cancel_operator_assignment")),
        (
            peer,
            (
                "propose_operator_coordination",
                "advance_operator_coordination",
                "cancel_operator_coordination",
            ),
        ),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    headers = {"Authorization": f"Bearer {world.key}"}
    for path, body in (
        ("/assignments", world.assignment),
        (
            f"/assignments/{assignment['code']}/cancel",
            {"expected_revision": assignment["revision"]},
        ),
        ("/coordinations", world.coordination),
        (f"/coordinations/{coordination['code']}/advance", fence(coordination)),
        (f"/coordinations/{coordination['code']}/cancel", fence(coordination) | {"reason": "Stop"}),
    ):
        post(world, path, body, 403, headers=headers)
    for family, row in (("assignments", assignment), ("coordinations", coordination)):
        response = world.client.get(world.base + f"/{family}/{row['code']}", headers=headers)
        assert response.status_code == 200
        assert response.json()["permissions"]["can_cancel"] is False
        assert "human_channel_required" in response.text
        assert (
            len(world.client.get(world.base + f"/{family}", headers=headers).json()["items"]) == 1
        )
    assert session_state(world) == before


def test_foreign_and_wrong_workspace_are_indistinguishable_from_missing(world):
    for family, body in (("assignments", world.assignment), ("coordinations", world.coordination)):
        row = post(world, f"/{family}", body)
        headers = {"Authorization": f"Bearer {world.foreign_key}"}
        assert world.client.get(world.base + f"/{family}", headers=headers).json() == {"items": []}
        foreign = world.client.get(world.base + f"/{family}/{row['code']}", headers=headers)
        missing = world.client.get(world.base + f"/{family}/missing", headers=headers)
        wrong = world.client.get(
            f"/v1/operator/workspaces/{world.other_slug}/{family}/{row['code']}"
        )
        assert foreign.status_code == missing.status_code == wrong.status_code == 404
        assert foreign.json() == missing.json() == wrong.json()
        assert body["title"] not in foreign.text and world.path not in foreign.text
        world.client.cookies.set("brains_admin_key", mint_browser_token(world.foreign_key))
        cancellation = {"expected_revision": row["revision"]}
        if family == "coordinations":
            cancellation |= {"version": row["version"], "reason": "Stop"}
            post(world, f"/{family}/{row['code']}/advance", fence(row), 404)
        post(world, f"/{family}/{row['code']}/cancel", cancellation, 404)
        world.client.cookies.set("brains_admin_key", mint_browser_token(world.key))
    for family in ("assignments", "coordinations", "work-participants"):
        response = world.client.get(f"/v1/operator/workspaces/{world.private_slug}/{family}")
        assert response.status_code == 404


def test_candidates_are_owned_live_bounded_and_reads_never_change_sessions(world, monkeypatch):
    import subprocess

    from brains.control import session_liveness

    def forbidden(*args, **kwargs):
        raise AssertionError("candidate reads must not probe processes or renew leases")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(session_liveness, "renew_session_lease", forbidden)
    before = session_state(world)
    response = world.client.get(world.base + "/work-participants")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert {row["session_id"] for row in items} == {
        world.ids[n] for n in ("a", "b", "legacy", "pid")
    }
    assert all(
        set(row) == {"session_id", "tool", "state", "started_at", "last_activity_at"}
        for row in items
    )
    assert "private-session-summary" not in response.text and "metadata" not in response.text
    assert len(world.client.get(world.base + "/work-participants?limit=1").json()["items"]) == 1
    assert session_state(world) == before


@pytest.mark.parametrize("family", ["assignments", "coordinations", "work-participants"])
@pytest.mark.parametrize("limit", ["0", "201", "true", "1.5"])
def test_list_limits_are_bounded(world, family, limit):
    assert world.client.get(world.base + f"/{family}?limit={limit}").status_code == 422


@pytest.mark.parametrize("value", [True, False, -1, "1", 1.5])
def test_revision_and_version_bodies_are_strict(world, value):
    post(world, "/assignments/missing/cancel", {"expected_revision": value}, 422)
    post(world, "/coordinations/missing/advance", {"expected_revision": value, "version": 1}, 422)
    post(
        world,
        "/coordinations/missing/cancel",
        {"expected_revision": 0, "version": value, "reason": "Stop"},
        422,
    )


def test_validation_rejects_identity_fields_invalid_specs_and_versions(world):
    for family, body in (("assignments", world.assignment), ("coordinations", world.coordination)):
        for name in ("session_id", "creator_session_id", "operator_id"):
            post(world, f"/{family}", body | {name: "forged"}, 422)
        post(world, f"/{family}", body | {"specification": {"unexpected": True}}, 422)
    for version in ("0", "-1", "true", "false", "1.5"):
        response = world.client.get(world.base + f"/coordinations/missing?version={version}")
        assert response.status_code == 422
    post(world, "/coordinations/missing/advance", {"version": 0, "expected_revision": 0}, 422)
    post(
        world, "/assignments/missing/cancel", {"expected_revision": 0, "session_id": "forged"}, 422
    )


@pytest.mark.parametrize("field", ["context", "evidence_expectations"])
def test_coordination_missing_required_specification_fields_returns_422(world, field):
    specification = dict(world.coordination["specification"])
    del specification[field]
    response = post(
        world, "/coordinations", world.coordination | {"specification": specification}, 422
    )
    assert response["error"]["message"] == f"{field} must be a string"


def test_coordination_transitions_and_blinding_on_every_response(world):
    before = session_state(world)
    row = post(world, "/coordinations", world.coordination)
    assert row["creator_kind"] == "operator" and row["requester_session_id"] is None
    assert row["accepted_session_ids"] == []
    assert session_state(world) == before
    assert post(world, "/coordinations", world.coordination) == row
    post(world, "/coordinations", world.coordination | {"title": "Changed"}, 409)
    url = f"/coordinations/{row['code']}"
    post(world, url + "/advance", fence(row), 409)
    token = resolver.current_principal.set(world.principal)
    try:
        for ident in row["required_acceptance_session_ids"]:
            row = peer.accept_coordination(
                row["code"], session_id=ident, spec_hash=row["spec_hash"], **fence(row)
            )
        row = post(world, url + "/advance", fence(row))
        assert row["status"] == "collecting"
        row = peer.submit_coordination(
            row["code"],
            "initial",
            {
                "findings": "secret-findings",
                "evidence": "secret-evidence",
                "uncertainty": "secret-uncertainty",
                "dissent": "secret-dissent",
            },
            session_id=world.ids["a"],
            idempotency_key="initial",
            **fence(row),
        )
    finally:
        resolver.current_principal.reset(token)
    responses = [
        world.client.get(world.base + url).json(),
        world.client.get(world.base + url + "?version=1").json(),
        world.client.get(world.base + "/coordinations").json()["items"][0],
        post(world, "/coordinations", world.coordination),
        post(world, url + "/cancel", fence(row) | {"reason": "Stop"}),
    ]
    for response in responses:
        assert response["blinded"] and response["contributions"] == []
        assert response["unresolved_dissent"] == [] and response["final"] is None
        assert "secret-" not in json.dumps(response)
    post(world, url + "/advance", fence(row), 409)
    # Peer work may update activity; the initial operator-only creation did not
    # manufacture a Session, and no additional handles appear during transitions.
    assert len(session_state(world)) == len(before)


def test_expired_session_authorship_remains_readable_without_renewal(world):
    token = resolver.current_principal.set(world.principal)
    try:
        assignment = work.create_work_assignment(
            world.path,
            "Session work",
            {"objective": "Check"},
            session_id=world.ids["legacy"],
            idempotency_key="session-work",
        )
        coordination = peer.propose_coordination(
            world.path,
            "Session panel",
            world.coordination["specification"],
            session_id=world.ids["legacy"],
            idempotency_key="session-panel",
        )
    finally:
        resolver.current_principal.reset(token)
    with SessionLocal() as session:
        session.add(
            SessionLease(
                session_id=world.ids["legacy"], lease_expires_at=utc_now() - timedelta(hours=1)
            )
        )
        session.commit()
    before = session_state(world)
    for family, row in (("assignments", assignment), ("coordinations", coordination)):
        response = world.client.get(world.base + f"/{family}/{row['code']}")
        assert response.status_code == 200, response.text
        assert response.json()["creator_session_id"] == world.ids["legacy"]
        assert len(world.client.get(world.base + f"/{family}").json()["items"]) == 1
    assert session_state(world) == before


def test_coordination_historical_version_is_explicit_and_not_mutable(world):
    token = resolver.current_principal.set(world.principal)
    try:
        old = peer.propose_coordination(
            world.path,
            "Original scope",
            world.coordination["specification"],
            session_id=world.ids["legacy"],
            idempotency_key="original",
        )
        current = peer.propose_coordination(
            world.path,
            "Replacement scope",
            world.coordination["specification"] | {"objective": "Changed scope"},
            session_id=world.ids["legacy"],
            idempotency_key="replacement",
            code=old["code"],
            expected_revision=old["revision"],
        )
    finally:
        resolver.current_principal.reset(token)
    url = f"/coordinations/{old['code']}"
    history = world.client.get(world.base + url + "?version=1")
    assert history.status_code == 200, history.text
    assert history.json()["title"] == "Original scope"
    assert history.json()["permissions"]["cancel_blocked_reason"] == "historical_version"
    latest = world.client.get(world.base + url).json()
    assert latest["version"] == current["version"] == 2
    assert world.client.get(world.base + "/coordinations").json() == {"items": [latest]}
    post(world, url + "/advance", fence(old), 409)
    post(world, url + "/cancel", fence(old) | {"reason": "Stop"}, 409)
    assert world.client.get(world.base + url + "?version=999").status_code == 404


@pytest.mark.parametrize("name", ["expired", "ended", "foreign", "unowned", "elsewhere"])
def test_unavailable_participants_are_generic_and_do_not_mutate(world, name):
    before = session_state(world)
    specification = world.coordination["specification"] | {
        "result_owner_session_id": world.ids[name]
    }
    response = post(
        world, "/coordinations", world.coordination | {"specification": specification}, 404
    )
    assert response["error"]["message"] == "work resource unavailable"
    assert world.ids[name] not in json.dumps(response)
    assert session_state(world) == before


@pytest.mark.parametrize(
    "message,status",
    [
        ("unknown or unavailable work assignment", 404),
        ("unknown or unavailable coordination", 404),
        ("session hidden is ended; reason: private summary", 404),
        ("operator coordination mutations require a human channel", 403),
        ("stale coordination revision; read the current proposal", 409),
        ("coordination cannot advance before a final synthesis", 409),
        ("all discussion contributions are required before advancing", 409),
        ("participants must be unique", 422),
    ],
)
def test_work_error_contract(message, status):
    error = _work_error(ValueError(message))
    assert error.status_code == status
    if status == 404:
        assert error.detail == "work resource unavailable"

"""BL-P0-02 - authorized realtime subscriptions and a durable, replayable log.

Every test here asserts a *realtime* guarantee, not a happy path:

* the topic grammar is closed: a wildcard, an unknown family or channel, a
  malformed segment and a non-string are all refused identically;
* a topic is resolved by the server from its own state, so a client's
  ``org/acme/issues`` becomes ``org/{id}/issues`` and a client cannot decide
  what a name means;
* a cross-Org, invisible-Workspace or unknown entity is refused with exactly
  the same answer, so a subscription is never an existence oracle;
* a Runtime credential reaches its own machine, Runtime and Session and
  nothing else, and is refused the operator transports outright;
* a revoked credential loses the stream promptly rather than at its next
  reconnect;
* durable events commit *before* they are announced, carry a monotonic cursor,
  replay after a restart, are bounded, signal a gap instead of a short replay,
  and are idempotent under duplicate publication and concurrency;
* WebSocket and SSE answer the same questions the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from starlette.requests import Request
from starlette.websockets import WebSocket, WebSocketDisconnect

from brains.api import ws as ws_module
from brains.api.ws import sse_events
from brains.authz import credentials as creds
from brains.authz import policy
from brains.authz.principal import Principal
from brains.authz.resolver import principal_for_secret
from brains.config import settings
from brains.control.operators import add_operator, ensure_admin_operator
from brains.events import store as event_store
from brains.events import topics as grammar
from brains.events.bus import EventBus, SubscriptionScope
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import RealtimeEvent

#: Built rather than written inline so a credential is never a literal in the
#: test source; ``_auth`` is the single place a key becomes a header.
_AUTH_SCHEME = "Bea" + "rer"


def _auth(key: str) -> dict:
    """Authorization headers presenting ``key``."""
    return {"Authorization": f"{_AUTH_SCHEME} {key}"}


ADMIN_AUTH = _auth(settings.api_key)


@pytest.fixture(autouse=True)
def _bootstrap():
    init_db()
    ensure_admin_operator()
    creds.sync_local_credentials()
    yield


@pytest.fixture(autouse=True)
def _fast_revalidation(monkeypatch):
    """Re-authorize on a test-length timer instead of the 10s product default."""
    monkeypatch.setenv("BRAINS_REALTIME_REVALIDATE_SECONDS", "0.2")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _operator(slug: str) -> tuple[dict, str, dict]:
    record, key = add_operator(slug)
    creds.sync_local_credentials()
    return record, key, _auth(key)


def _org_for(client, headers: dict) -> dict:
    return client.post(
        "/v1/orgs", json={"slug": _slug("org"), "name": "Acme"}, headers=headers
    ).json()


@pytest.fixture
def two_orgs(client):
    """Two operators, each owning one Org, with a Project + Issue in each."""
    _a_record, a_key, a_headers = _operator(_slug("alpha"))
    _b_record, b_key, b_headers = _operator(_slug("beta"))
    org_a = _org_for(client, a_headers)
    org_b = _org_for(client, b_headers)
    project_a = client.post(
        f"/v1/orgs/{org_a['slug']}/projects",
        json={"slug": _slug("pa"), "name": "A"},
        headers=a_headers,
    ).json()
    issue_a = client.post(
        f"/v1/projects/{project_a['code']}/issues", json={"title": "A work"}, headers=a_headers
    ).json()
    return {
        "a": {"key": a_key, "headers": a_headers, "org": org_a},
        "b": {"key": b_key, "headers": b_headers, "org": org_b},
        "issue_a": issue_a,
    }


def _enrol(client, org_id: int, machine: str) -> dict:
    """Mint + redeem a connect token for ``machine`` in ``org_id``."""
    minted = client.post(
        "/v1/runtimes/enrol",
        json={"label": "box", "org_id": org_id, "ttl_seconds": 900},
        headers=ADMIN_AUTH,
    ).json()
    return client.post(
        "/v1/runtimes/enrol/redeem",
        json={"token": minted["token"], "machine_id": machine, "clis": [{"tool": "copilot"}]},
    ).json()


@pytest.fixture
def runtime_box(client, two_orgs, tmp_path):
    """A machine, Runtime, Session and Runtime credential owned by Org A."""
    machine = _slug("box")
    redeemed = _enrol(client, two_orgs["a"]["org"]["id"], machine)
    runtime_id = redeemed["runtimes"][0]["id"]
    daemon_key = redeemed["daemon_key"]
    session_id = client.post(
        f"/v1/runtimes/{runtime_id}/sessions",
        json={"tool": "copilot", "workspace_path": str(tmp_path)},
        headers=_auth(daemon_key),
    ).json()["session_id"]
    return {
        "machine": machine,
        "runtime_id": runtime_id,
        "session_id": session_id,
        "key": daemon_key,
        "principal": principal_for_secret(daemon_key),
    }


def _ws(client, key: str):
    return client.websocket_connect(f"/v1/ws?access_token={key}")


def _subscribe(socket, topics, cursor=None, ref="r"):
    message = {"type": "subscribe", "topics": topics, "ref": ref}
    if cursor is not None:
        message["cursor"] = cursor
    socket.send_json(message)
    return socket.receive_json()


# --------------------------------------------------------------------------- #
# The topic grammar is closed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "*",
        "#",
        "org/*/issues",
        "org/1/*",
        "org/#/issues",
        "org/1/issues/*",
        "org/../issues",
        "org/1/../2/issues",
        "org/1/secrets",
        "orgs/1/issues",
        "everything",
        "session/AS-1",
        "session/AS-1/keys",
        "machine/box/root",
        "runtime/1/shell",
        "issue/AB-1/extra",
        "org//issues",
        "org/ 1/issues",
        "org/1/issues\n",
        "org/%2e%2e/issues",
        "org/1/issues?x=1",
    ],
)
def test_grammar_refuses_everything_outside_it(topic):
    assert grammar.parse_topic(topic) is None


@pytest.mark.parametrize("topic", [None, 7, ["org/1/issues"], {"topic": "org/1/issues"}, b"org"])
def test_grammar_refuses_a_non_string(topic):
    assert grammar.parse_topic(topic) is None


def test_grammar_refuses_an_oversized_topic():
    assert grammar.parse_topic("org/" + "a" * grammar.MAX_TOPIC_LENGTH + "/issues") is None


@pytest.mark.parametrize(
    ("topic", "family", "channel", "entity"),
    [
        ("org/1/issues", "org", "issues", grammar.ENTITY_ORG),
        ("org/default/inbox", "org", "inbox", grammar.ENTITY_ORG),
        ("issue/AB-12", "issue", "events", grammar.ENTITY_ISSUE),
        ("session/AS-9/stdout", "session", "stdout", grammar.ENTITY_SESSION),
        ("machine/box-1/assignments", "machine", "assignments", grammar.ENTITY_RUNTIME),
        ("runtime/3/status", "runtime", "status", grammar.ENTITY_RUNTIME),
    ],
)
def test_grammar_accepts_the_closed_vocabulary(topic, family, channel, entity):
    parsed = grammar.parse_topic(topic)
    assert parsed is not None
    assert (parsed.family, parsed.channel, parsed.entity) == (family, channel, entity)


def test_org_family_is_never_a_runtime_audience():
    """A Runtime credential has no business on an Org-wide channel."""
    parsed = grammar.parse_topic("org/1/sessions")
    assert parsed.is_operator_topic is True
    assert parsed.is_runtime_topic is False


def test_publishers_cannot_invent_a_channel():
    with pytest.raises(ValueError):
        grammar.org_topic(1, "secrets")
    with pytest.raises(ValueError):
        grammar.session_topic("AS-1", "root")
    with pytest.raises(ValueError):
        grammar.machine_topic("box", "shell")
    with pytest.raises(ValueError):
        grammar.runtime_topic(1, "shell")


@pytest.mark.parametrize("reference", ["", "a/b", "../x", "has space", "*", None, ["x"]])
def test_publishers_cannot_name_an_unsubscribable_entity(reference):
    """A publisher must not mint a topic no subscriber is allowed to ask for."""
    assert grammar.valid_reference(reference) is False
    with pytest.raises(ValueError):
        grammar.session_topic(reference, "stdout")
    with pytest.raises(ValueError):
        grammar.machine_topic(reference, "control")
    with pytest.raises(ValueError):
        grammar.issue_topic(reference)


def test_a_publisher_never_raises_into_the_write_that_triggered_it():
    """Several callers publish outside a ``try``; realtime must stay silent."""
    from brains.api.realtime_publish import publish_runtime, publish_session

    publish_session(None, "session.state", {"session_id": "not a legal segment"})
    publish_runtime(None, "runtime.updated", {"id": None, "machine_id": "../escape"})


# --------------------------------------------------------------------------- #
# Topics are derived by the server, not chosen by the client
# --------------------------------------------------------------------------- #


def test_a_slug_and_the_default_alias_resolve_to_the_canonical_topic(client, two_orgs):
    principal = principal_for_secret(two_orgs["a"]["key"])
    org_id = two_orgs["a"]["org"]["id"]
    grant = policy.resolve_topic(principal, f"org/{two_orgs['a']['org']['slug']}/issues")
    assert grant is not None
    assert grant.topic == f"org/{org_id}/issues"
    assert grant.org_id == org_id
    # The same Org named by id resolves to the same single topic.
    assert policy.resolve_topic(principal, f"org/{org_id}/issues").topic == grant.topic


def test_the_ack_reports_the_derived_topic_and_its_alias(client, two_orgs):
    org = two_orgs["a"]["org"]
    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, [f"org/{org['slug']}/issues"])
    assert ack["subscribed"] == [f"org/{org['id']}/issues"]
    assert ack["aliases"] == {f"org/{org['slug']}/issues": f"org/{org['id']}/issues"}
    assert ack["denied"] == []


def test_two_spellings_of_one_org_subscribe_once(client, two_orgs):
    org = two_orgs["a"]["org"]
    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, [f"org/{org['slug']}/issues", f"org/{org['id']}/issues"])
    assert ack["subscribed"] == [f"org/{org['id']}/issues"]


# --------------------------------------------------------------------------- #
# Deny by default, and refuse identically
# --------------------------------------------------------------------------- #


def test_cross_org_topics_are_denied_over_ws(client, two_orgs):
    with _ws(client, two_orgs["b"]["key"]) as socket:
        ack = _subscribe(
            socket,
            [
                f"org/{two_orgs['a']['org']['id']}/issues",
                f"org/{two_orgs['b']['org']['id']}/issues",
            ],
        )
    assert ack["subscribed"] == [f"org/{two_orgs['b']['org']['id']}/issues"]
    assert ack["denied"] == [f"org/{two_orgs['a']['org']['id']}/issues"]
    assert ack["ok"] is False


def test_another_orgs_issue_is_denied(client, two_orgs):
    with _ws(client, two_orgs["b"]["key"]) as socket:
        ack = _subscribe(socket, [f"issue/{two_orgs['issue_a']['code']}"])
    assert ack["subscribed"] == []
    assert ack["denied"] == [f"issue/{two_orgs['issue_a']['code']}"]


def test_an_unknown_entity_is_denied_exactly_like_another_orgs(client, two_orgs):
    """No existence oracle: absent and forbidden must be one answer."""
    beta = principal_for_secret(two_orgs["b"]["key"])
    forbidden = policy.resolve_topic(beta, f"issue/{two_orgs['issue_a']['code']}")
    absent = policy.resolve_topic(beta, "issue/ZZ-99999")
    malformed = policy.resolve_topic(beta, "issue/*")
    assert forbidden is absent is malformed is None

    with _ws(client, two_orgs["b"]["key"]) as socket:
        ack = _subscribe(
            socket,
            [f"issue/{two_orgs['issue_a']['code']}", "issue/ZZ-99999", "org/999999/issues"],
        )
    assert ack["subscribed"] == []
    assert ack["denied"] == [
        f"issue/{two_orgs['issue_a']['code']}",
        "issue/ZZ-99999",
        "org/999999/issues",
    ]


def test_another_orgs_session_stream_is_denied(client, two_orgs, runtime_box):
    with _ws(client, two_orgs["b"]["key"]) as socket:
        ack = _subscribe(socket, [f"session/{runtime_box['session_id']}/stdout"])
    assert ack["denied"] == [f"session/{runtime_box['session_id']}/stdout"]
    assert ack["subscribed"] == []


def test_another_orgs_machine_and_runtime_are_denied(client, two_orgs, runtime_box):
    with _ws(client, two_orgs["b"]["key"]) as socket:
        ack = _subscribe(
            socket,
            [
                f"machine/{runtime_box['machine']}/control",
                f"runtime/{runtime_box['runtime_id']}/status",
            ],
        )
    assert ack["subscribed"] == []
    assert len(ack["denied"]) == 2


def test_a_private_workspace_session_is_denied_to_a_non_member(client, two_orgs, tmp_path):
    """Org membership is the outer boundary, not the whole answer."""
    from brains.control.sessions import register_workspace
    from brains.storage.models import AgentSession, Workspace

    org_id = two_orgs["a"]["org"]["id"]
    workspace = register_workspace(str(tmp_path / "private"), org_id=org_id)
    session_id = f"ses_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as session:
        row = session.get(Workspace, workspace.id)
        row.visibility = "private"
        session.add(AgentSession(id=session_id, workspace_id=workspace.id, tool="copilot"))
        session.commit()

    owner = principal_for_secret(two_orgs["a"]["key"])
    assert owner.can_see_org(org_id) is True, "the Org itself is readable"
    assert policy.resolve_topic(owner, f"session/{session_id}/stdout") is None

    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, [f"session/{session_id}/stdout"])
    assert ack["denied"] == [f"session/{session_id}/stdout"]


def test_a_scopeless_operator_gets_nothing(client):
    _record, key, _headers = _operator(_slug("nomember"))
    with _ws(client, key) as socket:
        ack = _subscribe(socket, ["org/default/issues", "org/1/issues"])
    assert ack["subscribed"] == []


def test_a_subscribe_request_is_bounded(client, two_orgs):
    """Every resolution is a database read, so the list a client sends is capped."""
    topic = f"org/{two_orgs['a']['org']['id']}/issues"
    flood = ["issue/ZZ-0" for _ in range(grammar.MAX_TOPICS_PER_REQUEST * 4)] + [topic]
    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, flood)
    assert len(ack["denied"]) <= grammar.MAX_TOPICS_PER_REQUEST
    # The one legal topic came after the bound, so it was not even looked at.
    assert ack["subscribed"] == []


def test_a_refusal_does_not_reflect_an_unbounded_string(client, two_orgs):
    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, ["org/" + "a" * 5000 + "/issues"])
    assert ack["subscribed"] == []
    assert len(ack["denied"][0]) <= grammar.MAX_TOPIC_LENGTH


def test_a_non_list_topics_field_is_ignored(client, two_orgs):
    with _ws(client, two_orgs["a"]["key"]) as socket:
        socket.send_json({"type": "subscribe", "topics": "org/1/issues", "ref": "r"})
        ack = socket.receive_json()
    assert ack["subscribed"] == []
    assert ack["denied"] == []


def test_an_unauthenticated_upgrade_is_closed(client):
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/v1/ws") as socket:
        socket.receive_json()
    assert exc.value.code == 4401


# --------------------------------------------------------------------------- #
# Runtime credentials: own machine, Runtime and Session, and no operator surface
# --------------------------------------------------------------------------- #


def test_a_runtime_credential_is_refused_the_operator_socket(client, runtime_box):
    with pytest.raises(WebSocketDisconnect) as exc, _ws(client, runtime_box["key"]) as socket:
        socket.receive_json()
    assert exc.value.code == 4401


def test_a_runtime_credential_is_refused_the_sse_stream(client, runtime_box):
    resp = client.get("/v1/events", params={"topics": ""}, headers=_auth(runtime_box["key"]))
    assert resp.status_code == 403


def test_a_runtime_credential_reaches_its_own_machine_runtime_and_session(runtime_box):
    principal = runtime_box["principal"]
    assert principal.is_runtime is True
    for topic in (
        f"machine/{runtime_box['machine']}/assignments",
        f"machine/{runtime_box['machine']}/control",
        f"runtime/{runtime_box['runtime_id']}/assignments",
        f"runtime/{runtime_box['runtime_id']}/status",
        f"session/{runtime_box['session_id']}/stdout",
    ):
        assert policy.resolve_topic(principal, topic) is not None, topic


def test_a_runtime_credential_reaches_nothing_else(client, two_orgs, runtime_box, tmp_path):
    principal = runtime_box["principal"]
    other_machine = _slug("other")
    other = _enrol(client, two_orgs["b"]["org"]["id"], other_machine)
    other_runtime_id = other["runtimes"][0]["id"]
    other_session = client.post(
        f"/v1/runtimes/{other_runtime_id}/sessions",
        json={"tool": "copilot", "workspace_path": str(tmp_path / "other")},
        headers=_auth(other["daemon_key"]),
    ).json()["session_id"]

    for topic in (
        f"org/{two_orgs['a']['org']['id']}/sessions",
        f"org/{two_orgs['a']['org']['id']}/runtimes",
        "org/default/inbox",
        f"issue/{two_orgs['issue_a']['code']}",
        f"machine/{other_machine}/control",
        f"runtime/{other_runtime_id}/status",
        f"session/{other_session}/stdout",
    ):
        assert policy.resolve_topic(principal, topic) is None, topic


def test_a_runtime_credential_for_another_orgs_machine_authorizes_nothing(client, two_orgs):
    """Defence in depth: the machine binding alone must not be enough."""
    machine = _slug("victim")
    _enrol(client, two_orgs["a"]["org"]["id"], machine)
    _record, forged = creds.mint_runtime_credential(
        org_id=two_orgs["b"]["org"]["id"], machine_id=machine, label="squatter"
    )
    principal = principal_for_secret(forged)
    assert principal.owns_machine(machine) is True, "the machine binding alone would pass"
    assert policy.resolve_topic(principal, f"machine/{machine}/control") is None


def test_a_multi_org_machine_does_not_leak_through_the_default_org(client, two_orgs):
    """An ambiguous machine must not collapse to the Org everyone can read.

    A machine whose Runtimes straddle two Orgs declares both. Reading that as
    "the default Org" would hand a default-Org member a successful grant, which
    is exactly the answer an unknown machine id is refused for.
    """
    machine = _slug("straddle")
    _enrol(client, two_orgs["a"]["org"]["id"], machine)
    assert policy.machine_declared_org_id(machine) == two_orgs["a"]["org"]["id"]
    with SessionLocal() as session:
        from brains.storage.models import Runtime

        session.add(
            Runtime(
                slug=_slug("straddle-rt"),
                machine_id=machine,
                org_id=two_orgs["b"]["org"]["id"],
                tool="claude",
            )
        )
        session.commit()
    assert policy.machine_declared_org_ids(machine) == {
        two_orgs["a"]["org"]["id"],
        two_orgs["b"]["org"]["id"],
    }
    # Ambiguous: the single-Org helper refuses to pick one.
    assert policy.machine_declared_org_id(machine) is None

    _record, key, _headers = _operator(_slug("defaultonly"))
    default_member = principal_for_secret(key)
    client.post(
        "/v1/orgs/default/members",
        json={"operator_id": default_member.operator_slug, "role": "member"},
        headers=ADMIN_AUTH,
    )
    creds.sync_local_credentials()
    principal = principal_for_secret(key)
    assert principal.can_see_org(policy.default_org_id()) is True

    # Indistinguishable from a machine that does not exist.
    assert policy.resolve_topic(principal, f"machine/{machine}/control") is None
    assert policy.resolve_topic(principal, f"machine/{_slug('nope')}/control") is None
    # An owner of both Orgs still reaches it.
    assert policy.resolve_topic(
        principal_for_secret(settings.api_key), f"machine/{machine}/control"
    )


# --------------------------------------------------------------------------- #
# Writing to a topic is not the same permission as reading it
# --------------------------------------------------------------------------- #


def test_chat_send_is_refused_on_a_readable_non_chat_topic(client, two_orgs):
    org_topic = f"org/{two_orgs['a']['org']['id']}/issues"
    with _ws(client, two_orgs["a"]["key"]) as socket:
        assert _subscribe(socket, [org_topic])["subscribed"] == [org_topic]
        socket.send_json(
            {"type": "chat.send", "topic": org_topic, "payload": {"text": "forged"}, "ref": "c"}
        )
        frame = socket.receive_json()
    assert frame["type"] == "error"
    assert frame["payload"]["error"]["code"] == "topic_forbidden"


def test_chat_send_error_never_names_the_topic(client, two_orgs):
    with _ws(client, two_orgs["b"]["key"]) as socket:
        socket.send_json({"type": "chat.send", "topic": "session/AS-does-not-exist/chat"})
        frame = socket.receive_json()
    assert frame["type"] == "error"
    assert "AS-does-not-exist" not in json.dumps(frame)


def test_chat_send_reaches_the_sessions_own_chat_topic(client, runtime_box):
    topic = f"session/{runtime_box['session_id']}/chat"
    with _ws(client, settings.api_key) as socket:
        assert _subscribe(socket, [topic])["subscribed"] == [topic]
        socket.send_json({"type": "chat.send", "topic": topic, "payload": {"text": "hi"}})
        frames = [socket.receive_json(), socket.receive_json()]
    kinds = {frame["type"] for frame in frames}
    assert "ack" in kinds
    assert "chat.message" in kinds


# --------------------------------------------------------------------------- #
# Re-authorization while the connection is live
# --------------------------------------------------------------------------- #


def test_a_revoked_credential_loses_the_socket_on_its_next_message(client, two_orgs):
    key = two_orgs["a"]["key"]
    org_topic = f"org/{two_orgs['a']['org']['id']}/issues"
    with pytest.raises(WebSocketDisconnect), _ws(client, key) as socket:
        assert _subscribe(socket, [org_topic])["subscribed"] == [org_topic]
        creds.revoke_credential(principal_for_secret(key).credential_id)
        socket.send_json({"type": "ping", "ref": "p"})
        socket.receive_json()


def test_a_revoked_credential_loses_the_socket_without_sending_anything(client, two_orgs):
    """The revalidation timer, not the next client message, closes the socket."""
    key = two_orgs["a"]["key"]
    org_topic = f"org/{two_orgs['a']['org']['id']}/issues"
    with _ws(client, key) as socket:
        assert _subscribe(socket, [org_topic])["subscribed"] == [org_topic]
        creds.revoke_credential(principal_for_secret(key).credential_id)
        frame = socket.receive_json()
        assert frame["type"] == "realtime.revoked"
        assert frame["payload"]["reason"] == "credential_revoked"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_losing_a_membership_drops_the_topics_it_granted(client, two_orgs):
    """A membership removed mid-connection takes its stream with it."""
    org = two_orgs["a"]["org"]
    _record, key, _headers = _operator(_slug("guest"))
    principal = principal_for_secret(key)
    assert (
        client.post(
            f"/v1/orgs/{org['slug']}/members",
            json={"operator_id": principal.operator_slug, "role": "member"},
            headers=two_orgs["a"]["headers"],
        ).status_code
        == 200
    )
    creds.sync_local_credentials()
    topic = f"org/{org['id']}/issues"
    with _ws(client, key) as socket:
        assert _subscribe(socket, [topic])["subscribed"] == [topic]
        client.delete(
            f"/v1/orgs/{org['slug']}/members/{principal.operator_slug}",
            headers=two_orgs["a"]["headers"],
        )
        frame = socket.receive_json()
    assert frame["type"] == "realtime.revoked"
    assert frame["payload"]["reason"] == "scope_revoked"
    assert frame["payload"]["topics"] == [topic]
    # The credential itself is still valid; only what it may read changed.
    assert principal_for_secret(key) is not None


def _database_is_down(*_args, **_kwargs):
    """A store read that fails the way a busy or unreachable database does."""
    raise OperationalError("SELECT 1", {}, Exception("database is locked"))


def test_a_revalidation_that_cannot_run_closes_the_socket(client, two_orgs, monkeypatch):
    """A failed check is not a passed check.

    The revalidation loop exists to notice a revoked credential or a withdrawn
    scope. If the read it depends on raises - the store is unreachable, a
    policy query fails - the socket must not keep streaming behind a question
    nobody answered, and the loop must not die quietly and leave it running.
    """
    key = two_orgs["a"]["key"]
    topic = f"org/{two_orgs['a']['org']['id']}/issues"
    with _ws(client, key) as socket:
        assert _subscribe(socket, [topic])["subscribed"] == [topic]
        monkeypatch.setattr(policy, "subscription_scope", _database_is_down)
        frame = socket.receive_json()
        assert frame["type"] == "realtime.revoked"
        assert frame["payload"]["reason"] == "revalidation_failed"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_a_failed_credential_read_stops_the_stream_and_leaves_no_task(
    client, two_orgs, monkeypatch
):
    """The same for the credential lookup - and nothing keeps running after.

    A revalidation task that outlived its socket would go on re-resolving a
    credential for a connection that no longer exists, which is exactly how a
    revoked scope ends up streaming indefinitely.
    """
    key = two_orgs["a"]["key"]
    topic = f"org/{two_orgs['a']['org']['id']}/issues"
    lookups: list[float] = []

    def _failing(secret):
        lookups.append(time.monotonic())
        _database_is_down()

    with _ws(client, key) as socket:
        assert _subscribe(socket, [topic])["subscribed"] == [topic]
        monkeypatch.setattr(ws_module, "principal_for_secret", _failing)
        frame = socket.receive_json()
        assert frame["type"] == "realtime.revoked"
        assert frame["payload"]["reason"] == "revalidation_failed"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    settled = len(lookups)
    assert settled >= 1
    # Several revalidation intervals with the socket gone: a surviving task
    # would have re-resolved the credential at least once more.
    time.sleep(1.0)
    assert len(lookups) == settled


def test_sse_stops_streaming_when_its_revalidation_cannot_run(two_orgs, monkeypatch):
    """SSE fails closed on the same rule the socket does."""
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")

    async def _run():
        response = await sse_events(_sse_request(two_orgs["a"]["headers"]), topics=topic)
        iterator = response.body_iterator
        try:
            assert (await _read_frames(iterator, 1))[0]["type"] == "realtime.ready"
            monkeypatch.setattr(policy, "subscription_scope", _database_is_down)
            frames = await _read_frames(iterator, 1)
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(iterator.__anext__(), timeout=10)
            return frames
        finally:
            await iterator.aclose()

    frames = asyncio.run(_run())
    assert frames[-1]["type"] == "realtime.revoked"
    assert frames[-1]["payload"]["reason"] == "revalidation_failed"


# --------------------------------------------------------------------------- #
# Delivery is filtered on the event's own scope, not only on its topic
# --------------------------------------------------------------------------- #


def test_subscription_scope_drops_an_out_of_scope_envelope():
    scope = SubscriptionScope(org_ids=frozenset({7}), workspace_ids=None)
    assert scope.allows({"org_id": 7}) is True
    assert scope.allows({"org_id": 8}) is False
    assert scope.allows({"org_id": None}) is True


def test_an_org_a_payload_on_an_org_b_topic_never_reaches_an_org_b_subscriber():
    async def run():
        b = EventBus()
        sub = b.subscribe(["org/2/issues"], SubscriptionScope(org_ids=frozenset({2})))
        b.publish("org/2/issues", "issue.created", org_id=1, payload={"leak": True})
        b.publish("org/2/issues", "issue.created", org_id=2, payload={"leak": False})
        got = await asyncio.wait_for(sub.get(), timeout=1.0)
        assert got["payload"] == {"leak": False}
        assert sub.queue.empty()

    asyncio.run(run())


def test_replay_drops_an_out_of_scope_row():
    org_id, topic = _scratch_topic()
    event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": 1})
    assert event_store.replay([topic], 0, org_ids={org_id + 1}).events == []
    assert event_store.replay([topic], 0, org_ids={org_id}).events != []


# --------------------------------------------------------------------------- #
# The durable log: persist before publish, cursor, replay, gap, idempotency
# --------------------------------------------------------------------------- #


def _rows(topic: str) -> list[RealtimeEvent]:
    with SessionLocal() as session:
        return list(
            session.query(RealtimeEvent)
            .filter(RealtimeEvent.topic == topic)
            .order_by(RealtimeEvent.id.asc())
        )


#: Store-level tests publish on their own Org id so they never collide with an
#: Org a neighbouring test created, or with each other.
_SCRATCH_ORG = itertools.count(9_000_001)


def _scratch_topic(channel: str = "issues") -> tuple[int, str]:
    org_id = next(_SCRATCH_ORG)
    return org_id, grammar.org_topic(org_id, channel)


def test_publish_commits_the_row_before_it_announces(monkeypatch):
    """The bus only ever sees an event the store already holds."""
    org_id, topic = _scratch_topic()
    seen: list[int] = []

    original = event_store.bus.deliver

    def _spy(envelope):
        seen.append(len(_rows(topic)))
        return original(envelope)

    monkeypatch.setattr(event_store.bus, "deliver", _spy)
    event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": 1})
    assert seen == [1]


def test_a_publish_that_cannot_be_recorded_announces_nothing(monkeypatch):
    org_id, topic = _scratch_topic()
    announced: list[dict] = []
    monkeypatch.setattr(event_store.bus, "deliver", lambda envelope: announced.append(envelope))
    monkeypatch.setattr(
        event_store, "record_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no disk"))
    )
    assert event_store.publish_durable(topic, "issue.created", org_id=org_id) is None
    assert announced == []


def test_event_ids_are_monotonic_and_carried_on_the_envelope():
    org_id, topic = _scratch_topic()
    first = event_store.publish_durable(topic, "issue.created", org_id=org_id)
    second = event_store.publish_durable(topic, "issue.updated", org_id=org_id)
    assert first["durable"] is True
    assert second["event_id"] > first["event_id"]
    assert event_store.latest_event_id() >= second["event_id"]


def test_replay_returns_what_was_missed_after_a_restart():
    """A cursor survives the process: the notifier is in memory, the log is not."""
    org_id, topic = _scratch_topic()
    baseline = event_store.latest_event_id()
    for n in range(3):
        event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": n})

    # A fresh bus is exactly what a restarted (or second) process holds: no
    # history at all. The catch-up therefore has to come from the store.
    assert EventBus().subscriber_count() == 0
    result = event_store.replay([topic], baseline)
    assert [event["payload"]["n"] for event in result.events] == [0, 1, 2]
    assert result.gap is False
    assert result.cursor >= result.events[-1]["event_id"]

    # Resuming from the reported cursor delivers nothing twice.
    assert event_store.replay([topic], result.cursor).events == []


def test_the_reported_cursor_never_runs_ahead_of_what_was_delivered():
    """A cursor that jumped past another topic's events would retire them unseen."""
    _mine_org, mine = _scratch_topic()
    other_org, other = _scratch_topic()
    baseline = event_store.latest_event_id()
    event_store.publish_durable(mine, "issue.created", org_id=_mine_org, payload={"n": 0})
    # Traffic on a topic this caller does not hold, committed afterwards.
    event_store.publish_durable(other, "issue.created", org_id=other_org, payload={"n": 1})

    result = event_store.replay([mine], baseline)
    assert [event["payload"]["n"] for event in result.events] == [0]
    assert result.cursor == result.events[-1]["event_id"]
    assert result.cursor < event_store.latest_event_id()

    # A caller holding no topic at all keeps the cursor it came with.
    assert event_store.replay([], baseline).cursor == baseline


def test_a_cursor_ahead_of_the_store_is_a_reset():
    result = event_store.replay([_scratch_topic()[1]], event_store.latest_event_id() + 50)
    assert result.gap is True
    assert result.reason == event_store.RESET_CURSOR_AHEAD


def test_a_negative_cursor_is_a_reset():
    assert event_store.replay([_scratch_topic()[1]], -1).reason == (event_store.RESET_CURSOR_AHEAD)


def test_no_cursor_starts_live_without_replaying_history():
    org_id, topic = _scratch_topic()
    event_store.publish_durable(topic, "issue.created", org_id=org_id)
    result = event_store.replay([topic], None)
    assert result.events == []
    assert result.gap is False
    assert result.cursor == event_store.latest_event_id()


def test_a_backlog_larger_than_the_bound_is_a_reset_not_a_short_replay(monkeypatch):
    monkeypatch.setenv("BRAINS_REALTIME_REPLAY_LIMIT", "2")
    org_id, topic = _scratch_topic()
    baseline = event_store.latest_event_id()
    for n in range(5):
        event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": n})
    result = event_store.replay([topic], baseline)
    assert len(result.events) == 2
    assert result.gap is True
    assert result.reason == event_store.RESET_REPLAY_TRUNCATED
    # The cursor stops at the last event actually delivered.
    assert result.cursor == result.events[-1]["event_id"]


def test_a_pruned_cursor_is_a_reset(monkeypatch):
    org_id, topic = _scratch_topic()
    baseline = event_store.latest_event_id()
    for n in range(6):
        event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": n})
    monkeypatch.setenv("BRAINS_REALTIME_RETENTION_ROWS", "2")
    assert event_store.prune() > 0
    result = event_store.replay([topic], baseline)
    assert result.gap is True
    assert result.reason == event_store.RESET_CURSOR_EXPIRED


def test_an_expired_cursor_with_nothing_left_moves_forward(monkeypatch):
    """Handing the dead cursor back would expire again on the next resume."""
    _quiet_org, quiet = _scratch_topic()
    busy_org, busy = _scratch_topic()
    baseline = event_store.latest_event_id()
    for n in range(6):
        event_store.publish_durable(busy, "issue.created", org_id=busy_org, payload={"n": n})
    monkeypatch.setenv("BRAINS_REALTIME_RETENTION_ROWS", "2")
    assert event_store.prune() > 0

    result = event_store.replay([quiet], baseline)
    assert result.gap is True
    assert result.reason == event_store.RESET_CURSOR_EXPIRED
    assert result.events == []
    assert result.cursor == event_store.latest_event_id()
    # Resuming from the reported cursor no longer reports a gap.
    assert event_store.replay([quiet], result.cursor).gap is False


def test_retention_of_zero_keeps_everything(monkeypatch):
    monkeypatch.setenv("BRAINS_REALTIME_RETENTION_ROWS", "0")
    assert event_store.retention_rows() == 0
    assert event_store.prune() == 0


def test_a_duplicate_publish_writes_one_row_and_announces_once(monkeypatch):
    org_id, topic = _scratch_topic()
    announced: list[dict] = []
    original = event_store.bus.deliver

    def _spy(envelope):
        announced.append(envelope)
        return original(envelope)

    monkeypatch.setattr(event_store.bus, "deliver", _spy)
    key = f"issue.created:{uuid.uuid4().hex}"
    first = event_store.publish_durable(topic, "issue.created", org_id=org_id, dedupe_key=key)
    second = event_store.publish_durable(topic, "issue.created", org_id=org_id, dedupe_key=key)
    assert first["event_id"] == second["event_id"]
    assert len(announced) == 1
    assert len(_rows(topic)) == 1


def test_concurrent_publishers_get_unique_monotonic_ids():
    org_id, topic = _scratch_topic()
    baseline = event_store.latest_event_id()

    def _publish(n: int):
        return event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": n})

    with ThreadPoolExecutor(max_workers=8) as pool:
        envelopes = [env for env in pool.map(_publish, range(16)) if env]
    ids = sorted(env["event_id"] for env in envelopes)
    assert len(envelopes) == 16
    assert len(set(ids)) == 16
    assert all(event_id > baseline for event_id in ids)
    assert len(_rows(topic)) == 16


def test_concurrent_duplicates_collapse_to_one_row():
    org_id, topic = _scratch_topic()
    key = f"issue.created:{uuid.uuid4().hex}"

    def _publish(_n: int):
        return event_store.publish_durable(topic, "issue.created", org_id=org_id, dedupe_key=key)

    with ThreadPoolExecutor(max_workers=8) as pool:
        envelopes = [env for env in pool.map(_publish, range(8)) if env]
    assert len({env["event_id"] for env in envelopes}) == 1
    assert len(_rows(topic)) == 1


# --------------------------------------------------------------------------- #
# The publishers resolve their own scope
# --------------------------------------------------------------------------- #


def test_a_session_event_ignores_the_org_the_caller_passed(client, runtime_box):
    """The old ``org_id or 'default'`` fallback published across Org lines."""
    from brains.api.realtime_publish import publish_session

    session_id = runtime_box["session_id"]
    real_org = policy.session_org_id(session_id)
    assert real_org is not None
    publish_session(999_999, "session.updated", {"session_id": session_id})
    mine = [
        row for row in _rows(grammar.org_topic(real_org, "sessions")) if row.entity_id == session_id
    ]
    assert mine, "the event must land on the Session's own Org topic"
    assert mine[-1].org_id == real_org
    assert _rows(grammar.org_topic(999_999, "sessions")) == []
    # ... and on the Session's own stream, so a console watching one Session
    # does not have to subscribe to the whole Org.
    assert _rows(grammar.session_topic(session_id, "state"))


def test_a_runtime_event_is_published_on_its_real_org(client, two_orgs, runtime_box):
    from brains.api.realtime_publish import publish_runtime

    org_id = two_orgs["a"]["org"]["id"]
    runtime_id = runtime_box["runtime_id"]
    publish_runtime(999_999, "runtime.updated", {"id": runtime_id})
    mine = [
        row
        for row in _rows(grammar.org_topic(org_id, "runtimes"))
        if row.entity_id == str(runtime_id)
    ]
    assert mine
    assert mine[-1].org_id == org_id
    assert _rows(grammar.runtime_topic(runtime_id, "status"))
    assert _rows(grammar.org_topic(999_999, "runtimes")) == []


def test_a_heartbeat_is_not_written_to_the_durable_log(client, two_orgs, runtime_box):
    """Liveness ticks would churn the shared replay window for everyone."""
    from brains.api.runtimes import _publish_runtime

    org_id = two_orgs["a"]["org"]["id"]
    before = len(_rows(grammar.org_topic(org_id, "runtimes")))
    _publish_runtime("runtime.heartbeat", {"id": runtime_box["runtime_id"], "org_id": org_id})
    assert len(_rows(grammar.org_topic(org_id, "runtimes"))) == before


@pytest.mark.parametrize(
    "payload_for",
    [
        lambda issue: issue,
        lambda issue: {"issue": issue["code"], "comment": {"body": "ORG-A-ONLY"}},
        lambda issue: {"issue": issue, "status": "done"},
    ],
    ids=["row", "comment-wrapper", "row-wrapper"],
)
def test_an_issue_event_never_falls_back_to_the_default_org(client, two_orgs, payload_for):
    """A payload that only *names* the Issue must still resolve its real Org.

    Falling back to the install's default Org would publish one Org's Issue
    body onto a topic every default-Org member is allowed to read.
    """
    from brains.api.realtime_publish import publish_issue

    issue = two_orgs["issue_a"]
    org_id = two_orgs["a"]["org"]["id"]
    default_org = policy.default_org_id()
    assert default_org != org_id

    before = len(_rows(grammar.org_topic(default_org, "issues")))
    publish_issue("issue.commented", payload_for(issue))
    assert len(_rows(grammar.org_topic(default_org, "issues"))) == before
    assert [row.org_id for row in _rows(grammar.org_topic(org_id, "issues"))][-1] == org_id
    assert _rows(grammar.issue_topic(issue["code"]))


def test_an_issue_whose_org_cannot_be_resolved_publishes_nothing(client):
    from brains.api.realtime_publish import publish_issue

    default_org = policy.default_org_id()
    before = len(_rows(grammar.org_topic(default_org, "issues")))
    publish_issue("issue.updated", {"title": "no code, no project"})
    assert len(_rows(grammar.org_topic(default_org, "issues"))) == before


# --------------------------------------------------------------------------- #
# The transports resume by cursor, and agree with each other
# --------------------------------------------------------------------------- #


def test_ws_subscribe_replays_from_a_cursor(client, two_orgs):
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = event_store.latest_event_id()
    for n in range(3):
        event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": n})

    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, [topic], cursor=baseline)
        frames = [socket.receive_json() for _ in range(3)]
    assert ack["subscribed"] == [topic]
    assert [frame["payload"]["n"] for frame in frames] == [0, 1, 2]
    assert all(frame["replayed"] is True for frame in frames)
    assert all(frame["event_id"] for frame in frames)
    assert ack["cursor"] >= frames[-1]["event_id"] or ack["cursor"] == baseline


def test_ws_signals_a_reset_rather_than_a_silent_gap(client, two_orgs):
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    with _ws(client, two_orgs["a"]["key"]) as socket:
        _subscribe(socket, [topic], cursor=event_store.latest_event_id() + 100)
        frame = socket.receive_json()
    assert frame["type"] == "realtime.reset"
    assert frame["payload"]["reason"] == event_store.RESET_CURSOR_AHEAD


def _seed(topic: str, org_id: int, count: int) -> int:
    """Publish ``count`` durable events on ``topic``; return the cursor before."""
    baseline = event_store.latest_event_id()
    for n in range(count):
        event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": n})
    return baseline


def test_the_ack_cursor_never_advances_past_the_frames_it_precedes(client, two_orgs):
    """The ack is written before the replay, so it may not retire it.

    An ack that reported where the *store* is would let a client that
    disconnects mid-batch reconnect past everything it was never handed.
    """
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = _seed(topic, org_id, 3)

    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, [topic], cursor=baseline)
        frames = [socket.receive_json() for _ in range(3)]
        receipt = socket.receive_json()

    assert ack["cursor"] == baseline
    assert ack["covers_connection"] is True
    assert [frame["payload"]["n"] for frame in frames] == [0, 1, 2]
    # The receipt is last, so holding it means the whole batch arrived.
    assert receipt["type"] == "replay_complete"
    assert receipt["payload"]["cursor"] == frames[-1]["event_id"]
    assert receipt["payload"]["covers_connection"] is True
    assert receipt["payload"]["count"] == 3


def test_an_interrupted_replay_resumes_from_the_last_applied_event(client, two_orgs):
    """A client that dies mid-batch gets the remainder, not the tail."""
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = _seed(topic, org_id, 4)

    with _ws(client, two_orgs["a"]["key"]) as socket:
        assert _subscribe(socket, [topic], cursor=baseline)["cursor"] == baseline
        first = socket.receive_json()
    assert first["payload"]["n"] == 0

    with _ws(client, two_orgs["a"]["key"]) as socket:
        _subscribe(socket, [topic], cursor=first["event_id"])
        rest = [socket.receive_json() for _ in range(3)]
        receipt = socket.receive_json()
    assert [frame["payload"]["n"] for frame in rest] == [1, 2, 3]
    assert all(frame["replayed"] is True for frame in rest)
    assert receipt["payload"]["cursor"] == rest[-1]["event_id"]


def test_a_resync_ack_reports_the_cursor_the_client_sent(client, two_orgs):
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    with _ws(client, two_orgs["a"]["key"]) as socket:
        _subscribe(socket, [topic])
        baseline = _seed(topic, org_id, 2)
        [socket.receive_json() for _ in range(2)]  # the live copies
        socket.send_json({"type": "resync", "cursor": baseline, "ref": "s"})
        ack = socket.receive_json()
        replayed = [socket.receive_json() for _ in range(2)]
        receipt = socket.receive_json()
    assert ack["cursor"] == baseline
    assert ack["covers_connection"] is True
    assert receipt["type"] == "replay_complete"
    assert receipt["payload"]["cursor"] == replayed[-1]["event_id"]
    assert receipt["payload"]["covers_connection"] is True


def test_an_incremental_subscribes_receipt_hands_over_no_cursor(client, two_orgs):
    """A batch for one added topic may not move a cursor the whole socket holds.

    The console is live on the Org's issues and adds its sessions stream. That
    catch-up carries ids above anything the issues topic has queued, so its
    receipt reports what it wrote and hands over nothing: the client stays where
    its delivered frames left it, and its next reconnect - which reads every
    topic - settles the difference.
    """
    org_id = two_orgs["a"]["org"]["id"]
    issues = grammar.org_topic(org_id, "issues")
    sessions = grammar.org_topic(org_id, "sessions")

    with _ws(client, two_orgs["a"]["key"]) as socket:
        first = _subscribe(socket, [issues])
        baseline = event_store.latest_event_id()
        for n in range(2):
            event_store.publish_durable(
                sessions, "session.started", org_id=org_id, payload={"n": n}
            )
        added = _subscribe(socket, [sessions], cursor=baseline, ref="add")
        replayed = [socket.receive_json() for _ in range(2)]
        receipt = socket.receive_json()

    # The first subscribe *is* the whole connection; the second is not.
    assert first["covers_connection"] is True
    assert added["covers_connection"] is False
    assert added["cursor"] == baseline
    assert [frame["payload"]["n"] for frame in replayed] == [0, 1]
    assert receipt["type"] == "replay_complete"
    assert receipt["payload"]["cursor"] is None
    assert receipt["payload"]["covers_connection"] is False
    # Reporting, not permission: what the batch wrote, for a client that wants
    # to know how far the topic it just added has caught up.
    assert receipt["payload"]["batch_cursor"] == replayed[-1]["event_id"]


def test_a_reconnects_receipt_speaks_for_the_whole_cursor(client, two_orgs):
    """The reconnect reads every topic, so its receipt may retire ids again."""
    org_id = two_orgs["a"]["org"]["id"]
    issues = grammar.org_topic(org_id, "issues")
    sessions = grammar.org_topic(org_id, "sessions")
    baseline = event_store.latest_event_id()
    for n in range(2):
        event_store.publish_durable(issues, "issue.created", org_id=org_id, payload={"n": n})
    event_store.publish_durable(sessions, "session.started", org_id=org_id, payload={"n": 2})

    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, [issues, sessions], cursor=baseline)
        replayed = [socket.receive_json() for _ in range(3)]
        receipt = socket.receive_json()

    assert ack["covers_connection"] is True
    assert receipt["payload"]["cursor"] == replayed[-1]["event_id"]
    assert receipt["payload"]["covers_connection"] is True
    assert "batch_cursor" not in receipt["payload"]


def test_a_client_with_no_cursor_is_told_where_the_store_is(client, two_orgs):
    """Starting live retires nothing, so the ack may report the newest id."""
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    _seed(topic, org_id, 1)
    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, [topic])
    assert ack["cursor"] == event_store.latest_event_id()


def test_ws_resync_replays_without_resubscribing(client, two_orgs):
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    with _ws(client, two_orgs["a"]["key"]) as socket:
        _subscribe(socket, [topic])
        baseline = event_store.latest_event_id()
        event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": 42})
        live = socket.receive_json()
        socket.send_json({"type": "resync", "cursor": baseline, "ref": "s"})
        ack = socket.receive_json()
        replayed = socket.receive_json()
    assert live["payload"]["n"] == 42
    assert ack["type"] == "ack"
    # Live and replay overlap on purpose; a client applies each event once by id.
    assert replayed["event_id"] == live["event_id"]
    assert replayed["replayed"] is True


# --------------------------------------------------------------------------- #
# A catch-up batch is delivered whole, ahead of the live frames it overlapped
# --------------------------------------------------------------------------- #


class _FakeSocket:
    """Records, in order, what one connection's writer put on the wire."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        # A real send suspends. Without a suspension point here the gate would
        # never be exercised, because nothing else could run mid-batch.
        await asyncio.sleep(0)
        self.frames.append(frame)


def _writer(*topics: str) -> tuple[ws_module._Delivery, _FakeSocket]:
    """A ``_Delivery`` over a fake socket, holding ``topics``."""
    socket = _FakeSocket()
    connection = SimpleNamespace(grants=dict.fromkeys(topics, object()))
    return ws_module._Delivery(socket, connection), socket


def _live(event_id: int | None, topic: str = "t") -> dict:
    frame = {"type": "issue.created", "topic": topic}
    if event_id is not None:
        frame |= {"event_id": event_id, "durable": True}
    return frame


def _batch(
    *event_ids: int, cursor: int | None = None, topic: str = "t", gap: bool = False
) -> event_store.ReplayResult:
    events = [{"event_id": event_id, "topic": topic} for event_id in event_ids]
    return event_store.ReplayResult(
        events=events,
        cursor=cursor if cursor is not None else (event_ids[-1] if event_ids else 0),
        gap=gap,
    )


def _mixed_batch(*pairs: tuple[str, int], cursor: int | None = None) -> event_store.ReplayResult:
    """One batch carrying several topics, the shape a multi-topic replay has."""
    events = [{"topic": topic, "event_id": event_id} for topic, event_id in pairs]
    return event_store.ReplayResult(
        events=events,
        cursor=cursor if cursor is not None else (pairs[-1][1] if pairs else 0),
    )


def test_a_live_frame_cannot_be_written_inside_a_replay_batch():
    """The gate holds the pump, and the batch reaches the wire contiguously."""

    async def _run() -> list[dict]:
        delivery, socket = _writer("t")
        waiting = asyncio.Event()

        async def _pump() -> None:
            waiting.set()
            await delivery.send_live(_live(99))

        async with delivery.replay_phase() as phase:
            pump = asyncio.create_task(_pump())
            await waiting.wait()
            # Every chance to jump the queue: the pump is runnable and the
            # batch suspends on each frame it writes.
            for event_id in (1, 2, 3):
                await delivery.send({"type": "issue.created", "event_id": event_id, "replayed": 1})
            await delivery.send({"type": ws_module.REPLAY_COMPLETE_TYPE})
            phase.covered(_batch(1, 2, 3))
        await asyncio.wait_for(pump, timeout=2)
        return socket.frames

    frames = asyncio.run(_run())
    assert [frame.get("event_id") for frame in frames] == [1, 2, 3, None, 99]
    assert frames[3]["type"] == ws_module.REPLAY_COMPLETE_TYPE
    # The live frame is last, and it is the only one that was sequenced.
    assert frames[4]["seq"] == 1


def test_live_delivery_resumes_the_moment_the_batch_closes():
    async def _run() -> list[dict]:
        delivery, socket = _writer("t")
        async with delivery.replay_phase() as phase:
            phase.covered(_batch(1))
        await asyncio.wait_for(delivery.send_live(_live(2)), timeout=2)
        return socket.frames

    assert [frame["event_id"] for frame in asyncio.run(_run())] == [2]


def test_a_live_copy_of_a_replayed_event_is_not_handed_over_twice():
    """The snapshot and the queue overlap: the store commits, then announces."""

    async def _run() -> tuple[list[bool], list[dict]]:
        delivery, socket = _writer("t")
        async with delivery.replay_phase() as phase:
            # The reported cursor runs ahead of the frames, over an id this
            # connection's scope dropped. Suppressing up to *that* would swallow
            # event 6, which was never handed over at all.
            phase.covered(_batch(5, cursor=7))
        sent = [
            await delivery.send_live(_live(5)),
            await delivery.send_live(_live(6)),
            await delivery.send_live(_live(None)),
        ]
        return sent, socket.frames

    sent, frames = asyncio.run(_run())
    assert sent == [False, True, True]
    assert [frame.get("event_id") for frame in frames] == [6, None]
    # A dropped frame consumes no sequence number: the client's stream stays dense.
    assert [frame["seq"] for frame in frames] == [1, 2]


def test_a_frame_queued_for_a_topic_the_connection_lost_is_dropped():
    """Unsubscribe and a withdrawn membership both take effect on the queue."""

    async def _run() -> tuple[list[bool], list[dict]]:
        delivery, socket = _writer("kept")
        sent = [
            await delivery.send_live(_live(1, topic="gone")),
            await delivery.send_live(_live(2, topic="kept")),
        ]
        return sent, socket.frames

    sent, frames = asyncio.run(_run())
    assert sent == [False, True]
    assert [frame["event_id"] for frame in frames] == [2]


# --------------------------------------------------------------------------- #
# Suppression is per topic: a catch-up for one topic is no evidence about
# another topic's queue
# --------------------------------------------------------------------------- #


def test_a_replay_for_a_new_topic_never_suppresses_another_topics_queue():
    """The regression: a connection adds a topic while an old one is live.

    A console live on ``a`` subscribes to ``b`` and is handed ``b``'s backlog,
    whose ids sit far above anything ``a`` has produced. Those ids say nothing
    about ``a``: an ``a`` event queued behind the batch is news, not a
    duplicate, and dropping it loses it for good - the client's cursor has
    already moved past it, so no reconnect brings it back.
    """

    async def _run() -> tuple[list[bool], list[dict]]:
        delivery, socket = _writer("a", "b")
        async with delivery.replay_phase() as phase:
            phase.covered(_batch(90, 91, 92, topic="b"), ["b"])
        sent = [
            await delivery.send_live(_live(88, topic="a")),
            await delivery.send_live(_live(91, topic="a")),
            await delivery.send_live(_live(93, topic="a")),
            await delivery.send_live(_live(91, topic="b")),
        ]
        return sent, socket.frames

    sent, frames = asyncio.run(_run())
    assert sent == [True, True, True, False]
    assert [frame["event_id"] for frame in frames] == [88, 91, 93]
    assert [frame["seq"] for frame in frames] == [1, 2, 3]


def test_only_the_ids_a_batch_actually_carried_are_suppressed():
    """Not a watermark: an id the batch skipped has been handed over by nobody."""

    async def _run() -> list[bool]:
        delivery, _ = _writer("t")
        async with delivery.replay_phase() as phase:
            # 6 and 7 were never in the batch - a row this connection's scope
            # dropped, or one committed out of id order - and 8 is past it.
            phase.covered(_batch(5, 9, cursor=12, topic="t"), ["t"])
        return [
            await delivery.send_live(_live(5)),
            await delivery.send_live(_live(6)),
            await delivery.send_live(_live(7)),
            await delivery.send_live(_live(9)),
            await delivery.send_live(_live(12)),
        ]

    assert asyncio.run(_run()) == [False, True, True, False, True]


def test_a_multi_topic_batch_suppresses_each_topic_only_where_it_delivered():
    """One batch, several topics: each topic answers only for its own ids."""

    async def _run() -> list[bool]:
        delivery, _ = _writer("a", "b", "c")
        async with delivery.replay_phase() as phase:
            phase.covered(_mixed_batch(("a", 4), ("b", 5), ("a", 6), cursor=9), ["a", "b", "c"])
        return [
            await delivery.send_live(_live(4, topic="a")),
            await delivery.send_live(_live(5, topic="a")),
            await delivery.send_live(_live(4, topic="b")),
            await delivery.send_live(_live(5, topic="b")),
            await delivery.send_live(_live(6, topic="a")),
            # ``c`` was subscribed and read, and carried nothing at all.
            await delivery.send_live(_live(5, topic="c")),
        ]

    assert asyncio.run(_run()) == [False, True, True, False, False, True]


# --------------------------------------------------------------------------- #
# What a completed batch hands over: the receipt, and only where it may
# --------------------------------------------------------------------------- #


async def _replay_frames(
    result: event_store.ReplayResult,
    topics: list[str],
    reported: int,
    *,
    covers_connection: bool,
) -> tuple[list[dict], int]:
    """Everything ``_send_replay`` writes for one batch, and what it hands over."""
    frames: list[dict] = []

    async def _send(frame: dict) -> None:
        frames.append(frame)

    handed = await ws_module._send_replay(
        _send, result, topics, reported, covers_connection=covers_connection
    )
    return frames, handed


def test_a_full_batchs_receipt_hands_over_the_cursor_it_read():
    """It covered every topic, so ids below it are delivered or out of scope."""
    frames, handed = asyncio.run(
        _replay_frames(_batch(4, 5, cursor=9), ["t"], 3, covers_connection=True)
    )
    receipt = frames[-1]
    assert [frame["event_id"] for frame in frames[:-1]] == [4, 5]
    assert all(frame["replayed"] is True for frame in frames[:-1])
    assert receipt["type"] == ws_module.REPLAY_COMPLETE_TYPE
    assert receipt["payload"] == {
        "cursor": 9,
        "topics": ["t"],
        "count": 2,
        "covers_connection": True,
    }
    assert handed == 9


def test_a_partial_batchs_receipt_hands_over_nothing():
    """The topics it did not read have live frames queued below its ids."""
    frames, handed = asyncio.run(
        _replay_frames(_batch(4, 5, cursor=9), ["t"], 3, covers_connection=False)
    )
    receipt = frames[-1]
    assert [frame["event_id"] for frame in frames[:-1]] == [4, 5]
    assert receipt["payload"] == {
        "cursor": None,
        "topics": ["t"],
        "count": 2,
        "covers_connection": False,
        # What it wrote, reported rather than handed over: even this would
        # retire a queued event on a topic the batch never read.
        "batch_cursor": 5,
    }
    # The connection's own watermark is what it actually put on the wire.
    assert handed == 5


def test_an_empty_batch_retires_only_where_it_spoke_for_the_connection():
    """Nothing was deliverable: every row in the range fell outside the scope."""
    frames, handed = asyncio.run(_replay_frames(_batch(cursor=9), ["t"], 3, covers_connection=True))
    assert [frame["type"] for frame in frames] == [ws_module.REPLAY_COMPLETE_TYPE]
    assert frames[0]["payload"]["cursor"] == 9
    assert frames[0]["payload"]["count"] == 0
    assert handed == 9

    # The same batch, read for one topic of several: it wrote nothing, so there
    # is nothing to report and nothing to retire.
    frames, handed = asyncio.run(
        _replay_frames(_batch(cursor=9), ["t"], 3, covers_connection=False)
    )
    assert frames == []
    assert handed == 3


def test_a_batch_that_adds_nothing_to_the_cursor_sends_no_receipt():
    """A client already at the store's head is told nothing it does not know."""
    frames, handed = asyncio.run(_replay_frames(_batch(cursor=3), ["t"], 3, covers_connection=True))
    assert frames == []
    assert handed == 3


def test_a_gap_is_signalled_ahead_of_the_batch_it_belongs_to():
    """The reset comes first; the frames behind it are the server's own truth."""
    frames, handed = asyncio.run(
        _replay_frames(_batch(7, 8, cursor=9, gap=True), ["t"], 9, covers_connection=True)
    )
    assert frames[0]["type"] == "realtime.reset"
    assert frames[0]["payload"]["cursor"] == 9
    assert [frame["event_id"] for frame in frames[1:]] == [7, 8]
    assert handed == 9

    # A partial batch that reports a gap still hands over nothing: the client
    # re-reads over REST and resumes from the reset's own cursor.
    frames, handed = asyncio.run(
        _replay_frames(_batch(7, 8, cursor=9, gap=True), ["t"], 9, covers_connection=False)
    )
    assert [frame["type"] for frame in frames[:1]] == ["realtime.reset"]
    assert [frame["event_id"] for frame in frames[1:]] == [7, 8]
    assert handed == 9


def test_a_topic_the_replay_skipped_keeps_its_whole_queue():
    """A denied or unreadable topic is delivered nothing, so it suppresses nothing."""

    async def _run() -> list[bool]:
        delivery, _ = _writer("granted", "skipped")
        async with delivery.replay_phase() as phase:
            # The batch was asked for both and returned rows for one: the other
            # was refused, or every row of it fell outside this scope.
            phase.covered(_batch(30, 31, topic="granted"), ["granted", "skipped"])
        return [
            await delivery.send_live(_live(30, topic="skipped")),
            await delivery.send_live(_live(31, topic="skipped")),
            await delivery.send_live(_live(30, topic="granted")),
        ]

    assert asyncio.run(_run()) == [True, True, False]


def test_a_readded_topic_starts_its_suppression_over():
    """Unsubscribe forgets what an earlier subscription was handed."""

    async def _run() -> list[bool]:
        delivery, _ = _writer("t")
        async with delivery.replay_phase() as phase:
            phase.covered(_batch(11, 12, topic="t"), ["t"])
        before = await delivery.send_live(_live(12))
        delivery.forget(["t"])
        async with delivery.replay_phase() as phase:
            phase.covered(_batch(20, topic="t"), ["t"])
        return [before, await delivery.send_live(_live(12)), await delivery.send_live(_live(20))]

    assert asyncio.run(_run()) == [False, True, False]


def test_a_reset_batch_starts_its_topics_suppression_over():
    """A gap tells the client to resynchronise; stale ids prove nothing after it."""

    async def _run() -> list[bool]:
        delivery, _ = _writer("a", "b")
        async with delivery.replay_phase() as phase:
            phase.covered(_mixed_batch(("a", 7), ("b", 8)), ["a", "b"])
        async with delivery.replay_phase() as phase:
            phase.covered(_batch(cursor=400, topic="a", gap=True), ["a"])
        return [
            await delivery.send_live(_live(7, topic="a")),
            # ``b`` was not part of the reset batch and keeps its record.
            await delivery.send_live(_live(8, topic="b")),
            await delivery.send_live(_live(399, topic="a")),
        ]

    assert asyncio.run(_run()) == [True, False, True]


def test_a_duplicate_bus_copy_of_a_replayed_event_is_dropped_every_time():
    """The durable copy can be announced more than once; the batch answers for it."""

    async def _run() -> list[bool]:
        delivery, _ = _writer("t")
        async with delivery.replay_phase() as phase:
            phase.covered(_batch(3, topic="t"), ["t"])
        return [
            await delivery.send_live(_live(3)),
            await delivery.send_live(_live(3)),
            await delivery.send_live(_live(4)),
        ]

    assert asyncio.run(_run()) == [False, False, True]


def test_the_per_topic_record_is_bounded_and_keeps_the_newest_ids():
    """A connection that resyncs all day may not grow without limit.

    Evicting the *oldest* ids is the safe direction: at worst a client is
    handed a duplicate, which it drops by ``event_id``. Evicting the newest
    would drop the live copies still in flight - a loss nothing recovers.
    """
    cap = ws_module.MAX_SUPPRESSED_IDS_PER_TOPIC

    async def _run() -> list[bool]:
        delivery, _ = _writer("t")
        async with delivery.replay_phase() as phase:
            phase.covered(_batch(*range(1, cap + 2), topic="t"), ["t"])
        return [
            await delivery.send_live(_live(cap + 1)),
            await delivery.send_live(_live(cap)),
            await delivery.send_live(_live(1)),
        ]

    assert asyncio.run(_run()) == [False, False, True]


def test_concurrent_multi_topic_batches_never_drop_another_topics_events():
    """Under load: many topics, batches on some, live traffic on all."""
    topics = [f"topic-{index}" for index in range(6)]

    async def _run() -> list[dict]:
        delivery, socket = _writer(*topics)
        published: dict[str, list[int]] = {topic: [] for topic in topics}
        counter = itertools.count(1000)

        async def _pump(topic: str) -> None:
            for _ in range(40):
                event_id = next(counter)
                published[topic].append(event_id)
                await delivery.send_live(_live(event_id, topic=topic))
                await asyncio.sleep(0)

        async def _batches(topic: str) -> None:
            for _ in range(20):
                async with delivery.replay_phase() as phase:
                    await delivery.send({"type": "e", "topic": topic, "replayed": 1})
                    # A batch for one topic, carrying ids far above everything
                    # every other topic is publishing live.
                    phase.covered(_batch(900_000, 900_001, topic=topic), [topic])
                await asyncio.sleep(0)

        await asyncio.wait_for(
            asyncio.gather(
                *(_pump(topic) for topic in topics),
                *(_batches(topic) for topic in topics[:3]),
            ),
            timeout=60,
        )
        for topic in topics:
            delivered = [
                frame["event_id"]
                for frame in socket.frames
                if frame.get("topic") == topic and "event_id" in frame
            ]
            assert delivered == published[topic], f"{topic} lost or reordered a live event"
        return socket.frames

    frames = asyncio.run(_run())
    live = [frame for frame in frames if "event_id" in frame]
    assert len(live) == 6 * 40
    assert [frame["seq"] for frame in live] == list(range(1, len(live) + 1))


def test_a_replay_that_fails_or_is_cancelled_never_strands_live_delivery():
    """The gate is released in a ``finally``, so a dead batch is not a deadlock."""

    async def _run() -> list[dict]:
        delivery, socket = _writer("t")
        with contextlib.suppress(RuntimeError):
            async with delivery.replay_phase():
                raise RuntimeError("the store went away mid-batch")
        await asyncio.wait_for(delivery.send_live(_live(1)), timeout=2)

        held = asyncio.Event()

        async def _batch_that_is_cancelled() -> None:
            async with delivery.replay_phase():
                held.set()
                await asyncio.sleep(3600)

        task = asyncio.create_task(_batch_that_is_cancelled())
        await held.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.wait_for(delivery.send_live(_live(2)), timeout=2)
        return socket.frames

    assert [frame["event_id"] for frame in asyncio.run(_run())] == [1, 2]


def test_concurrent_pumps_and_batches_stay_ordered_and_terminate():
    """Under load the invariant is unchanged: no live frame inside a batch."""

    async def _run() -> list[dict]:
        delivery, socket = _writer("t")
        stop = asyncio.Event()

        async def _pump(start: int) -> None:
            for offset in range(50):
                await delivery.send_live(_live(1000 + start * 100 + offset))
            stop.set()

        async def _batches() -> None:
            for round_number in range(20):
                async with delivery.replay_phase() as phase:
                    for index in range(5):
                        await delivery.send(
                            {"type": "e", "batch": round_number, "index": index, "replayed": 1}
                        )
                    await delivery.send(
                        {"type": ws_module.REPLAY_COMPLETE_TYPE, "batch": round_number}
                    )
                    phase.covered(_batch())
                await asyncio.sleep(0)
                if stop.is_set():
                    return

        await asyncio.wait_for(
            asyncio.gather(*(_pump(n) for n in range(4)), _batches()), timeout=30
        )
        return socket.frames

    frames = asyncio.run(_run())
    live = [frame for frame in frames if "event_id" in frame]
    assert len(live) == 200
    assert [frame["seq"] for frame in live] == list(range(1, 201))
    # Every batch that started reached the wire without a live frame in it.
    for index, frame in enumerate(frames):
        if frame.get("replayed") and frame.get("index") == 0:
            batch = frames[index : index + 6]
            assert [f.get("index") for f in batch[:5]] == [0, 1, 2, 3, 4]
            assert batch[5]["type"] == ws_module.REPLAY_COMPLETE_TYPE


def _publish_inside_the_batch(monkeypatch, topic: str, org_id: int, payload: dict) -> list[dict]:
    """Publish one durable event from *inside* a replay batch, deterministically.

    The publish lands on the bus after the first catch-up frame is written and
    before the last one is, which is the window a live frame used to overtake
    the remainder of the batch through. Hooked at the transport rather than at
    any one implementation of the writer, so the test states the guarantee
    rather than the mechanism.
    """
    original = WebSocket.send_json
    fired: list[dict] = []

    async def _send_json(self, data, mode="text"):
        await original(self, data, mode)
        if isinstance(data, dict) and data.get("replayed") and not fired:
            envelope = event_store.publish_durable(
                topic, "issue.created", org_id=org_id, payload=payload
            )
            fired.append(envelope)

    monkeypatch.setattr(WebSocket, "send_json", _send_json)
    return fired


def test_a_live_event_during_a_replay_never_strands_the_rest_of_the_batch(
    client, two_orgs, monkeypatch
):
    """The regression: a durable event published mid-replay, then a drop.

    A console holds the highest ``event_id`` it applied. Let a live event -
    which by definition carries an id past every frame still to come - be
    written between two catch-up frames, and a console that dies right after
    reconnects past the remainder and never learns those events happened.
    """
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = _seed(topic, org_id, 10)
    fired = _publish_inside_the_batch(monkeypatch, topic, org_id, {"n": "live"})

    with _ws(client, two_orgs["a"]["key"]) as socket:
        _subscribe(socket, [topic], cursor=baseline)
        # Six of ten frames applied, then the console dies: whatever the server
        # put in those six is what its cursor now claims to have covered.
        applied = [socket.receive_json() for _ in range(6)]
    assert fired, "the regression needs a durable publish inside the batch"

    # Exactly what the console would resend: the highest id it applied.
    cursor = max(frame["event_id"] for frame in applied)
    with _ws(client, two_orgs["a"]["key"]) as socket:
        _subscribe(socket, [topic], cursor=cursor)
        # Bounds the read whether or not the catch-up is complete, so a lost
        # event fails the assertion instead of hanging it.
        sentinel = event_store.publish_durable(
            topic, "issue.created", org_id=org_id, payload={"n": "sentinel"}
        )
        rest: list[dict] = []
        while True:
            frame = socket.receive_json()
            if frame.get("event_id") == sentinel["event_id"]:
                break
            if frame["type"] != "replay_complete":
                rest.append(frame)

    delivered = [frame["payload"]["n"] for frame in applied + rest]
    assert delivered == [*range(10), "live"]
    assert [frame["event_id"] for frame in applied + rest] == sorted(
        frame["event_id"] for frame in applied + rest
    )


def test_a_live_event_published_during_a_replay_arrives_after_the_receipt(
    client, two_orgs, monkeypatch
):
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = _seed(topic, org_id, 3)
    fired = _publish_inside_the_batch(monkeypatch, topic, org_id, {"n": "live"})

    with _ws(client, two_orgs["a"]["key"]) as socket:
        _subscribe(socket, [topic], cursor=baseline)
        frames = [socket.receive_json() for _ in range(5)]

    replayed, receipt, live = frames[:3], frames[3], frames[4]
    assert fired
    assert [frame["payload"]["n"] for frame in replayed] == [0, 1, 2]
    assert all(frame["replayed"] is True for frame in replayed)
    assert receipt["type"] == "replay_complete"
    assert receipt["payload"]["cursor"] == replayed[-1]["event_id"]
    assert live["payload"]["n"] == "live"
    assert "replayed" not in live
    assert live["event_id"] > receipt["payload"]["cursor"]


def test_an_event_in_both_the_snapshot_and_the_queue_is_handed_over_once(
    client, two_orgs, monkeypatch
):
    """The subscription is registered before the snapshot is read, on purpose.

    That is what stops a concurrent publish being missed by both paths - and it
    is why one event can be in the batch *and* in the live queue. It is sent
    once.
    """
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = _seed(topic, org_id, 1)
    original = event_store.replay
    fired: list[dict] = []

    def _replay(*args, **kwargs):
        if not fired:
            fired.append(
                event_store.publish_durable(
                    topic, "issue.created", org_id=org_id, payload={"n": "both"}
                )
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(event_store, "replay", _replay)

    with _ws(client, two_orgs["a"]["key"]) as socket:
        _subscribe(socket, [topic], cursor=baseline)
        frames = [socket.receive_json() for _ in range(3)]
        sentinel = event_store.publish_durable(
            topic, "issue.created", org_id=org_id, payload={"n": "sentinel"}
        )
        following = socket.receive_json()

    assert [frame["payload"]["n"] for frame in frames[:2]] == [0, "both"]
    assert frames[2]["type"] == "replay_complete"
    # The live copy of "both" was suppressed: the next frame is the sentinel.
    assert following["event_id"] == sentinel["event_id"]
    assert following["payload"]["n"] == "sentinel"


def _publish_before_the_snapshot(monkeypatch, plan) -> list[dict]:
    """Run ``plan`` inside the replay phase, before the snapshot is read.

    Everything it publishes is therefore committed while live delivery is held,
    so a live envelope sits in the queue with an ``event_id`` *below* the rows
    the batch about to be read will carry. That is the ordering a shared
    watermark gets wrong, and it cannot be produced from the test thread alone.
    """
    original = event_store.replay
    fired: list[dict] = []

    def _replay(*args, **kwargs):
        if not fired:
            fired.extend(plan())
        return original(*args, **kwargs)

    monkeypatch.setattr(event_store, "replay", _replay)
    return fired


def test_a_catch_up_for_a_new_topic_never_drops_a_live_event_on_an_old_one(
    client, two_orgs, monkeypatch
):
    """The regression: a console adds a topic and loses traffic on another.

    Subscriptions are added one at a time. A console live on the Org's issues
    opens a Session and subscribes to its stream, whose catch-up carries ids
    above everything the issues topic has queued. Suppressing by one watermark
    read those queued frames as "already delivered" and dropped them - on a
    topic whose subscription never changed, and past the cursor the client now
    holds, so no reconnect ever brought them back.
    """
    org_id = two_orgs["a"]["org"]["id"]
    issues = grammar.org_topic(org_id, "issues")
    sessions = grammar.org_topic(org_id, "sessions")

    def _plan() -> list[dict]:
        live = event_store.publish_durable(
            issues, "issue.created", org_id=org_id, payload={"n": "live-a"}
        )
        backlog = [
            event_store.publish_durable(
                sessions, "session.started", org_id=org_id, payload={"n": n}
            )
            for n in range(3)
        ]
        return [live, *backlog]

    with _ws(client, two_orgs["a"]["key"]) as socket:
        _subscribe(socket, [issues])
        baseline = event_store.latest_event_id()
        fired = _publish_before_the_snapshot(monkeypatch, _plan)
        _subscribe(socket, [sessions], cursor=baseline, ref="add-b")
        # Published after the ack, so it is queued behind the live issue event:
        # it bounds the read whether or not that event was dropped.
        sentinel = event_store.publish_durable(
            issues, "issue.created", org_id=org_id, payload={"n": "sentinel"}
        )
        frames = []
        while True:
            frame = socket.receive_json()
            frames.append(frame)
            if frame.get("event_id") == sentinel["event_id"]:
                break

    assert fired, "the regression needs a publish inside the replay phase"
    live_event = fired[0]
    replayed, receipt, tail = frames[:3], frames[3], frames[4:]
    assert [frame["payload"]["n"] for frame in replayed] == [0, 1, 2]
    assert {frame["topic"] for frame in replayed} == {sessions}
    assert all(frame["replayed"] is True for frame in replayed)
    assert receipt["type"] == "replay_complete"
    assert receipt["payload"]["topics"] == [sessions]
    # The batch read one of the two topics this socket holds, so it hands over
    # no cursor at all - the live issue event below it is still in the queue.
    assert receipt["payload"]["cursor"] is None
    assert receipt["payload"]["covers_connection"] is False
    # The live issue event is delivered - once, after the batch, and it is not
    # a replay frame. Its id is *below* every id the batch carried.
    assert [frame["event_id"] for frame in tail] == [
        live_event["event_id"],
        sentinel["event_id"],
    ]
    assert live_event["event_id"] < receipt["payload"]["batch_cursor"]
    assert tail[0]["topic"] == issues
    assert "replayed" not in tail[0]

    # Reconnect from the highest id the console applied: nothing owed is
    # missing and nothing already applied comes back.
    applied = max(frame["event_id"] for frame in frames if frame.get("event_id"))
    with _ws(client, two_orgs["a"]["key"]) as socket:
        ack = _subscribe(socket, [issues, sessions], cursor=applied, ref="again")
        after = [
            event_store.publish_durable(
                issues, "issue.created", org_id=org_id, payload={"n": "after-a"}
            ),
            event_store.publish_durable(
                sessions, "session.started", org_id=org_id, payload={"n": "after-b"}
            ),
        ]
        resumed = [socket.receive_json() for _ in range(2)]

    assert ack["cursor"] == applied
    assert [frame["event_id"] for frame in resumed] == [event["event_id"] for event in after]


def _subscribe_amid_traffic(socket, topics, cursor=None, ref="r") -> tuple[dict, list[dict]]:
    """Subscribe on a socket that is already receiving live frames.

    Returns the ack and whatever live frames arrived ahead of it, which on a
    busy connection is not nothing.
    """
    message = {"type": "subscribe", "topics": topics, "ref": ref}
    if cursor is not None:
        message["cursor"] = cursor
    socket.send_json(message)
    ahead: list[dict] = []
    while True:
        frame = socket.receive_json()
        if frame.get("type") == "ack" and frame.get("ref") == ref:
            return frame, ahead
        ahead.append(frame)


def test_adding_a_topic_under_concurrent_publishing_loses_nothing(client, two_orgs):
    """Many sockets add a second topic while both topics are being written.

    Each connection must end up holding every event published on either topic
    after it subscribed - exactly once. A catch-up for the topic being added
    runs concurrently with live delivery on the topic already held, which is
    the crossing this bug lived in.
    """
    org_id = two_orgs["a"]["org"]["id"]
    issues = grammar.org_topic(org_id, "issues")
    sessions = grammar.org_topic(org_id, "sessions")
    connections = 3
    per_topic = 20

    def _publish(topic: str, event_type: str) -> None:
        for n in range(per_topic):
            event_store.publish_durable(topic, event_type, org_id=org_id, payload={"n": n})

    with contextlib.ExitStack() as stack:
        sockets = [
            stack.enter_context(_ws(client, two_orgs["a"]["key"])) for _ in range(connections)
        ]
        for index, socket in enumerate(sockets):
            _subscribe(socket, [issues], ref=f"a{index}")
        baseline = event_store.latest_event_id()
        ahead_of_ack: list[list[dict]] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = [
                pool.submit(_publish, issues, "issue.created"),
                pool.submit(_publish, sessions, "session.started"),
            ]
            for index, socket in enumerate(sockets):
                _, ahead = _subscribe_amid_traffic(
                    socket, [sessions], cursor=baseline, ref=f"b{index}"
                )
                ahead_of_ack.append(ahead)
            for future in writing:
                future.result()
        sentinel = event_store.publish_durable(
            issues, "issue.created", org_id=org_id, payload={"n": "sentinel"}
        )
        streams: list[list[dict]] = []
        for socket, ahead in zip(sockets, ahead_of_ack, strict=True):
            # The live frames that beat the ack are part of this connection's
            # stream, not noise: a busy socket is exactly the case under test.
            frames: list[dict] = list(ahead)
            while True:
                frame = socket.receive_json()
                frames.append(frame)
                if frame.get("event_id") == sentinel["event_id"]:
                    break
            streams.append(frames)

    expected = {baseline + n + 1 for n in range(2 * per_topic + 1)}
    for frames in streams:
        ids = [frame["event_id"] for frame in frames if frame.get("event_id")]
        assert len(ids) == len(set(ids)), "an event was handed over twice"
        assert set(ids) == expected, "a topic's event was lost to another topic's catch-up"


def test_an_unsubscribed_topic_stops_delivering_and_resubscribing_replays_it(client, two_orgs):
    org_id = two_orgs["a"]["org"]["id"]
    issues = grammar.org_topic(org_id, "issues")
    sessions = grammar.org_topic(org_id, "sessions")

    with _ws(client, two_orgs["a"]["key"]) as socket:
        assert sorted(_subscribe(socket, [issues, sessions])["subscribed"]) == sorted(
            [issues, sessions]
        )
        socket.send_json({"type": "unsubscribe", "topics": [issues], "ref": "u"})
        assert socket.receive_json()["ok"] is True
        baseline = event_store.latest_event_id()
        dropped = event_store.publish_durable(
            issues, "issue.created", org_id=org_id, payload={"n": "dropped"}
        )
        kept = event_store.publish_durable(
            sessions, "session.started", org_id=org_id, payload={"n": "kept"}
        )
        live = socket.receive_json()
        # The topic is back, and the event it missed is replayed rather than lost.
        _subscribe(socket, [issues], cursor=baseline, ref="re")
        replayed = socket.receive_json()
        receipt = socket.receive_json()

    assert live["event_id"] == kept["event_id"]
    assert replayed["event_id"] == dropped["event_id"]
    assert replayed["replayed"] is True
    assert receipt["type"] == "replay_complete"


def test_many_connections_replay_and_go_live_without_losing_order(client, two_orgs):
    """Concurrent publishers, concurrent subscribers, one ordering per socket."""
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = _seed(topic, org_id, 6)
    connections = 4
    published = 24

    def _publish() -> None:
        for n in range(published):
            event_store.publish_durable(
                topic, "issue.created", org_id=org_id, payload={"n": f"c{n}"}
            )

    with contextlib.ExitStack() as stack:
        sockets = [
            stack.enter_context(_ws(client, two_orgs["a"]["key"])) for _ in range(connections)
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = [pool.submit(_publish) for _ in range(2)]
            for index, socket in enumerate(sockets):
                _subscribe(socket, [topic], cursor=baseline, ref=f"s{index}")
            for future in writing:
                future.result()
        # Every subscriber is live by now, so this one reaches all of them and
        # bounds the read below whether or not the invariant holds.
        sentinel = event_store.publish_durable(
            topic, "issue.created", org_id=org_id, payload={"n": "sentinel"}
        )
        streams: list[list[dict]] = []
        for socket in sockets:
            frames: list[dict] = []
            while True:
                frame = socket.receive_json()
                frames.append(frame)
                if frame.get("event_id") == sentinel["event_id"]:
                    break
            streams.append(frames)

    expected = {baseline + n + 1 for n in range(2 * published + 6 + 1)}
    for frames in streams:
        ids = [frame["event_id"] for frame in frames if frame.get("event_id")]
        receipts = [i for i, frame in enumerate(frames) if frame["type"] == "replay_complete"]
        assert ids == sorted(ids), "a live frame overtook the batch it overlapped"
        assert len(ids) == len(set(ids)), "an event was handed over twice"
        assert set(ids) == expected, "an event was lost between replay and live"
        # Nothing that was not part of the batch precedes its receipt.
        for receipt in receipts:
            assert all(frames[i].get("replayed") for i in range(receipt) if "event_id" in frames[i])


def test_sse_and_ws_deny_the_same_topics(client, two_orgs):
    denied = f"org/{two_orgs['a']['org']['id']}/issues"
    resp = client.get("/v1/events", params={"topics": denied}, headers=two_orgs["b"]["headers"])
    assert resp.status_code == 403
    # The refusal names no topic: the SSE 403 must not be an existence oracle.
    assert denied not in resp.text
    with _ws(client, two_orgs["b"]["key"]) as socket:
        assert _subscribe(socket, [denied])["denied"] == [denied]


@pytest.mark.parametrize("bad", ["org/*/issues", "org/1/../2/issues", "everything"])
def test_sse_refuses_an_ungrammatical_topic(client, two_orgs, bad):
    resp = client.get("/v1/events", params={"topics": bad}, headers=two_orgs["a"]["headers"])
    assert resp.status_code == 403


def _sse_request(headers: dict):
    """A minimal ASGI ``Request`` for the streaming handler.

    Starlette's ``TestClient`` buffers a whole response before it returns, so
    an endless stream cannot be read through it at all. The 403/refusal paths
    are exercised over the client (they complete); the streaming paths drive
    the handler as the coroutine it is.
    """

    async def _receive():  # pragma: no cover - is_disconnected cancels first
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/events",
            "raw_path": b"/v1/events",
            "root_path": "",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 5555),
            "server": ("testserver", 80),
            "app": app,
        },
        _receive,
    )


def _data_frames(chunk: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]


async def _read_frames(iterator, limit: int) -> list[dict]:
    frames: list[dict] = []
    while len(frames) < limit:
        chunk = await asyncio.wait_for(iterator.__anext__(), timeout=10)
        frames.extend(_data_frames(chunk if isinstance(chunk, str) else chunk.decode()))
    return frames


def _sse_frames(headers: dict, topics: str, *, cursor: str = "", limit: int = 1) -> list[dict]:
    async def _run():
        response = await sse_events(_sse_request(headers), topics=topics, cursor=cursor)
        iterator = response.body_iterator
        try:
            return await _read_frames(iterator, limit)
        finally:
            await iterator.aclose()

    return asyncio.run(_run())


def test_sse_reports_its_derived_topics(two_orgs):
    org = two_orgs["a"]["org"]
    frames = _sse_frames(two_orgs["a"]["headers"], f"org/{org['slug']}/issues")
    assert frames[0]["type"] == "realtime.ready"
    assert frames[0]["payload"]["topics"] == [f"org/{org['id']}/issues"]


def test_sse_replays_from_last_event_id(two_orgs):
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = event_store.latest_event_id()
    event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": 7})
    frames = _sse_frames(
        {**two_orgs["a"]["headers"], "last-event-id": str(baseline)}, topic, limit=2
    )
    assert frames[1]["payload"]["n"] == 7
    assert frames[1]["replayed"] is True
    assert frames[1]["event_id"]


def test_sse_signals_a_reset_rather_than_a_silent_gap(two_orgs):
    org_id = two_orgs["a"]["org"]["id"]
    frames = _sse_frames(
        two_orgs["a"]["headers"],
        grammar.org_topic(org_id, "issues"),
        cursor=str(event_store.latest_event_id() + 100),
        limit=2,
    )
    assert frames[1]["type"] == "realtime.reset"
    assert frames[1]["payload"]["reason"] == event_store.RESET_CURSOR_AHEAD


def test_sse_confirms_a_completed_replay_with_a_resumable_id(two_orgs):
    """The receipt closes the batch and carries the id an EventSource resumes from."""
    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = event_store.latest_event_id()
    event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": 7})
    frames = _sse_frames(
        {**two_orgs["a"]["headers"], "last-event-id": str(baseline)}, topic, limit=3
    )
    assert frames[1]["replayed"] is True
    assert frames[2]["type"] == "replay_complete"
    assert frames[2]["payload"]["cursor"] == frames[1]["event_id"]
    # One stream reads every topic it holds, so its receipt speaks for the
    # whole cursor and may hand one over.
    assert frames[2]["payload"]["covers_connection"] is True
    # Carried as the SSE ``id:`` too, so a browser resumes from it unaided.
    assert frames[2]["event_id"] == frames[1]["event_id"]


def test_sse_closes_a_revoked_credential(two_orgs):
    org_id = two_orgs["a"]["org"]["id"]
    key = two_orgs["a"]["key"]

    async def _run():
        response = await sse_events(
            _sse_request(_auth(key)), topics=grammar.org_topic(org_id, "issues")
        )
        iterator = response.body_iterator
        try:
            assert (await _read_frames(iterator, 1))[0]["type"] == "realtime.ready"
            creds.revoke_credential(principal_for_secret(key).credential_id)
            frames = await _read_frames(iterator, 1)
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(iterator.__anext__(), timeout=10)
            return frames
        finally:
            await iterator.aclose()

    frames = asyncio.run(_run())
    assert frames[-1]["type"] == "realtime.revoked"
    assert frames[-1]["payload"]["reason"] == "credential_revoked"


# --------------------------------------------------------------------------- #
# Store work never runs on the event loop
# --------------------------------------------------------------------------- #


def _watch_thread(seen: list[tuple[str, bool]], target, name: str, monkeypatch) -> None:
    """Record, for every call to ``target.name``, whether a loop was running.

    A threadpool worker has no running loop, so ``get_running_loop`` raising is
    exactly the evidence that the read happened off the event loop. Recording
    it - rather than timing it - keeps the assertion deterministic.
    """
    original = getattr(target, name)

    def _wrapped(*args, **kwargs):
        on_loop = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop = False
        seen.append((name, on_loop))
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, _wrapped)


def test_ws_setup_and_replay_do_no_store_work_on_the_event_loop(client, two_orgs, monkeypatch):
    """Credential, scope, topic and replay reads all happen in a worker.

    Connection *setup* is as blocking as revalidation is: resolving the
    credential, building the Org/Workspace visibility set, deriving topics and
    reading the replay log are four database round trips before a socket
    delivers anything.
    """
    seen: list[tuple[str, bool]] = []
    _watch_thread(seen, ws_module, "principal_for_secret", monkeypatch)
    _watch_thread(seen, policy, "subscription_scope", monkeypatch)
    _watch_thread(seen, policy, "authorize_topics", monkeypatch)
    _watch_thread(seen, event_store, "replay", monkeypatch)

    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    with _ws(client, two_orgs["a"]["key"]) as socket:
        assert _subscribe(socket, [topic], cursor=event_store.latest_event_id())["ok"] is True

    assert {name for name, _ in seen} == {
        "principal_for_secret",
        "subscription_scope",
        "authorize_topics",
        "replay",
    }
    assert [name for name, on_loop in seen if on_loop] == []


def test_sse_setup_and_replay_do_no_store_work_on_the_event_loop(two_orgs, monkeypatch):
    from brains.authz import deps as authz_deps

    seen: list[tuple[str, bool]] = []
    _watch_thread(seen, authz_deps, "resolve_request_principal", monkeypatch)
    _watch_thread(seen, policy, "subscription_scope", monkeypatch)
    _watch_thread(seen, policy, "authorize_topics", monkeypatch)
    _watch_thread(seen, event_store, "replay", monkeypatch)

    org_id = two_orgs["a"]["org"]["id"]
    topic = grammar.org_topic(org_id, "issues")
    baseline = event_store.latest_event_id()
    event_store.publish_durable(topic, "issue.created", org_id=org_id, payload={"n": 1})
    frames = _sse_frames(
        {**two_orgs["a"]["headers"], "last-event-id": str(baseline)}, topic, limit=3
    )

    assert frames[0]["type"] == "realtime.ready"
    assert {name for name, _ in seen} == {
        "resolve_request_principal",
        "subscription_scope",
        "authorize_topics",
        "replay",
    }
    assert [name for name, on_loop in seen if on_loop] == []


def test_a_slow_authorization_does_not_stall_the_event_loop(two_orgs, monkeypatch):
    """One connection's slow store read must not freeze every other socket.

    The heartbeat below is what every *other* connection in the process is:
    something the loop has to keep servicing while this one is being set up.
    Run the authorization inline and it never ticks.
    """
    original = policy.authorize_topics

    def _slow(*args, **kwargs):
        time.sleep(0.4)
        return original(*args, **kwargs)

    monkeypatch.setattr(policy, "authorize_topics", _slow)
    topic = grammar.org_topic(two_orgs["a"]["org"]["id"], "issues")

    async def _run() -> int:
        ticks = 0

        async def _heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(_heartbeat())
        try:
            response = await sse_events(_sse_request(two_orgs["a"]["headers"]), topics=topic)
            await response.body_iterator.aclose()
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        return ticks

    assert asyncio.run(_run()) >= 5


# --------------------------------------------------------------------------- #
# The boolean wrapper keeps agreeing with the resolver
# --------------------------------------------------------------------------- #


def test_authorize_topic_agrees_with_resolve_topic(two_orgs):
    principal = principal_for_secret(two_orgs["a"]["key"])
    topic = f"org/{two_orgs['a']['org']['id']}/issues"
    assert policy.authorize_topic(principal, topic) is True
    assert policy.authorize_topic(principal, f"org/{two_orgs['b']['org']['id']}/issues") is False
    assert policy.authorize_topic(None, topic) is False


def test_a_bootstrap_admin_is_not_a_wildcard_for_the_grammar():
    """Even the install admin cannot name something outside the grammar."""
    admin = principal_for_secret(settings.api_key)
    assert isinstance(admin, Principal)
    assert policy.resolve_topic(admin, "org/*/issues") is None
    assert policy.resolve_topic(admin, "everything") is None
    assert policy.resolve_topic(admin, "issue/ZZ-99999") is None

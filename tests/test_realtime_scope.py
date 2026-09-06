"""Core realtime authorization, replay, and containment acceptance.

Realtime remains a local coordination transport. Historical Issue, Runtime,
machine, Project, Persona, Pod, and automation rows never create subscribable
topics.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from brains.api.ws import sse_events
from brains.authz import credentials as creds
from brains.authz import policy
from brains.authz.resolver import principal_for_secret
from brains.control.operators import add_operator, ensure_admin_operator
from brains.events import store as event_store
from brains.events import topics as grammar
from brains.events.bus import SubscriptionScope
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import RealtimeEvent

_AUTH_SCHEME = "Bea" + "rer"
_SCRATCH_ORG = itertools.count(9_000_001)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"{_AUTH_SCHEME} {key}"}


@pytest.fixture(autouse=True)
def _bootstrap(monkeypatch):
    init_db()
    ensure_admin_operator()
    creds.sync_local_credentials()
    monkeypatch.setenv("BRAINS_REALTIME_REVALIDATE_SECONDS", "0.2")


@pytest.fixture
def client():
    return TestClient(app)


def _new_operator(client: TestClient, prefix: str) -> dict:
    slug = f"{prefix}-{uuid.uuid4().hex[:8]}"
    _record, key = add_operator(slug)
    creds.sync_local_credentials()
    headers = _auth(key)
    org = client.post(
        "/v1/orgs",
        json={"slug": f"org-{uuid.uuid4().hex[:8]}", "name": "Local"},
        headers=headers,
    ).json()
    return {"key": key, "headers": headers, "org": org}


@pytest.fixture
def two_scopes(client):
    """Two synthetic principals used only to prove non-enumerating denial."""
    return {"a": _new_operator(client, "alpha"), "b": _new_operator(client, "beta")}


def _ws(client: TestClient, key: str):
    return client.websocket_connect(f"/v1/ws?access_token={key}")


def _subscribe(socket, topics, cursor=None):
    message = {"type": "subscribe", "topics": topics, "ref": "r"}
    if cursor is not None:
        message["cursor"] = cursor
    socket.send_json(message)
    return socket.receive_json()


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "*",
        "org/*/inbox",
        "org/1/../2/inbox",
        "org/1/issues",
        "org/1/projects",
        "org/1/personas",
        "org/1/pods",
        "org/1/automation",
        "issue/AB-1",
        "machine/box/control",
        "runtime/1/status",
    ],
)
def test_topic_grammar_rejects_malformed_and_withdrawn_families(topic):
    assert grammar.parse_topic(topic) is None


@pytest.mark.parametrize("topic", ["org/1/inbox", "org/1/sessions", "session/abc/state"])
def test_topic_grammar_accepts_only_core_coordination_topics(topic):
    assert grammar.parse_topic(topic) is not None


def test_server_derives_the_canonical_topic(client, two_scopes):
    owner = two_scopes["a"]
    requested = f"org/{owner['org']['slug']}/inbox"
    expected = f"org/{owner['org']['id']}/inbox"
    grant = policy.resolve_topic(principal_for_secret(owner["key"]), requested)
    assert grant is not None
    assert grant.topic == expected
    with _ws(client, owner["key"]) as socket:
        ack = _subscribe(socket, [requested])
    assert ack["subscribed"] == [expected]
    assert ack["aliases"] == {requested: expected}


def test_cross_scope_and_unknown_topics_are_denied_without_enumeration(client, two_scopes):
    owner = two_scopes["a"]
    other = two_scopes["b"]
    forbidden = f"org/{owner['org']['id']}/inbox"
    unknown = "org/999999999/inbox"
    with _ws(client, other["key"]) as socket:
        ack = _subscribe(socket, [forbidden, unknown])
    assert ack["subscribed"] == []
    assert ack["denied"] == [forbidden, unknown]


def test_unauthenticated_websocket_upgrade_is_closed(client):
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/v1/ws") as socket:
        socket.receive_json()
    assert exc.value.code == 4401


def test_revoked_credential_loses_live_websocket(client, two_scopes):
    owner = two_scopes["a"]
    topic = f"org/{owner['org']['id']}/inbox"
    with _ws(client, owner["key"]) as socket:
        assert _subscribe(socket, [topic])["subscribed"] == [topic]
        principal = principal_for_secret(owner["key"])
        creds.revoke_credential(principal.credential_id)
        frame = socket.receive_json()
        assert frame["type"] == "realtime.revoked"
        assert frame["payload"]["reason"] == "credential_revoked"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_subscription_scope_filters_each_envelope():
    scope = SubscriptionScope(org_ids=frozenset({7}), workspace_ids=None)
    assert scope.allows({"org_id": 7}) is True
    assert scope.allows({"org_id": 8}) is False


def _scratch_topic() -> tuple[int, str]:
    org_id = next(_SCRATCH_ORG)
    return org_id, grammar.org_topic(org_id, "inbox")


def _rows(topic: str) -> list[RealtimeEvent]:
    with SessionLocal() as session:
        return list(
            session.query(RealtimeEvent)
            .filter(RealtimeEvent.topic == topic)
            .order_by(RealtimeEvent.id.asc())
        )


def test_publish_commits_before_announcing(monkeypatch):
    org_id, topic = _scratch_topic()
    seen = []
    original = event_store.bus.deliver

    def spy(envelope):
        seen.append(len(_rows(topic)))
        return original(envelope)

    monkeypatch.setattr(event_store.bus, "deliver", spy)
    event_store.publish_durable(topic, "mailbox.message", org_id=org_id)
    assert seen == [1]


def test_failed_persistence_announces_nothing(monkeypatch):
    org_id, topic = _scratch_topic()
    announced = []
    monkeypatch.setattr(event_store.bus, "deliver", announced.append)
    monkeypatch.setattr(
        event_store,
        "record_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )
    assert event_store.publish_durable(topic, "mailbox.message", org_id=org_id) is None
    assert announced == []


def test_replay_is_monotonic_resumable_and_never_runs_ahead():
    org_id, topic = _scratch_topic()
    other_org, other = _scratch_topic()
    baseline = event_store.latest_event_id()
    first = event_store.publish_durable(topic, "mailbox.message", org_id=org_id, payload={"n": 1})
    second = event_store.publish_durable(topic, "mailbox.message", org_id=org_id, payload={"n": 2})
    event_store.publish_durable(other, "mailbox.message", org_id=other_org)
    result = event_store.replay([topic], baseline)
    assert [event["event_id"] for event in result.events] == [first["event_id"], second["event_id"]]
    assert result.cursor == second["event_id"]
    assert result.cursor < event_store.latest_event_id()
    assert event_store.replay([topic], result.cursor).events == []


def test_invalid_and_pruned_cursors_signal_a_gap(monkeypatch):
    org_id, topic = _scratch_topic()
    assert event_store.replay([topic], -1).gap is True
    baseline = event_store.latest_event_id()
    for n in range(6):
        event_store.publish_durable(topic, "mailbox.message", org_id=org_id, payload={"n": n})
    monkeypatch.setenv("BRAINS_REALTIME_RETENTION_ROWS", "2")
    assert event_store.prune() > 0
    result = event_store.replay([topic], baseline)
    assert result.gap is True
    assert result.reason == event_store.RESET_CURSOR_EXPIRED


def test_duplicate_publish_is_idempotent(monkeypatch):
    org_id, topic = _scratch_topic()
    announced = []
    original = event_store.bus.deliver

    def spy(envelope):
        announced.append(envelope)
        return original(envelope)

    monkeypatch.setattr(event_store.bus, "deliver", spy)
    key = f"mailbox.message:{uuid.uuid4().hex}"
    first = event_store.publish_durable(topic, "mailbox.message", org_id=org_id, dedupe_key=key)
    second = event_store.publish_durable(topic, "mailbox.message", org_id=org_id, dedupe_key=key)
    assert first["event_id"] == second["event_id"]
    assert len(announced) == 1
    assert len(_rows(topic)) == 1


def test_concurrent_publishers_get_unique_monotonic_ids():
    org_id, topic = _scratch_topic()

    def publish(n):
        return event_store.publish_durable(
            topic, "mailbox.message", org_id=org_id, payload={"n": n}
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        envelopes = list(pool.map(publish, range(16)))
    ids = sorted(envelope["event_id"] for envelope in envelopes)
    assert len(ids) == len(set(ids)) == 16
    assert ids == sorted(ids)


def _sse_request(headers: dict[str, str]) -> Request:
    async def receive():
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
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "client": ("127.0.0.1", 5555),
            "server": ("testserver", 80),
            "app": app,
        },
        receive,
    )


def _data_frames(chunk: str) -> list[dict]:
    return [json.loads(line[6:]) for line in chunk.splitlines() if line.startswith("data: ")]


async def _read_frames(iterator, count: int) -> list[dict]:
    frames = []
    while len(frames) < count:
        chunk = await asyncio.wait_for(iterator.__anext__(), timeout=10)
        frames.extend(_data_frames(chunk if isinstance(chunk, str) else chunk.decode()))
    return frames


def test_sse_and_websocket_have_matching_scope_denial(client, two_scopes):
    owner = two_scopes["a"]
    other = two_scopes["b"]
    forbidden = f"org/{owner['org']['id']}/inbox"
    response = client.get("/v1/events", params={"topics": forbidden}, headers=other["headers"])
    assert response.status_code == 403
    assert forbidden not in response.text
    with _ws(client, other["key"]) as socket:
        assert _subscribe(socket, [forbidden])["denied"] == [forbidden]


def test_sse_replays_and_closes_a_revoked_credential(two_scopes):
    owner = two_scopes["a"]
    topic = grammar.org_topic(owner["org"]["id"], "inbox")
    baseline = event_store.latest_event_id()
    event_store.publish_durable(
        topic, "mailbox.message", org_id=owner["org"]["id"], payload={"n": 7}
    )

    async def run():
        response = await sse_events(
            _sse_request({**owner["headers"], "last-event-id": str(baseline)}), topics=topic
        )
        iterator = response.body_iterator
        try:
            frames = await _read_frames(iterator, 3)
            creds.revoke_credential(principal_for_secret(owner["key"]).credential_id)
            frames.extend(await _read_frames(iterator, 1))
            return frames
        finally:
            await iterator.aclose()

    frames = asyncio.run(run())
    assert frames[0]["type"] == "realtime.ready"
    assert frames[1]["payload"]["n"] == 7
    assert frames[1]["replayed"] is True
    assert frames[2]["type"] == "replay_complete"
    assert frames[-1]["type"] == "realtime.revoked"

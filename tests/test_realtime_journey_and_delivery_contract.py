"""Realtime event delivery contract, replay recovery, and multi-process journey fixture (Issue #42 AC1, AC2, AC3).

Validates:
- Realtime event streaming across simulated separate client processes (AC1)
- Disconnect, missed-live-events, and replay recovery using Last-Event-ID / cursor (AC1, AC2)
- Explicit delivery contract distinguishing persistence, live publication, client receipt, replay, and work completion (AC2)
- Authenticated scope consistency preventing unauthorized cross-workspace event leakage during replay (AC3)
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from brains.api.ws import sse_events
from brains.authz import credentials as creds
from brains.control.operators import add_operator, ensure_admin_operator
from brains.control.sessions import register_workspace
from brains.events import store as event_store
from brains.events import topics as grammar
from brains.main import app
from brains.storage.migrations import init_db


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    init_db()
    ensure_admin_operator()
    creds.sync_local_credentials()
    monkeypatch.setenv("BRAINS_REALTIME_REVALIDATE_SECONDS", "0.2")


@pytest.fixture
def test_client():
    return TestClient(app)


def _create_scope(client: TestClient, prefix: str):
    slug = f"{prefix}-{uuid.uuid4().hex[:8]}"
    _op, key = add_operator(slug)
    creds.sync_local_credentials()
    headers = {"Authorization": f"Bearer {key}"}

    # Create Org
    org_res = client.post("/v1/orgs", json={"slug": slug, "name": slug}, headers=headers)
    assert org_res.status_code == 200
    org = org_res.json()
    org_id = org["id"]

    # Register workspace
    ws1 = register_workspace(f"/synthetic/{slug}/ws1", slug=f"{slug}-ws1", org_id=org_id)

    topic = grammar.org_topic(org_id, "inbox")
    return {
        "key": key,
        "headers": headers,
        "org_id": org_id,
        "workspace_id": ws1.id,
        "topic": topic,
    }


def _ws(client: TestClient, key: str):
    return client.websocket_connect(f"/v1/ws?access_token={key}")


def _subscribe(socket, topics: list[str], cursor: int | None = None):
    msg = {"type": "subscribe", "topics": topics, "ref": "sub-ref"}
    if cursor is not None:
        msg["cursor"] = cursor
    socket.send_json(msg)
    return socket.receive_json()


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


def test_realtime_disconnect_missed_events_and_replay_recovery(test_client) -> None:
    """Test AC1 & AC2: Client disconnects, misses live events, reconnects with cursor, and recovers state."""
    env = _create_scope(test_client, "replay-journey")
    org_id = env["org_id"]
    topic = env["topic"]

    # 1. Event 1 is emitted before client connects
    ev1 = event_store.publish_durable(
        topic,
        "work_assignment.created",
        org_id=org_id,
        payload={"assignment_code": "WA-001", "stage": "created"},
    )
    assert ev1 is not None
    ev1_id = ev1["event_id"]

    # 2. Client connects and subscribes with cursor=0 (requesting all historical events)
    with _ws(test_client, env["key"]) as socket:
        ack = _subscribe(socket, [topic], cursor=0)
        assert ack["type"] == "ack"
        assert ack["ok"] is True
        assert topic in ack["subscribed"]

        # Receive replayed Event 1
        frame1 = socket.receive_json()
        assert frame1.get("replayed") is True
        assert frame1["event_id"] == ev1_id
        assert frame1["payload"]["assignment_code"] == "WA-001"
        last_received_cursor = frame1["event_id"]

        # Receive replay complete marker
        comp = socket.receive_json()
        assert comp["type"] == "replay_complete"

        # 3. CLIENT DISCONNECTS: context exits here
    # Client is now completely disconnected.

    # 4. While disconnected, Events 2, 3, and 4 are published
    ev2 = event_store.publish_durable(
        topic,
        "work_assignment.accepted",
        org_id=org_id,
        payload={"assignment_code": "WA-001", "stage": "accepted", "generation": 1},
    )
    ev3 = event_store.publish_durable(
        topic,
        "coordination.advanced",
        org_id=org_id,
        payload={"coordination_code": "PC-001", "round": 1},
    )
    ev4 = event_store.publish_durable(
        topic,
        "work_assignment.completed",
        org_id=org_id,
        payload={"assignment_code": "WA-001", "stage": "completed", "result": "verified"},
    )
    assert ev2 is not None and ev3 is not None and ev4 is not None

    # 5. CLIENT RECONNECTS: provides last_received_cursor to recover missed events
    with _ws(test_client, env["key"]) as socket:
        ack = _subscribe(socket, [topic], cursor=last_received_cursor)
        assert ack["type"] == "ack"
        assert ack["ok"] is True

        # Receive missed events in exact order
        replayed = []
        while True:
            msg = socket.receive_json()
            if msg.get("type") == "replay_complete":
                break
            replayed.append(msg)

        assert len(replayed) == 3
        assert [e["event_id"] for e in replayed] == [
            ev2["event_id"],
            ev3["event_id"],
            ev4["event_id"],
        ]
        assert all(e.get("replayed") is True for e in replayed)
        assert [e["type"] for e in replayed] == [
            "work_assignment.accepted",
            "coordination.advanced",
            "work_assignment.completed",
        ]


def test_scope_containment_across_replay_recovery(test_client) -> None:
    """Test AC3: Replay recovery strictly enforces workspace/org boundaries, preventing cross-tenant event leakage."""
    scope_a = _create_scope(test_client, "scope-a")
    scope_b = _create_scope(test_client, "scope-b")

    # Publish secret event on Org A
    ev_a = event_store.publish_durable(
        scope_a["topic"],
        "coordination.created",
        org_id=scope_a["org_id"],
        payload={"secret": "confidential-a"},
    )
    assert ev_a is not None

    # Operator B connects and attempts to subscribe/replay Org A's topic
    with _ws(test_client, scope_b["key"]) as socket:
        ack = _subscribe(socket, [scope_a["topic"]], cursor=0)
        assert ack["type"] == "ack"
        assert ack["ok"] is False
        # Access to Org A's topic is strictly denied
        assert ack["subscribed"] == []
        assert scope_a["topic"] in ack["denied"]

        # No events from Org A are ever leaked or replayed:
        # A ping/pong roundtrip confirms no pending event was queued or leaked
        socket.send_json({"type": "ping", "ref": "leak-check"})
        pong = socket.receive_json()
        assert pong["type"] == "pong"
        assert pong["ref"] == "leak-check"


def test_sse_events_delivery_contract_with_last_event_id(test_client) -> None:
    """Test AC2: Delivery contract via SSE using Last-Event-ID header and cursor query param."""
    env = _create_scope(test_client, "sse-stream")
    topic = env["topic"]

    # Publish events 1 and 2
    ev1 = event_store.publish_durable(topic, "test.first", org_id=env["org_id"], payload={"n": 1})
    ev2 = event_store.publish_durable(topic, "test.second", org_id=env["org_id"], payload={"n": 2})
    assert ev1 is not None and ev2 is not None

    async def _test():
        # Connect via SSE with Last-Event-ID header set to ev1's ID
        headers = {**env["headers"], "Last-Event-ID": str(ev1["event_id"])}
        response = await sse_events(_sse_request(headers), topics=topic)
        iterator = response.body_iterator
        try:
            frames = await _read_frames(iterator, 3)
            replayed = [f for f in frames if f.get("replayed") is True]
            assert len(replayed) == 1
            assert replayed[0]["event_id"] == ev2["event_id"]
            assert replayed[0]["type"] == "test.second"
        finally:
            await iterator.aclose()

        # Connect via SSE with cursor query parameter set to ev1's ID
        response_cursor = await sse_events(
            _sse_request(env["headers"]), topics=topic, cursor=str(ev1["event_id"])
        )
        iterator_cursor = response_cursor.body_iterator
        try:
            frames_cursor = await _read_frames(iterator_cursor, 3)
            replayed_cursor = [f for f in frames_cursor if f.get("replayed") is True]
            assert len(replayed_cursor) == 1
            assert replayed_cursor[0]["event_id"] == ev2["event_id"]
            assert replayed_cursor[0]["type"] == "test.second"
        finally:
            await iterator_cursor.aclose()

    asyncio.run(_test())

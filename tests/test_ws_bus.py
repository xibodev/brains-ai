"""Tests for the in-process event bus + WebSocket realtime surface (WS3 §3).

* Bus unit: publish → subscriber receives the WS3 envelope; topic isolation.
* WebSocket: auth required (reject no-key); subscribe → receive a published
  envelope through ``TestClient.websocket_connect``.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brains.events.bus import EventBus, bus
from brains.main import app


@pytest.fixture(autouse=True)
def _bootstrap():
    from brains.storage.migrations import init_db

    init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Bus unit tests
# --------------------------------------------------------------------------- #


def test_publish_delivers_to_subscriber():
    async def run():
        b = EventBus()
        sub = b.subscribe(["org/1/issues"])
        env = b.publish("org/1/issues", "issue.created", entity="issue", id=7, payload={"a": 1})
        got = await asyncio.wait_for(sub.get(), timeout=1.0)
        assert got["type"] == "issue.created"
        assert got["topic"] == "org/1/issues"
        assert got["entity"] == "issue"
        assert got["id"] == 7
        assert got["payload"] == {"a": 1}
        assert got["v"] == 1
        assert got["seq"] == env["seq"]

    asyncio.run(run())


def test_publish_topic_isolation():
    async def run():
        b = EventBus()
        sub = b.subscribe(["org/1/issues"])
        b.publish("org/2/issues", "issue.created")  # different topic
        b.publish("org/1/issues", "issue.updated")
        got = await asyncio.wait_for(sub.get(), timeout=1.0)
        assert got["type"] == "issue.updated"
        assert sub.queue.empty()

    asyncio.run(run())


def test_subscribe_unsubscribe():
    async def run():
        b = EventBus()
        sub = b.subscribe([])
        sub.add(["session/AS-1/stdout"])
        b.publish("session/AS-1/stdout", "session.stdout", payload={"chunk": "hi"})
        got = await asyncio.wait_for(sub.get(), timeout=1.0)
        assert got["payload"]["chunk"] == "hi"
        sub.remove(["session/AS-1/stdout"])
        b.publish("session/AS-1/stdout", "session.stdout")
        assert sub.queue.empty()

    asyncio.run(run())


def test_seq_is_monotonic():
    b = EventBus()
    a = b.publish("t", "x")
    c = b.publish("t", "x")
    assert c["seq"] == a["seq"] + 1


# --------------------------------------------------------------------------- #
# WebSocket tests
# --------------------------------------------------------------------------- #


def test_ws_rejects_without_key(client):
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/v1/ws"):
        pass
    assert exc.value.code == 4401


def test_ws_subscribe_and_receive(client):
    with client.websocket_connect("/v1/ws?access_token=local-dev-key") as ws:
        ws.send_json({"type": "subscribe", "topics": ["org/1/inbox"], "ref": "c1"})
        ack = ws.receive_json()
        assert ack["type"] == "ack"
        assert ack["ref"] == "c1"
        assert ack["ok"] is True
        # Publish from the test thread; the WS handler's subscription delivers
        # cross-thread via call_soon_threadsafe.
        bus.publish(
            "org/1/inbox",
            "mailbox.message",
            entity="mailbox",
            id=42,
            payload={"subject": "Ready"},
        )
        frame = ws.receive_json()
        assert frame["type"] == "mailbox.message"
        assert frame["id"] == 42
        assert frame["payload"]["subject"] == "Ready"
        assert frame["seq"] == 1  # per-connection counter


def test_ws_unknown_message_returns_error(client):
    with client.websocket_connect("/v1/ws?access_token=local-dev-key") as ws:
        ws.send_json({"type": "bogus", "ref": "x"})
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["payload"]["error"]["code"] == "unknown_ws_message"


def test_ws_accepts_bearer_header(client):
    with client.websocket_connect(
        "/v1/ws", headers={"authorization": "Bearer local-dev-key"}
    ) as ws:
        ws.send_json({"type": "ping", "ref": "p"})
        frame = ws.receive_json()
        assert frame["type"] == "pong"

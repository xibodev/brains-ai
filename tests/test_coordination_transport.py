"""Real locked FastMCP protocol over in-process ASGI, with synthetic credentials/DB.

Unlike direct-dispatch surface unit tests, these initialize SDK sessions and send
JSON-RPC tools/call through SDK validation, task dispatch and HTTP authentication.
No listener, provider, host configuration or external network is involved.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from brains.api.auth import reset_rate_limit_state
from brains.authz import resolver
from brains.config import settings
from brains.control import coordination as peer
from brains.control.operators import add_operator
from brains.control.sessions import start_session
from brains.mcp import server
from brains.storage.db import SessionLocal
from brains.storage.models import AgentSession, SessionLease


@pytest.fixture
def world(tmp_path, monkeypatch):
    workspace = str(tmp_path / "transport-peers")
    actors = [
        start_session(workspace, tool=tool, operator="admin")["session_id"]
        for tool in ("opencode", "codex", "opencode")
    ]
    _, foreign_key = add_operator(f"transport-{uuid4().hex[:10]}")
    owner_key = settings.api_key
    principal = resolver.principal_for_secret(owner_key)
    foreign = resolver.principal_for_secret(foreign_key)
    assert principal and foreign and principal.operator_id != foreign.operator_id
    handle = resolver.current_principal.set(principal)
    try:
        row = peer.propose_coordination(
            workspace,
            "Synthetic private transport review",
            {
                "version": 1,
                "objective": "Private synthetic objective",
                "context": "Inert synthetic context",
                "evidence_expectations": "Synthetic evidence",
                "participants": [{"session_id": actor, "model": None} for actor in actors[1:]],
            },
            session_id=actors[0],
            idempotency_key="transport",
        )
        row = peer.accept_coordination(
            row["code"],
            session_id=actors[1],
            version=1,
            expected_revision=0,
            spec_hash=row["spec_hash"],
        )
        assert row["revision"] == row["version"] == 1
    finally:
        resolver.current_principal.reset(handle)
    monkeypatch.setattr(settings, "allow_unauthenticated_api", False)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    reset_rate_limit_state()
    return workspace, actors[0], row, owner_key, foreign_key


def lease(actor):
    with SessionLocal() as db:
        row = db.get(AgentSession, actor)
        live = db.get(SessionLease, actor)
        return row.last_activity_at, live.lease_expires_at, live.renewed_at


class FakeMCPClient:
    """Small JSON-RPC peer; the server side is the real FastMCP ASGI app."""

    def __init__(self, app, key):
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:9877",
            headers={"Accept": "application/json, text/event-stream"},
        )
        self.key = key
        self.session_id = None
        self.next_id = 0

    async def request(self, method, params=None, *, key=None, notification=False):
        self.next_id += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self.next_id
        if params is not None:
            body["params"] = params
        headers = {"Authorization": f"Bearer {key or self.key}"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = "2025-11-25"
        response = await self.http.post(server.MCP_STREAMABLE_HTTP_PATH, json=body, headers=headers)
        if method == "initialize" and response.status_code == 200:
            self.session_id = response.headers["Mcp-Session-Id"]
        return response

    @staticmethod
    def message(response):
        if "text/event-stream" in response.headers.get("content-type", ""):
            messages = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ") and line[6:].strip()
            ]
            return next(message for message in messages if "id" in message)
        return response.json()

    async def initialize(self):
        response = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-transport-probe", "version": "1"},
            },
        )
        assert response.status_code == 200, response.text
        assert self.message(response)["result"]["protocolVersion"] == "2025-11-25"
        assert self.session_id
        response = await self.request("notifications/initialized", notification=True)
        assert response.status_code == 202

    async def call(self, action, args, *, key=None):
        return await self.request(
            "tools/call",
            {
                "name": f"brains_coordination_{action}",
                "arguments": args,
            },
            key=key,
        )


class FakeSSEClient(FakeMCPClient):
    """Consume a long-lived ASGI SSE response without httpx buffering its GET."""

    def __init__(self, app, key):
        super().__init__(app, key)
        self.app = app
        self.events = asyncio.Queue()
        self.endpoint = None
        self.buffer = ""

    async def listen(self):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": server.MCP_LEGACY_SSE_PATH,
            "raw_path": server.MCP_LEGACY_SSE_PATH.encode(),
            "query_string": b"",
            "root_path": "",
            "server": ("127.0.0.1", 9877),
            "client": ("127.0.0.1", 1234),
            "headers": [
                (b"host", b"127.0.0.1:9877"),
                (b"accept", b"text/event-stream"),
                (b"authorization", f"Bearer {self.key}".encode()),
            ],
        }

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 200
            elif message["type"] == "http.response.body":
                self.buffer += message.get("body", b"").decode().replace("\r\n", "\n")
                while "\n\n" in self.buffer:
                    event, self.buffer = self.buffer.split("\n\n", 1)
                    data = "\n".join(
                        line[6:] for line in event.splitlines() if line.startswith("data: ")
                    )
                    if data:
                        await self.events.put(data)

        await self.app(scope, receive, send)

    async def request(self, method, params=None, *, key=None, notification=False):
        self.next_id += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self.next_id
        if params is not None:
            body["params"] = params
        response = await self.http.post(
            self.endpoint,
            json=body,
            headers={"Authorization": f"Bearer {key or self.key}"},
        )
        if response.status_code != 202 or notification:
            return response
        message = json.loads(await self.events.get())
        assert message["id"] == self.next_id
        return httpx.Response(200, json=message)

    async def initialize(self):
        self.endpoint = await self.events.get()
        assert "session_id=" in self.endpoint
        # The legacy session id is delivered by the endpoint event, not a header.
        self.session_id = self.endpoint.split("session_id=", 1)[1]
        await super().initialize()


@asynccontextmanager
async def connection(world, *, empty_slot=False, mode="streamable-http"):
    app = server._build_http_app(mode, "127.0.0.1")
    inner = app.app
    if empty_slot:
        # Integration probe of an unfilled request carrier; intentionally bypass
        # the credential middleware to exercise the core's fail-closed fallback.
        async def app(scope, receive, send):
            with resolver.principal_slot():
                await inner(scope, receive, send)

    client = FakeSSEClient(app, world[3]) if mode == "sse" else FakeMCPClient(app, world[3])
    async with inner.router.lifespan_context(inner), client.http:
        async with asyncio.timeout(15):
            listener = asyncio.create_task(client.listen()) if mode == "sse" else None
            try:
                await client.initialize()
                yield client
            finally:
                if listener:
                    listener.cancel()
                    with suppress(asyncio.CancelledError):
                        await listener


def arguments(world, action):
    workspace, actor, row, _, _ = world
    if action == "list":
        return {"workspace_path": workspace, "session_id": actor}
    args = {"code": row["code"], "session_id": actor}
    if action == "cancel":
        args.update(version=1, expected_revision=1, reason="Synthetic cancellation")
    return args


def assert_denied(response, world):
    assert response.status_code in (200, 403, 404), response.text
    if response.status_code == 200:
        message = FakeMCPClient.message(response)
        assert message.get("error") or message.get("result", {}).get("isError"), message
    assert world[2]["title"] not in response.text
    assert "Private synthetic objective" not in response.text


def assert_success(response, world, action):
    assert response.status_code == 200, response.text
    result = FakeMCPClient.message(response)["result"]
    assert not result.get("isError"), result
    assert world[2]["code"] in json.dumps(result)
    if action == "cancel":
        assert '"cancelled"' in json.dumps(result).replace('\\"', '"')


@pytest.mark.parametrize("action", ["get", "list", "cancel"])
@pytest.mark.parametrize("mode", ["streamable-http", "sse"])
def test_http_session_cannot_capture_another_operators_authority(world, action, mode):
    async def scenario():
        async with connection(world, mode=mode) as client:
            before = lease(world[1])
            response = await client.call(action, arguments(world, action), key=world[4])
            assert_denied(response, world)
            assert lease(world[1]) == before
            response = await client.call(action, arguments(world, action))
            assert_success(response, world, action)
            if action == "cancel":
                assert lease(world[1]) != before

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["get", "cancel"])
@pytest.mark.parametrize("mode", ["streamable-http", "sse"])
def test_http_empty_request_slot_cannot_bootstrap(world, action, mode):
    async def scenario():
        async with connection(world, empty_slot=True, mode=mode) as client:
            before = lease(world[1])
            assert_denied(await client.call(action, arguments(world, action)), world)
            assert lease(world[1]) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["version", "expected_revision"])
@pytest.mark.parametrize("mode", ["streamable-http", "sse"])
def test_http_rejects_boolean_revision_before_sdk_coercion(world, field, mode):
    async def scenario():
        async with connection(world, mode=mode) as client:
            before = lease(world[1])
            args = arguments(world, "cancel")
            args[field] = True
            assert_denied(await client.call("cancel", args), world)
            assert lease(world[1]) == before
            assert_success(await client.call("cancel", arguments(world, "cancel")), world, "cancel")

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["streamable-http", "sse"])
def test_foreign_operator_own_transport_still_cannot_use_owner_session(world, mode):
    async def scenario():
        foreign_world = (*world[:3], world[4], world[3])
        async with connection(foreign_world, mode=mode) as client:
            before = lease(world[1])
            assert_denied(await client.call("get", arguments(world, "get")), world)
            assert_denied(await client.call("cancel", arguments(world, "cancel")), world)
            assert lease(world[1]) == before

    asyncio.run(scenario())


def test_coordination_sdk_strict_integer_schemas_keep_public_contract(world):
    async def scenario():
        async with connection(world) as client:
            response = await client.request("tools/list")
            advertised = {
                tool["name"]: tool for tool in client.message(response)["result"]["tools"]
            }
            for action in ("propose", "get", "list", "accept", "advance", "submit", "cancel"):
                name = f"brains_coordination_{action}"
                properties = advertised[name]["inputSchema"]["properties"]
                for field in ("version", "expected_revision", "limit"):
                    if field in properties:
                        schema = properties[field]
                        assert schema.get("type") == "integer" or {
                            entry["type"] for entry in schema.get("anyOf", [])
                        } == {"integer", "null"}
            before = lease(world[1])
            for action, extra in (("get", {"version": True}), ("list", {"limit": True})):
                args = {**arguments(world, action), **extra}
                assert_denied(await client.call(action, args), world)
            assert lease(world[1]) == before

    asyncio.run(scenario())


def test_protocol_probe_uses_locked_sdk_versions():
    import tomllib

    lock = tomllib.loads((Path(__file__).parents[1] / "uv.lock").read_text())
    locked = {package["name"]: package["version"] for package in lock["package"]}
    for package in ("mcp", "fastapi", "starlette", "pydantic"):
        assert version(package) == locked[package]

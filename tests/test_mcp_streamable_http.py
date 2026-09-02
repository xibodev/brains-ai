"""Protocol acceptance tests for the canonical Streamable HTTP MCP endpoint."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from brains.api.auth import reset_rate_limit_state
from brains.config import settings
from brains.mcp.server import _build_http_app
from brains.mcp.transport import MCP_MODE_STREAMABLE_HTTP
from brains.service.common import _mcp_protocol_handshake, mcp_protocol_status


@pytest.fixture
def streamable_http_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Serve the real ASGI app on an ephemeral loopback port.

    The fixture is intended for isolated test environments. It never uses the
    installed Brains ports, state directory, service, or client configuration.
    """
    monkeypatch.setattr(settings, "allow_unauthenticated_api", False)
    reset_rate_limit_state()
    app = _build_http_app(MCP_MODE_STREAMABLE_HTTP, "127.0.0.1")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on", access_log=False)
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])),
        daemon=True,
        name="test-mcp-streamable-http",
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("ephemeral Streamable HTTP MCP server did not start")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), "ephemeral MCP server did not stop"


def test_streamable_http_protocol_auth_and_host_contract(
    streamable_http_url: str,
) -> None:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "auth-probe", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}

    response = httpx.post(streamable_http_url, json=request, headers=headers)
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"

    response = httpx.post(
        streamable_http_url,
        json=request,
        headers={
            **headers,
            "Host": "attacker.example",
            "Authorization": f"Bearer {settings.api_key}",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Host header: 'attacker.example'"

    rejected = "synthetic-rejected-credential"
    rejected_report = asyncio.run(
        _mcp_protocol_handshake(streamable_http_url, rejected, 3.0)
    )
    assert rejected_report["ready"] is False
    assert rejected_report["stage"] == "authentication"
    assert rejected_report["status_code"] == 401
    assert rejected not in repr(rejected_report)

    async def exercise() -> None:
        headers = {"Authorization": f"Bearer {settings.api_key}"}
        async with (
            httpx.AsyncClient(headers=headers) as http_client,
            streamable_http_client(streamable_http_url, http_client=http_client) as streams,
        ):
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "Brains v2"
                result = await session.list_tools()
                assert "brains_start_session" in {tool.name for tool in result.tools}

    asyncio.run(exercise())
    host_port = streamable_http_url.removeprefix("http://").split("/", 1)[0]
    host, raw_port = host_port.rsplit(":", 1)
    report = mcp_protocol_status(host, int(raw_port))
    assert report["ready"] is True
    assert report["stage"] == "ready"
    assert report["tool_count"] > 0

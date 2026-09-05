"""Protocol acceptance tests for the canonical Streamable HTTP MCP endpoint."""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import brains.service.common as service_common
from brains.api.auth import reset_rate_limit_state
from brains.config import settings
from brains.mcp.server import _build_http_app
from brains.mcp.transport import MCP_MODE_STREAMABLE_HTTP
from brains.service.common import _mcp_protocol_handshake, mcp_protocol_status


@contextmanager
def _serve_streamable_http() -> Iterator[str]:
    """Serve the real app on an ephemeral port and tear it down completely."""
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


@pytest.fixture
def streamable_http_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Serve the real ASGI app without touching an installed Brains instance."""
    monkeypatch.setattr(settings, "allow_unauthenticated_api", False)
    reset_rate_limit_state()
    with _serve_streamable_http() as url:
        yield url


async def _initialize_and_list(url: str) -> set[str]:
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    async with (
        httpx.AsyncClient(headers=headers) as http_client,
        streamable_http_client(url, http_client=http_client) as streams,
    ):
        read_stream, write_stream, _ = streams
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Brains v2"
            result = await session.list_tools()
            return {tool.name for tool in result.tools}


@contextmanager
def _serve_streamable_http_subprocess() -> Iterator[str]:
    """Run one fresh server process, matching a supervised-service restart."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    url = f"http://127.0.0.1:{port}/mcp"
    process = subprocess.Popen(  # noqa: S603 - fixed module and synthetic ephemeral port
        [
            sys.executable,
            "-m",
            "brains.mcp.server",
            "--mode",
            MCP_MODE_STREAMABLE_HTTP,
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        report: dict[str, object] = {"ready": False}
        while process.poll() is None and time.monotonic() < deadline:
            report = asyncio.run(_mcp_protocol_handshake(url, settings.api_key, 1.0))
            if report["ready"]:
                break
            time.sleep(0.05)
        if not report["ready"]:
            pytest.fail(f"MCP subprocess did not become ready: {report}")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_streamable_http_protocol_auth_and_host_contract(
    streamable_http_url: str,
    monkeypatch: pytest.MonkeyPatch,
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
    rejected_report = asyncio.run(_mcp_protocol_handshake(streamable_http_url, rejected, 3.0))
    assert rejected_report["ready"] is False
    assert rejected_report["stage"] == "authentication"
    assert rejected_report["status_code"] == 401
    assert rejected not in repr(rejected_report)

    assert "brains_start_session" in asyncio.run(_initialize_and_list(streamable_http_url))
    host_port = streamable_http_url.removeprefix("http://").split("/", 1)[0]
    host, raw_port = host_port.rsplit(":", 1)
    report = mcp_protocol_status(host, int(raw_port))
    assert report["ready"] is True
    assert report["stage"] == "ready"
    assert report["tool_count"] > 0

    from brains.control.readiness import mcp_protocol_readiness

    monkeypatch.setattr(
        service_common,
        "read_service_config",
        lambda: {"gateway_host": host, "mcp_port": int(raw_port)},
    )
    readiness = mcp_protocol_readiness()
    assert readiness["ready"] is True
    assert readiness["tool_count"] > 0


@pytest.mark.acceptance
def test_streamable_http_reconnects_after_restart_and_rejects_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real SDK client across a complete isolated server restart."""
    monkeypatch.setattr(settings, "allow_unauthenticated_api", False)
    reset_rate_limit_state()

    with _serve_streamable_http_subprocess() as first_url:
        assert "brains_start_session" in asyncio.run(_initialize_and_list(first_url))
        legacy_url = first_url.removesuffix("/mcp") + "/sse"
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "legacy-path-probe", "version": "1"},
            },
        }
        response = httpx.post(
            legacy_url,
            json=request,
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {settings.api_key}",
            },
        )
        assert response.status_code in {404, 405}
        legacy_report = asyncio.run(_mcp_protocol_handshake(legacy_url, settings.api_key, 3.0))
        assert legacy_report["ready"] is False

    with _serve_streamable_http_subprocess() as restarted_url:
        assert restarted_url != first_url or restarted_url.endswith("/mcp")
        assert "brains_start_session" in asyncio.run(_initialize_and_list(restarted_url))
        host_port = restarted_url.removeprefix("http://").split("/", 1)[0]
        host, raw_port = host_port.rsplit(":", 1)
        assert mcp_protocol_status(host, int(raw_port))["ready"] is True

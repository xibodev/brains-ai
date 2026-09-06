"""Real-process readiness journeys for the retained HTTP control gateway."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI

import brains.service.common as service_common
from brains.config import RUNTIME_OVERLAY_SCHEMA_VERSION
from brains.control.readiness import gateway_protocol_readiness


@contextmanager
def _serve(app: FastAPI) -> Iterator[int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on", access_log=False)
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])),
        daemon=True,
        name="test-gateway-readiness",
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("ephemeral gateway did not start")
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "ephemeral gateway did not stop"


def _configure(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    monkeypatch.setattr(
        service_common,
        "read_service_config",
        lambda: {"gateway_host": "127.0.0.1", "gateway_port": port, "mcp_port": 1},
    )


def test_shipped_gateway_health_and_auth_boundary_are_ready(monkeypatch):
    from brains.main import app

    with _serve(app) as port:
        _configure(monkeypatch, port)
        report = gateway_protocol_readiness()

    assert report == {
        "ready": True,
        "stage": "ready",
        "reason": "health-and-auth-boundary-succeeded",
        "health_status_code": 200,
        "auth_status_code": 401,
    }


def test_gateway_unavailable_is_real_connection_failure(monkeypatch):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = int(reserved.getsockname()[1])
        _configure(monkeypatch, port)
        assert gateway_protocol_readiness() == {
            "ready": False,
            "stage": "connect",
            "reason": "endpoint-unavailable",
        }


def test_wrong_http_service_is_rejected(monkeypatch):
    wrong = FastAPI()

    @wrong.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    with _serve(wrong) as port:
        _configure(monkeypatch, port)
        report = gateway_protocol_readiness()
    assert report == {"ready": False, "stage": "health", "reason": "wrong-service"}


def test_gateway_with_broken_auth_boundary_is_rejected(monkeypatch):
    broken = FastAPI()

    @broken.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "schema_version": RUNTIME_OVERLAY_SCHEMA_VERSION}

    @broken.get("/v1/admin/readiness")
    def unprotected() -> dict[str, str]:
        return {"status": "ready"}

    with _serve(broken) as port:
        _configure(monkeypatch, port)
        report = gateway_protocol_readiness()
    assert report == {
        "ready": False,
        "stage": "authentication",
        "reason": "authentication-boundary-failed",
        "status_code": 200,
    }

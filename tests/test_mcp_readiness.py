from __future__ import annotations

from brains import service
from brains.api import admin_key
from brains.config import settings
from brains.service import common as service_common


def _listener_report(*, protocol_ready: bool, mcp_listener: bool = True) -> dict:
    return {
        "listeners": {"gateway": True, "mcp": mcp_listener},
        "mcp_protocol": {
            "ready": protocol_ready,
            "stage": "ready" if protocol_ready else "protocol",
        },
        "serving": protocol_ready,
        "endpoints": {
            "gateway": "http://127.0.0.1:8787",
            "console": "http://127.0.0.1:8787/app",
            "mcp": "http://127.0.0.1:9877/mcp",
        },
    }


def test_mcp_protocol_status_uses_secret_without_returning_it(monkeypatch) -> None:
    synthetic_secret = "synthetic-readiness-secret"
    monkeypatch.setattr(settings, "api_key", synthetic_secret)
    observed: dict[str, object] = {}

    async def handshake(url: str, api_key: str | None, timeout: float) -> dict:
        observed.update(url=url, api_key=api_key, timeout=timeout)
        return {"ready": True, "stage": "ready", "tool_count": 1}

    monkeypatch.setattr(service_common, "_mcp_protocol_handshake", handshake)
    report = service_common.mcp_protocol_status("localhost", 1234, timeout=0.5)

    assert observed == {
        "url": "http://localhost:1234/mcp",
        "api_key": synthetic_secret,
        "timeout": 0.5,
    }
    assert synthetic_secret not in repr(report)


def test_mcp_protocol_status_fails_closed_when_credential_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "allow_unauthenticated_api", False)
    monkeypatch.setattr(admin_key, "read_persisted_key", lambda: None)
    monkeypatch.setattr(
        service_common,
        "_mcp_protocol_handshake",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not connect")),
    )

    report = service_common.mcp_protocol_status()

    assert report == {
        "ready": False,
        "stage": "authentication",
        "reason": "credential-unavailable",
    }


def test_service_status_requires_owned_pid_and_protocol_readiness(monkeypatch) -> None:
    monkeypatch.setattr(service, "supported", lambda: True)
    monkeypatch.setattr(
        service,
        "_backend",
        lambda: type("Backend", (), {"status": staticmethod(lambda: {"installed": True})}),
    )
    monkeypatch.setattr(service, "read_pidfile_record", lambda: {"pid": 42})
    monkeypatch.setattr(
        service,
        "verify_pid",
        lambda _record: {"running": True, "confidence": "verified"},
    )
    monkeypatch.setattr(service, "listener_status", lambda: _listener_report(protocol_ready=False))

    report = service.status()

    assert report["healthy"] is False
    assert report["runtime_classification"] == "installed-owned-unready"


def test_service_status_distinguishes_owned_manual_stale_and_unknown(monkeypatch) -> None:
    monkeypatch.setattr(service, "supported", lambda: True)
    backend_state = {"installed": True}
    pid_state = {"running": True, "confidence": "verified"}
    listener_state = _listener_report(protocol_ready=True)
    monkeypatch.setattr(
        service,
        "_backend",
        lambda: type("Backend", (), {"status": staticmethod(lambda: dict(backend_state))}),
    )
    monkeypatch.setattr(service, "read_pidfile_record", lambda: {"pid": 42})
    monkeypatch.setattr(service, "verify_pid", lambda _record: dict(pid_state))
    monkeypatch.setattr(service, "listener_status", lambda: dict(listener_state))

    owned = service.status()
    assert owned["healthy"] is True
    assert owned["runtime_classification"] == "installed-owned-ready"

    backend_state["installed"] = False
    pid_state.update(running=False, confidence="absent")
    manual = service.status()
    assert manual["healthy"] is False
    assert manual["runtime_classification"] == "manual-running"

    pid_state.update(running=False, confidence="stale")
    stale = service.status()
    assert stale["runtime_classification"] == "stale-pid"

    listener_state.update(
        mcp_protocol={"ready": False, "stage": "protocol"},
        serving=False,
    )
    pid_state.update(running=False, confidence="absent")
    unknown = service.status()
    assert unknown["runtime_classification"] == "unknown-port-owner"
    assert unknown["healthy"] is False

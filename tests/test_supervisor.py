"""Tests for the brains-supervisor (PR-4 of the consolidation plan).

We exercise the ``Child`` supervisor and the argparse wiring without
launching real uvicorn workers. Two strategies:

* ``Child`` runs are exercised against ``python -c '...'`` snippets so
  the test is portable across shells and finishes in <1s.
* The CLI builder is exercised by passing flags and inspecting the
  resulting child argv lists.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

import brains.control.supervisor as supervisor


def test_build_children_includes_gateway_and_mcp_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BRAINS_LEGACY_SURFACES", raising=False)
    parser = supervisor._build_parser()
    args = parser.parse_args([])
    children = supervisor._build_children(args)
    names = {c.name for c in children}
    assert names == {"gateway", "mcp"}
    by_name = {child.name: child for child in children}
    assert by_name["gateway"].listener == ("127.0.0.1", 8787)
    assert by_name["gateway"].listener_path == "/health"
    assert by_name["gateway"].listener_status == 200
    assert by_name["mcp"].listener == ("127.0.0.1", 9877)


def test_build_children_dashboard_is_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("BRAINS_LEGACY_SURFACES", raising=False)
    parser = supervisor._build_parser()

    by_flag = supervisor._build_children(parser.parse_args(["--dashboard"]))
    assert {c.name for c in by_flag} == {"gateway", "dashboard", "mcp"}

    monkeypatch.setenv("BRAINS_LEGACY_SURFACES", "1")
    by_env = supervisor._build_children(parser.parse_args([]))
    assert {c.name for c in by_env} == {"gateway", "dashboard", "mcp"}

    # --no-dashboard is a back-compat veto that wins over the env opt-in.
    vetoed = supervisor._build_children(parser.parse_args(["--no-dashboard"]))
    assert {c.name for c in vetoed} == {"gateway", "mcp"}


def test_build_children_respects_no_mcp() -> None:
    parser = supervisor._build_parser()
    args = parser.parse_args(["--no-mcp"])
    children = supervisor._build_children(args)
    assert {c.name for c in children} == {"gateway"}


def test_mcp_child_argv_passes_port_override() -> None:
    parser = supervisor._build_parser()
    args = parser.parse_args(["--mcp-port", "19877"])
    children = {c.name: c for c in supervisor._build_children(args)}
    assert "19877" in children["mcp"].argv
    assert "brains.mcp.server" in children["mcp"].argv


def test_build_children_respects_disable_flags() -> None:
    parser = supervisor._build_parser()
    args = parser.parse_args(["--no-dashboard", "--no-mcp"])
    children = supervisor._build_children(args)
    assert {c.name for c in children} == {"gateway"}


def test_child_argv_passes_host_and_port_overrides() -> None:
    parser = supervisor._build_parser()
    args = parser.parse_args(["--dashboard", "--gateway-port", "18787"])
    children = {c.name: c for c in supervisor._build_children(args)}
    assert "18787" in children["gateway"].argv
    assert "brains.main:app" in children["gateway"].argv
    assert "brains.dashboard.app:app" in children["dashboard"].argv


def test_state_dir_respects_env_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path / "custom"))
    assert supervisor._state_dir() == (tmp_path / "custom").resolve()
    assert supervisor._log_path().parent == (tmp_path / "custom").resolve() / "sessions"


def test_child_runs_short_command_and_stops_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    supervisor._setup_logging()

    # A child that exits after printing 3 lines then sleeps 60s; we stop it
    # after the first restart-attempt has started.
    snippet = (
        "import sys, time;"
        "[print(f'line {i}') for i in range(3)];"
        "sys.stdout.flush();"
        "time.sleep(0.1);"
        "sys.exit(0)"
    )
    child = supervisor.Child("test-child", [sys.executable, "-c", snippet])
    child.start()
    # Give the supervisor enough time to spawn once and start the backoff sleep.
    time.sleep(2.0)
    child.stop(timeout=5.0)
    # If we got here without hanging, the start/stop contract holds.
    assert not (child.proc and child.proc.poll() is None)


def test_listener_probe_host_maps_wildcard_binds_to_loopback() -> None:
    assert supervisor._listener_probe_host("0.0.0.0") == "127.0.0.1"
    assert supervisor._listener_probe_host("::") == "::1"
    assert supervisor._listener_probe_host("127.0.0.1") == "127.0.0.1"


def test_listener_probe_requires_a_complete_expected_http_response(monkeypatch) -> None:
    requests: list[tuple[str, str]] = []

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return b"ok"

    class Connection:
        def __init__(self, _host: str, _port: int, timeout: float) -> None:
            assert timeout == 2.0

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            requests.append((method, path))
            assert headers == {"Connection": "close"}

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr(supervisor.http.client, "HTTPConnection", Connection)

    assert supervisor._listener_responding("127.0.0.1", 8787, path="/health", expected_status=200)
    assert not supervisor._listener_responding(
        "127.0.0.1", 8787, path="/health", expected_status=204
    )
    assert requests == [("GET", "/health"), ("GET", "/health")]


def test_listener_watchdog_restarts_alive_child_after_listener_loss(monkeypatch) -> None:
    child = supervisor.Child("mcp", ["python"], listener=("127.0.0.1", 9877))
    process = SimpleNamespace(poll=lambda: None)
    responses = iter([True, False, False, False])
    terminated: list[object] = []

    monkeypatch.setattr(supervisor, "_listener_responding", lambda *_args, **_kw: next(responses))
    monkeypatch.setattr(child._watch_stop, "wait", lambda _seconds: False)
    monkeypatch.setattr(child, "_terminate_process_tree", terminated.append)

    child._watch_listener(process)

    assert terminated == [process]


def test_listener_watchdog_restarts_child_that_never_becomes_ready(monkeypatch) -> None:
    child = supervisor.Child("mcp", ["python"], listener=("127.0.0.1", 9877))
    process = SimpleNamespace(poll=lambda: None)
    clock = iter([0.0, supervisor.LISTENER_STARTUP_GRACE_SECONDS + 1.0])
    terminated: list[object] = []

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(supervisor, "_listener_responding", lambda *_args, **_kw: False)
    monkeypatch.setattr(child._watch_stop, "wait", lambda _seconds: False)
    monkeypatch.setattr(child, "_terminate_process_tree", terminated.append)

    child._watch_listener(process)

    assert terminated == [process]


def test_windows_listener_recovery_terminates_the_owned_process_tree(monkeypatch) -> None:
    child = supervisor.Child("mcp", ["python"])
    signals: list[int] = []
    process = SimpleNamespace(
        pid=4242,
        poll=lambda: None,
        send_signal=signals.append,
    )
    commands: list[list[str]] = []

    monkeypatch.setattr(supervisor.os, "name", "nt")
    monkeypatch.setattr(
        supervisor.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or SimpleNamespace(returncode=0),
    )

    child._terminate_process_tree(process)

    assert commands == [["taskkill", "/PID", "4242", "/T", "/F"]]
    assert signals == []


def test_listener_watchdog_terminates_an_alive_unserving_process_group(monkeypatch) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    snippet = (
        "import http.server, threading, time;"
        f"server=http.server.ThreadingHTTPServer(('127.0.0.1',{port}),"
        "http.server.SimpleHTTPRequestHandler);"
        "threading.Thread(target=server.serve_forever,daemon=True).start();"
        "print('listener-ready',flush=True);"
        "time.sleep(0.5);server.shutdown();server.server_close();"
        "print('listener-closed',flush=True);time.sleep(60)"
    )
    child = supervisor.Child(
        "listener-drill",
        [sys.executable, "-c", snippet],
        listener=("127.0.0.1", port),
    )
    monkeypatch.setattr(supervisor, "LISTENER_STARTUP_GRACE_SECONDS", 5.0)
    monkeypatch.setattr(supervisor, "LISTENER_PROBE_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "LISTENER_FAILURE_LIMIT", 2)
    completed = threading.Event()

    def run_once() -> None:
        child._spawn_once()
        completed.set()

    worker = threading.Thread(target=run_once, daemon=True)
    worker.start()

    assert completed.wait(10), "watchdog did not terminate the alive unserving child"
    worker.join(timeout=1)
    assert child.proc is not None
    assert child.proc.poll() is not None


def test_pidfile_written_and_cleared(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    supervisor._write_pidfile()
    pid_path = supervisor._pid_path()
    assert pid_path.exists()
    record = json.loads(pid_path.read_text(encoding="utf-8"))
    assert record["pid"] == os.getpid()
    assert record["format"] == 2
    assert "exe" in record and "start_time" in record
    supervisor._clear_pidfile()
    assert not pid_path.exists()
    # Second clear is a no-op, not an error.
    supervisor._clear_pidfile()


def test_pidfile_verifies_as_running_for_this_process(tmp_path, monkeypatch) -> None:
    """The pidfile the supervisor just wrote for itself must verify as at
    least running — never "stale" — right after it is written (BL-P1-09)."""
    from brains.service.common import read_pidfile_record, verify_pid

    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    supervisor._write_pidfile()
    check = verify_pid(read_pidfile_record(supervisor._pid_path()))
    assert check["running"] is True
    assert check["confidence"] in ("verified", "degraded", "unverified")
    supervisor._clear_pidfile()


def test_run_returns_2_when_all_children_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    rc = supervisor.run(["--no-gateway", "--no-dashboard", "--no-mcp"])
    assert rc == 2


def test_run_refuses_unavailable_gateway_before_starting_children(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAINS_SUPERVISOR_PREFLIGHT_WAIT_SECONDS", "0")
    monkeypatch.setattr(supervisor, "_port_bindable", lambda _host, _port: False)
    monkeypatch.setattr(
        supervisor,
        "_build_children",
        lambda _args: (_ for _ in ()).throw(AssertionError("children must not start")),
    )
    assert supervisor.run([]) == 3


def test_run_preflights_every_enabled_listener_with_actual_host(tmp_path, monkeypatch) -> None:
    from brains.mcp import sse_auth

    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAINS_SUPERVISOR_PREFLIGHT_WAIT_SECONDS", "0")
    monkeypatch.setattr(sse_auth, "resolve_bind_host", lambda: "mcp-host")
    checked: list[tuple[str, int]] = []

    def bindable(host: str, port: int) -> bool:
        checked.append((host, port))
        return port != 9877

    monkeypatch.setattr(supervisor, "_port_bindable", bindable)
    monkeypatch.setattr(
        supervisor,
        "_build_children",
        lambda _args: (_ for _ in ()).throw(AssertionError("children must not start")),
    )

    assert supervisor.run(["--dashboard"]) == 3
    assert checked == [("127.0.0.1", 8787), ("127.0.0.1", 9876), ("mcp-host", 9877)]


def test_run_waits_in_a_degraded_state_for_a_blocked_listener(tmp_path, monkeypatch) -> None:
    """A blocked bind is usually a predecessor still shutting down: the
    supervisor holds a bounded degraded state instead of exiting into a
    service-manager relaunch loop."""
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAINS_SUPERVISOR_PREFLIGHT_WAIT_SECONDS", "30")
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    attempts: list[int] = []

    def bindable(_host: str, port: int) -> bool:
        attempts.append(port)
        return attempts.count(port) > 1

    monkeypatch.setattr(supervisor, "_port_bindable", bindable)
    started: list[str] = []

    def build(_args):
        started.append("built")
        return []

    monkeypatch.setattr(supervisor, "_build_children", build)

    # No children to supervise (2), i.e. the preflight let the run continue.
    assert supervisor.run(["--no-mcp"]) == 2
    assert attempts == [8787, 8787]
    assert started == ["built"]


def test_run_refuses_enabled_listener_port_collision_before_binding(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        supervisor,
        "_port_bindable",
        lambda _host, _port: (_ for _ in ()).throw(AssertionError("collision binds nothing")),
    )

    assert supervisor.run(["--gateway-port", "9877"]) == 3

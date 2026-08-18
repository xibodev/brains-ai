"""CLI-level tests for ``brains-ai daemon <status|drain|stop|start>`` (BL-P1-13).

The daemon's *behavior* (detect/poll/claim/spawn cycle, GC sweep, enrolment)
is covered end-to-end in ``tests/test_daemon.py`` against the real ``Daemon``
class. This file proves the missing wiring: that the Typer commands
themselves invoke the right ``Daemon`` methods, format output correctly, and
- the BL-P1-09 addition - that ``daemon stop`` verifies the recorded PID's
identity before signalling it rather than trusting a bare number.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from brains.cli.app import app


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Every test gets its own ``daemon.pid`` location."""
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    return tmp_path


def _daemon_pidfile(state_dir):
    return state_dir / "daemon.pid"


# --------------------------------------------------------------------------- #
# status / drain — prove the CLI calls through to the real Daemon methods
# --------------------------------------------------------------------------- #


def test_daemon_status_cli_prints_daemon_status_as_json(monkeypatch):
    fake_status = {
        "machine_id": "machine-x",
        "machine_label": "Test Box",
        "hub_url": "http://testserver",
        "hub_reachable": True,
        "detected": [{"tool": "copilot"}],
        "runtimes": [
            {
                "id": 1,
                "slug": "machine-x-copilot",
                "status": "online",
                "health": "ok",
                "last_heartbeat_at": "2026-01-01T00:00:00Z",
            }
        ],
    }
    monkeypatch.setattr("brains.daemon.Daemon.status", lambda self: fake_status)
    result = CliRunner().invoke(app, ["daemon", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == fake_status


def test_daemon_status_cli_human_readable_output(monkeypatch):
    fake_status = {
        "machine_id": "machine-x",
        "machine_label": "Test Box",
        "hub_url": "http://testserver",
        "hub_reachable": False,
        "detected": [{"tool": "copilot"}],
        "runtimes": [],
    }
    monkeypatch.setattr("brains.daemon.Daemon.status", lambda self: fake_status)
    result = CliRunner().invoke(app, ["daemon", "status"])
    assert result.exit_code == 0, result.output
    assert "machine-x" in result.output
    assert "reachable=False" in result.output


def test_daemon_drain_cli_invokes_drain_and_prints_result(monkeypatch):
    drained = [{"id": 1, "slug": "machine-x-copilot", "status": "draining"}]
    monkeypatch.setattr("brains.daemon.Daemon.drain", lambda self: drained)
    result = CliRunner().invoke(app, ["daemon", "drain"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == drained


# --------------------------------------------------------------------------- #
# stop — no pidfile / stale pidfile / verified pidfile
# --------------------------------------------------------------------------- #


def test_daemon_stop_cli_without_a_pidfile_fails_clearly():
    result = CliRunner().invoke(app, ["daemon", "stop"])
    assert result.exit_code == 1
    assert "no daemon pidfile" in result.output.lower()


def test_daemon_stop_cli_refuses_a_stale_pid(monkeypatch, tmp_path):
    """A pidfile naming a PID the OS has since reused for something else must
    never be signalled by number alone (BL-P1-09)."""
    from brains.service.common import write_pidfile

    pidfile = _daemon_pidfile(tmp_path)
    write_pidfile(pidfile, pid=999999)
    monkeypatch.setattr("brains.service.common._read_process_identity", lambda pid: None)

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    result = CliRunner().invoke(app, ["daemon", "stop"])

    assert result.exit_code == 1
    assert killed == []  # never signalled
    assert not pidfile.exists()  # stale pidfile cleaned up


def test_daemon_stop_cli_refuses_an_unverified_legacy_pid(monkeypatch, tmp_path):
    pidfile = _daemon_pidfile(tmp_path)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(
        "brains.service.common._read_process_identity",
        lambda pid: {"exe": sys.executable, "start_time": 1000.0},
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    result = CliRunner().invoke(app, ["daemon", "stop"])
    assert result.exit_code == 1
    assert "unverified" in result.output
    assert killed == []
    assert pidfile.exists()


def test_daemon_stop_cli_windows_requests_graceful_stop(monkeypatch, tmp_path):
    from brains.service.common import write_pidfile

    monkeypatch.setattr("brains.service.common.current_platform", lambda: "windows")
    monkeypatch.setattr(
        "brains.service.common._read_process_identity",
        lambda pid: {"exe": sys.executable, "start_time": 1000.0},
    )
    pidfile = _daemon_pidfile(tmp_path)
    write_pidfile(pidfile, pid=os.getpid())
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("os.kill must not be used"))
    result = CliRunner().invoke(app, ["daemon", "stop"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "daemon.stop").read_text(encoding="utf-8") == str(os.getpid())


def test_daemon_stop_cli_signals_a_verified_pid(tmp_path, monkeypatch):
    """End-to-end against a real child process: the pidfile records the
    daemon's own executable/start time (as ``daemon start`` would), and
    ``daemon stop`` must verify + signal it."""
    from brains.service.common import write_pidfile

    monkeypatch.setattr("brains.service.common.current_platform", lambda: "linux")
    monkeypatch.setattr(
        "brains.service.common._read_process_identity",
        lambda pid: {"exe": sys.executable, "start_time": 1000.0},
    )

    child = subprocess.Popen(  # noqa: S603 - fixed, argument-free interpreter call
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        pidfile = _daemon_pidfile(tmp_path)
        write_pidfile(pidfile, pid=child.pid, cmdline=[sys.executable, "-c", "sleep"])

        result = CliRunner().invoke(app, ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert f"pid {child.pid}" in result.output
        # Give the OS a moment to tear the process down, then confirm it did.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
        assert child.poll() is not None, "signalled process did not terminate"
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            child.kill()
        with contextlib.suppress(Exception):
            child.wait(timeout=5)


def test_daemon_stop_cli_force_uses_sigkill(monkeypatch, tmp_path):
    from brains.service.common import write_pidfile

    monkeypatch.setattr("brains.service.common.current_platform", lambda: "linux")
    monkeypatch.setattr(
        "brains.service.common._read_process_identity",
        lambda pid: {"exe": sys.executable, "start_time": 1000.0},
    )
    pidfile = _daemon_pidfile(tmp_path)
    write_pidfile(pidfile, pid=os.getpid())
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    result = CliRunner().invoke(app, ["daemon", "stop", "--force"])

    assert result.exit_code == 0, result.output
    assert len(signalled) == 1
    import signal as signal_mod

    expected_sig = signal_mod.SIGKILL if hasattr(signal_mod, "SIGKILL") else signal_mod.SIGTERM
    assert signalled[0][1] == expected_sig

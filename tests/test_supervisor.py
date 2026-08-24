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
import sys
import time

import brains.control.supervisor as supervisor


def test_build_children_includes_gateway_and_mcp_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BRAINS_LEGACY_SURFACES", raising=False)
    parser = supervisor._build_parser()
    args = parser.parse_args([])
    children = supervisor._build_children(args)
    names = {c.name for c in children}
    assert names == {"gateway", "mcp"}


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

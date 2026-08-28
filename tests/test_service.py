"""Tests for ``brains.service`` — the OS-service installers.

The unit-definition renderers (Task Scheduler XML / launchd plist / systemd
unit) are pure functions, so they are exercised directly on any host OS. The
install/uninstall/start/stop verbs shell out to platform tools and are only
covered here via their ``dry_run`` plans (which must touch nothing).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from brains import service
from brains.service import common as service_common
from brains.service import linux, macos, windows
from brains.service.common import (
    ServiceSpec,
    UnsupportedPlatform,
    cleanup_stale_pidfile,
    current_platform,
    default_spec,
    read_pidfile,
    read_pidfile_record,
    verify_pid,
    write_pidfile,
)


@pytest.fixture
def spec() -> ServiceSpec:
    return ServiceSpec(
        program=r"C:\venv\Scripts\pythonw.exe",
        user="USER-PC\\user",
        working_dir=r"C:\Users\user",
        state_dir=r"C:\Users\user\.brains",
    )


# --- spec + dispatch -------------------------------------------------------


def test_default_spec_execs_brains_module() -> None:
    s = default_spec()
    assert s.args[:3] == ["-m", "brains", "serve-all"]
    assert s.args[-6:] == [
        "--gateway-host",
        "127.0.0.1",
        "--gateway-port",
        str(s.gateway_port),
        "--mcp-port",
        "9877",
    ]
    assert s.program  # the running interpreter
    assert "-m brains serve-all" in s.command_line
    assert Path(s.program).resolve() == Path(sys.executable).resolve()


def test_install_refuses_interpreter_that_cannot_import_brains(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "verify_service_interpreter",
        lambda _program: {"ok": False, "detail": "No module named brains"},
    )
    report = service.install(ServiceSpec(program="bad-python"), dry_run=True)
    assert report["ok"] is False
    assert report["action"] == "refused"


def test_service_status_requires_live_listeners(monkeypatch) -> None:
    monkeypatch.setattr(service, "supported", lambda: True)
    monkeypatch.setattr(
        service,
        "_backend",
        lambda: type(
            "Backend",
            (),
            {"status": staticmethod(lambda: {"installed": True, "state": "Running"})},
        ),
    )
    monkeypatch.setattr(
        service,
        "verify_pid",
        lambda _record: {"running": True, "confidence": "verified"},
    )
    monkeypatch.setattr(service, "read_pidfile_record", lambda: {"pid": 1})
    monkeypatch.setattr(
        service,
        "listener_status",
        lambda: {"listeners": {"gateway": True, "mcp": False}, "serving": False},
    )
    report = service.status()
    assert report["healthy"] is False
    assert report["listeners"]["mcp"] is False


def test_default_service_port_falls_back_and_is_persisted(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        service_common,
        "probe_listener_port",
        lambda _host, port: {"available": port == 8878},
    )
    spec = default_spec()
    assert spec.gateway_port == 8878

    service_common.write_service_config(spec)
    assert default_spec().gateway_port == 8878
    assert service_common.read_service_config()["gateway_port"] == 8878


def test_explicit_unavailable_service_port_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        service_common,
        "probe_listener_port",
        lambda _host, _port: {"available": False},
    )
    report = service.install(gateway_port=8877, dry_run=True)
    assert report["ok"] is False
    assert report["action"] == "refused"
    assert "8877" in report["detail"]


def test_listener_status_uses_persisted_service_ports(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    spec = ServiceSpec(program="python", gateway_port=8877, mcp_port=9988)
    service_common.write_service_config(spec)
    attempted: list[tuple[str, int]] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def connect(target, timeout):
        attempted.append(target)
        return Connection()

    monkeypatch.setattr(service_common.socket, "create_connection", connect)
    report = service_common.listener_status()
    assert attempted == [("127.0.0.1", 8877), ("127.0.0.1", 9988)]
    assert report["endpoints"]["console"] == "http://127.0.0.1:8877/app"


def test_pid_identity_accepts_exact_brains_command_when_start_time_drifts(monkeypatch) -> None:
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda _pid: {
            "exe": r"C:\venv\Scripts\python.exe",
            "start_time": 999.0,
            "cmdline": r"C:\venv\Scripts\python.exe -m brains serve-all",
        },
    )
    result = verify_pid(
        {
            "format": 2,
            "pid": 42,
            "exe": r"C:\venv\Scripts\python.exe",
            "start_time": 1.0,
            "cmdline": r"C:\venv\Scripts\python.exe -m brains serve-all",
        }
    )
    assert result["confidence"] == "verified"
    assert result["identity_verified"] is True


def test_current_platform_is_known_or_passthrough() -> None:
    assert current_platform() in {"windows", "macos", "linux"} or isinstance(
        current_platform(), str
    )


def test_run_cmd_bounds_a_missing_platform_utility(monkeypatch) -> None:
    def missing(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "systemctl")

    monkeypatch.setattr(service_common.subprocess, "run", missing)

    rc, out, err = service_common.run_cmd(["systemctl", "--user", "is-active"])

    assert rc == 127
    assert out == ""
    assert "systemctl" in err


def test_supported_matches_backend_table() -> None:
    assert service.supported() == (current_platform() in {"windows", "macos", "linux"})


def test_unsupported_platform_raises(monkeypatch) -> None:
    monkeypatch.setattr(service, "current_platform", lambda: "sunos")
    with pytest.raises(UnsupportedPlatform):
        service.install()


def test_status_on_unsupported_platform_is_graceful(monkeypatch) -> None:
    monkeypatch.setattr(service, "current_platform", lambda: "sunos")
    report = service.status()
    assert report["supported"] is False
    assert report["installed"] is False


# --- Windows (Task Scheduler XML) ------------------------------------------


def test_windows_task_xml_encodes_policy(spec: ServiceSpec) -> None:
    xml = windows.render_task_xml(spec)
    assert "<URI>\\BrainsServeAll</URI>" in xml
    assert "<LogonTrigger>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml  # runs as the user
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml  # not admin
    assert "<Interval>PT1M</Interval>" in xml and "<Count>9999</Count>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml  # no time cap
    assert "<Hidden>true</Hidden>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert spec.program in xml
    assert "-m brains serve-all" in xml
    assert "USER-PC\\user" in xml


def test_windows_definition_path_under_state_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    p = windows.definition_path()
    assert p == tmp_path / "service" / "BrainsServeAll.xml"


# --- macOS (launchd plist) -------------------------------------------------


def test_macos_plist_runatload_keepalive(spec: ServiceSpec) -> None:
    plist = macos.render_plist(spec)
    assert "<string>com.brains.serve-all</string>" in plist
    assert "<key>RunAtLoad</key>" in plist and "<key>KeepAlive</key>" in plist
    # program + each arg become ProgramArguments entries
    assert f"<string>{spec.program}</string>" in plist
    assert "<string>-m</string>" in plist
    assert "<string>brains</string>" in plist
    assert "<string>serve-all</string>" in plist


def test_macos_plist_path_in_launchagents(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = macos.plist_path()
    assert p == tmp_path / "Library" / "LaunchAgents" / "com.brains.serve-all.plist"


# --- Linux (systemd --user unit) -------------------------------------------


def test_linux_unit_restart_and_target(spec: ServiceSpec) -> None:
    unit = linux.render_unit(spec)
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit
    assert "ExecStart=" in unit and "-m brains serve-all" in unit
    assert "StartLimitIntervalSec=0" in unit


def test_linux_unit_path_in_user_systemd(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = linux.unit_path()
    assert p == tmp_path / ".config" / "systemd" / "user" / "brains-serve-all.service"


# --- render_definition dispatch + dry-run safety ---------------------------


def test_render_definition_matches_platform(spec: ServiceSpec) -> None:
    text = service.render_definition(spec)
    plat = current_platform()
    if plat == "windows":
        assert "BrainsServeAll" in text
    elif plat == "macos":
        assert "com.brains.serve-all" in text
    elif plat == "linux":
        assert "WantedBy=default.target" in text


@pytest.mark.skipif(not service.supported(), reason="no service backend for this platform")
def test_install_dry_run_touches_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path / ".brains"))
    report = service.install(dry_run=True)
    assert report["action"] == "would-install"
    # No unit file should have been written anywhere under the fake home.
    written = (
        list(tmp_path.rglob("*BrainsServeAll*"))
        + list(tmp_path.rglob("*brains-serve-all*"))
        + list(tmp_path.rglob("*com.brains.serve-all*"))
    )
    assert written == []


# --- PID identity — BL-P1-09 ------------------------------------------------
#
# The pidfile historically held a bare integer with no proof the recorded PID
# still names the process that wrote it. These tests exercise the additive
# identity record + verification contract without depending on any real OS
# process table: ``_read_process_identity`` (the one platform-dependent call)
# is monkeypatched so the logic is proven the same way on every host OS.


def test_write_pidfile_records_identity_for_the_current_process(tmp_path) -> None:
    """Writing the pidfile for our own PID must always verify as running -
    never "stale" - immediately after it is written."""
    path = tmp_path / "service.pid"
    record = write_pidfile(path)
    assert record["format"] == 2
    assert record["pid"] == os.getpid()
    assert "exe" in record and "cmdline" in record and "start_time" in record
    check = verify_pid(read_pidfile_record(path))
    assert check["running"] is True
    assert check["confidence"] != "stale"
    assert read_pidfile(path) == os.getpid()


def test_read_pidfile_record_parses_legacy_plain_integer(tmp_path) -> None:
    path = tmp_path / "service.pid"
    path.write_text("4242", encoding="utf-8")
    record = read_pidfile_record(path)
    assert record == {
        "format": "legacy",
        "pid": 4242,
        "exe": None,
        "cmdline": None,
        "start_time": None,
        "recorded_at": None,
    }


def test_verify_pid_absent_when_no_pidfile() -> None:
    check = verify_pid(None)
    assert check == {
        "pid": None,
        "running": False,
        "identity_verified": None,
        "confidence": "absent",
        "reason": "no pidfile recorded",
    }


def test_verify_pid_stale_when_recorded_pid_is_not_running(monkeypatch) -> None:
    monkeypatch.setattr(service_common, "_read_process_identity", lambda pid: None)
    record = {"format": 2, "pid": 999999, "exe": "python.exe", "start_time": 1000.0}
    check = verify_pid(record)
    assert check["running"] is False
    assert check["confidence"] == "stale"


def test_verify_pid_stale_when_executable_and_start_time_mismatch(monkeypatch) -> None:
    """A live process with the recorded PID, but a *different* executable and
    start time, means the OS reused the PID for something else entirely."""
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": "notepad.exe", "start_time": 5000.0},
    )
    record = {"format": 2, "pid": 4242, "exe": "python.exe", "start_time": 1000.0}
    check = verify_pid(record)
    assert check["running"] is True
    assert check["identity_verified"] is False
    assert check["confidence"] == "stale"
    assert "reused" in check["reason"]


def test_verify_pid_verified_when_executable_and_start_time_match(monkeypatch) -> None:
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": r"C:\Python\python.exe", "start_time": 1000.4},
    )
    record = {"format": 2, "pid": 4242, "exe": r"C:\Python\python.exe", "start_time": 1000.0}
    check = verify_pid(record)
    assert check["running"] is True
    assert check["identity_verified"] is True
    assert check["confidence"] == "verified"


def test_verify_pid_degraded_when_identity_cannot_be_confirmed(monkeypatch) -> None:
    """The process exists, but this platform exposed neither exe nor start
    time for it - never confidently report that as "running"."""
    monkeypatch.setattr(
        service_common, "_read_process_identity", lambda pid: {"exe": None, "start_time": None}
    )
    record = {"format": 2, "pid": 4242, "exe": "python.exe", "start_time": 1000.0}
    check = verify_pid(record)
    assert check["running"] is True
    assert check["identity_verified"] is None
    assert check["confidence"] == "degraded"


def test_verify_pid_degraded_when_only_generic_executable_matches(monkeypatch) -> None:
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": "python.exe", "start_time": None},
    )
    record = {"format": 2, "pid": 4242, "exe": "python.exe", "start_time": 1000.0}
    check = verify_pid(record)
    assert check["identity_verified"] is None
    assert check["confidence"] == "degraded"
    assert "start time" in check["reason"]


def test_windows_identity_uses_cim_creation_time(monkeypatch) -> None:
    def _fake_run(cmd, **_kwargs):
        if cmd[0] == "tasklist":
            return 0, '"python.exe","4242","Console","1","10,000 K"', ""
        if cmd[0] == "powershell":
            assert "ToUniversalTime().ToString('o')" in cmd[-1]
            return (
                0,
                '{"ExecutablePath":"C:\\\\Python\\\\python.exe",'
                '"CreationDate":"2026-08-05T05:00:00-06:00"}',
                "",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(service_common, "run_cmd", _fake_run)
    identity = service_common._windows_identity(4242)
    assert identity is not None
    assert identity["exe"] == r"C:\Python\python.exe"
    assert isinstance(identity["start_time"], float)


def test_verify_pid_unverified_for_legacy_pidfile_of_a_live_process(monkeypatch) -> None:
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": "python.exe", "start_time": 1000.0},
    )
    check = verify_pid(4242)
    assert check["running"] is True
    assert check["identity_verified"] is None
    assert check["confidence"] == "unverified"


def test_cleanup_stale_pidfile_removes_stale_but_not_verified(tmp_path, monkeypatch) -> None:
    path = tmp_path / "service.pid"
    monkeypatch.setattr(service_common, "_read_process_identity", lambda pid: None)
    path.write_text(json.dumps({"format": 2, "pid": 999999, "exe": "x", "start_time": 1.0}))
    result = cleanup_stale_pidfile(path)
    assert result["confidence"] == "stale"
    assert result["removed"] is True
    assert not path.exists()

    # A verifiable, running record is left alone.
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": "python.exe", "start_time": 1000.0},
    )
    record = write_pidfile(path, pid=os.getpid())
    assert record["pid"] == os.getpid()
    result2 = cleanup_stale_pidfile(path)
    assert result2["removed"] is False
    assert path.exists()


def test_service_status_reports_service_pid_block(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    write_pidfile(tmp_path / "sessions" / "service.pid", pid=os.getpid())
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": "python.exe", "start_time": 1000.0},
    )
    monkeypatch.setattr(service, "supported", lambda: True)
    monkeypatch.setattr(
        service,
        "_backend",
        lambda: type("B", (), {"status": staticmethod(lambda: {"platform": "test"})}),
    )
    report = service.status()
    assert "service_pid" in report
    assert report["service_pid"]["pid"] == os.getpid()


# --- stop() refuses to act on a stale/reused pid ---------------------------


def test_windows_stop_skips_tree_kill_for_stale_pid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    write_pidfile(tmp_path / "sessions" / "service.pid", pid=999999)
    # The pidfile names a pid; the live process table says nothing answers it.
    monkeypatch.setattr(service_common, "_read_process_identity", lambda pid: None)

    calls: list[list[str]] = []

    def _fake_run_cmd(cmd, **_kw):
        calls.append(cmd)
        if cmd[0] == "schtasks":
            return 0, "", ""
        return 1, "", "should not be called"

    monkeypatch.setattr(windows, "run_cmd", _fake_run_cmd)
    report = windows.stop()
    assert report["ok"] is True
    assert not any(cmd[0] == "taskkill" for cmd in calls)
    assert "stale" in report["detail"]
    # The stale pidfile is cleaned up rather than left to mislead the next read.
    assert not (tmp_path / "sessions" / "service.pid").exists()


def test_windows_stop_tree_kills_a_verified_pid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": "python.exe", "start_time": 1000.0},
    )
    write_pidfile(tmp_path / "sessions" / "service.pid", pid=os.getpid())

    calls: list[list[str]] = []

    def _fake_run_cmd(cmd, **_kw):
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(windows, "run_cmd", _fake_run_cmd)
    report = windows.stop()
    assert report["ok"] is True
    assert any(cmd[0] == "taskkill" for cmd in calls)


def test_windows_stop_reports_failed_tree_kill(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": "python.exe", "start_time": 1000.0},
    )
    write_pidfile(tmp_path / "sessions" / "service.pid", pid=os.getpid())

    def _fake_run_cmd(cmd, **_kw):
        return (1, "", "failed") if cmd[0] == "taskkill" else (0, "", "")

    monkeypatch.setattr(windows, "run_cmd", _fake_run_cmd)
    assert windows.stop()["ok"] is False


def test_windows_restart_refuses_start_after_incomplete_stop(monkeypatch) -> None:
    monkeypatch.setattr(windows, "stop", lambda: {"ok": False, "detail": "failed"})
    monkeypatch.setattr(windows, "start", lambda: pytest.fail("start must not run"))
    assert windows.restart()["ok"] is False


def test_windows_stop_refuses_unverified_legacy_pid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    pidfile = tmp_path / "sessions" / "service.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": "python.exe", "start_time": 1000.0},
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        windows,
        "run_cmd",
        lambda cmd, **_kw: calls.append(cmd) or (0, "", ""),
    )
    report = windows.stop()
    assert not any(cmd[0] == "taskkill" for cmd in calls)
    assert "unverified" in report["detail"]
    assert pidfile.exists()


def test_macos_stop_skips_signal_for_stale_pid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    write_pidfile(tmp_path / "sessions" / "service.pid", pid=999999)
    monkeypatch.setattr(service_common, "_read_process_identity", lambda pid: None)

    calls: list[list[str]] = []

    def _fake_run_cmd(cmd, **_kw):
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(macos, "run_cmd", _fake_run_cmd)
    report = macos.stop()
    assert report["ok"] is True
    assert not any(cmd[0] == "/bin/kill" for cmd in calls)
    assert "stale" in report["detail"]
    assert not (tmp_path / "sessions" / "service.pid").exists()


def test_macos_stop_refuses_degraded_pid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    write_pidfile(tmp_path / "sessions" / "service.pid", pid=os.getpid())
    monkeypatch.setattr(service_common, "_read_process_identity", lambda pid: {})
    calls: list[list[str]] = []
    monkeypatch.setattr(
        macos,
        "run_cmd",
        lambda cmd, **_kw: calls.append(cmd) or (0, "", ""),
    )
    report = macos.stop()
    assert not any(cmd[0] == "/bin/kill" for cmd in calls)
    assert "degraded" in report["detail"]


def test_macos_stop_reports_failed_child_kill(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda pid: {"exe": "python", "start_time": 1000.0},
    )
    write_pidfile(tmp_path / "sessions" / "service.pid", pid=os.getpid())

    def _fake_run_cmd(cmd, **_kw):
        if cmd[:2] == ["/bin/launchctl", "bootout"]:
            return 0, "", ""
        return (1, "", "failed") if cmd[0] == "/bin/kill" else (0, "", "")

    monkeypatch.setattr(macos, "run_cmd", _fake_run_cmd)
    assert macos.stop()["ok"] is False


def test_macos_restart_refuses_start_after_incomplete_stop(monkeypatch) -> None:
    monkeypatch.setattr(macos, "stop", lambda: {"ok": False, "detail": "failed"})
    monkeypatch.setattr(macos, "start", lambda: pytest.fail("start must not run"))
    assert macos.restart()["ok"] is False

"""Tests for ``brains.service`` — the OS-service installers.

The unit-definition renderers (Task Scheduler XML / launchd plist / systemd
unit) are pure functions, so they are exercised directly on any host OS. The
install/uninstall/start/stop verbs shell out to platform tools and are only
covered here via their ``dry_run`` plans (which must touch nothing).
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from brains import service
from brains.control import supervisor
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
    expected = Path(sys.executable)
    if current_platform() == "windows":
        expected = expected.with_name("pythonw.exe")
    assert Path(s.program).resolve() == expected.resolve()


def test_windows_default_spec_requires_same_environment_pythonw(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path / "state"))
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    monkeypatch.setattr(service_common, "current_platform", lambda: "windows")

    assert Path(default_spec(executable=str(python)).program) == pythonw

    pythonw.unlink()
    with pytest.raises(ValueError, match="windowless Python launcher"):
        default_spec(executable=str(python))


def test_windows_install_refuses_caller_supplied_console_interpreter(monkeypatch) -> None:
    monkeypatch.setattr(service, "current_platform", lambda: "windows")
    monkeypatch.setattr(
        service,
        "verify_service_interpreter",
        lambda _program: pytest.fail("headed interpreter must be refused before verification"),
    )

    report = service.install(ServiceSpec(program=r"C:\venv\Scripts\python.exe"), dry_run=True)

    assert report["ok"] is False
    assert report["action"] == "refused"
    assert "pythonw.exe" in report["detail"]


def test_install_refuses_interpreter_that_cannot_import_brains(monkeypatch) -> None:
    monkeypatch.setattr(service, "current_platform", lambda: "linux")
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
            {"status": staticmethod(lambda *_args: {"installed": True, "state": "Running"})},
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
        lambda: {
            "listeners": {"gateway": True, "mcp": False},
            "mcp_protocol": {"ready": False, "stage": "connect"},
            "serving": False,
        },
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


def test_install_dry_run_does_not_probe_an_implicit_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        service_common,
        "probe_listener_port",
        lambda _host, _port: (_ for _ in ()).throw(
            AssertionError("an implicit dry-run port must not inspect live listeners")
        ),
    )
    monkeypatch.setattr(
        service,
        "verify_service_interpreter",
        lambda program: {"ok": True, "program": program, "detail": ""},
    )

    report = service.install(dry_run=True)

    assert report["action"] == "would-install"
    assert report["endpoints"]["console"] == "http://127.0.0.1:8787/app"
    assert report["endpoints"]["mcp"] == "http://127.0.0.1:9877/mcp"


def test_install_refuses_identical_gateway_and_mcp_ports(monkeypatch, tmp_path) -> None:
    """The supervisor rejects that pair deterministically, so installing it
    would only persist a service that can never come up."""
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    report = service.install(gateway_port=9877, mcp_port=9877, dry_run=True)
    assert report["ok"] is False
    assert report["action"] == "refused"
    assert "9877" in report["detail"]


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
    monkeypatch.setattr(
        service_common,
        "mcp_protocol_status",
        lambda _host, _port: {"ready": True, "stage": "ready"},
    )
    report = service_common.listener_status()
    assert attempted == [("127.0.0.1", 8877), ("127.0.0.1", 9988)]
    assert report["endpoints"]["console"] == "http://127.0.0.1:8877/app"
    assert report["endpoints"]["mcp"] == "http://127.0.0.1:9988/mcp"
    assert report["serving"] is True


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
    assert spec.program.endswith("pythonw.exe")
    assert spec.program in xml
    arguments = ET.fromstring(xml).findtext(".//{*}Arguments")
    bootstrap = (
        "import os,runpy; "
        f"os.environ['BRAINS_STATE_DIR']={str(spec.state_dir)!r}; "
        "runpy.run_module('brains.service.windows_runner',run_name='__main__',alter_sys=True)"
    )
    assert arguments == subprocess.list2cmdline(["-c", bootstrap, spec.program, "serve-all"])
    assert "USER-PC\\user" in xml


@pytest.mark.parametrize(
    "state_root",
    [
        r"C:\private state\brains",
        'C:\\private "quoted" & state\\brains',
        "C:\\private\\\u96ea\u00e9\\brains",
        "C:\\private state\\brains\\",
        "C:\\private\\'; raise RuntimeError('not code'); #",
    ],
)
@pytest.mark.parametrize("inherited_state", [None, r"C:\different state"])
@pytest.mark.parametrize(
    "gateway_host", ["", "with spaces", 'with "quotes" & \u96ea', "C:\\trailing space\\"]
)
def test_windows_bootstrap_sets_only_state_before_dispatch(
    spec, state_root, inherited_state, gateway_host, monkeypatch
) -> None:
    spec.state_dir = state_root
    spec.program = r"C:\Python & tools\pythonw.exe"
    spec.working_dir = r"C:\neutral & work"
    spec.args = [
        "-m",
        "brains",
        "serve-all",
        "--gateway-host",
        gateway_host,
        "--gateway-port",
        "8877",
        "--mcp-port",
        "9988",
    ]
    original_args = spec.args.copy()
    canary = "synthetic-secret-do-not-persist-8392"
    monkeypatch.setenv("BRAINS_API_KEY", canary)
    monkeypatch.setenv("BRAINS_DB_URL", "sqlite:///synthetic-external.db")
    monkeypatch.setenv("BRAINS_STATE_DIR", r"C:\installing shell state")
    encoded: list[list[str]] = []
    list2cmdline = subprocess.list2cmdline

    def encode(arguments):
        encoded.append(arguments.copy())
        return list2cmdline(arguments)

    monkeypatch.setattr(windows.subprocess, "list2cmdline", encode)
    xml = windows.render_task_xml(spec)
    root = ET.fromstring(xml)
    assert len(encoded) == 1
    action = encoded[0]
    assert action[0] == "-c"
    assert action[2:] == [spec.program, *original_args[2:]]
    assert root.findtext(".//{*}Arguments") == list2cmdline(action)
    assert root.findtext(".//{*}Command") == spec.program
    assert root.findtext(".//{*}WorkingDirectory") == spec.working_dir
    assert "&amp;" in xml
    assert canary not in xml
    assert "BRAINS_API_KEY" not in xml
    assert "BRAINS_DB_URL" not in xml
    assert "synthetic-external.db" not in xml
    assert spec.args == original_args
    assert os.environ["BRAINS_STATE_DIR"] == r"C:\installing shell state"

    if inherited_state is None:
        monkeypatch.delenv("BRAINS_STATE_DIR")
    else:
        monkeypatch.setenv("BRAINS_STATE_DIR", inherited_state)
    environment = dict(os.environ)
    # Python -c supplies this argv; run_module(alter_sys=True) replaces argv[0].
    monkeypatch.setattr(sys, "argv", ["-c", *action[2:]])
    dispatched = []

    def dispatch(module, *, run_name, alter_sys):
        assert dict(os.environ) == {**environment, "BRAINS_STATE_DIR": state_root}
        assert sys.argv == ["-c", spec.program, *original_args[2:]]
        dispatched.append((module, run_name, alter_sys))

    monkeypatch.setattr(runpy, "run_module", dispatch)
    exec(action[1], {})
    assert dispatched == [("brains.service.windows_runner", "__main__", True)]


@pytest.mark.parametrize(
    "arguments",
    [[], ["-m"], ["serve-all"], ["-m", "other"], ["-c", "pass"], ["-I", "-m", "brains"]],
)
def test_windows_renderer_rejects_unsupported_arguments(spec, arguments) -> None:
    spec.args = arguments
    with pytest.raises(ValueError, match="must start with '-m brains'"):
        windows.render_task_xml(spec)


@pytest.mark.parametrize(
    "tail",
    [[], ["service", "stop"], ["serve-all", "--daemon"], ["serve-all", "--gateway-port"]],
)
def test_windows_renderer_rejects_non_foreground_supervisor(spec, tail) -> None:
    spec.args = ["-m", "brains", *tail]
    with pytest.raises(ValueError):
        windows.render_task_xml(spec)


def test_windows_render_and_dry_run_are_write_free(spec, monkeypatch, tmp_path) -> None:
    ambient = tmp_path / "ambient"
    selected = tmp_path / "selected"
    monkeypatch.setenv("BRAINS_STATE_DIR", str(ambient))
    spec.state_dir = str(selected)

    def forbidden(*_args, **_kwargs):
        pytest.fail("rendering and dry-run must not write or invoke the service manager")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(windows, "run_cmd", forbidden)
    xml = windows.render_task_xml(spec)
    report = windows.install(spec, dry_run=True)
    assert report["xml"] == xml
    assert report["definition"] == str(selected / "service" / "BrainsServeAll.xml")
    assert report["action"] == "would-install"
    assert not selected.exists()
    assert not ambient.exists()


def test_windows_install_writes_definition_to_spec_state_dir(spec, monkeypatch, tmp_path) -> None:
    ambient = tmp_path / "ambient"
    selected = tmp_path / "selected"
    monkeypatch.setenv("BRAINS_STATE_DIR", str(ambient))
    spec.state_dir = str(selected)
    spec.label = "brains-serve-all-state-test"
    path = selected / "service" / "BrainsServeAll-state-test.xml"
    calls = []

    def run(command):
        assert path.read_text(encoding="utf-16") == windows.render_task_xml(spec)
        calls.append(command)
        return 0, "ok", ""

    monkeypatch.setattr(windows, "run_cmd", run)
    report = windows.install(spec)
    assert report["ok"] is True
    assert report["started"] is True
    assert report["definition"] == str(path)
    assert calls == [
        ["schtasks", "/Create", "/TN", "BrainsServeAll-state-test", "/XML", str(path), "/F"],
        ["schtasks", "/Run", "/TN", "BrainsServeAll-state-test"],
    ]
    assert not ambient.exists()


def test_windows_definition_path_under_state_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    p = windows.definition_path()
    assert p == tmp_path / "service" / "BrainsServeAll.xml"


def test_namespaced_service_identity_is_confined_to_brains_namespace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    custom = ServiceSpec(program="python", label="brains-serve-all-evidence-a1")
    assert windows.definition_path(custom.label).name == "BrainsServeAll-evidence-a1.xml"
    assert macos.plist_path(custom.label).name == "com.brains.serve-all.evidence-a1.plist"
    assert linux.unit_path(custom.label).name == "brains-serve-all-evidence-a1.service"
    with pytest.raises(ValueError, match="service label"):
        ServiceSpec(program="python", label="unrelated-service")


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
    assert "<key>BRAINS_STATE_DIR</key>" in plist
    assert spec.state_dir in plist


def test_macos_plist_path_in_launchagents(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = macos.plist_path()
    assert p == tmp_path / "Library" / "LaunchAgents" / "com.brains.serve-all.plist"


# --- Linux (systemd --user unit) -------------------------------------------


def test_linux_unit_restart_and_target(spec: ServiceSpec) -> None:
    unit = linux.render_unit(spec)
    assert "Restart=always" in unit
    # A configuration/preflight refusal must not be relaunched forever.
    assert f"RestartPreventExitStatus={supervisor.CONFIG_EXIT_CODE}" in unit
    assert "WantedBy=default.target" in unit
    assert "ExecStart=" in unit and "-m brains serve-all" in unit
    assert "StartLimitIntervalSec=0" in unit
    assert f'Environment="BRAINS_STATE_DIR={spec.state_dir.replace(chr(92), chr(92) * 2)}"' in unit


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
    monkeypatch.setattr(
        service,
        "verify_service_interpreter",
        lambda program: {"ok": True, "program": program, "detail": ""},
    )
    report = service.install(dry_run=True)
    assert report["action"] == "would-install"
    # No unit file should have been written anywhere under the fake home.
    written = (
        list(tmp_path.rglob("*BrainsServeAll*"))
        + list(tmp_path.rglob("*brains-serve-all*"))
        + list(tmp_path.rglob("*com.brains.serve-all*"))
    )
    assert written == []


# --- PID identity -----------------------------------------------------------
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


def test_macos_identity_samples_one_complete_row(monkeypatch) -> None:
    calls = []
    row = "4242 Tue Sep  8 12:34:56 2026 /Applications/Python App/Python"
    monkeypatch.setattr(service_common, "run_cmd", lambda cmd: calls.append(cmd) or (0, row, ""))
    identity = service_common._macos_identity(4242)
    assert calls == [["ps", "-ww", "-p", "4242", "-o", "pid=,lstart=,comm="]]
    assert identity["exe"] == "/Applications/Python App/Python"
    assert isinstance(identity["start_time"], float)
    assert service_common._normalize_exe(identity["exe"]) == "python"


@pytest.mark.parametrize(
    "result",
    [
        (127, "", "missing utility"),
        (1, "", "permission denied"),
        (0, "", ""),
        (0, "4242", ""),
        (0, "4343 Tue Sep 8 12:34:56 2026 python", ""),
        (0, "4242 Tue Sep 8 12:34:56 2026 python\n4343", ""),
        (0, "4242 Tue Bad 8 12:34:56 2026 python", ""),
    ],
)
def test_macos_identity_failed_or_partial_sample_is_not_exit(monkeypatch, result) -> None:
    monkeypatch.setattr(service_common, "run_cmd", lambda cmd: result)
    monkeypatch.setattr(service_common, "current_platform", lambda: "macos")
    check = verify_pid({"format": 2, "pid": 4242, "exe": "python", "start_time": 1000.0})
    assert check["running"] is True
    assert check["confidence"] == "degraded"
    assert not check.get("identity_mismatch")


def test_macos_identity_empty_selection_proves_exit(monkeypatch) -> None:
    monkeypatch.setattr(service_common, "run_cmd", lambda cmd: (1, "", ""))
    assert service_common._macos_identity(4242) is None


@pytest.mark.parametrize("value", [None, True, "5000", float("nan"), float("inf"), -1, 0])
@pytest.mark.parametrize("field", ["recorded", "live"])
def test_verify_pid_invalid_start_time_never_proves_reuse(monkeypatch, value, field) -> None:
    record = {"format": 2, "pid": 4242, "exe": "python", "start_time": 1000.0}
    live = {"exe": "foreign", "start_time": 5000.0}
    (record if field == "recorded" else live)["start_time"] = value
    monkeypatch.setattr(service_common, "_read_process_identity", lambda pid: live)
    check = verify_pid(record)
    assert check["identity_verified"] is not True
    assert check.get("identity_mismatch") is False


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
        lambda: type("B", (), {"status": staticmethod(lambda *_args: {"platform": "test"})}),
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
        if cmd[0] == "taskkill":
            monkeypatch.setattr(service_common, "_read_process_identity", lambda _pid: None)
        return 0, "", ""

    monkeypatch.setattr(windows, "run_cmd", _fake_run_cmd)
    report = windows.stop()
    assert report["ok"] is True
    assert calls == [
        ["schtasks", "/End", "/TN", "BrainsServeAll"],
        ["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
    ]
    assert not (tmp_path / "sessions" / "service.pid").exists()


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


def test_windows_stop_does_not_kill_child_if_runner_cannot_be_ended(monkeypatch) -> None:
    record = {"pid": 4242}
    monkeypatch.setattr(windows, "read_pidfile_record", lambda: record)
    monkeypatch.setattr(
        windows, "verify_pid", lambda _record: pytest.fail("runner is still allowed to respawn")
    )
    calls = []
    monkeypatch.setattr(
        windows, "run_cmd", lambda command: calls.append(command) or (1, "", "end refused")
    )
    report = windows.stop()
    assert report["ok"] is False
    assert report["error_code"] == "native-stop-failed"
    assert calls == [["schtasks", "/End", "/TN", "BrainsServeAll"]]


def test_windows_restart_refuses_start_after_incomplete_stop(monkeypatch) -> None:
    monkeypatch.setattr(windows, "stop", lambda *_args: {"ok": False, "detail": "failed"})
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


@pytest.fixture
def macos_stop_state(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    state = SimpleNamespace(
        elapsed=0.0,
        exit_at=None,
        identity={"exe": "python", "start_time": 1000.0},
        after_exit=None,
        calls=[],
        signal_result=(0, "", ""),
        unload_result=(0, "", ""),
        pidfile=tmp_path / "sessions" / "service.pid",
    )

    def identity(pid):
        assert pid == 4242
        if state.exit_at is not None and state.elapsed >= state.exit_at:
            return state.after_exit
        return state.identity

    def sleep(seconds):
        assert 0 < seconds <= macos._STOP_POLL_SECONDS
        state.elapsed += seconds

    def run(cmd):
        state.calls.append(cmd)
        if cmd[0] == "/bin/kill":
            assert cmd == ["/bin/kill", "-TERM", "4242"]
            return state.signal_result
        assert cmd[0] == "launchctl" and cmd[1] in {"bootout", "unload"}
        return state.unload_result

    monkeypatch.setattr(service_common, "_read_process_identity", identity)
    monkeypatch.setattr(macos, "run_cmd", run)
    monkeypatch.setattr(
        macos, "time", SimpleNamespace(monotonic=lambda: state.elapsed, sleep=sleep)
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    write_pidfile(state.pidfile, pid=4242)
    return state


@pytest.mark.parametrize("reused", [False, True])
def test_macos_stop_skips_signal_for_stale_pid(macos_stop_state, reused) -> None:
    state = macos_stop_state
    state.identity = {"exe": "foreign", "start_time": 5000.0} if reused else None
    report = macos.stop()
    assert report["ok"] is (not reused)
    assert not any(cmd[0] == "/bin/kill" for cmd in state.calls)
    assert "stale" in report["detail"]
    assert state.pidfile.exists() is reused


@pytest.mark.parametrize("confidence", ["degraded", "unverified"])
def test_macos_stop_refuses_uncertain_pid(macos_stop_state, confidence) -> None:
    state = macos_stop_state
    if confidence == "degraded":
        state.identity = {}
    else:
        state.pidfile.write_text("4242", encoding="utf-8")
    report = macos.stop()
    assert report["ok"] is False
    assert not any(cmd[0] == "/bin/kill" for cmd in state.calls)
    assert confidence in report["detail"]
    assert state.pidfile.exists()


def test_macos_stop_reports_failed_signal(macos_stop_state) -> None:
    state = macos_stop_state
    state.signal_result = (1, "", "failed")
    report = macos.stop()
    assert report["ok"] is False
    assert "failed" in report["detail"]
    assert state.pidfile.exists()
    assert state.elapsed == 0


@pytest.mark.parametrize("exit_at", [0.2, 1.0, 35.0])
def test_macos_stop_waits_for_exit_and_cleans_pid_after_term(macos_stop_state, exit_at) -> None:
    state = macos_stop_state
    state.exit_at = exit_at
    assert macos.stop()["ok"] is True
    assert exit_at <= state.elapsed < exit_at + macos._STOP_POLL_SECONDS + 0.001
    assert [cmd[1] for cmd in state.calls] == ["bootout", "-TERM"]
    assert not state.pidfile.exists()


def test_macos_stop_does_not_signal_reused_pid_after_term(macos_stop_state) -> None:
    state = macos_stop_state
    state.exit_at = 0.2
    state.after_exit = {"exe": "foreign", "start_time": 5000.0}
    assert macos.stop()["ok"] is True
    assert [cmd[1] for cmd in state.calls] == ["bootout", "-TERM"]
    assert not state.pidfile.exists()
    assert state.after_exit == {"exe": "foreign", "start_time": 5000.0}


def test_macos_stop_retains_pid_when_identity_degrades_after_term(macos_stop_state) -> None:
    state = macos_stop_state
    state.exit_at = 0.2
    state.after_exit = {}
    report = macos.stop()
    assert report["ok"] is False
    assert "degraded" in report["detail"]
    assert state.pidfile.exists()
    assert [cmd[1] for cmd in state.calls] == ["bootout", "-TERM"]


@pytest.mark.parametrize("gone", [False, True])
def test_macos_stop_keeps_identity_if_unload_removes_pidfile(
    macos_stop_state, monkeypatch, gone
) -> None:
    state = macos_stop_state

    def unload(_label):
        state.pidfile.unlink()
        state.exit_at = 0 if gone else 0.4
        return 0, "unloaded", ""

    monkeypatch.setattr(macos, "_unload", unload)
    assert macos.stop()["ok"] is True
    assert state.elapsed == (0 if gone else 0.4)
    assert len(state.calls) == (0 if gone else 1)


@pytest.mark.parametrize("disappearance", ["verify", "current-read", "cleanup"])
def test_macos_stop_accepts_pidfile_disappearance_after_observed_exit(
    macos_stop_state, monkeypatch, disappearance
) -> None:
    state = macos_stop_state
    state.identity = None
    verify = macos.verify_pid
    read = macos.read_pidfile_record
    cleanup = macos.cleanup_stale_pidfile

    def verified(record):
        check = verify(record)
        if disappearance == "verify":
            state.pidfile.unlink(missing_ok=True)
        return check

    reads = 0

    def current():
        nonlocal reads
        reads += 1
        if disappearance == "current-read" and reads == 2:
            state.pidfile.unlink()
        return read()

    def cleaned(**kwargs):
        if disappearance == "cleanup":
            state.pidfile.unlink()
        return cleanup(**kwargs)

    monkeypatch.setattr(macos, "verify_pid", verified)
    monkeypatch.setattr(macos, "read_pidfile_record", current)
    monkeypatch.setattr(macos, "cleanup_stale_pidfile", cleaned)
    report = macos.stop()
    assert report["ok"] is True
    assert report["error_code"] is None
    assert not state.pidfile.exists()
    assert [cmd[1] for cmd in state.calls] == ["bootout"]


@pytest.mark.parametrize("identity", [{"exe": "python", "start_time": 1000.0}, {}])
def test_macos_stop_disappearance_does_not_hide_live_captured_pid(
    macos_stop_state, monkeypatch, identity
) -> None:
    state = macos_stop_state

    def unloaded(_label):
        state.pidfile.unlink()
        state.identity = identity
        return 0, "unloaded", ""

    monkeypatch.setattr(macos, "_unload", unloaded)
    report = macos.stop()
    assert report["ok"] is False
    assert report["error_code"] == ("pid-still-running" if identity else "pid-identity-unsafe")
    assert not state.pidfile.exists()


def test_macos_stop_rechecks_identity_after_unload(macos_stop_state, monkeypatch) -> None:
    state = macos_stop_state

    def unload(_label):
        state.identity = {"exe": "foreign", "start_time": 5000.0}
        return 0, "unloaded", ""

    monkeypatch.setattr(macos, "_unload", unload)
    assert macos.stop()["ok"] is True
    assert not state.calls
    assert not state.pidfile.exists()
    assert state.identity == {"exe": "foreign", "start_time": 5000.0}


@pytest.mark.parametrize("removed_by_supervisor", [False, True])
def test_macos_uninstall_accepts_verified_to_reused_transition(
    macos_stop_state, monkeypatch, removed_by_supervisor
):
    state = macos_stop_state
    definition = macos.plist_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")

    def unloaded(_label):
        state.identity = {"exe": "foreign", "start_time": 5000.0}
        if removed_by_supervisor:
            state.pidfile.unlink()
        return 0, "", ""

    monkeypatch.setattr(macos, "_unload", unloaded)
    report = macos.uninstall()
    assert report["ok"] is True
    assert report["pid_confidence"] == "stale"
    assert report["pid_identity_evidence"] == "executable-and-start-time-mismatch"
    assert not definition.exists()
    assert not state.pidfile.exists()
    assert not state.calls
    assert state.identity == {"exe": "foreign", "start_time": 5000.0}


def test_macos_reuse_after_failed_unload_retains_record(macos_stop_state, monkeypatch):
    state = macos_stop_state
    before = state.pidfile.read_bytes()

    def unloaded(_label):
        state.identity = {"exe": "foreign", "start_time": 5000.0}
        return 1, "", "manager failure"

    monkeypatch.setattr(macos, "_unload", unloaded)
    report = macos.stop()
    assert report["ok"] is False
    assert report["error_code"] == "native-unload-failed"
    assert state.pidfile.read_bytes() == before
    assert not state.calls


@pytest.mark.parametrize("cleanup_identity", [{}, {"exe": "python", "start_time": 1000.0}])
def test_macos_reuse_cleanup_requires_fresh_complete_mismatch(
    macos_stop_state, monkeypatch, cleanup_identity
):
    state = macos_stop_state
    before = state.pidfile.read_bytes()
    cleanup = macos.cleanup_stale_pidfile

    def unloaded(_label):
        state.identity = {"exe": "foreign", "start_time": 5000.0}
        return 0, "", ""

    def cleaned(**kwargs):
        state.identity = cleanup_identity
        return cleanup(**kwargs)

    monkeypatch.setattr(macos, "_unload", unloaded)
    monkeypatch.setattr(macos, "cleanup_stale_pidfile", cleaned)
    assert macos.stop()["ok"] is False
    assert state.pidfile.read_bytes() == before
    assert not state.calls


def test_macos_reuse_cleanup_checks_bytes_at_unlink_guard(macos_stop_state, monkeypatch):
    state = macos_stop_state
    read_bytes = Path.read_bytes
    cleanup = macos.cleanup_stale_pidfile
    replacement = b"{malformed replacement"

    def unloaded(_label):
        state.identity = {"exe": "foreign", "start_time": 5000.0}
        return 0, "", ""

    def cleaned(**kwargs):
        reads = 0

        def changed(path):
            nonlocal reads
            if path == state.pidfile:
                reads += 1
                if reads == 3:
                    path.write_bytes(replacement)
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", changed)
        return cleanup(**kwargs)

    monkeypatch.setattr(macos, "_unload", unloaded)
    monkeypatch.setattr(macos, "cleanup_stale_pidfile", cleaned)
    assert macos.stop()["ok"] is False
    assert state.pidfile.read_bytes() == replacement
    assert not state.calls


@pytest.mark.parametrize(
    "identity",
    [
        {"exe": "foreign", "start_time": 1000.0},
        {"exe": "python", "start_time": 5000.0},
        {"exe": "foreign", "start_time": None},
        {"exe": None, "start_time": 5000.0},
        {"exe": "foreign", "start_time": float("nan")},
        {"exe": "foreign", "start_time": float("inf")},
        {"exe": "foreign", "start_time": "5000"},
        {"exe": 12, "start_time": 5000.0},
        {"exe": " ", "start_time": 5000.0},
        {},
    ],
)
def test_macos_stop_partial_mismatch_waits_read_only_and_preserves_record(
    macos_stop_state, monkeypatch, identity
) -> None:
    state = macos_stop_state
    before = state.pidfile.read_bytes()

    def unloaded(_label):
        state.identity = identity
        return 0, "", ""

    monkeypatch.setattr(macos, "_unload", unloaded)
    report = macos.stop()
    assert report["ok"] is False
    assert report["error_code"] == "pid-identity-unsafe"
    assert state.elapsed == macos._STOP_TIMEOUT_SECONDS
    assert state.pidfile.read_bytes() == before
    assert not state.calls


@pytest.mark.parametrize("stage", ["before-cleanup", "during-verify"])
@pytest.mark.parametrize("replacement", [b'{"format": 2, "pid": 4343}', b"{broken", b"\xff", b" "])
def test_macos_reuse_cleanup_preserves_changed_bytes(
    macos_stop_state, monkeypatch, stage, replacement
) -> None:
    state = macos_stop_state
    foreign = {"exe": "foreign", "start_time": 5000.0}
    cleanup = macos.cleanup_stale_pidfile

    def unloaded(_label):
        state.identity = foreign
        return 0, "", ""

    def cleaned(**kwargs):
        if stage == "before-cleanup":
            state.pidfile.write_bytes(replacement)
        else:

            def changed(pid):
                state.pidfile.write_bytes(replacement)
                return foreign

            monkeypatch.setattr(service_common, "_read_process_identity", changed)
        return cleanup(**kwargs)

    monkeypatch.setattr(macos, "_unload", unloaded)
    monkeypatch.setattr(macos, "cleanup_stale_pidfile", cleaned)
    report = macos.stop()
    assert report["ok"] is False
    assert state.pidfile.read_bytes() == replacement
    assert not state.calls
    assert state.identity == foreign


def test_macos_reuse_cleanup_preserves_semantically_equal_rewrite(macos_stop_state, monkeypatch):
    state = macos_stop_state
    record = read_pidfile_record(state.pidfile)
    replacement = json.dumps(record, indent=2).encode()
    cleanup = macos.cleanup_stale_pidfile

    def unloaded(_label):
        state.identity = {"exe": "foreign", "start_time": 5000.0}
        return 0, "", ""

    def cleaned(**kwargs):
        state.pidfile.write_bytes(replacement)
        return cleanup(**kwargs)

    monkeypatch.setattr(macos, "_unload", unloaded)
    monkeypatch.setattr(macos, "cleanup_stale_pidfile", cleaned)
    assert macos.stop()["ok"] is False
    assert state.pidfile.read_bytes() == replacement
    assert not state.calls


@pytest.mark.parametrize("payload", [b"{broken", b"\xff", b'{"pid": -1}', b'{"pid": true}'])
def test_macos_stop_preserves_initial_malformed_record(macos_stop_state, payload):
    state = macos_stop_state
    state.pidfile.write_bytes(payload)
    report = macos.stop()
    assert report["ok"] is False
    assert state.pidfile.read_bytes() == payload
    assert not any(cmd[0] == "/bin/kill" for cmd in state.calls)


def test_macos_stop_accepts_signal_exit_race(macos_stop_state, monkeypatch) -> None:
    state = macos_stop_state
    run = macos.run_cmd

    def exited(cmd):
        if cmd[0] == "/bin/kill":
            state.identity = None
            state.signal_result = (1, "", "no such process")
        return run(cmd)

    monkeypatch.setattr(macos, "run_cmd", exited)
    assert macos.stop()["ok"] is True
    assert not state.pidfile.exists()


@pytest.mark.parametrize("action", ["stop", "uninstall"])
def test_macos_stop_cutoff_retains_definition_and_pid(macos_stop_state, action) -> None:
    state = macos_stop_state
    definition = macos.plist_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")
    report = getattr(macos, action)()
    assert report["ok"] is False
    assert "has not exited" in report["detail"]
    assert state.elapsed == macos._STOP_TIMEOUT_SECONDS
    assert state.pidfile.exists()
    assert definition.read_text(encoding="utf-8") == "owned"
    assert [cmd[1] for cmd in state.calls] == ["bootout", "-TERM"]


def test_macos_uninstall_removes_definition_only_after_exit(macos_stop_state, monkeypatch) -> None:
    state = macos_stop_state
    state.exit_at = 0.4
    definition = macos.plist_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")
    unlink = Path.unlink

    def after_exit(path, **kwargs):
        assert state.elapsed >= state.exit_at
        return unlink(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", after_exit)
    assert macos.uninstall()["ok"] is True
    assert not definition.exists()
    assert not state.pidfile.exists()


def test_macos_stop_retains_replacement_pidfile(macos_stop_state, monkeypatch) -> None:
    state = macos_stop_state
    replacement = {"format": 2, "pid": 4343, "exe": "python", "start_time": 2000.0}

    def sleep(_seconds):
        state.identity = None
        state.pidfile.write_text(json.dumps(replacement), encoding="utf-8")

    monkeypatch.setattr(macos.time, "sleep", sleep)
    report = macos.stop()
    assert report["ok"] is False
    assert "pidfile changed" in report["detail"]
    assert read_pidfile_record(state.pidfile) == replacement


def test_macos_stop_reports_pidfile_cleanup_failure(macos_stop_state, monkeypatch) -> None:
    state = macos_stop_state
    state.exit_at = 0.2

    def denied(_path, **_kwargs):
        raise PermissionError("synthetic denial")

    monkeypatch.setattr(Path, "unlink", denied)
    report = macos.stop()
    assert report["ok"] is False
    assert "cleanup incomplete" in report["detail"]
    assert state.pidfile.exists()


def test_macos_stop_does_not_signal_when_unload_fails(macos_stop_state) -> None:
    state = macos_stop_state
    state.unload_result = (1, "", "manager refused")
    assert macos.stop()["ok"] is False
    assert [cmd[1] for cmd in state.calls] == ["bootout", "unload"]
    assert state.pidfile.exists()


@pytest.mark.parametrize("loaded", [False, True])
def test_macos_start_never_force_restarts_supervisor(monkeypatch, loaded) -> None:
    calls = []

    def run(cmd):
        calls.append(cmd)
        return (1, "", "not loaded") if cmd[1] == "kickstart" and not loaded else (0, "", "")

    monkeypatch.setattr(macos, "run_cmd", run)
    assert macos.start()["ok"] is True
    assert calls[0] == ["launchctl", "kickstart", f"{macos._domain()}/com.brains.serve-all"]
    assert [cmd[1] for cmd in calls] == (["kickstart"] if loaded else ["kickstart", "bootstrap"])


def test_macos_restart_refuses_start_after_incomplete_stop(monkeypatch) -> None:
    monkeypatch.setattr(macos, "stop", lambda *_args: {"ok": False, "detail": "failed"})
    monkeypatch.setattr(macos, "start", lambda: pytest.fail("start must not run"))
    assert macos.restart()["ok"] is False


@pytest.fixture(params=[windows, macos], ids=["windows", "macos"])
def native_stop_state(request, macos_stop_state, monkeypatch):
    state = macos_stop_state
    backend = request.param
    if backend is windows:

        def run(cmd):
            state.calls.append(cmd)
            if cmd[0] == "taskkill":
                assert cmd == ["taskkill", "/PID", "4242", "/T", "/F"]
                return state.signal_result
            assert cmd[:2] == ["schtasks", "/End"]
            return state.unload_result

        monkeypatch.setattr(windows, "run_cmd", run)
        monkeypatch.setattr(windows, "time", macos.time)
    return backend, state


def test_native_stop_waits_for_observed_exit_and_removes_stale_pid(native_stop_state) -> None:
    backend, state = native_stop_state
    state.exit_at = 0.4
    assert backend.stop()["ok"] is True
    assert state.elapsed == 0.4
    assert not state.pidfile.exists()


@pytest.mark.parametrize("identity", [{}, {"exe": "foreign", "start_time": 5000.0}])
def test_native_stop_preserves_degraded_or_reused_pid_after_kill(
    native_stop_state, identity
) -> None:
    backend, state = native_stop_state
    state.exit_at = 0.2
    state.after_exit = identity
    before = state.pidfile.read_bytes()
    report = backend.stop()
    if backend is macos and identity:
        assert report["ok"] is True
        assert report["error_code"] is None
        assert not state.pidfile.exists()
        assert len(state.calls) == 2
        return
    assert report["ok"] is False
    assert report["error_code"] == "pid-identity-unsafe"
    assert state.pidfile.read_bytes() == before
    assert len(state.calls) == 2


@pytest.mark.parametrize("replacement", ["{broken", '{"format": 2, "pid": 4343}'])
def test_native_stop_preserves_changed_pidfile(native_stop_state, monkeypatch, replacement) -> None:
    backend, state = native_stop_state

    def sleep(_seconds):
        state.identity = None
        state.pidfile.write_text(replacement, encoding="utf-8")

    monkeypatch.setattr(backend.time, "sleep", sleep)
    report = backend.stop()
    assert report["ok"] is False
    assert report["error_code"] == "pidfile-changed"
    assert state.pidfile.read_text(encoding="utf-8") == replacement


def test_native_stop_successful_command_does_not_prove_exit(native_stop_state) -> None:
    backend, state = native_stop_state
    report = backend.stop()
    assert report["ok"] is False
    assert report["error_code"] == "pid-still-running"
    assert state.elapsed == backend._STOP_TIMEOUT_SECONDS
    assert state.pidfile.exists()


def test_native_stop_confirms_cleanup_despite_helper_removed_flag(
    native_stop_state, monkeypatch
) -> None:
    backend, state = native_stop_state
    state.identity = None

    def denied(_path, **_kwargs):
        raise PermissionError("synthetic denial")

    monkeypatch.setattr(Path, "unlink", denied)
    report = backend.stop()
    assert report["ok"] is False
    assert report["error_code"] == "pidfile-cleanup-incomplete"
    assert state.pidfile.exists()


def test_native_stop_keeps_identity_when_manager_removes_pidfile(
    native_stop_state, monkeypatch
) -> None:
    backend, state = native_stop_state
    run = backend.run_cmd

    def ended(cmd):
        if cmd[1] in {"/End", "bootout"}:
            state.pidfile.unlink()
            state.exit_at = 0.4
        return run(cmd)

    monkeypatch.setattr(backend, "run_cmd", ended)
    assert backend.stop()["ok"] is True
    assert state.elapsed == 0.4
    assert len(state.calls) == 2


def test_macos_already_unloaded_rechecks_pid_after_listing(macos_stop_state, monkeypatch) -> None:
    state = macos_stop_state
    state.identity = None
    state.unload_result = (3, "", "not loaded")
    run = macos.run_cmd

    def listed(cmd):
        if cmd == ["launchctl", "list"]:
            state.identity = {"exe": "python", "start_time": 1000.0}
            return 0, "PID Status Label", ""
        return run(cmd)

    monkeypatch.setattr(macos, "run_cmd", listed)
    assert macos.stop()["ok"] is False
    assert state.pidfile.exists()
    assert [cmd[1] for cmd in state.calls] == ["bootout", "unload"]


def test_native_stop_retains_pid_if_cleanup_identity_degrades(
    native_stop_state, monkeypatch
) -> None:
    backend, state = native_stop_state
    state.identity = None
    cleanup = backend.cleanup_stale_pidfile

    def degraded(**kwargs):
        state.identity = {}
        return cleanup(**kwargs)

    monkeypatch.setattr(backend, "cleanup_stale_pidfile", degraded)
    report = backend.stop()
    assert report["ok"] is False
    assert report["error_code"] == "pidfile-cleanup-incomplete"
    assert state.pidfile.exists()


def test_native_stop_never_signals_or_removes_initial_reused_pid(native_stop_state) -> None:
    backend, state = native_stop_state
    state.identity = {"exe": "foreign", "start_time": 5000.0}
    before = state.pidfile.read_bytes()
    report = backend.stop()
    assert report["ok"] is False
    assert report["error_code"] == "pid-identity-unsafe"
    assert state.pidfile.read_bytes() == before
    assert len(state.calls) == 1


def test_macos_uninstall_reports_definition_cleanup_failure(macos_stop_state, monkeypatch) -> None:
    state = macos_stop_state
    state.identity = None
    state.unload_result = (0, "unloaded", "")
    definition = macos.plist_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")
    unlink = Path.unlink

    def denied(path, **kwargs):
        if path == definition:
            raise PermissionError("synthetic private failure")
        return unlink(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    report = macos.uninstall()
    assert report["ok"] is False
    assert report["error_code"] == "definition-cleanup-incomplete"
    assert "definition could not be removed" in report["detail"]
    assert "synthetic private failure" not in report["detail"]
    assert definition.exists()
    assert not state.pidfile.exists()

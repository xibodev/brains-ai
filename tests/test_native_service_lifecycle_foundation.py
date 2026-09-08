from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brains import service
from brains.cli.app import app
from brains.service import common as service_common
from brains.service import linux, macos, windows
from brains.service.common import ServiceSpec, read_service_config, write_service_config


def test_windows_install_is_failed_when_immediate_start_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        windows,
        "run_cmd",
        lambda command, **_kwargs: (
            (1, "", "start failed") if command[1].casefold() == "/run" else (0, "ok", "")
        ),
    )
    report = windows.install(ServiceSpec(program="python"))
    assert report["ok"] is False
    assert report["started"] is False


def test_windows_uninstall_stops_before_delete_and_retains_definition_on_failure(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    definition = windows.definition_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return (1, "", "stop failed") if command[1].casefold() == "/end" else (0, "ok", "")

    monkeypatch.setattr(windows, "run_cmd", run)
    monkeypatch.setattr(
        windows,
        "verify_pid",
        lambda _record: {"pid": 42, "confidence": "degraded", "reason": "uncertain"},
    )
    report = windows.uninstall()
    assert report["ok"] is False
    assert definition.is_file()
    assert not any(command[1].casefold() == "/delete" for command in calls)


def test_windows_uninstall_reaps_verified_tree_before_task_deletion(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    definition = windows.definition_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda _pid: {"exe": "python", "start_time": 1000.0},
    )
    service_common.write_pidfile(pid=42)

    def run(command):
        calls.append(command)
        if command[0] == "taskkill":
            monkeypatch.setattr(service_common, "_read_process_identity", lambda _pid: None)
        if command[1] == "/Delete":
            assert not service_common.default_pidfile_path().exists()
        return 0, "ok", ""

    monkeypatch.setattr(windows, "run_cmd", run)
    report = windows.uninstall()
    assert report["ok"] is True
    verbs = [
        command[0].casefold() if command[0] == "taskkill" else command[1].casefold()
        for command in calls
    ]
    assert verbs == ["/end", "taskkill", "/delete"]
    assert not definition.exists()


def test_macos_stop_boots_out_keepalive_job_and_uninstall_retains_failed_definition(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path / "state"))
    definition = macos.plist_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] in {"bootout", "unload", "print", "list"}:
            return 1, "", "manager refused"
        return 0, "ok", ""

    monkeypatch.setattr(macos, "run_cmd", run)
    monkeypatch.setattr(
        macos,
        "verify_pid",
        lambda _record: {"pid": 42, "confidence": "degraded", "reason": "uncertain"},
    )
    report = macos.uninstall()
    assert report["ok"] is False
    assert definition.is_file()
    assert any(command[1] == "bootout" for command in calls)
    assert not any(command[1] == "stop" for command in calls)


@pytest.mark.parametrize("stale_pid", [False, True])
def test_macos_repeated_uninstall_accepts_authoritative_absence(
    tmp_path, monkeypatch, stale_pid
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(service_common, "_read_process_identity", lambda _pid: None)
    definition = macos.plist_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")
    foreign = definition.with_name("com.example.foreign.plist")
    foreign.write_text("foreign", encoding="utf-8")
    if stale_pid:
        service_common.write_pidfile(pid=4242)
    calls = []

    def run(command):
        calls.append(command)
        if command == ["launchctl", "list"]:
            return 0, "PID\tStatus\tLabel\n-\t0\tcom.example.foreign", ""
        assert command[1] in {"bootout", "unload"}
        return 3, "", "not loaded"

    monkeypatch.setattr(macos, "run_cmd", run)
    for _ in range(2):
        assert macos.uninstall()["ok"] is True
        assert not definition.exists()
        assert not service_common.default_pidfile_path().exists()
        assert foreign.read_text(encoding="utf-8") == "foreign"
    assert [cmd[1] for cmd in calls] == ["bootout", "unload", "list"] * 2


@pytest.mark.parametrize(
    "listing",
    [
        (1, "", "manager unavailable"),
        (127, "", "utility unavailable"),
        (0, "", ""),
        (0, "unexpected output", ""),
        (0, "PID Status Label\nmalformed", ""),
        (0, "PID Status Label\n- 0 com.brains.serve-all", ""),
    ],
)
def test_macos_uninstall_requires_authoritative_absence(tmp_path, monkeypatch, listing) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path / "state"))
    definition = macos.plist_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")
    canary = "synthetic-private-command-output"

    def run(command):
        if command == ["launchctl", "list"]:
            return listing
        assert command[1] in {"bootout", "unload"}
        return 5, "", canary

    monkeypatch.setattr(macos, "run_cmd", run)
    report = macos.uninstall()
    assert report["ok"] is False
    assert report["error_code"] == "native-unload-failed"
    assert canary not in report["error_code"]
    assert definition.read_text(encoding="utf-8") == "owned"


@pytest.mark.parametrize(
    "identity",
    [{"exe": "python", "start_time": 1000.0}, {}, {"exe": "foreign", "start_time": 5000.0}],
)
def test_macos_already_unloaded_preserves_live_pid_and_definition(
    tmp_path, monkeypatch, identity
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        service_common,
        "_read_process_identity",
        lambda _pid: {"exe": "python", "start_time": 1000.0},
    )
    service_common.write_pidfile(pid=4242)
    pidfile = service_common.default_pidfile_path()
    before = pidfile.read_bytes()
    monkeypatch.setattr(service_common, "_read_process_identity", lambda _pid: identity)
    definition = macos.plist_path()
    definition.parent.mkdir(parents=True)
    definition.write_text("owned", encoding="utf-8")

    def run(command):
        assert command[0] == "launchctl" and command[1] in {"bootout", "unload"}
        return 3, "", "not loaded"

    monkeypatch.setattr(macos, "run_cmd", run)
    assert macos.uninstall()["ok"] is False
    assert pidfile.read_bytes() == before
    assert definition.read_text(encoding="utf-8") == "owned"


def test_linux_install_never_changes_linger_and_failed_uninstall_retains_unit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if "disable" in command:
            return 1, "", "disable failed"
        return 0, "ok", ""

    monkeypatch.setattr(linux, "run_cmd", run)
    spec = ServiceSpec(program="python", state_dir=str(tmp_path / "state"))
    assert linux.install(spec)["ok"] is True
    unit = linux.unit_path()
    assert unit.is_file()
    assert linux.uninstall()["ok"] is False
    assert unit.is_file()
    assert all(command[0] != "loginctl" for command in calls)


def test_linux_uninstall_restores_definition_when_reload_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    calls: list[list[str]] = []
    reloads = 0

    def run(command, **_kwargs):
        nonlocal reloads
        calls.append(command)
        if "daemon-reload" in command:
            reloads += 1
            if reloads == 2:
                return 1, "", "reload failed"
        return 0, "ok", ""

    monkeypatch.setattr(linux, "run_cmd", run)
    spec = ServiceSpec(program="python", state_dir=str(tmp_path / "state"))
    assert linux.install(spec)["ok"] is True
    unit = linux.unit_path()
    before = unit.read_text(encoding="utf-8")
    report = linux.uninstall()
    assert report["ok"] is False
    assert unit.read_text(encoding="utf-8") == before


def test_public_install_rolls_back_when_owned_protocol_readiness_never_arrives(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class Backend:
        @staticmethod
        def install(_spec, *, dry_run=False):
            return {"ok": True, "action": "install"}

        @staticmethod
        def uninstall(*, dry_run=False, label):
            calls.append(("uninstall", label))
            return {"ok": True, "action": "uninstall"}

    monkeypatch.setattr(service, "current_platform", lambda: "linux")
    monkeypatch.setattr(service, "_backend", lambda: Backend)
    monkeypatch.setattr(
        service,
        "verify_service_interpreter",
        lambda program: {"ok": True, "program": program, "detail": ""},
    )
    monkeypatch.setattr(service, "_wait_for_ready", lambda _spec: {"ready": False})
    report = service.install(
        ServiceSpec(program=sys.executable, label="brains-serve-all-evidence-a1")
    )
    assert report["ok"] is False
    assert report["action"] == "install-rolled-back"
    assert calls == [("uninstall", "brains-serve-all-evidence-a1")]


def test_public_install_persists_config_only_after_readiness(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))

    class Backend:
        @staticmethod
        def install(_spec, *, dry_run=False):
            return {"ok": True, "action": "install"}

    monkeypatch.setattr(service, "current_platform", lambda: "linux")
    monkeypatch.setattr(service, "_backend", lambda: Backend)
    monkeypatch.setattr(
        service,
        "verify_service_interpreter",
        lambda program: {"ok": True, "program": program, "detail": ""},
    )
    monkeypatch.setattr(
        service,
        "_wait_for_ready",
        lambda _spec: {"ready": True, "listeners": {}, "service_pid": {}},
    )
    report = service.install(
        ServiceSpec(
            program=sys.executable,
            label="brains-serve-all-evidence-a1",
            gateway_port=18878,
            mcp_port=19878,
        )
    )
    assert report["ok"] is True
    assert Path(report["config"]).is_file()
    assert read_service_config() == {
        "gateway_host": "127.0.0.1",
        "gateway_port": 18878,
        "mcp_port": 19878,
        "service_label": "brains-serve-all-evidence-a1",
    }


def test_persisted_service_label_routes_subsequent_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    write_service_config(ServiceSpec(program="python", label="brains-serve-all-evidence-a1"))
    assert read_service_config()["service_label"] == "brains-serve-all-evidence-a1"


def test_service_cli_returns_nonzero_for_structured_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "start",
        lambda **_kwargs: {"ok": False, "action": "start", "detail": "bounded failure"},
    )
    result = CliRunner().invoke(app, ["service", "start"])
    assert result.exit_code == 1
    assert '"ok": false' in result.stdout


def test_native_evidence_scaffold_is_manual_guarded_and_truthful() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/native-service-evidence.yml").read_text(encoding="utf-8")
    probe = (root / "scripts/probe_native_service_lifecycle.py").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow and "pull_request:" not in workflow
    assert "windows-2022" in workflow
    assert "macos-14" in workflow
    assert "ubuntu-24.04" in workflow
    assert "manager-cycle" in workflow
    assert "BRAINS_NATIVE_EVIDENCE_DISPOSABLE" in workflow
    assert "BRAINS_NATIVE_EVIDENCE_ROOT" in workflow
    assert "opencode-ai@1.18.25" in workflow
    assert 'python: ["3.11", "3.12"]' in workflow
    assert "adapter: [copilot-cli, claude-code, codex, opencode]" in workflow
    assert "transport: [streamable-http]" in workflow
    assert "create_provenance" in probe
    assert "provenance_sha256" in probe
    assert "FORBIDDEN_PORTS = {9876, 9877}" in probe
    assert '"login_transition_attestation": None' in probe
    assert "--login-transition-observed" not in probe
    assert 'choices=("prepare", "verify", "manager-cycle", "cleanup")' in probe
    assert "if: always()" in workflow
    assert "if: success()" in workflow
    assert '"error_type": type(exc).__name__' in probe
    assert '"error": str(exc)' not in probe


def test_native_evidence_probe_refuses_without_guard_and_redacts_bad_input(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/probe_native_service_lifecycle.py"
    env = dict(os.environ)
    env.pop("BRAINS_NATIVE_EVIDENCE_DISPOSABLE", None)
    env["BRAINS_NATIVE_EVIDENCE_ROOT"] = str(tmp_path / "runtime")
    env["BRAINS_STATE_DIR"] = str(tmp_path / "runtime" / "state")
    common = [
        "--wheel",
        str(tmp_path / "candidate.whl"),
        "--package-manifest",
        str(tmp_path / "package-manifest.json"),
        "--git-executable",
        str(Path(shutil.which("git") or "")),
        "--adapter",
        "codex",
    ]
    guarded_output = tmp_path / "guarded.json"
    guarded = subprocess.run(
        [
            sys.executable,
            str(script),
            "prepare",
            "--candidate",
            "a" * 40,
            *common,
            "--output",
            str(guarded_output),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert guarded.returncode == 1
    assert json.loads(guarded_output.read_text(encoding="utf-8"))["error_type"] == (
        "EvidenceFailure"
    )

    invalid_output = tmp_path / "invalid.json"
    invalid_value = "must-not-be-echoed"
    invalid = subprocess.run(
        [
            sys.executable,
            str(script),
            "prepare",
            "--candidate",
            invalid_value,
            *common,
            "--output",
            str(invalid_output),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert invalid.returncode == 1
    artifact = invalid_output.read_text(encoding="utf-8")
    assert invalid_value not in artifact
    assert invalid_value not in invalid.stdout
    assert invalid_value not in invalid.stderr

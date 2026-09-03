from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from brains import service
from brains.cli.app import app
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
        windows,
        "run_cmd",
        lambda command, **_kwargs: calls.append(command) or (0, "ok", ""),
    )
    monkeypatch.setattr(
        windows,
        "verify_pid",
        lambda _record: {"pid": 42, "confidence": "verified", "reason": "owned"},
    )
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
    assert "BRAINS_EVIDENCE_WHEEL_SHA256" in workflow
    assert "FORBIDDEN_PORTS = {9876, 9877}" in probe
    assert '"login_persistence": False' in probe
    assert 'choices=("prepare", "verify", "manager-cycle", "cleanup")' in probe
    assert "if: always()" in workflow
    assert '"error_type": type(exc).__name__' in probe
    assert '"error": str(exc)' not in probe


def test_native_evidence_probe_refuses_without_guard_and_redacts_bad_input(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/probe_native_service_lifecycle.py"
    env = dict(os.environ)
    env.pop("BRAINS_NATIVE_EVIDENCE_DISPOSABLE", None)
    guarded_output = tmp_path / "guarded.json"
    guarded = subprocess.run(
        [
            sys.executable,
            str(script),
            "prepare",
            "--candidate",
            "a" * 40,
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

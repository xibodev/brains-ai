from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def installation_step() -> dict:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (root / ".github/workflows/native-service-evidence.yml").read_text(encoding="utf-8")
    )
    return next(
        step
        for step in workflow["jobs"]["manager-cycle"]["steps"]
        if step.get("name")
        == "Build and install the exact candidate in a fresh private environment"
    )


def test_native_service_workflow_uses_portable_paths_and_digest(installation_step: dict) -> None:
    script = installation_step["run"]
    assert installation_step["shell"] == "bash"
    assert installation_step["env"]["EXPECTED_MANIFEST_SHA256"] == (
        "${{ needs.package.outputs.manifest-sha256 }}"
    )
    assert 'temp_dir="$(cygpath -u "$RUNNER_TEMP")"' in script
    assert 'temp_dir="$RUNNER_TEMP"' in script
    assert script.count('find "$temp_dir/native-service-dist"') == 2
    assert 'find "$RUNNER_TEMP/' not in script
    assert "sha256sum" not in script
    for name in ("artifact", "runtime", "venv"):
        assert f'{name}="$temp_dir/native-service-{name}-' in script
    assert 'test "$wheel_count" = 1' in script
    assert "BRAINS_NATIVE_PACKAGE_MANIFEST=$temp_dir/native-service-dist/" in script
    assert script.index('runtime="$(cygpath -w "$runtime")"') < script.index(
        'echo "BRAINS_NATIVE_EVIDENCE_ROOT=$runtime"'
    )
    assert 'echo "BRAINS_STATE_DIR=$runtime/state"' in script
    assert script.index("python - <<'PY'") < script.index('python -m venv "$venv"')


@pytest.mark.parametrize("case", ("matching", "mismatched", "missing"))
def test_native_service_workflow_checks_manifest_bytes(
    installation_step: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    dist = tmp_path / "native-service-dist"
    dist.mkdir()
    content = b'{"synthetic": true}\n'
    if case != "missing":
        (dist / "native-wakeup-package-provenance.json").write_bytes(content)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv(
        "EXPECTED_MANIFEST_SHA256",
        "0" * 64 if case == "mismatched" else hashlib.sha256(content).hexdigest(),
    )
    source = installation_step["run"].split("python - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    code = compile(source, "native-service-workflow-manifest", "exec")
    if case == "matching":
        exec(code, {})
    elif case == "mismatched":
        with pytest.raises(SystemExit, match="candidate package provenance does not match"):
            exec(code, {})
    else:
        with pytest.raises(FileNotFoundError):
            exec(code, {})


def test_native_service_workflow_keeps_failure_logs_separate_from_qualification() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (root / ".github/workflows/native-service-evidence.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["manager-cycle"]["steps"]
    probes = [step for step in steps if "probe_native_service_lifecycle.py" in step.get("run", "")]
    assert len(probes) == 2
    assert probes[1]["if"] == "always()"
    for step in probes:
        assert "continue-on-error" not in step
        assert "2>" not in step["run"]
        assert "||" not in step["run"]
        assert "tee " not in step["run"]
    uploads = [step for step in steps if step.get("uses") == "actions/upload-artifact@v4"]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "success()"
    assert all(
        path.endswith(
            (
                "/native-service-prepare.json",
                "/native-service-prepare.xml",
                "/native-service-cleanup.json",
                "/native-service-cleanup.xml",
            )
        )
        for path in uploads[0]["with"]["path"].splitlines()
    )
    verifier = next(step for step in steps if "verify_native_evidence.py" in step.get("run", ""))
    assert steps.index(verifier) < steps.index(uploads[0])
    assert verifier.get("if", "success()") == "success()"
    assert "continue-on-error" not in verifier


def test_opencode_provisioning_creates_only_isolated_parents() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (root / ".github/workflows/native-service-evidence.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["manager-cycle"]["steps"]
    step = next(step for step in steps if step.get("name") == "Provision pinned supported OpenCode")
    script = step["run"]
    assert step["if"] == "matrix.adapter == 'opencode'"
    assert step["shell"] == "bash"
    assert (
        'provisioning="$(mktemp -d "$temp_dir/native-service-opencode-provision.XXXXXX")"' in script
    )
    assert 'temp_dir="$(cygpath -u "$RUNNER_TEMP")"' in script
    mkdir = next(line.strip() for line in script.splitlines() if line.strip().startswith("mkdir "))
    assert mkdir == (
        'mkdir "$provisioning/config" "$provisioning/data" '
        '"$provisioning/cache" "$provisioning/state"'
    )
    assert script.index(mkdir) < script.index('provisioning="$(cygpath -w "$provisioning")"')
    for name, directory in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
        ("OPENCODE_CONFIG_DIR", "config/opencode"),
    ):
        export = f'export {name}="$provisioning/{directory}"'
        assert script.index('provisioning="$(cygpath -w "$provisioning")"') < script.index(export)
        assert script.index(export) < script.index("npm install --global opencode-ai@1.18.25")
    for forbidden in ("GITHUB_ENV", "$HOME", "rm ", "rmdir", "--ignore-scripts"):
        assert forbidden not in script
    assert sum(line.strip().startswith("mkdir ") for line in script.splitlines()) == 1
    probe = next(step for step in steps if step.get("name", "").startswith("Exercise native"))
    assert steps.index(step) < steps.index(probe)


def test_linux_preflight_requires_real_user_manager_and_owned_bus() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (root / ".github/workflows/native-service-evidence.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["manager-cycle"]
    linux = [
        host for host in job["strategy"]["matrix"]["host"] if host["manager"] == "systemd-user"
    ]
    assert linux == [{"os": "ubuntu-24.04", "manager": "systemd-user"}]
    steps = job["steps"]
    step = next(step for step in steps if step.get("name") == "Preflight real Linux user manager")
    assert step["if"] == "runner.os == 'Linux'"
    assert step["shell"] == "bash"
    assert step["timeout-minutes"] == 2
    assert "continue-on-error" not in step
    script = step["run"]
    required = (
        "set -euo pipefail",
        'test "$BRAINS_NATIVE_EVIDENCE_DISPOSABLE" = disposable-native-service-host',
        'uid="$(id -u)"',
        'sudo -n systemctl start "user@$uid.service"',
        'sudo -n systemctl is-active --quiet "user@$uid.service"',
        'export XDG_RUNTIME_DIR="/run/user/$uid"',
        'test -d "$XDG_RUNTIME_DIR"',
        'test -O "$XDG_RUNTIME_DIR"',
        'test -L "$XDG_RUNTIME_DIR"',
        'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"',
        "systemctl --user start dbus.socket",
        'test -S "$XDG_RUNTIME_DIR/bus"',
        'test -O "$XDG_RUNTIME_DIR/bus"',
        'test -L "$XDG_RUNTIME_DIR/bus"',
        "systemctl --user show --property=Version --value",
        'echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR" >> "$GITHUB_ENV"',
        'echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS" >> "$GITHUB_ENV"',
    )
    positions = [script.index(command) for command in required]
    assert positions == sorted(positions)
    assert script.count("::error::native-preflight:") == script.count("exit 1") == 6
    for forbidden in (
        "mkdir",
        "dbus-run-session",
        "dbus-launch",
        "enable-linger",
        "journalctl",
        "printenv",
        "show-environment",
        "systemctl status",
        "set -x",
        "|| true",
    ):
        assert forbidden not in script
    probe = next(step for step in steps if step.get("name", "").startswith("Exercise native"))
    assert steps.index(step) < steps.index(probe)


def test_windows_operational_log_is_enabled_only_on_disposable_runner() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (root / ".github/workflows/native-service-evidence.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["manager-cycle"]
    steps = job["steps"]
    step = next(
        row
        for row in steps
        if row.get("name") == "Enable disposable Windows task event diagnostics"
    )
    assert step["if"] == "runner.os == 'Windows'"
    assert step["shell"] == "pwsh" and step["timeout-minutes"] == 2
    script = step["run"]
    assert script.index("disposable-native-service-host") < script.index("wevtutil.exe")
    assert "/e:true *> $null" in script
    assert "Get-WinEvent -ListLog" in script
    assert "$_" not in script and "Format-List" not in script
    assert '"enabled":true' in script and '"enabled":false' in script
    assert "continue-on-error" not in step
    assert steps.index(step) < next(
        i for i, row in enumerate(steps) if row.get("name", "").startswith("Exercise native")
    )
    assert len(job["strategy"]["matrix"]["host"]) == 3

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "sandbox" / "pivot" / "try" / "up.ps1"
DOWN = ROOT / "sandbox" / "pivot" / "try" / "down.ps1"
STACK = ROOT / "tests" / "e2e" / "fixtures" / "stack.ts"
GLOBAL_SETUP = ROOT / "tests" / "e2e" / "fixtures" / "global-setup.ts"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_windows_e2e_stack_keeps_labs_disabled() -> None:
    script = _text(UP)

    assert "$PSScriptRoot" in script
    assert "E:\\open-source-projects" not in script
    assert '"/v1/runtimes/register"' in script
    assert "E2E Simulated Runtime" in script
    assert "daemon start" not in script
    assert "daemonlaunch.py" not in script
    assert "--allow-all" not in script
    assert "copilot -p" not in script
    assert "Start-Process powershell" not in script
    assert "WorkingDirectory $state" in script
    assert "configured (value hidden)" in script
    assert "BRAINS_CONFIG" in script
    assert "BRAINS_RUNTIME_OVERLAY" in script
    assert "OPENAI_" in script
    assert "GH_" in script
    assert "UVICORN_" in script
    assert "WEB_CONCURRENCY" in script
    assert '"--workers", "1"' in script
    assert "BRAINS_UI_LABS" not in script


def test_ci_e2e_stack_uses_the_normal_install_contract() -> None:
    workflow = _text(CI)

    assert "BRAINS_UI_LABS" not in workflow
    assert "/v1/runtimes/register" not in workflow
    assert "/v1/orgs/demo/personas" not in workflow
    assert "/v1/orgs/demo/projects" not in workflow


def test_windows_e2e_stack_guards_the_worktree() -> None:
    up = _text(UP)
    down = _text(DOWN)
    status_command = "status --porcelain=v1 --untracked-files=all"

    assert status_command in up
    assert status_command in down
    assert "git-status.before" in up
    assert "git-status.before" in down
    assert "hash-object" in up
    assert "hash-object" in down
    assert "E2E changed the repository worktree" in down


def test_windows_e2e_teardown_only_stops_the_recorded_process_tree() -> None:
    script = _text(DOWN)

    assert "Get-CimInstance Win32_Process" in script
    assert "ParentProcessId = $ParentId" in script
    assert "ExecutablePath" in script
    assert "StartTime.ToFileTimeUtc()" in script
    assert "uvicorn\\s+brains\\.main:app" in script
    assert "Stop-Process -InputObject $hubHandle" in script
    assert "Stop-Process -InputObject $handle" in script
    assert "Stop-Process -InputObject $hub" in _text(UP)
    assert "Get-NetTCPConnection" in script
    assert "refusing to stop an unverified process" in script
    assert "ForEach-Object { Stop-Process" not in script


def test_windows_e2e_state_requires_an_ownership_marker() -> None:
    up = _text(UP)
    down = _text(DOWN)

    assert "^[a-z0-9][a-z0-9_-]{0,62}$" in up
    assert "^[a-z0-9][a-z0-9_-]{0,62}$" in down
    assert "brains-e2e-harness" in up
    assert "brains-e2e-harness" in down
    assert "refusing to delete it" in down


def test_windows_auto_stack_rejects_non_loopback_targets() -> None:
    stack = _text(STACK)
    setup = _text(GLOBAL_SETUP)

    assert "127.0.0.1" in stack
    assert "localhost" in stack
    assert "requires a plain loopback HTTP base URL" in stack
    assert "BRAINS_E2E_STACK_NAME" in stack
    assert "BRAINS_E2E_STACK_KEY" in setup
    assert "'-Port'" in setup
    assert "'-Name'" in setup
    assert "'-Key'" not in setup


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell harness")
def test_windows_e2e_stack_rejects_path_traversal_names() -> None:
    powershell = shutil.which("powershell")
    assert powershell is not None

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(UP),
            "-Name",
            r"..\escape",
            "-Port",
            "8899",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "Harness name must be a lowercase slug" in (result.stdout + result.stderr)

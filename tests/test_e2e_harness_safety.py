from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "sandbox" / "pivot" / "try" / "up.ps1"
DOWN = ROOT / "sandbox" / "pivot" / "try" / "down.ps1"
STACK = ROOT / "tests" / "e2e" / "fixtures" / "stack.ts"
GLOBAL_SETUP = ROOT / "tests" / "e2e" / "fixtures" / "global-setup.ts"
DOCKER_E2E = ROOT / "scripts" / "run_docker_e2e.ps1"
DOCKER_CLI_UAT = ROOT / "scripts" / "run_docker_cli_uat.ps1"
DOCKER_CLI_UAT_FILE = ROOT / "docker" / "Dockerfile.cli-uat"
CLAUDE_WAKEUP_UAT = ROOT / "scripts" / "run_docker_claude_wakeup_probe.ps1"
REAL_CLI_ACTOR = ROOT / "tests" / "uat" / "real_cli_actor.py"
DOCKER_QUALITY = ROOT / "scripts" / "run_docker_quality.ps1"
DOCKER_QUALITY_FILE = ROOT / "docker" / "Dockerfile.quality"
DOCKERIGNORE = ROOT / ".dockerignore"
GITATTRIBUTES = ROOT / ".gitattributes"
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
    assert "npm run typecheck" in workflow


def test_ci_workflow_is_valid_yaml() -> None:
    assert isinstance(yaml.safe_load(_text(CI)), dict)


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


def test_docker_e2e_is_private_owned_and_volume_free() -> None:
    script = _text(DOCKER_E2E)

    assert "network create --internal" in script
    assert "PowerShell 7 or newer is required" in script
    assert "--cap-drop ALL" in script
    assert "no-new-privileges:true" in script
    assert '--tmpfs "/tmp:rw,nosuid,nodev,mode=1777"' in script
    assert "PortBindings" in script
    assert "unexpectedly publishes a host port" in script
    assert "unexpectedly uses a persistent or host mount" in script
    assert '--tmpfs "/data:' in script
    assert "BRAINS_DB_URL=sqlite:////data/brains.db" in script
    assert "BRAINS_STATE_DIR=/data/.brains" in script
    assert '-e "HOME=/tmp"' in script
    assert "Refusing to reuse pre-existing container" in script
    assert "Refusing to reuse pre-existing network" in script
    assert "Refusing to overwrite pre-existing image" in script
    assert "docker network rm $network" in script
    assert "Container teardown was incomplete" in script
    assert "Network teardown was incomplete" in script
    assert "Image teardown was incomplete" in script
    assert "--mount" not in script and "--volume" not in script
    assert "-p " not in script and "--publish" not in script


def test_docker_quality_has_no_network_mount_or_capabilities() -> None:
    script = _text(DOCKER_QUALITY)
    dockerfile = _text(DOCKER_QUALITY_FILE)

    assert "--network none" in script
    assert "PowerShell 7 or newer is required" in script
    assert "--cap-drop ALL" in script
    assert "no-new-privileges:true" in script
    assert '--tmpfs "/tmp:rw,exec,nosuid,nodev,mode=1777"' in script
    assert "--mount" not in script and "--volume" not in script
    assert "Refusing to reuse pre-existing container" in script
    assert "Refusing to overwrite pre-existing image" in script
    assert "Quality container teardown was incomplete" in script
    assert "Quality image teardown was incomplete" in script
    assert "COPY . ." in dockerfile
    assert "uv sync --extra dev --python 3.12" in dockerfile
    assert 'uv pip install "setuptools==84.0.0" "wheel==0.48.0"' in dockerfile
    quality_runner = _text(ROOT / "docker" / "run-quality-gates.sh")
    assert quality_runner.count("PYTHONPATH=/work pytest") == 2
    assert "uv build --no-build-isolation" in quality_runner
    assert "python scripts/check_core_surface.py --dist dist" in quality_runner


def test_real_cli_uat_uses_owned_isolation_and_real_resume_contracts() -> None:
    script = _text(DOCKER_CLI_UAT)
    dockerfile = _text(DOCKER_CLI_UAT_FILE)
    actor = _text(REAL_CLI_ACTOR)

    assert "network create --internal" in script
    assert "Refusing to reuse pre-existing Docker" in script
    assert "type=bind,source=$primary,target=/run/credentials/primary,readonly" in script
    assert "type=volume,source=$stateVolume,target=/data" in script
    assert '"/home/node:rw,exec,nosuid,nodev,uid=1000,gid=1000,mode=0700"' in script
    assert "PortBindings" in script
    assert "published a host port" in script
    assert "teardown_verified" in script
    assert "Real-CLI UAT changed the candidate worktree" in script
    assert "candidate_worktree_hash" in script
    assert "beforeUntracked" in script
    assert '2 "mailbox_registration"' in script
    assert "maximum_attempts" in script
    assert "docker volume rm $stateVolume" in script
    assert "docker network rm $controlNetwork" in script
    assert "docker image rm $cliImage" in script
    assert '"-p"' not in script and '"--publish"' not in script
    assert "source=$root" not in script

    for package in (
        "@anthropic-ai/claude-code",
        "@github/copilot",
        "opencode-ai",
        "@openai/codex",
    ):
        assert package in dockerfile
    for token in ("PELICAN", "KIWI", "MANGO", "ZEBRA"):
        assert token in script
    for native_key in ("session_id", "sessionId", "sessionID", "thread_id"):
        assert native_key in actor
    assert '"--resume", session_id' in actor
    assert '"--session-id", session_id' in actor
    assert '"--session", session_id' in actor
    assert '"exec",\n                "resume"' in actor
    assert actor.count('"--dangerously-bypass-approvals-and-sandbox"') == 2
    assert '"features.shell_tool=false"' in actor
    assert '"features.apps=false"' in actor
    assert "brains_mailbox_send" in actor
    assert "brains_mailbox_inbox" in actor
    assert "brains_mailbox_reply" in actor
    assert 'transport="stdio"' in actor
    assert '"BRAINS_DB_URL": os.environ["BRAINS_DB_URL"]' in actor
    assert "shutil.copyfile(source, target)" in actor
    assert '_copy_secret(primary, HOME / ".copilot/config.json")' in actor


def test_claude_wakeup_probe_is_pinned_checked_and_isolated() -> None:
    script = _text(CLAUDE_WAKEUP_UAT)
    dockerfile = _text(DOCKER_CLI_UAT_FILE)
    workflow = _text(ROOT / ".github" / "workflows" / "ci.yml")

    assert "ARG CLAUDE_VERSION=2.1.259" in dockerfile
    assert "COPY tests/uat/claude_wakeup_probe.py" in dockerfile
    assert "SOURCE_COMMIT" in dockerfile
    assert "status --porcelain=v1 --untracked-files=all" in script
    assert "SOURCE_COMMIT=$commit" in script
    assert "--network none" in script
    assert "--cap-drop ALL" in script
    assert "no-new-privileges:true" in script
    assert "--mount" not in script and "--volume" not in script
    assert "-p " not in script and "--publish" not in script
    assert "function Invoke-DockerQuiet" in script
    assert '$ErrorActionPreference = "SilentlyContinue"' in script
    assert 'Invoke-DockerQuiet @("container", "inspect", $container)' in script
    assert 'Invoke-DockerQuiet @("image", "inspect", $image)' in script
    assert 'Invoke-DockerQuiet @("rm", "-f", $container)' in script
    assert 'Invoke-DockerQuiet @("image", "rm", $image)' in script
    assert "run_docker_claude_wakeup_probe.ps1" in workflow


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5 regression")
def test_claude_wakeup_probe_tolerates_expected_absence_in_windows_powershell_5(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell")
    assert powershell is not None
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    probe = scripts / CLAUDE_WAKEUP_UAT.name
    shutil.copyfile(CLAUDE_WAKEUP_UAT, probe)
    command_log = tmp_path / "docker-commands.txt"
    (tmp_path / "git.cmd").write_text(
        "@echo off\n"
        'if "%3"=="status" exit /b 0\n'
        'if "%3"=="rev-parse" echo 0000000000000000000000000000000000000000& exit /b 0\n'
        "exit /b 1\n",
        encoding="ascii",
    )
    (tmp_path / "docker.cmd").write_text(
        "@echo off\n"
        'echo %*>>"%FAKE_DOCKER_LOG%"\n'
        'if "%1 %2"=="container inspect" echo expected absent 1>&2& exit /b 1\n'
        'if "%1 %2"=="image inspect" echo expected absent 1>&2& exit /b 1\n'
        "exit /b 0\n",
        encoding="ascii",
    )
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(command_log),
    }

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
            "-Name",
            "brains-claude-ps5-regression",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = command_log.read_text(encoding="utf-8")
    assert "container inspect brains-claude-ps5-regression" in commands
    assert "image inspect brains-claude-ps5-regression:local" in commands
    assert "rm -f brains-claude-ps5-regression" in commands
    assert "image rm brains-claude-ps5-regression:local" in commands


def test_docker_context_excludes_private_host_state_and_linux_script_keeps_lf() -> None:
    ignored = _text(DOCKERIGNORE)

    assert "brains.db-*" in ignored
    assert "*.pem" in ignored
    assert "*.key" in ignored
    assert ".brains" in ignored
    assert ".opencode" in ignored
    assert "docker/run-quality-gates.sh text eol=lf" in _text(GITATTRIBUTES)


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
    assert "Harness name must be a lowercase slug" in result.stdout + result.stderr

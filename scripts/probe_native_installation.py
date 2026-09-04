"""Prove an exact wheel's clean-home native installation and wire contract.

This probe never invokes a native service manager. It validates the installed
wheel, rendered manager definition, and one explicitly selected adapter on a
fresh synthetic home. Real manager/login/reboot execution remains BL-P0-06.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from native_evidence import (
    ProvenanceFailure,
    account_managed_backups,
    assert_sanitized,
    canonical_sha256,
    create_provenance,
    explicit_runtime_tools,
    file_sha256,
    require_fresh_output,
    snapshot_files,
)

TOOLS = ("copilot-cli", "claude-code", "codex", "opencode")
OPENCODE_VERSION = "1.18.25"
FORBIDDEN_PORTS = {9876, 9877}


def _run(executable: Path, env: dict[str, str], *args: str) -> dict[str, Any]:
    completed = subprocess.run(
        [str(executable), *args],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=120,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProvenanceFailure("installed executable returned a non-JSON result") from exc
    if completed.returncode != 0 or payload.get("ok") is False:
        raise ProvenanceFailure("installed executable reported failure")
    return payload


def _config_path(home: Path, tool: str) -> Path:
    return {
        "copilot-cli": home / ".copilot" / "mcp-config.json",
        "claude-code": home / ".claude.json",
        "codex": home / ".codex" / "config.toml",
        "opencode": home / ".config" / "opencode" / "opencode.json",
    }[tool]


def _config_root(home: Path, tool: str) -> Path:
    return {
        "copilot-cli": home / ".copilot",
        "claude-code": home / ".claude.json",
        "codex": home / ".codex",
        "opencode": home / ".config" / "opencode",
    }[tool]


def _seed(path: Path, tool: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if tool == "codex":
        path.write_text('model = "synthetic-native-probe"\n', encoding="utf-8")
        return
    servers_key = "mcp" if tool == "opencode" else "mcpServers"
    path.write_text(
        json.dumps(
            {
                "synthetic_unmanaged": True,
                servers_key: {"other": {"command": "synthetic-other-server"}},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _port() -> int:
    while True:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            selected = int(listener.getsockname()[1])
        if selected not in FORBIDDEN_PORTS:
            return selected


def _verify_harness(tool: str) -> dict[str, Any]:
    if tool != "opencode":
        return {"adapter": tool, "binary_required_for_wire": False}
    raw = shutil.which("opencode")
    if raw is None:
        raise ProvenanceFailure("pinned OpenCode executable is absent")
    executable = Path(raw).resolve(strict=True)
    completed = subprocess.run(
        [str(executable), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0 or OPENCODE_VERSION not in completed.stdout.split():
        raise ProvenanceFailure("OpenCode does not match the pinned supported version")
    return {
        "adapter": tool,
        "binary_required_for_wire": True,
        "version": OPENCODE_VERSION,
        "executable_sha256": file_sha256(executable),
    }


def _manager() -> str:
    return {
        "Windows": "task-scheduler",
        "Darwin": "launchd",
        "Linux": "systemd-user",
    }.get(platform.system(), "unsupported")


def _remove_synthetic_root(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def run_probe(
    *,
    candidate: str,
    wheel: Path,
    tool: str,
    output: Path,
    repo: Path,
    git_executable: Path,
    runtime_tool_json: str,
) -> dict[str, Any]:
    executable_name = "brains-ai.exe" if os.name == "nt" else "brains-ai"
    executable = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin") / executable_name
    runtime_tools, controlled_path = explicit_runtime_tools(
        runtime_tool_json,
        required=("node", "opencode") if tool == "opencode" else (),
        prepend_paths=(Path(sys.executable).parent,),
    )
    os.environ["PATH"] = controlled_path
    provenance = create_provenance(
        candidate=candidate,
        repo=repo,
        wheel=wheel,
        executable=executable,
        git_executable=git_executable,
        runtime_tools=runtime_tools,
    )
    binding = str(provenance["binding_sha256"])
    steps: list[dict[str, Any]] = []

    def record(step: str, evidence: dict[str, Any]) -> None:
        steps.append(
            {
                "sequence": len(steps) + 1,
                "step": step,
                "passed": True,
                "provenance_sha256": binding,
                "evidence": evidence,
            }
        )

    with tempfile.TemporaryDirectory(prefix="brains-native-install-") as raw:
        root = Path(raw)
        home, state, workspace = root / "home", root / "state", root / "workspace"
        for path in (home, state, workspace):
            path.mkdir(mode=0o700)
        config_root = _config_root(home, tool)
        config_roots = ((tool, config_root),)
        original_snapshot = snapshot_files(config_roots)
        if original_snapshot:
            raise ProvenanceFailure("synthetic client home was not initially empty")
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "BRAINS_STATE_DIR": str(state),
                "BRAINS_API_KEY": "synthetic-native-probe-key",
                "BRAINS_MCP_BEARER_TOKEN": "synthetic-native-probe-key",
            }
        )
        record(
            "provenance",
            {
                "candidate_bound": True,
                "wheel_bound": True,
                "installed_distribution_bound": True,
                "executable_bound": True,
            },
        )
        record("harness", _verify_harness(tool))

        first = _run(executable, env, "setup", "--path", str(workspace), "--no-wire", "--json")
        first_workspace = next(row for row in first["steps"] if row["step"] == "init")["workspace"][
            "slug"
        ]
        gateway_port, mcp_port = _port(), _port()
        while mcp_port == gateway_port:
            mcp_port = _port()
        rendered = _run(
            executable,
            env,
            "service",
            "install",
            "--dry-run",
            "--gateway-port",
            str(gateway_port),
            "--mcp-port",
            str(mcp_port),
        )
        if rendered.get("action") != "would-install" or rendered.get("platform") not in {
            "windows",
            "macos",
            "linux",
        }:
            raise ProvenanceFailure("native manager definition rendering failed")
        record(
            "manager-definition",
            {
                "manager": _manager(),
                "platform": rendered["platform"],
                "native_execution": False,
                "identity": "brains-serve-all",
            },
        )

        path = _config_path(home, tool)
        _seed(path, tool)
        baseline = snapshot_files(config_roots)
        if not baseline:
            raise ProvenanceFailure("synthetic configuration snapshot is empty")
        wired = _run(
            executable,
            env,
            "wire",
            "--tool",
            tool,
            "--force",
            "--no-rules",
            "--transport",
            "streamable-http",
            "--port",
            str(mcp_port),
        )
        if wired.get("ok") is not True:
            raise ProvenanceFailure("adapter wire failed")
        status = _run(executable, env, "wire", "--status")
        selected = next(row for row in status["tools"] if row["tool"] == tool)
        parsed = urllib.parse.urlparse(str(selected.get("mcp_url", "")))
        if (
            selected.get("mcp_wired") is not True
            or selected.get("mcp_transport") != "streamable-http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != mcp_port
            or parsed.path != "/mcp"
        ):
            raise ProvenanceFailure("adapter endpoint or transport identity differs")
        wired_snapshot = snapshot_files(config_roots)
        record(
            "wire",
            {
                "adapter": tool,
                "protocol": "streamable-http",
                "endpoint": {"host": "loopback", "port": mcp_port, "path": "/mcp"},
                "baseline_config_sha256": canonical_sha256(baseline),
                "wired_config_sha256": canonical_sha256(wired_snapshot),
            },
        )

        _run(executable, env, "unwire", "--tool", tool, "--no-rules")
        restored = snapshot_files(config_roots)
        managed_backups = account_managed_backups(baseline, wired_snapshot, restored)
        second = _run(executable, env, "setup", "--path", str(workspace), "--no-wire", "--json")
        second_workspace = next(row for row in second["steps"] if row["step"] == "init")[
            "workspace"
        ]["slug"]
        if second_workspace != first_workspace:
            raise ProvenanceFailure("clean-home setup was not idempotent")
        _remove_synthetic_root(config_root)
        if snapshot_files(config_roots) != original_snapshot:
            raise ProvenanceFailure("synthetic client home was not restored to its initial state")
        record(
            "restoration",
            {
                "baseline_config_sha256": canonical_sha256(baseline),
                "restored_config_sha256": canonical_sha256(restored),
                "primary_configuration_restored": True,
                "managed_backup_count": len(managed_backups),
                "managed_backup_manifest_sha256": canonical_sha256(managed_backups),
                "initial_home_restored": True,
                "setup_idempotent": True,
            },
        )

    result: dict[str, Any] = {
        "schema": "brains.native-installation-evidence.v1",
        "passed": True,
        "matrix": {
            "manager": _manager(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "adapter": tool,
            "transport": "streamable-http",
        },
        "provenance": provenance,
        "steps": steps,
    }
    assert_sanitized(
        result,
        (
            str(Path.home()),
            os.environ.get("USERPROFILE", ""),
            getpass.getuser(),
            "synthetic-native-probe-key",
        ),
    )
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--git-executable", type=Path, required=True)
    parser.add_argument("--tool", choices=TOOLS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        require_fresh_output(args.output)
    except Exception:  # noqa: BLE001 - never overwrite or echo stale-output details
        return 1
    try:
        run_probe(
            candidate=args.candidate,
            wheel=args.wheel,
            tool=args.tool,
            output=args.output,
            repo=Path.cwd(),
            git_executable=args.git_executable,
            runtime_tool_json=os.environ.get("BRAINS_NATIVE_TOOL_PATHS", "{}"),
        )
    except Exception as exc:  # noqa: BLE001 - public artifact exposes type only
        failure = {
            "schema": "brains.native-installation-evidence.v1",
            "passed": False,
            "error_type": type(exc).__name__,
        }
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(failure, sort_keys=True) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Prove an exact wheel's clean-home native installation and wire contract.

This probe never invokes a native service manager. It validates the installed
wheel, rendered manager definition, and one explicitly selected adapter on a
fresh synthetic home. Real manager/login/reboot execution is a separate recurring
release-qualification condition.
"""

from __future__ import annotations

import argparse
import getpass
import itertools
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

from brains.service.common import native_service_identity

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
    # Name the subcommand so a failing host says which call broke. Only the
    # leading literal words are used, never an argument value, so no path or
    # host detail reaches the public record.
    command = " ".join(itertools.takewhile(lambda item: not item.startswith("-"), args))
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProvenanceFailure(
            f"installed executable returned a non-JSON result for {command!r}"
        ) from exc
    if completed.returncode != 0 or payload.get("ok") is False:
        raise ProvenanceFailure(f"installed executable reported failure for {command!r}")
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


def _manager_definition_evidence(
    rendered: dict[str, Any], *, gateway_port: int, mcp_port: int
) -> dict[str, Any]:
    platform_slug = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(
        platform.system()
    )
    manager = _manager()
    if platform_slug is None or manager == "unsupported":
        raise ProvenanceFailure("native manager platform is unsupported")
    definition_key = {"windows": "xml", "macos": "plist", "linux": "unit"}[platform_slug]
    command = rendered.get("command")
    definition = rendered.get(definition_key)
    endpoints = rendered.get("endpoints")
    expected_label = native_service_identity(platform_slug, "brains-serve-all")
    expected_arguments = [
        "-m",
        "brains",
        "serve-all",
        "--gateway-host",
        "127.0.0.1",
        "--gateway-port",
        str(gateway_port),
        "--mcp-port",
        str(mcp_port),
    ]
    # ServiceSpec.command_line is a display string: the interpreter followed by
    # its arguments, quoted only where a token contains a space. No expected
    # argument contains one, so the rendered command must end with exactly this
    # suffix and start with the absolute interpreter that carries it. The
    # interpreter is not compared to sys.executable: Windows services run the
    # windowless pythonw.exe while this probe runs python.exe, and the wheel that
    # supplies both is already bound by distribution_provenance.
    argument_suffix = " ".join(expected_arguments)
    interpreter_token = ""
    if isinstance(command, str) and command.endswith(f" {argument_suffix}"):
        interpreter_token = command[: -(len(argument_suffix) + 1)].strip().strip('"')
    if (
        rendered.get("action") != "would-install"
        or rendered.get("platform") != platform_slug
        or rendered.get("label") != expected_label
        or not interpreter_token
        or not Path(interpreter_token).is_absolute()
    ):
        raise ProvenanceFailure("native manager definition command differs")
    if (
        not isinstance(endpoints, dict)
        or endpoints
        != {
            "console": f"http://127.0.0.1:{gateway_port}/app",
            "mcp": f"http://127.0.0.1:{mcp_port}/mcp",
        }
        or not isinstance(definition, str)
    ):
        raise ProvenanceFailure("native manager definition endpoint differs")
    policy_tokens = {
        "windows": ("<LogonTrigger>", "<RestartOnFailure>"),
        "macos": ("<key>RunAtLoad</key>", "<key>KeepAlive</key>"),
        "linux": ("WantedBy=default.target", "Restart=always"),
    }[platform_slug]
    if not all(token in definition for token in policy_tokens):
        raise ProvenanceFailure("native manager autostart or restart policy differs")
    return {
        "manager": manager,
        "platform": platform_slug,
        "native_execution": False,
        "identity": expected_label,
        "gateway_port": gateway_port,
        "mcp_port": mcp_port,
        "command_sha256": canonical_sha256(command),
        "definition_sha256": canonical_sha256(definition),
        "autostart": True,
        "restart_on_failure": True,
    }


def _assert_setup_idempotent(
    first: dict[str, Any],
    second: dict[str, Any],
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> None:
    first_init = [row for row in first.get("steps", []) if row.get("step") == "init"]
    second_init = [row for row in second.get("steps", []) if row.get("step") == "init"]
    if (
        len(first_init) != 1
        or len(second_init) != 1
        or second_init[0].get("workspace", {}).get("slug")
        != first_init[0].get("workspace", {}).get("slug")
        or second_init[0].get("admin_key", {}).get("source") != "existing"
        or before != after
    ):
        raise ProvenanceFailure("clean-home setup was not idempotent")


def run_probe(
    *,
    candidate: str,
    wheel: Path,
    package_manifest: Path,
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
        package_manifest=package_manifest,
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
        record(
            "manager-definition",
            _manager_definition_evidence(rendered, gateway_port=gateway_port, mcp_port=mcp_port),
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
        selected_rows = [row for row in status.get("tools", []) if row.get("tool") == tool]
        wired_rows = [row for row in wired.get("tools", []) if row.get("tool") == tool]
        if len(selected_rows) != 1 or len(wired_rows) != 1 or len(wired.get("tools", [])) != 1:
            raise ProvenanceFailure("wire result does not contain exactly one selected adapter")
        selected = selected_rows[0]
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
        setup_roots = (("state", state), ("workspace", workspace))
        before_second_setup = snapshot_files(setup_roots)
        second = _run(executable, env, "setup", "--path", str(workspace), "--no-wire", "--json")
        after_second_setup = snapshot_files(setup_roots)
        _assert_setup_idempotent(first, second, before_second_setup, after_second_setup)
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
                "setup_state_sha256": canonical_sha256(before_second_setup),
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
    parser.add_argument("--package-manifest", type=Path, required=True)
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
            package_manifest=args.package_manifest,
            tool=args.tool,
            output=args.output,
            repo=Path.cwd(),
            git_executable=args.git_executable,
            runtime_tool_json=os.environ.get("BRAINS_NATIVE_TOOL_PATHS", "{}"),
        )
    except Exception as exc:  # noqa: BLE001 - public artifact exposes type only
        failure: dict[str, Any] = {
            "schema": "brains.native-installation-evidence.v1",
            "passed": False,
            "error_type": type(exc).__name__,
        }
        # ProvenanceFailure reasons are a fixed, curated set of strings that name
        # the rejected contract and never interpolate a path or host value, so a
        # failing host stays diagnosable. Any other exception still exposes only
        # its type.
        if isinstance(exc, ProvenanceFailure):
            failure["reason"] = str(exc)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(failure, sort_keys=True) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

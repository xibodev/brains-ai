"""Guarded native service-manager evidence for a disposable OS account.

This script mutates the current user's real Task Scheduler, LaunchAgent, or
systemd-user state.  It therefore refuses any non-empty Brains/client home and
requires an explicit disposable-host acknowledgement.  ``prepare`` leaves the
service installed for an external login/reboot boundary; ``verify`` validates
that boundary and tears everything down.  ``manager-cycle`` proves the native
manager lifecycle in one disposable CI job but does not claim login evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from native_evidence import (
    SHA1_RE,
    account_managed_backups,
    assert_sanitized,
    canonical_sha256,
    create_provenance,
    explicit_runtime_tools,
    require_fresh_output,
    snapshot_files,
)

ACKNOWLEDGEMENT = "disposable-native-service-host"
TOOLS = ("copilot-cli", "claude-code", "codex", "opencode")
FORBIDDEN_PORTS = {9876, 9877}


class EvidenceFailure(RuntimeError):
    pass


def _evidence_root() -> Path:
    raw = os.environ.get("BRAINS_NATIVE_EVIDENCE_ROOT", "")
    if not raw:
        raise EvidenceFailure("fresh private evidence root is absent")
    return Path(raw).resolve()


def _plan_path() -> Path:
    return _evidence_root() / "journey-plan.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_path(tool: str) -> Path:
    return {
        "copilot-cli": Path.home() / ".copilot" / "mcp-config.json",
        "claude-code": Path.home() / ".claude.json",
        "codex": Path.home() / ".codex" / "config.toml",
        "opencode": Path.home() / ".config" / "opencode" / "opencode.json",
    }[tool]


def _config_root(tool: str) -> Path:
    return {
        "copilot-cli": Path.home() / ".copilot",
        "claude-code": Path.home() / ".claude.json",
        "codex": Path.home() / ".codex",
        "opencode": Path.home() / ".config" / "opencode",
    }[tool]


def _config_snapshot(tool: str) -> dict[str, dict[str, Any]]:
    return snapshot_files(((tool, _config_root(tool)),))


def _seed(path: Path, tool: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if tool == "codex":
        path.write_text('model = "synthetic-native-evidence"\n', encoding="utf-8")
        return
    servers_key = "mcp" if tool == "opencode" else "mcpServers"
    path.write_text(
        json.dumps(
            {
                "synthetic_unmanaged": True,
                servers_key: {"other": {"command": "synthetic-other-server"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _run(executable: str, args: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EvidenceFailure(f"{args[0]} returned a non-JSON result") from exc
    if completed.returncode != 0 or payload.get("ok") is False:
        raise EvidenceFailure(f"{args[0]} reported failure")
    return payload


def _status(executable: str, label: str) -> dict[str, Any]:
    return _run(executable, ["service", "status", "--label", label])


def _wait_healthy(executable: str, label: str, timeout: float = 150) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = _status(executable, label)
        if report.get("healthy") is True:
            return report
        time.sleep(1)
    raise EvidenceFailure("service did not become healthy")


def _wait_removed(executable: str, label: str, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _status(executable, label)
        listeners = last.get("listeners", {})
        if not last.get("installed") and not any(
            listeners.get(name) for name in ("gateway", "mcp")
        ):
            return last
        time.sleep(0.5)
    raise EvidenceFailure("native identity or listener survived bounded uninstall")


def _port() -> int:
    while True:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            selected = int(listener.getsockname()[1])
        if selected not in FORBIDDEN_PORTS:
            return selected


def _guard(phase: str) -> None:
    if os.environ.get("BRAINS_NATIVE_EVIDENCE_DISPOSABLE") != ACKNOWLEDGEMENT:
        raise EvidenceFailure("disposable-host acknowledgement is absent")
    root = _evidence_root()
    state_raw = os.environ.get("BRAINS_STATE_DIR", "")
    if not state_raw or Path(state_raw).resolve() != root / "state":
        raise EvidenceFailure("synthetic state must be confined to the fresh evidence root")
    if phase == "prepare":
        if root.exists() or root.is_symlink():
            raise EvidenceFailure("fresh private evidence root already exists")
        if (Path.home() / ".brains").exists():
            raise EvidenceFailure("the real user already has Brains state")
        occupied = [path for path in map(_config_root, TOOLS) if path.exists()]
        if occupied:
            raise EvidenceFailure(f"client configuration already exists (count={len(occupied)})")
        root.mkdir(parents=True, mode=0o700)
    elif not _plan_path().is_file():
        raise EvidenceFailure("the prepared evidence plan is absent")


def _boot_marker() -> str:
    """Return a one-way marker for the current OS boot when available."""
    system = platform.system()
    if system == "Linux":
        raw = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    elif system == "Darwin":
        raw = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    elif system == "Windows":
        raw = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip()
    else:
        raise EvidenceFailure("unsupported native evidence platform")
    if not raw:
        raise EvidenceFailure("OS boot marker is unavailable")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _manager() -> str:
    return {
        "Windows": "task-scheduler",
        "Darwin": "launchd",
        "Linux": "systemd-user",
    }.get(platform.system(), "unsupported")


def _required_runtime_tools(adapter: str) -> tuple[str, ...]:
    required = {
        "Windows": ("powershell", "schtasks", "taskkill"),
        "Darwin": ("launchctl", "ps", "sysctl"),
        "Linux": ("ps", "systemctl"),
    }.get(platform.system())
    if required is None:
        raise EvidenceFailure("unsupported native evidence platform")
    return (*required, "node", "opencode") if adapter == "opencode" else required


def _write_plan(plan: dict[str, Any]) -> None:
    target = _plan_path()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    temporary.replace(target)


def _record(plan: dict[str, Any], step: str, evidence: dict[str, Any]) -> None:
    plan["steps"].append(
        {
            "sequence": len(plan["steps"]) + 1,
            "step": step,
            "passed": True,
            "provenance_sha256": plan["provenance"]["binding_sha256"],
            "evidence": evidence,
        }
    )
    _write_plan(plan)


def _status_evidence(report: dict[str, Any]) -> dict[str, Any]:
    identity = report.get("service_pid", {})
    pid = identity.get("pid")
    if report.get("healthy") and (
        not isinstance(pid, int) or identity.get("confidence") != "verified"
    ):
        raise EvidenceFailure("healthy service lacks verified owned process identity")
    marker_path = _evidence_root() / "state" / "sessions" / "service.pid"
    marker_sha256 = _digest(marker_path) if marker_path.is_file() else None
    listeners = report.get("listeners", {})
    protocol = report.get("mcp_protocol", {})
    return {
        "manager": _manager(),
        "installed": bool(report.get("installed")),
        "healthy": bool(report.get("healthy")),
        "runtime_classification": report.get("runtime_classification"),
        "owned_process": {
            "pid": pid if isinstance(pid, int) else None,
            "confidence": identity.get("confidence"),
            "start_marker_sha256": marker_sha256,
        },
        "listeners": {
            "gateway": bool(listeners.get("gateway")),
            "mcp": bool(listeners.get("mcp")),
        },
        "mcp_protocol_ready": bool(protocol.get("ready")),
    }


def _remove_synthetic_config(tool: str) -> None:
    root = _config_root(tool)
    if root.is_dir():
        shutil.rmtree(root)
    else:
        root.unlink(missing_ok=True)


def _kill_owned_tree(pid: int) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise EvidenceFailure("owned Windows process tree could not be terminated")
        return
    listing = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=True
    ).stdout
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        child, parent = (int(value) for value in line.split())
        children.setdefault(parent, []).append(child)
    ordered: list[int] = []

    def visit(parent: int) -> None:
        for child in children.get(parent, []):
            visit(child)
            ordered.append(child)

    visit(pid)
    for target in [*ordered, pid]:
        with contextlib.suppress(ProcessLookupError):
            os.kill(target, getattr(signal, "SIGKILL", 9))


def _write_result(output: Path, result: dict[str, Any], *, passed: bool) -> None:
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    suite = ET.Element("testsuite", name="native-service-lifecycle", tests="1")
    case = ET.SubElement(suite, "testcase", name=result.get("phase", "unknown"))
    if not passed:
        ET.SubElement(case, "failure", message="native lifecycle evidence failed")
    with output.with_suffix(".xml").open("x", encoding="utf-8") as stream:
        ET.ElementTree(suite).write(stream, encoding="unicode", xml_declaration=True)


def prepare(
    executable: str,
    candidate: str,
    adapter: str,
    provenance: dict[str, Any],
    *,
    guard_completed: bool = False,
) -> dict[str, Any]:
    if not guard_completed:
        _guard("prepare")
    if not SHA1_RE.fullmatch(candidate.casefold()):
        raise EvidenceFailure("candidate must be a full Git commit SHA")
    label = f"brains-serve-all-evidence-{candidate[:8].lower()}"
    root = _evidence_root()
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    gateway_port, mcp_port = _port(), _port()
    while mcp_port == gateway_port:
        mcp_port = _port()
    original_snapshot = _config_snapshot(adapter)
    if original_snapshot:
        raise EvidenceFailure("synthetic adapter home was not initially empty")
    plan: dict[str, Any] = {
        "candidate": candidate.lower(),
        "adapter": adapter,
        "provenance": provenance,
        "executable": executable,
        "label": label,
        "gateway_port": gateway_port,
        "mcp_port": mcp_port,
        "boot_marker": _boot_marker(),
        "original_snapshot": original_snapshot,
        "baseline_snapshot": {},
        "wired_snapshot": {},
        "steps": [],
    }
    _write_plan(plan)
    _record(
        plan,
        "provenance",
        {
            "candidate_bound": True,
            "wheel_bound": True,
            "installed_distribution_bound": True,
            "executable_bound": True,
        },
    )
    _record(
        plan,
        "manager-identity",
        {
            "manager": _manager(),
            "label": label,
            "platform": platform.system(),
        },
    )
    _record(
        plan,
        "endpoint-contract",
        {
            "host": "loopback",
            "gateway_port": gateway_port,
            "mcp_port": mcp_port,
            "mcp_path": "/mcp",
            "transport": "streamable-http",
        },
    )
    _run(executable, ["setup", "--path", str(workspace), "--no-wire", "--json"])
    state_key = _evidence_root() / "state" / "admin-key"
    if not state_key.is_file():
        raise EvidenceFailure("synthetic state admin key is absent")
    key = state_key.read_text(encoding="utf-8").strip()
    wire_env = {**os.environ, "BRAINS_MCP_BEARER_TOKEN": key}
    path = _config_path(adapter)
    _seed(path, adapter)
    plan["baseline_snapshot"] = _config_snapshot(adapter)
    _write_plan(plan)
    _run(
        executable,
        [
            "wire",
            "--tool",
            adapter,
            "--force",
            "--no-rules",
            "--transport",
            "streamable-http",
            "--port",
            str(mcp_port),
        ],
        wire_env,
    )
    plan["wired_snapshot"] = _config_snapshot(adapter)
    _record(
        plan,
        "adapter-wired",
        {
            "adapter": adapter,
            "transport": "streamable-http",
            "baseline_config_sha256": canonical_sha256(plan["baseline_snapshot"]),
            "wired_config_sha256": canonical_sha256(plan["wired_snapshot"]),
        },
    )
    _run(
        executable,
        [
            "service",
            "install",
            "--label",
            label,
            "--gateway-port",
            str(gateway_port),
            "--mcp-port",
            str(mcp_port),
        ],
    )
    installed = _wait_healthy(executable, label)
    _record(plan, "installed", _status_evidence(installed))
    _run(executable, ["service", "stop", "--label", label])
    _record(plan, "stopped", _status_evidence(_status(executable, label)))
    _run(executable, ["service", "start", "--label", label])
    started = _wait_healthy(executable, label)
    _record(plan, "started", _status_evidence(started))
    _run(executable, ["service", "restart", "--label", label])
    restarted = _wait_healthy(executable, label)
    _record(plan, "restarted", _status_evidence(restarted))
    old_pid = int(restarted["service_pid"]["pid"])
    _kill_owned_tree(old_pid)
    recovered = _wait_healthy(executable, label)
    if int(recovered["service_pid"]["pid"]) == old_pid:
        raise EvidenceFailure("native manager did not establish a new owned incarnation")
    _record(plan, "manager-recovered-owned-process", _status_evidence(recovered))
    _record(
        plan,
        "boundary-prepared",
        {
            "boot_marker_sha256": plan["boot_marker"],
            "login_transition_operator_attested": False,
        },
    )
    return {
        "schema": "brains.native-service-evidence.v1",
        "phase": "prepare",
        "matrix": {
            "manager": _manager(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "adapter": adapter,
            "transport": "streamable-http",
        },
        "provenance": provenance,
        "steps": plan["steps"],
        "boundary": {
            "boot_changed": False,
            "login_transition_operator_attested": False,
        },
    }


def verify(
    candidate: str,
    *,
    adapter: str,
    login_observed: bool,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    _guard("verify")
    plan = json.loads(_plan_path().read_text(encoding="utf-8"))
    if str(plan["candidate"]) != candidate.lower():
        raise EvidenceFailure("candidate differs from the prepared native journey")
    if plan.get("provenance", {}).get("binding_sha256") != provenance["binding_sha256"]:
        raise EvidenceFailure("installed provenance differs across the native boundary")
    executable = str(plan["executable"])
    label = str(plan["label"])
    if str(plan["adapter"]) != adapter:
        raise EvidenceFailure("adapter differs from the prepared native journey")
    boot_changed = _boot_marker() != str(plan["boot_marker"])
    healthy = _wait_healthy(executable, label)
    _record(
        plan,
        "boundary-verified",
        {
            **_status_evidence(healthy),
            "boot_changed": boot_changed,
            "login_transition_operator_attested": login_observed,
        },
    )
    if _config_snapshot(adapter) != plan["wired_snapshot"]:
        raise EvidenceFailure("managed client configuration changed across the boundary")
    service_log = _evidence_root() / "state" / "sessions" / "service.log"
    service_log_text = (
        service_log.read_text(encoding="utf-8", errors="replace") if service_log.is_file() else ""
    )
    if "starting" not in service_log_text:
        raise EvidenceFailure("bounded supervisor lifecycle log evidence is absent")
    _run(executable, ["unwire", "--tool", adapter, "--no-rules"])
    restored_snapshot = _config_snapshot(adapter)
    managed_backups = account_managed_backups(
        plan["baseline_snapshot"], plan["wired_snapshot"], restored_snapshot
    )
    _record(
        plan,
        "configuration-restored",
        {
            "baseline_config_sha256": canonical_sha256(plan["baseline_snapshot"]),
            "restored_config_sha256": canonical_sha256(restored_snapshot),
            "primary_configuration_restored": True,
            "managed_backup_count": len(managed_backups),
            "managed_backup_manifest_sha256": canonical_sha256(managed_backups),
        },
    )
    _run(executable, ["service", "uninstall", "--label", label])
    removed = _wait_removed(executable, label)
    removed_evidence = _status_evidence(removed)
    if removed_evidence["installed"] or any(removed_evidence["listeners"].values()):
        raise EvidenceFailure("native identity or listener survived teardown")
    _remove_synthetic_config(adapter)
    if _config_snapshot(adapter) != plan["original_snapshot"]:
        raise EvidenceFailure("synthetic client home was not exactly restored")
    _record(
        plan,
        "teardown",
        {
            **removed_evidence,
            "definition_removed": True,
            "listeners_removed": True,
            "initial_client_home_restored": True,
            "service_log_sha256": hashlib.sha256(service_log_text.encode("utf-8")).hexdigest(),
            "service_log_line_count": len(service_log_text.splitlines()),
        },
    )
    steps = list(plan["steps"])
    result = {
        "schema": "brains.native-service-evidence.v1",
        "phase": "verify",
        "matrix": {
            "manager": _manager(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "adapter": adapter,
            "transport": "streamable-http",
        },
        "provenance": provenance,
        "steps": steps,
        "boundary": {
            "boot_changed": boot_changed,
            "login_transition_operator_attested": login_observed,
        },
    }
    root = _evidence_root()
    shutil.rmtree(root)
    if root.exists():
        raise EvidenceFailure("synthetic evidence runtime root survived teardown")
    return result


def cleanup() -> bool:
    """Best-effort bounded cleanup for a failed disposable-host journey."""
    root = _evidence_root()
    plan_path = _plan_path()
    if not root.exists():
        return True
    if not plan_path.is_file():
        try:
            root.rmdir()
        except OSError:
            return False
        return True
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        executable = str(plan.get("executable", ""))
        label = str(plan.get("label", ""))
        adapter = str(plan.get("adapter", ""))
    except (OSError, json.JSONDecodeError):
        return False
    complete = bool(executable and label and adapter in TOOLS)
    if complete:
        result = subprocess.run(
            [executable, "unwire", "--tool", adapter, "--no-rules"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        complete = result.returncode == 0
        expected = plan.get("baseline_snapshot")
        wired = plan.get("wired_snapshot")
        if isinstance(expected, dict) and isinstance(wired, dict):
            try:
                account_managed_backups(expected, wired, _config_snapshot(adapter))
            except Exception:  # noqa: BLE001 - cleanup returns one bounded result
                complete = False
        result = subprocess.run(
            [executable, "service", "uninstall", "--label", label],
            capture_output=True,
            timeout=180,
            check=False,
        )
        complete = complete and result.returncode == 0
        try:
            removed = _wait_removed(executable, label)
            complete = (
                complete
                and not removed.get("installed")
                and not any(removed.get("listeners", {}).values())
            )
        except Exception:  # noqa: BLE001 - cleanup returns one bounded result
            complete = False
    if complete:
        if plan.get("original_snapshot") == {}:
            _remove_synthetic_config(adapter)
            complete = _config_snapshot(adapter) == {}
        if complete:
            shutil.rmtree(root)
            complete = not root.exists()
    return complete


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "verify", "manager-cycle", "cleanup"))
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--git-executable", type=Path, required=True)
    parser.add_argument("--adapter", choices=TOOLS, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--login-transition-observed", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("native-service-evidence.json"))
    args = parser.parse_args()
    try:
        require_fresh_output(args.output)
    except Exception:  # noqa: BLE001 - never overwrite or echo stale-output details
        return 1
    executable_name = "brains-ai.exe" if os.name == "nt" else "brains-ai"
    executable_path = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin") / executable_name
    executable = str(executable_path)
    result: dict[str, Any] = {"phase": args.phase}
    try:
        if not SHA1_RE.fullmatch(args.candidate.casefold()):
            raise EvidenceFailure("candidate must be a full Git commit SHA")
        if args.phase in {"prepare", "manager-cycle"}:
            _guard("prepare")
        elif args.phase == "verify":
            _guard("verify")
        elif os.environ.get("BRAINS_NATIVE_EVIDENCE_DISPOSABLE") != ACKNOWLEDGEMENT:
            raise EvidenceFailure("disposable-host acknowledgement is absent")
        if not executable_path.is_file():
            raise EvidenceFailure("installed brains-ai executable is unavailable")
        runtime_tools, controlled_path = explicit_runtime_tools(
            os.environ.get("BRAINS_NATIVE_TOOL_PATHS", "{}"),
            required=_required_runtime_tools(args.adapter),
            prepend_paths=(Path(sys.executable).parent,),
        )
        os.environ["PATH"] = controlled_path
        provenance = create_provenance(
            candidate=args.candidate,
            repo=args.repo,
            wheel=args.wheel,
            executable=executable_path,
            git_executable=args.git_executable,
            runtime_tools=runtime_tools,
        )
        if args.phase == "cleanup":
            plan = (
                json.loads(_plan_path().read_text(encoding="utf-8"))
                if _plan_path().is_file()
                else {}
            )
            if not cleanup():
                raise EvidenceFailure("disposable-host cleanup was incomplete")
            result = {
                "schema": "brains.native-service-evidence.v1",
                "phase": "cleanup",
                "matrix": {
                    "manager": _manager(),
                    "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                    "adapter": args.adapter,
                    "transport": "streamable-http",
                },
                "provenance": provenance,
                "cleanup": {
                    "definition_removed": True,
                    "listeners_removed": True,
                    "runtime_root_removed": True,
                    "prepared_binding_matched": (
                        plan.get("provenance", {}).get("binding_sha256")
                        == provenance["binding_sha256"]
                        if plan
                        else None
                    ),
                },
            }
        elif args.phase == "prepare":
            result = prepare(
                executable,
                args.candidate,
                args.adapter,
                provenance,
                guard_completed=True,
            )
        elif args.phase == "verify":
            result = verify(
                args.candidate,
                adapter=args.adapter,
                login_observed=args.login_transition_observed,
                provenance=provenance,
            )
        else:
            prepare(
                executable,
                args.candidate,
                args.adapter,
                provenance,
                guard_completed=True,
            )
            result = verify(
                args.candidate,
                adapter=args.adapter,
                login_observed=False,
                provenance=provenance,
            )
    except Exception as exc:  # noqa: BLE001 - artifact exposes type only
        with contextlib.suppress(Exception):
            cleanup()
        result.update({"passed": False, "error_type": type(exc).__name__})
        assert_sanitized(
            result,
            (str(Path.home()), os.environ.get("USERPROFILE", ""), getpass.getuser()),
        )
        _write_result(args.output, result, passed=False)
        return 1
    result["passed"] = True
    assert_sanitized(
        result,
        (str(Path.home()), os.environ.get("USERPROFILE", ""), getpass.getuser()),
    )
    _write_result(args.output, result, passed=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

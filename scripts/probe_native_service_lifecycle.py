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
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ACKNOWLEDGEMENT = "disposable-native-service-host"
TOOLS = ("copilot-cli", "claude-code", "codex", "opencode")
FORBIDDEN_PORTS = {9876, 9877}
PLAN = Path.home() / ".brains" / "native-service-evidence.json"
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class EvidenceFailure(RuntimeError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_path(tool: str) -> Path:
    return {
        "copilot-cli": Path.home() / ".copilot" / "mcp-config.json",
        "claude-code": Path.home() / ".claude.json",
        "codex": Path.home() / ".codex" / "config.toml",
        "opencode": Path.home() / ".config" / "opencode" / "opencode.json",
    }[tool]


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
    if phase == "prepare":
        if PLAN.parent.exists():
            raise EvidenceFailure("the real user already has Brains state")
        occupied = [str(path) for path in map(_config_path, TOOLS) if path.exists()]
        if occupied:
            raise EvidenceFailure(f"client configuration already exists (count={len(occupied)})")
    elif not PLAN.is_file():
        raise EvidenceFailure("the prepared evidence plan is absent")


def _wheel_sha256() -> str:
    value = os.environ.get("BRAINS_EVIDENCE_WHEEL_SHA256", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise EvidenceFailure("exact wheel SHA-256 is absent")
    return value


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
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    suite = ET.Element("testsuite", name="native-service-lifecycle", tests="1")
    case = ET.SubElement(suite, "testcase", name=result.get("phase", "unknown"))
    if not passed:
        ET.SubElement(case, "failure", message="native lifecycle evidence failed")
    ET.ElementTree(suite).write(
        output.with_suffix(".xml"), encoding="unicode", xml_declaration=True
    )


def prepare(executable: str, candidate: str) -> dict[str, Any]:
    _guard("prepare")
    if not SHA_RE.fullmatch(candidate):
        raise EvidenceFailure("candidate must be a full Git commit SHA")
    label = f"brains-serve-all-evidence-{candidate[:8].lower()}"
    if _status(executable, label).get("installed"):
        raise EvidenceFailure("the namespaced native identity already exists")
    root = Path.home() / "brains-native-evidence"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    gateway_port, mcp_port = _port(), _port()
    while mcp_port == gateway_port:
        mcp_port = _port()
    _run(executable, ["setup", "--path", str(workspace), "--no-wire", "--json"])
    plan: dict[str, Any] = {
        "candidate": candidate.lower(),
        "wheel_sha256": _wheel_sha256(),
        "executable": executable,
        "label": label,
        "gateway_port": gateway_port,
        "mcp_port": mcp_port,
        "boot_marker": _boot_marker(),
        "original_hashes": {},
        "wired_hashes": {},
    }
    PLAN.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    original: dict[str, str] = {}
    wired: dict[str, str] = {}
    plan["original_hashes"] = original
    plan["wired_hashes"] = wired
    key = (Path.home() / ".brains" / "admin-key").read_text(encoding="utf-8").strip()
    wire_env = {**os.environ, "BRAINS_MCP_BEARER_TOKEN": key}
    for tool in TOOLS:
        path = _config_path(tool)
        _seed(path, tool)
        original[tool] = _digest(path)
        PLAN.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
        _run(
            executable,
            [
                "wire",
                "--tool",
                tool,
                "--force",
                "--no-rules",
                "--transport",
                "streamable-http",
                "--port",
                str(mcp_port),
            ],
            wire_env,
        )
        wired[tool] = _digest(path)
        PLAN.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
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
    _wait_healthy(executable, label)
    _run(executable, ["service", "stop", "--label", label])
    _run(executable, ["service", "start", "--label", label])
    _wait_healthy(executable, label)
    _run(executable, ["service", "restart", "--label", label])
    restarted = _wait_healthy(executable, label)
    old_pid = int(restarted["service_pid"]["pid"])
    _kill_owned_tree(old_pid)
    recovered = _wait_healthy(executable, label)
    if int(recovered["service_pid"]["pid"]) == old_pid:
        raise EvidenceFailure("native manager did not establish a new owned incarnation")
    PLAN.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
    return {
        "phase": "prepare",
        "candidate": candidate,
        "wheel_sha256": plan["wheel_sha256"],
        "platform": platform.system(),
        "python": platform.python_version(),
        "adapters": list(TOOLS),
        "manager_cycle": True,
        "recovery": True,
        "login_persistence": False,
    }


def verify(candidate: str, *, login_observed: bool) -> dict[str, Any]:
    _guard("verify")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    if str(plan["candidate"]) != candidate.lower():
        raise EvidenceFailure("candidate differs from the prepared native journey")
    executable = str(plan["executable"])
    label = str(plan["label"])
    boot_changed = _boot_marker() != str(plan["boot_marker"])
    _wait_healthy(executable, label)
    for tool in TOOLS:
        if _digest(_config_path(tool)) != plan["wired_hashes"][tool]:
            raise EvidenceFailure("managed client configuration changed across the boundary")
    service_log = Path.home() / ".brains" / "sessions" / "service.log"
    service_log_text = (
        service_log.read_text(encoding="utf-8", errors="replace") if service_log.is_file() else ""
    )
    if "starting" not in service_log_text:
        raise EvidenceFailure("bounded supervisor lifecycle log evidence is absent")
    for tool in TOOLS:
        _run(executable, ["unwire", "--tool", tool, "--no-rules"])
        if _digest(_config_path(tool)) != plan["original_hashes"][tool]:
            raise EvidenceFailure("unmanaged client configuration was not restored")
    _run(executable, ["service", "uninstall", "--label", label])
    _wait_removed(executable, label)
    PLAN.unlink(missing_ok=True)
    return {
        "phase": "verify",
        "candidate": plan["candidate"],
        "wheel_sha256": plan["wheel_sha256"],
        "platform": platform.system(),
        "python": platform.python_version(),
        "adapters": list(TOOLS),
        "manager_cycle": True,
        "recovery": True,
        "login_persistence": bool(login_observed or boot_changed),
        "boundary_evidence": {
            "boot_changed": boot_changed,
            "login_transition_operator_attested": login_observed,
        },
        "configuration_preserved": True,
        "log_evidence": {
            "sha256": hashlib.sha256(service_log_text.encode("utf-8")).hexdigest(),
            "line_count": len(service_log_text.splitlines()),
            "startup_marker": True,
        },
        "teardown": True,
    }


def cleanup() -> bool:
    """Best-effort bounded cleanup for a failed disposable-host journey."""
    if not PLAN.is_file():
        return True
    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        executable = str(plan.get("executable", ""))
        label = str(plan.get("label", ""))
    except (OSError, json.JSONDecodeError):
        return False
    complete = bool(executable and label)
    if executable and label:
        for tool in TOOLS:
            result = subprocess.run(
                [executable, "unwire", "--tool", tool, "--no-rules"],
                capture_output=True,
                timeout=60,
                check=False,
            )
            complete = complete and result.returncode == 0
            expected = plan.get("original_hashes", {}).get(tool)
            if expected:
                try:
                    complete = complete and _digest(_config_path(tool)) == expected
                except OSError:
                    complete = False
        result = subprocess.run(
            [executable, "service", "uninstall", "--label", label],
            capture_output=True,
            timeout=180,
            check=False,
        )
        complete = complete and result.returncode == 0
    if complete:
        PLAN.unlink(missing_ok=True)
    return complete


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "verify", "manager-cycle", "cleanup"))
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--login-transition-observed", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("native-service-evidence.json"))
    args = parser.parse_args()
    executable = shutil.which("brains-ai")
    result: dict[str, Any] = {"phase": args.phase}
    try:
        if not SHA_RE.fullmatch(args.candidate):
            raise EvidenceFailure("candidate must be a full Git commit SHA")
        result["candidate"] = args.candidate.lower()
        if executable is None:
            raise EvidenceFailure("installed brains-ai executable is unavailable")
        if args.phase == "cleanup":
            if not cleanup():
                raise EvidenceFailure("disposable-host cleanup was incomplete")
            result = {"phase": "cleanup", "candidate": args.candidate.lower(), "passed": True}
        elif args.phase == "prepare":
            result = prepare(executable, args.candidate)
        elif args.phase == "verify":
            result = verify(args.candidate, login_observed=args.login_transition_observed)
        else:
            prepare(executable, args.candidate)
            result = verify(args.candidate, login_observed=False)
    except Exception as exc:  # noqa: BLE001 - artifact exposes type only
        cleanup()
        result.update({"passed": False, "error_type": type(exc).__name__})
        _write_result(args.output, result, passed=False)
        return 1
    result["passed"] = True
    _write_result(args.output, result, passed=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

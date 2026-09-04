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
import re
import secrets
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

from brains.service.common import native_service_identity

ACKNOWLEDGEMENT = "disposable-native-service-host"
TOOLS = ("copilot-cli", "claude-code", "codex", "opencode")
FORBIDDEN_PORTS = {9876, 9877}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLAN_CORE_FIELDS = {
    "candidate",
    "adapter",
    "provenance",
    "journey",
    "executable",
    "label",
    "gateway_port",
    "mcp_port",
    "boot_marker",
    "original_snapshot",
    "baseline_snapshot",
    "wired_snapshot",
    "steps",
}
PLAN_FIELDS = set(PLAN_CORE_FIELDS)


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


def _status_identity(report: dict[str, Any], label: str) -> tuple[str, str]:
    platform_slug = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(
        platform.system()
    )
    if platform_slug is None:
        raise EvidenceFailure("unsupported service status platform")
    expected_label = native_service_identity(platform_slug, label)
    state = report.get("state")
    if (
        report.get("platform") != platform_slug
        or report.get("label") != expected_label
        or not isinstance(state, str)
        or not state
    ):
        raise EvidenceFailure("native service status identity differs")
    return expected_label, state


def _wait_healthy(executable: str, label: str, timeout: float = 150) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = _status(executable, label)
        _status_identity(report, label)
        listeners = report.get("listeners", {})
        protocol = report.get("mcp_protocol", {})
        if (
            report.get("installed") is True
            and report.get("healthy") is True
            and listeners.get("gateway") is True
            and listeners.get("mcp") is True
            and protocol.get("ready") is True
        ):
            return report
        time.sleep(1)
    raise EvidenceFailure("service did not become fully ready")


def _wait_removed(executable: str, label: str, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _status(executable, label)
        _status_identity(last, label)
        listeners = last.get("listeners", {})
        process = last.get("service_pid", {})
        protocol = last.get("mcp_protocol", {})
        if (
            last.get("installed") is False
            and last.get("healthy") is False
            and listeners == {"gateway": False, "mcp": False}
            and protocol.get("ready") is False
            and process.get("pid") is None
            and process.get("confidence") == "absent"
            and last.get("runtime_classification") == "stopped"
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


def _seal_plan(plan: dict[str, Any]) -> str:
    if set(plan) != PLAN_FIELDS:
        raise EvidenceFailure("native operational plan schema differs")
    digest = canonical_sha256({key: plan[key] for key in PLAN_CORE_FIELDS})
    plan["plan_core_sha256"] = digest
    _write_plan(plan)
    return digest


def _validated_plan_digest(plan: dict[str, Any]) -> str:
    if set(plan) != {*PLAN_FIELDS, "plan_core_sha256"}:
        raise EvidenceFailure("native operational plan schema differs")
    claimed = plan["plan_core_sha256"]
    bound = {key: plan[key] for key in PLAN_CORE_FIELDS}
    if not isinstance(claimed, str) or not SHA256_RE.fullmatch(claimed):
        raise EvidenceFailure("native operational plan digest is invalid")
    if claimed != canonical_sha256(bound):
        raise EvidenceFailure("native operational plan digest differs")
    return claimed


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


def _status_evidence(report: dict[str, Any], label: str) -> dict[str, Any]:
    reported_label, reported_state = _status_identity(report, label)
    identity = report.get("service_pid", {})
    pid = identity.get("pid")
    active = report.get("healthy") is True
    if report.get("runtime_classification") != ("installed-owned-ready" if active else "stopped"):
        raise EvidenceFailure("native runtime classification differs")
    if active:
        listeners = report.get("listeners", {})
        protocol = report.get("mcp_protocol", {})
        if (
            report.get("installed") is not True
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or identity.get("confidence") != "verified"
            or listeners.get("gateway") is not True
            or listeners.get("mcp") is not True
            or protocol.get("ready") is not True
        ):
            raise EvidenceFailure("healthy service lacks complete readiness evidence")
    marker_path = _evidence_root() / "state" / "sessions" / "service.pid"
    marker_sha256 = (
        _digest(marker_path)
        if isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and identity.get("confidence") == "verified"
        and marker_path.is_file()
        else None
    )
    listeners = report.get("listeners", {})
    protocol = report.get("mcp_protocol", {})
    return {
        "manager": _manager(),
        "platform": {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform.system()],
        "label": reported_label,
        "state": reported_state,
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


def _ready_incarnation(evidence: dict[str, Any]) -> tuple[int, str]:
    process = evidence.get("owned_process", {})
    pid = process.get("pid")
    marker = process.get("start_marker_sha256")
    if (
        evidence.get("installed") is not True
        or evidence.get("healthy") is not True
        or evidence.get("listeners") != {"gateway": True, "mcp": True}
        or evidence.get("mcp_protocol_ready") is not True
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or process.get("confidence") != "verified"
        or not isinstance(marker, str)
        or not SHA256_RE.fullmatch(marker)
    ):
        raise EvidenceFailure("service readiness evidence is incomplete")
    return pid, marker


def _assert_stopped(evidence: dict[str, Any]) -> None:
    if (
        evidence.get("installed") is not True
        or evidence.get("healthy") is not False
        or evidence.get("listeners") != {"gateway": False, "mcp": False}
        or evidence.get("mcp_protocol_ready") is not False
        or evidence.get("owned_process")
        != {"pid": None, "confidence": "absent", "start_marker_sha256": None}
    ):
        raise EvidenceFailure("native service did not reach the stopped state")


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


def _journey(candidate: str, adapter: str, provenance_sha256: str) -> dict[str, Any]:
    bound = {
        "candidate": candidate.casefold(),
        "manager": _manager(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "adapter": adapter,
        "transport": "streamable-http",
        "provenance_sha256": provenance_sha256,
        "journey_id": secrets.token_hex(32),
    }
    return {
        "schema": "brains.native-service-journey.v1",
        **bound,
        "binding_sha256": canonical_sha256(bound),
    }


def _valid_journey(journey: Any, *, candidate: str, adapter: str, provenance_sha256: str) -> bool:
    if not isinstance(journey, dict):
        return False
    keys = {
        "schema",
        "candidate",
        "manager",
        "python",
        "adapter",
        "transport",
        "provenance_sha256",
        "journey_id",
        "binding_sha256",
    }
    if set(journey) != keys:
        return False
    bound = {key: journey[key] for key in keys - {"schema", "binding_sha256"}}
    return bool(
        journey["schema"] == "brains.native-service-journey.v1"
        and journey["candidate"] == candidate.casefold()
        and journey["manager"] == _manager()
        and journey["python"] == f"{sys.version_info.major}.{sys.version_info.minor}"
        and journey["adapter"] == adapter
        and journey["transport"] == "streamable-http"
        and journey["provenance_sha256"] == provenance_sha256
        and isinstance(journey["journey_id"], str)
        and SHA256_RE.fullmatch(journey["journey_id"])
        and journey["binding_sha256"] == canonical_sha256(bound)
    )


def _prior_normal_record(prior: Path, binding: str, candidate: str, adapter: str) -> dict[str, Any]:
    if prior.is_symlink() or not prior.is_file():
        raise EvidenceFailure("prior normal-cycle evidence is absent")
    try:
        record = json.loads(prior.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceFailure("prior normal-cycle evidence is unreadable") from exc
    if (
        not isinstance(record, dict)
        or set(record)
        != {
            "schema",
            "phase",
            "passed",
            "matrix",
            "provenance",
            "journey",
            "plan_core_sha256",
            "steps",
            "boundary",
        }
        or record.get("schema") != "brains.native-service-evidence.v1"
        or record.get("phase") not in {"prepare", "verify"}
        or record.get("passed") is not True
        or record.get("provenance", {}).get("binding_sha256") != binding
        or not isinstance(record.get("plan_core_sha256"), str)
        or not SHA256_RE.fullmatch(record["plan_core_sha256"])
        or not _valid_journey(
            record.get("journey"),
            candidate=candidate,
            adapter=adapter,
            provenance_sha256=binding,
        )
    ):
        raise EvidenceFailure("prior normal-cycle evidence is not provenance-bound")
    return {
        "sha256": _digest(prior),
        "phase": record["phase"],
        "journey": record["journey"],
        "plan_core_sha256": record["plan_core_sha256"],
        "prepare_record_sha256": record.get("prepare_record_sha256"),
        "record": record,
    }


def _prepared_binding_matched(
    plan: dict[str, Any],
    prior_journey: dict[str, Any],
    prior_plan_core_sha256: str,
    *,
    candidate: str,
    adapter: str,
    provenance_sha256: str,
) -> bool:
    if not plan:
        return False
    plan_core_sha256 = _validated_plan_digest(plan)
    if (
        not _valid_journey(
            plan.get("journey"),
            candidate=candidate,
            adapter=adapter,
            provenance_sha256=provenance_sha256,
        )
        or plan["journey"] != prior_journey
        or plan_core_sha256 != prior_plan_core_sha256
    ):
        raise EvidenceFailure("prepared cleanup journey differs from normal evidence")
    return True


def _read_prepare_record(
    path: Path,
    expected_sha256: str,
    *,
    candidate: str,
    adapter: str,
    provenance_sha256: str,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or not SHA256_RE.fullmatch(expected_sha256):
        raise EvidenceFailure("immutable prepare evidence is absent")
    if _digest(path) != expected_sha256:
        raise EvidenceFailure("immutable prepare evidence digest differs")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceFailure("immutable prepare evidence is unreadable") from exc
    if (
        not isinstance(record, dict)
        or record.get("schema") != "brains.native-service-evidence.v1"
        or record.get("phase") != "prepare"
        or record.get("passed") is not True
        or record.get("provenance", {}).get("binding_sha256") != provenance_sha256
        or not _valid_journey(
            record.get("journey"),
            candidate=candidate,
            adapter=adapter,
            provenance_sha256=provenance_sha256,
        )
        or not isinstance(record.get("plan_core_sha256"), str)
        or not SHA256_RE.fullmatch(record["plan_core_sha256"])
        or record.get("matrix")
        != {
            "manager": _manager(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "adapter": adapter,
            "transport": "streamable-http",
        }
        or record.get("boundary") != {"boot_changed": False, "login_transition_attestation": None}
    ):
        raise EvidenceFailure("immutable prepare evidence identity differs")
    return record


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
    journey = _journey(candidate, adapter, str(provenance["binding_sha256"]))
    plan: dict[str, Any] = {
        "candidate": candidate.lower(),
        "adapter": adapter,
        "provenance": provenance,
        "journey": journey,
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
    installed = _status_evidence(_wait_healthy(executable, label), label)
    installed_incarnation = _ready_incarnation(installed)
    _record(plan, "installed", installed)
    _run(executable, ["service", "stop", "--label", label])
    stopped = _status_evidence(_status(executable, label), label)
    _assert_stopped(stopped)
    _record(plan, "stopped", stopped)
    _run(executable, ["service", "start", "--label", label])
    started = _status_evidence(_wait_healthy(executable, label), label)
    started_incarnation = _ready_incarnation(started)
    if started_incarnation[0] == installed_incarnation[0]:
        raise EvidenceFailure("native start reused the installed process identity")
    _record(plan, "started", started)
    _run(executable, ["service", "restart", "--label", label])
    restarted = _status_evidence(_wait_healthy(executable, label), label)
    restarted_incarnation = _ready_incarnation(restarted)
    if restarted_incarnation[0] == started_incarnation[0]:
        raise EvidenceFailure("native restart reused the prior process identity")
    _record(plan, "restarted", restarted)
    old_pid = restarted_incarnation[0]
    _kill_owned_tree(old_pid)
    recovered = _status_evidence(_wait_healthy(executable, label), label)
    recovered_incarnation = _ready_incarnation(recovered)
    if recovered_incarnation[0] == old_pid:
        raise EvidenceFailure("native manager did not establish a new owned incarnation")
    _record(plan, "manager-recovered-owned-process", recovered)
    _record(
        plan,
        "boundary-prepared",
        {
            "boot_marker_sha256": plan["boot_marker"],
            "login_transition_attestation": None,
        },
    )
    plan_core_sha256 = _seal_plan(plan)
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
        "journey": journey,
        "plan_core_sha256": plan_core_sha256,
        "steps": plan["steps"],
        "boundary": {
            "boot_changed": False,
            "login_transition_attestation": None,
        },
    }


def verify(
    candidate: str,
    *,
    adapter: str,
    provenance: dict[str, Any],
    prepare_record_path: Path,
    prepare_record_sha256: str,
    installed_executable: Path,
) -> dict[str, Any]:
    _guard("verify")
    prepare_record = _read_prepare_record(
        prepare_record_path,
        prepare_record_sha256,
        candidate=candidate,
        adapter=adapter,
        provenance_sha256=str(provenance["binding_sha256"]),
    )
    plan = json.loads(_plan_path().read_text(encoding="utf-8"))
    plan_core_sha256 = _validated_plan_digest(plan)
    if str(plan["candidate"]) != candidate.lower():
        raise EvidenceFailure("candidate differs from the prepared native journey")
    if plan.get("provenance", {}).get("binding_sha256") != provenance["binding_sha256"]:
        raise EvidenceFailure("installed provenance differs across the native boundary")
    if not _valid_journey(
        plan.get("journey"),
        candidate=candidate,
        adapter=adapter,
        provenance_sha256=str(provenance["binding_sha256"]),
    ):
        raise EvidenceFailure("native journey binding differs across the boundary")
    executable = str(plan["executable"])
    if Path(executable).resolve(strict=True) != installed_executable.resolve(strict=True):
        raise EvidenceFailure("prepared executable differs from current provenance")
    if (
        prepare_record["journey"] != plan["journey"]
        or prepare_record["plan_core_sha256"] != plan_core_sha256
        or prepare_record.get("steps") != plan["steps"]
    ):
        raise EvidenceFailure("runtime plan differs from immutable prepare evidence")
    label = str(plan["label"])
    if str(plan["adapter"]) != adapter:
        raise EvidenceFailure("adapter differs from the prepared native journey")
    prepared_boot_marker = str(plan["boot_marker"])
    observed_boot_marker = _boot_marker()
    boot_changed = observed_boot_marker != prepared_boot_marker
    if not boot_changed:
        raise EvidenceFailure("native boundary has no machine-observed reboot")
    healthy = _wait_healthy(executable, label)
    _record(
        plan,
        "boundary-verified",
        {
            **_status_evidence(healthy, label),
            "boot_changed": boot_changed,
            "prepared_boot_marker_sha256": prepared_boot_marker,
            "observed_boot_marker_sha256": observed_boot_marker,
            "login_transition_attestation": None,
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
    removed_evidence = _status_evidence(removed, label)
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
    _seal_plan({key: plan[key] for key in PLAN_FIELDS})
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
        "journey": plan["journey"],
        "plan_core_sha256": plan_core_sha256,
        "prepare_record_sha256": prepare_record_sha256,
        "steps": steps,
        "boundary": {
            "boot_changed": boot_changed,
            "prepared_boot_marker_sha256": prepared_boot_marker,
            "observed_boot_marker_sha256": observed_boot_marker,
            "login_transition_attestation": None,
        },
    }
    return result


def cleanup(
    *,
    expected_executable: Path | None = None,
    completed_restoration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform bounded cleanup and return only directly measured evidence."""
    root = _evidence_root()
    plan_path = _plan_path()
    if not root.exists():
        raise EvidenceFailure("native runtime root is absent before cleanup")
    if not plan_path.is_file():
        raise EvidenceFailure("native operational plan is absent")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        _validated_plan_digest(plan)
        executable = str(plan.get("executable", ""))
        label = str(plan.get("label", ""))
        adapter = str(plan.get("adapter", ""))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceFailure("native operational plan is unreadable") from exc
    if not executable or adapter not in TOOLS:
        raise EvidenceFailure("native operational plan cleanup identity differs")
    resolved_executable = Path(executable).resolve(strict=True)
    if expected_executable is not None and resolved_executable != expected_executable.resolve(
        strict=True
    ):
        raise EvidenceFailure("cleanup executable differs from current provenance")
    baseline = plan.get("baseline_snapshot")
    wired = plan.get("wired_snapshot")
    if not isinstance(baseline, dict) or not isinstance(wired, dict):
        raise EvidenceFailure("native cleanup snapshots are invalid")
    if completed_restoration is None:
        _run(str(resolved_executable), ["unwire", "--tool", adapter, "--no-rules"])
        restored = _config_snapshot(adapter)
        managed_backups = account_managed_backups(baseline, wired, restored)
        restoration = {
            "baseline_config_sha256": canonical_sha256(baseline),
            "restored_config_sha256": canonical_sha256(restored),
            "primary_configuration_restored": True,
            "managed_backup_count": len(managed_backups),
            "managed_backup_manifest_sha256": canonical_sha256(managed_backups),
        }
    else:
        restoration = completed_restoration
        if (
            set(restoration)
            != {
                "baseline_config_sha256",
                "restored_config_sha256",
                "primary_configuration_restored",
                "managed_backup_count",
                "managed_backup_manifest_sha256",
            }
            or restoration.get("primary_configuration_restored") is not True
        ):
            raise EvidenceFailure("completed restoration evidence differs")
    _run(str(resolved_executable), ["service", "uninstall", "--label", label])
    removed = _status_evidence(_wait_removed(str(resolved_executable), label), label)
    if plan.get("original_snapshot") != {}:
        raise EvidenceFailure("native cleanup initial home was not empty")
    _remove_synthetic_config(adapter)
    initial_restored = _config_snapshot(adapter) == {}
    if not initial_restored:
        raise EvidenceFailure("native cleanup did not restore the initial client home")
    result = {
        "final_status": removed,
        **restoration,
        "initial_client_home_restored": initial_restored,
        "definition_removed": removed["installed"] is False,
        "listeners_removed": removed["listeners"] == {"gateway": False, "mcp": False},
    }
    shutil.rmtree(root)
    result["runtime_root_removed"] = not root.exists()
    if not result["runtime_root_removed"]:
        raise EvidenceFailure("native runtime root survived cleanup")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "verify", "manager-cycle", "cleanup"))
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--package-manifest", type=Path, required=True)
    parser.add_argument("--git-executable", type=Path, required=True)
    parser.add_argument("--adapter", choices=TOOLS, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--prepare-record", type=Path)
    parser.add_argument("--prepare-record-sha256")
    parser.add_argument("--prior-record", type=Path)
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
            package_manifest=args.package_manifest,
            executable=executable_path,
            git_executable=args.git_executable,
            runtime_tools=runtime_tools,
        )
        if args.phase == "cleanup":
            if args.prior_record is None:
                raise EvidenceFailure("cleanup prior record is required")
            plan = (
                json.loads(_plan_path().read_text(encoding="utf-8"))
                if _plan_path().is_file()
                else {}
            )
            prior = _prior_normal_record(
                args.prior_record,
                str(provenance["binding_sha256"]),
                args.candidate,
                args.adapter,
            )
            if prior["phase"] == "prepare":
                prepared_binding_matched = _prepared_binding_matched(
                    plan,
                    prior["journey"],
                    prior["plan_core_sha256"],
                    candidate=args.candidate,
                    adapter=args.adapter,
                    provenance_sha256=str(provenance["binding_sha256"]),
                )
                prepare_sha256 = prior["sha256"]
            else:
                if args.prepare_record is None or args.prepare_record_sha256 is None:
                    raise EvidenceFailure("verified cleanup requires immutable prepare evidence")
                prepared = _read_prepare_record(
                    args.prepare_record,
                    args.prepare_record_sha256,
                    candidate=args.candidate,
                    adapter=args.adapter,
                    provenance_sha256=str(provenance["binding_sha256"]),
                )
                _validated_plan_digest(plan)
                if (
                    prior["prepare_record_sha256"] != args.prepare_record_sha256
                    or prior["journey"] != prepared["journey"]
                    or prior["plan_core_sha256"] != prepared["plan_core_sha256"]
                    or plan.get("journey") != prepared["journey"]
                    or str(plan.get("candidate")) != args.candidate.lower()
                    or plan.get("adapter") != args.adapter
                    or plan.get("provenance", {}).get("binding_sha256")
                    != provenance["binding_sha256"]
                ):
                    raise EvidenceFailure("verified cleanup evidence chain differs")
                prepared_binding_matched = False
                prepare_sha256 = args.prepare_record_sha256
            completed_restoration = None
            if prior["phase"] == "verify":
                completed_restoration = next(
                    step["evidence"]
                    for step in prior["record"]["steps"]
                    if step["step"] == "configuration-restored"
                )
            cleanup_evidence = cleanup(
                expected_executable=executable_path,
                completed_restoration=completed_restoration,
            )
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
                "journey": prior["journey"],
                "plan_core_sha256": prior["plan_core_sha256"],
                "cleanup": {
                    **cleanup_evidence,
                    "prepared_binding_matched": prepared_binding_matched,
                    "prior_normal_record_sha256": prior["sha256"],
                    "prepare_record_sha256": prepare_sha256,
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
            if args.prepare_record is None or args.prepare_record_sha256 is None:
                raise EvidenceFailure("immutable prepare evidence is required for verification")
            result = verify(
                args.candidate,
                adapter=args.adapter,
                provenance=provenance,
                prepare_record_path=args.prepare_record,
                prepare_record_sha256=args.prepare_record_sha256,
                installed_executable=executable_path,
            )
        else:
            result = prepare(
                executable,
                args.candidate,
                args.adapter,
                provenance,
                guard_completed=True,
            )
    except Exception as exc:  # noqa: BLE001 - artifact exposes type only
        with contextlib.suppress(Exception):
            cleanup(expected_executable=executable_path)
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

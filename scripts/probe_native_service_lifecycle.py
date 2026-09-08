"""Guarded native service-manager evidence for a disposable OS account.

This script mutates the current user's real Task Scheduler, LaunchAgent, or
systemd-user state.  It therefore refuses any non-empty Brains/client home and
requires an explicit disposable-host acknowledgement. ``prepare`` leaves the
service installed for an optional reboot boundary; ``verify`` validates that
boundary and removes owned service/configuration. Cleanup removes the private runtime
only after ownership and quiescence checks. ``manager-cycle`` does not claim reboot evidence.
Failures emit allowlisted JSON diagnostics to stderr, never command output or
exception text. These diagnostics are not qualification evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import getpass
import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from native_evidence import (
    SHA1_RE,
    ProvenanceFailure,
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
    "native_definition",
    "runtime_owner",
    "runtime_inventory",
    "config_directories",
}
PLAN_FIELDS = set(PLAN_CORE_FIELDS)


class EvidenceFailure(RuntimeError):
    pass


# Only these reviewed literals may become diagnostic codes. Never normalize an
# exception's arbitrary text, command output, paths, or environment into a log.
_DIAGNOSTIC_MESSAGES = (
    "fresh private evidence root is absent",
    "private evidence root must be absolute",
    "evidence paths may not traverse links",
    "unexpected client configuration resource",
    "unexpected Claude backup resource",
    "unexpected client configuration directory",
    "configuration backup name already exists",
    "command returned a non-JSON result",
    "command reported failure",
    "unsupported service status platform",
    "native service status identity differs",
    "service did not become fully ready",
    "native identity or listener survived bounded uninstall",
    "disposable-host acknowledgement is absent",
    "synthetic state must be confined to the fresh evidence root",
    "fresh private evidence root already exists",
    "the real user already has Brains state",
    "client configuration already exists",
    "the prepared evidence plan is absent",
    "unsupported native evidence platform",
    "OS boot marker is unavailable",
    "native operational plan schema differs",
    "native operational plan digest is invalid",
    "native operational plan digest differs",
    "native runtime classification differs",
    "healthy service lacks complete readiness evidence",
    "service readiness evidence is incomplete",
    "native service did not reach the stopped state",
    "native service did not reach the stopped state before timeout",
    "synthetic configuration changed before removal",
    "client configuration directory inventory differs",
    "preexisting client configuration changed",
    "synthetic configuration snapshot path differs",
    "Claude backup path differs",
    "synthetic configuration changed at removal",
    "native definition is not a regular file",
    "local native definition differs",
    "native definition has unexpected hard links",
    "Task Scheduler observation failed",
    "Task Scheduler observation schema differs",
    "registered task definition is incomplete",
    "registered task action or trigger differs",
    "registered task principal or identity differs",
    "registered task trigger principal differs",
    "systemd observation failed",
    "systemd absence is ambiguous",
    "loaded systemd definition differs",
    "unexpected native enablement link",
    "unexpected native enablement resource",
    "launchd observation failed",
    "loaded launchd definition differs",
    "preexisting or unaccounted native definition",
    "expected native definition is unavailable",
    "native cleanup executable or state identity differs",
    "native ownership identity differs",
    "owned native definition is absent",
    "native definition is not owned by this journey",
    "native registration changed during cleanup",
    "runtime ownership marker differs",
    "unexpected runtime directory",
    "unexpected or changed runtime file",
    "unexpected runtime resource",
    "inventoried runtime file disappeared",
    "runtime plan changed before removal",
    "runtime file changed at removal",
    "runtime ownership marker changed at removal",
    "runtime plan changed at removal",
    "owned Windows process tree could not be terminated",
    "prior normal-cycle evidence is absent",
    "prior normal-cycle evidence is unreadable",
    "prior normal-cycle evidence is not provenance-bound",
    "prepared cleanup journey differs from normal evidence",
    "immutable prepare evidence is absent",
    "immutable prepare evidence digest differs",
    "immutable prepare evidence is unreadable",
    "immutable prepare evidence identity differs",
    "candidate must be a full Git commit SHA",
    "native identity and definition are not positively absent",
    "synthetic adapter home was not initially empty",
    "synthetic state admin key is absent",
    "client home appeared before seeding",
    "native start reused the installed process identity",
    "native restart reused the prior process identity",
    "native manager did not establish a new owned incarnation",
    "candidate differs from the prepared native journey",
    "installed provenance differs across the native boundary",
    "native journey binding differs across the boundary",
    "prepared executable differs from current provenance",
    "runtime plan differs from immutable prepare evidence",
    "adapter differs from the prepared native journey",
    "native boundary has no machine-observed reboot",
    "managed client configuration changed across the boundary",
    "client configuration directories changed across the boundary",
    "bounded supervisor lifecycle log evidence is absent",
    "native identity or listener survived teardown",
    "synthetic client home was not exactly restored",
    "native runtime root is absent before cleanup",
    "native operational plan is absent",
    "cleanup plan differs from validated evidence",
    "native operational plan is unreadable",
    "native operational plan cleanup identity differs",
    "cleanup executable differs from current provenance",
    "native cleanup snapshots are invalid",
    "managed client configuration changed before cleanup",
    "client configuration directories changed before cleanup",
    "client home changed after completed restoration",
    "completed restoration evidence differs",
    "native cleanup did not restore the initial client home",
    "installed brains-ai executable is unavailable",
    "cleanup prior record is required",
    "prepared cleanup evidence chain differs",
    "verified cleanup requires immutable prepare evidence",
    "verified cleanup evidence chain differs",
    "immutable prepare evidence is required for verification",
    "native cleanup runtime removal is incomplete",
    # Shared provenance helpers also raise only reviewed, exact static messages.
    "candidate repository validation failed",
    "candidate must be a full Git SHA-1 commit id",
    "explicit Git executable identity differs",
    "candidate does not equal the checked-out commit",
    "checked-out candidate is not clean",
    "installed distribution has no direct wheel provenance",
    "installed direct wheel provenance is malformed",
    "installed distribution did not originate from a local wheel",
    "installed distribution references a different wheel",
    "installed distribution wheel hash does not match",
    "wheel contains an unsafe member path",
    "candidate wheel is unreadable",
    "candidate wheel has no verifiable payload",
    "wheel distribution identity is ambiguous",
    "wheel RECORD verification failed",
    "package provenance is unreadable",
    "package provenance does not match candidate wheel",
    "package provenance output already exists",
    "package source identity differs",
    "probe interpreter or executable is outside its environment",
    "native evidence requires a fresh virtual environment",
    "installed brains-ai distribution is absent",
    "installed distribution identity differs",
    "installed console entry point differs",
    "installed payload differs from the candidate wheel",
    "installed distribution reports a path outside its environment",
    "installed RECORD uses an unsupported hash",
    "installed file differs from its RECORD hash",
    "installed file differs from its RECORD size",
    "installed distribution manifest is empty",
    "installed RECORD does not cover the wheel payload",
    "explicit native tool map is malformed",
    "explicit native tool map differs from the required set",
    "native tool path is not absolute",
    "native tool executable identity differs",
    "native tool resolution differs from hashed executable",
    "synthetic configuration root may not be a symlink",
    "synthetic configuration tree may not contain symlinks",
    "primary client configuration was not exactly restored",
    "unexpected managed configuration artifact remains",
    "managed backup does not preserve a known lifecycle state",
    "native evidence output already exists",
    "native evidence contains a forbidden host value",
)
_DIAGNOSTIC_CODES = {message: message.lower().replace(" ", "-") for message in _DIAGNOSTIC_MESSAGES}
_DIAGNOSTIC_STEPS = {
    "provenance",
    "manager-identity",
    "endpoint-contract",
    "adapter-wired",
    "installed",
    "stopped",
    "started",
    "restarted",
    "manager-recovered-owned-process",
    "boundary-prepared",
    "boundary-verified",
    "configuration-restored",
    "teardown",
}


def _diagnose(
    exc: Exception | None,
    *,
    phase: str,
    stage: str,
    context: dict[str, Any] | None = None,
    outcomes: dict[str, Any] | None = None,
) -> None:
    """Emit failure-only diagnostics separately from qualification records."""
    context = context or {}
    steps = context.get("diagnostic_steps", context.get("plan", {}).get("steps", []))
    last_step = steps[-1].get("step") if steps and isinstance(steps[-1], dict) else None
    error_types = (
        EvidenceFailure,
        ProvenanceFailure,
        FileNotFoundError,
        FileExistsError,
        PermissionError,
        OSError,
        json.JSONDecodeError,
        ET.ParseError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
    )
    error_type = next((kind.__name__ for kind in error_types if type(exc) is kind), "Exception")
    code = "unexpected-error"
    if exc is not None and type(exc) in (EvidenceFailure, ProvenanceFailure):
        message = exc.args[0] if len(exc.args) == 1 and type(exc.args[0]) is str else ""
        code = _DIAGNOSTIC_CODES.get(message, "unclassified-evidence-failure")
    operation = None
    command = None
    trace = exc.__traceback__ if exc is not None else None
    while trace is not None:
        name = trace.tb_frame.f_code.co_name
        if name in {
            "_run",
            "_guard",
            "_boot_marker",
            "_native_observation",
            "_expected_native_definition",
            "_wait_healthy",
            "_wait_stopped",
            "_wait_removed",
            "_assert_native_ownership",
            "_remove_synthetic_config",
            "_remove_runtime",
            "_record",
            "_seal_plan",
            "_prior_normal_record",
            "_read_prepare_record",
            "_kill_owned_tree",
        }:
            operation = name
        if name == "_run":
            arguments = trace.tb_frame.f_locals.get("args")
            if type(arguments) is list and arguments:
                verb = arguments[0]
                if type(verb) is str and verb in {"setup", "wire", "unwire", "service"}:
                    command = verb
                    if verb == "service" and len(arguments) > 1:
                        action = arguments[1]
                        if type(action) is str and action in {
                            "install",
                            "status",
                            "stop",
                            "start",
                            "restart",
                            "uninstall",
                        }:
                            command = "service-" + action
        trace = trace.tb_next
    record: dict[str, Any] = {
        "diagnostic": "native-service-failure",
        "phase": phase if phase in {"prepare", "verify", "manager-cycle", "cleanup"} else "unknown",
        "stage": stage
        if stage
        in {
            "output-preflight",
            "guard",
            "runtime-tools",
            "provenance",
            "lifecycle",
            "cleanup-binding",
            "cleanup",
            "cleanup-native",
            "cleanup-configuration",
            "cleanup-runtime",
            "rollback-outcome",
            "result-export",
        }
        else "unknown",
        "operation": operation,
        "command": command,
        "last_step": last_step
        if type(last_step) is str and last_step in _DIAGNOSTIC_STEPS
        else None,
        "error_type": error_type if exc is not None else None,
        "error_code": code if exc is not None else None,
    }
    if outcomes is not None:
        record["cleanup"] = {
            key: outcomes.get(key) is True
            for key in ("native_removed", "configuration_removed", "runtime_root_removed")
        }
    print(json.dumps(record, sort_keys=True), file=sys.stderr)


def _evidence_root() -> Path:
    raw = os.environ.get("BRAINS_NATIVE_EVIDENCE_ROOT", "")
    if not raw:
        raise EvidenceFailure("fresh private evidence root is absent")
    if not Path(raw).is_absolute():
        raise EvidenceFailure("private evidence root must be absolute")
    _assert_plain_path(Path(raw))
    return Path(raw).resolve()


def _assert_plain_path(path: Path) -> None:
    for item in (path, *path.parents):
        reparse = item.exists() and (
            getattr(item.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
        if item.is_symlink() or reparse:
            raise EvidenceFailure("evidence paths may not traverse links")


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
    root = _config_root(tool)
    _assert_plain_path(root)
    if root.is_dir():
        for path in root.rglob("*"):
            _assert_plain_path(path)
            if not path.is_dir() and not path.is_file():
                raise EvidenceFailure("unexpected client configuration resource")
    roots = [(tool, root)]
    if tool == "claude-code":
        for path in root.parent.glob(root.name + ".bak-*"):
            _assert_plain_path(path)
            if (
                not re.fullmatch(r"\.claude\.json\.bak-\d{8}-\d{6}", path.name)
                or not path.is_file()
            ):
                raise EvidenceFailure("unexpected Claude backup resource")
            roots.append((f"{tool}/{path.name}", path))
    return snapshot_files(roots)


def _config_directories(tool: str) -> list[str]:
    root = _config_root(tool)
    _assert_plain_path(root)
    if not root.is_dir():
        return []
    result = ["."]
    for path in root.rglob("*"):
        _assert_plain_path(path)
        if path.is_dir():
            relative = path.relative_to(root).as_posix()
            if tool != "opencode" or relative != "plugins":
                raise EvidenceFailure("unexpected client configuration directory")
            result.append(relative)
    return sorted(result)


def _check_backup_collision(tool: str) -> None:
    # wire's timestamped backup writer overwrites collisions. Refuse a known
    # collision before invoking it; this probe requires a single-writer account.
    path = _config_path(tool)
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    if backup.exists() or backup.is_symlink():
        raise EvidenceFailure("configuration backup name already exists")


def _seed(path: Path, tool: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if tool == "codex":
        with path.open("x", encoding="utf-8") as stream:
            stream.write('model = "synthetic-native-evidence"\n')
        return
    servers_key = "mcp" if tool == "opencode" else "mcpServers"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "synthetic_unmanaged": True,
                    servers_key: {"other": {"command": "synthetic-other-server"}},
                }
            )
            + "\n"
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
        raise EvidenceFailure("command returned a non-JSON result") from exc
    if completed.returncode != 0 or payload.get("ok") is False:
        raise EvidenceFailure("command reported failure")
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
        if (Path.home() / ".brains").exists() or (Path.home() / ".brains").is_symlink():
            raise EvidenceFailure("the real user already has Brains state")
        occupied = [path for path in map(_config_root, TOOLS) if path.exists() or path.is_symlink()]
        if occupied:
            raise EvidenceFailure("client configuration already exists")
        root.mkdir(parents=True, mode=0o700)
    else:
        _assert_plain_path(_plan_path())
        if not _plan_path().is_file():
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
    # Verification advances only its in-memory copy, not the sealed preparation.
    if "plan_core_sha256" not in plan:
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
        (evidence.get("installed") is not True and platform.system() != "Darwin")
        or evidence.get("healthy") is not False
        or evidence.get("listeners") != {"gateway": False, "mcp": False}
        or evidence.get("mcp_protocol_ready") is not False
        or evidence.get("owned_process")
        != {"pid": None, "confidence": "absent", "start_marker_sha256": None}
    ):
        raise EvidenceFailure("native service did not reach the stopped state")


def _wait_stopped(plan: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _assert_native_ownership(plan)
        report = _status(plan["executable"], plan["label"])
        _status_identity(report, plan["label"])
        if (
            report.get("healthy") is False
            and report.get("listeners") == {"gateway": False, "mcp": False}
            and report.get("mcp_protocol", {}).get("ready") is False
            and report.get("service_pid", {}).get("pid") is None
            and report.get("service_pid", {}).get("confidence") == "absent"
            and report.get("runtime_classification") == "stopped"
        ):
            evidence = _status_evidence(report, plan["label"])
            _assert_stopped(evidence)
            return evidence
        time.sleep(0.5)
    raise EvidenceFailure("native service did not reach the stopped state before timeout")


def _remove_synthetic_config(
    tool: str,
    expected: dict[str, Any],
    *,
    directories: list[str] | None = None,
    original: dict[str, Any] | None = None,
) -> None:
    root = _config_root(tool)
    original = original or {}
    if _config_snapshot(tool) != expected:
        raise EvidenceFailure("synthetic configuration changed before removal")
    actual_directories = _config_directories(tool)
    if directories is None:
        directories = ["."] if expected and tool != "claude-code" else []
    if actual_directories != sorted(directories):
        raise EvidenceFailure("client configuration directory inventory differs")
    if any(expected.get(key) != value for key, value in original.items()):
        raise EvidenceFailure("preexisting client configuration changed")
    for logical in expected:
        if logical in original:
            continue
        relative = logical.removeprefix(f"{tool}/")
        if logical != tool and (
            not logical.startswith(f"{tool}/")
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
        ):
            raise EvidenceFailure("synthetic configuration snapshot path differs")
        if tool == "claude-code" and logical != tool:
            if not re.fullmatch(r"\.claude\.json\.bak-\d{8}-\d{6}", relative):
                raise EvidenceFailure("Claude backup path differs")
            path = root.parent / relative
        else:
            path = root if logical == tool else root / relative
        _assert_plain_path(path)
        if (
            path.stat().st_size != expected[logical]["size"]
            or _digest(path) != expected[logical]["sha256"]
        ):
            raise EvidenceFailure("synthetic configuration changed at removal")
        path.unlink()
    for relative in sorted(directories, key=lambda value: len(Path(value).parts), reverse=True):
        path = root / relative
        _assert_plain_path(path)
        path.rmdir()


def _native_command(
    args: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=30, check=False, env=env)


def _native_observation(label: str, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read registration and local definition independently; errors are not absence."""
    system = platform.system()
    slug = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[system]
    identity = native_service_identity(slug, label)
    path = {
        "Windows": _evidence_root() / "state" / "service" / f"{identity}.xml",
        "Darwin": Path.home() / "Library" / "LaunchAgents" / f"{identity}.plist",
        "Linux": Path.home() / ".config" / "systemd" / "user" / identity,
    }[system]
    _assert_plain_path(path)
    if path.exists() and not path.is_file():
        raise EvidenceFailure("native definition is not a regular file")
    local = (
        path.read_text(encoding="utf-16" if system == "Windows" else "utf-8")
        if path.exists()
        else None
    )
    if expected is not None and (
        expected["label"] != identity
        or Path(expected["definition_path"]) != path
        or (local is not None and local != expected["content"])
    ):
        raise EvidenceFailure("local native definition differs")
    if local is not None and path.stat().st_nlink != 1:
        raise EvidenceFailure("native definition has unexpected hard links")
    registered = False
    if system == "Windows":
        # COM exposes the unlocalized not-found HRESULT. Only GetTask's precise
        # missing-task error is absence; connection/access/XML errors fail closed.
        user = ""
        if expected is not None:
            xml = ET.fromstring(expected["content"])
            user = xml.findtext("{*}Principals/{*}Principal/{*}UserId", "")
        command = (
            "$ErrorActionPreference='Stop'; try {"
            "$s=New-Object -ComObject Schedule.Service; $s.Connect(); $f=$s.GetFolder('\\');"
            "try {$t=$f.GetTask($env:BRAINS_EVIDENCE_TASK)} catch {"
            "$e=$_.Exception; while ($e.InnerException) {$e=$e.InnerException};"
            "if ($e.HResult -eq -2147024894) {"
            "@{xml=$null;principal_matches=$true}|ConvertTo-Json -Compress; exit 0}; throw};"
            "$match=$false; if ($env:BRAINS_EVIDENCE_USER) {"
            "$u=$t.Definition.Principal.UserId;"
            "$a=New-Object System.Security.Principal.NTAccount($env:BRAINS_EVIDENCE_USER);"
            "$sid=$a.Translate([System.Security.Principal.SecurityIdentifier]).Value;"
            "if ($u -match '^S-1-') {$match=$u -eq $sid} else {"
            "$b=New-Object System.Security.Principal.NTAccount($u);"
            "$match=$b.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $sid}};"
            "@{xml=$t.Xml;principal_matches=$match}|ConvertTo-Json -Compress"
            "} catch {exit 2}"
        )
        result = _native_command(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            env={**os.environ, "BRAINS_EVIDENCE_TASK": identity, "BRAINS_EVIDENCE_USER": user},
        )
        if result.returncode != 0:
            raise EvidenceFailure("Task Scheduler observation failed")
        payload = json.loads(result.stdout)
        if set(payload) != {"xml", "principal_matches"}:
            raise EvidenceFailure("Task Scheduler observation schema differs")
        registered = payload["xml"] is not None
        if registered and expected is not None:
            actual = ET.fromstring(payload["xml"])
            wanted = ET.fromstring(expected["content"])

            def fields(node: ET.Element) -> list[tuple[str, str]]:
                return [
                    (item.tag.rsplit("}", 1)[-1], (item.text or "").strip()) for item in node.iter()
                ]

            for field in ("Actions", "Triggers"):
                left, right = actual.find(f"{{*}}{field}"), wanted.find(f"{{*}}{field}")
                if left is None or right is None:
                    raise EvidenceFailure("registered task definition is incomplete")
                # Task Scheduler may canonicalize the trigger's user to a SID.
                if [item for item in fields(left) if item[0] != "UserId"] != [
                    item for item in fields(right) if item[0] != "UserId"
                ]:
                    raise EvidenceFailure("registered task action or trigger differs")
            if (
                payload["principal_matches"] is not True
                or actual.findtext("{*}RegistrationInfo/{*}URI") != f"\\{identity}"
                or actual.findtext("{*}Principals/{*}Principal/{*}LogonType") != "InteractiveToken"
                or actual.findtext("{*}Principals/{*}Principal/{*}RunLevel") != "LeastPrivilege"
            ):
                raise EvidenceFailure("registered task principal or identity differs")
            triggers = actual.findall("{*}Triggers/{*}LogonTrigger/{*}UserId")
            principal = actual.findtext("{*}Principals/{*}Principal/{*}UserId")
            if len(triggers) != 1 or triggers[0].text not in {principal, user}:
                raise EvidenceFailure("registered task trigger principal differs")
    elif system == "Linux":
        result = _native_command(
            [
                "systemctl",
                "--user",
                "show",
                identity,
                "--no-pager",
                "--property=Id,LoadState,ActiveState,FragmentPath,ExecStart,Environment,WorkingDirectory,DropInPaths,NeedDaemonReload",
            ]
        )
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        required = {
            "Id",
            "LoadState",
            "ActiveState",
            "FragmentPath",
            "ExecStart",
            "Environment",
            "WorkingDirectory",
            "DropInPaths",
            "NeedDaemonReload",
        }
        if (
            set(properties) != required
            or properties["Id"] != identity
            or result.returncode not in (0, 1)
            or result.stderr.strip()
        ):
            raise EvidenceFailure("systemd observation failed")
        registered = properties["LoadState"] != "not-found"
        if not registered:
            if properties["ActiveState"] != "inactive" or any(
                properties[key]
                for key in (
                    "FragmentPath",
                    "ExecStart",
                    "Environment",
                    "WorkingDirectory",
                    "DropInPaths",
                )
            ):
                raise EvidenceFailure("systemd absence is ambiguous")
        elif expected is not None:
            unit = dict(
                line.split("=", 1)
                for line in expected["content"].splitlines()
                if "=" in line and not line.startswith("#")
            )
            execution = re.fullmatch(
                r"\{ path=(.*?) ; argv\[\]=(.*?) ; ignore_errors=no ; .*\}", properties["ExecStart"]
            )
            if (
                result.returncode != 0
                or properties["LoadState"] != "loaded"
                or properties["FragmentPath"] != str(path)
                or properties["DropInPaths"]
                or properties["NeedDaemonReload"] != "no"
                or execution is None
                or execution[1] != shlex.split(unit["ExecStart"])[0]
                or execution[2] not in {unit["ExecStart"], " ".join(shlex.split(unit["ExecStart"]))}
                or shlex.split(properties["Environment"]) != shlex.split(unit["Environment"])
                or properties["WorkingDirectory"] != unit["WorkingDirectory"]
            ):
                raise EvidenceFailure("loaded systemd definition differs")
        link = path.parent / "default.target.wants" / identity
        if link.is_symlink():
            _assert_plain_path(link.parent)
            if expected is None or local is None or link.resolve() != path:
                raise EvidenceFailure("unexpected native enablement link")
        elif link.exists():
            raise EvidenceFailure("unexpected native enablement resource")
    else:
        domain = f"gui/{os.getuid()}"
        result = _native_command(["launchctl", "print", f"{domain}/{identity}"])
        if result.returncode != 0:
            missing = f'Could not find service "{identity}" in domain for user gui: {os.getuid()}'
            if result.returncode not in (3, 113) or result.stderr.strip() not in {
                missing,
                "Bad request.\n" + missing,
            }:
                raise EvidenceFailure("launchd observation failed")
        else:
            registered = True
            if expected is not None:
                plist = plistlib.loads(expected["content"].encode())
                text = result.stdout
                arguments = re.search(r"(?m)^\s*arguments = \{\n(.*?)^\s*\}", text, re.S | re.M)
                values = dict(
                    re.findall(r"(?m)^\s*(path|program|working directory) = (.*?)\s*$", text)
                )
                state = re.findall(r"(?m)^\s*BRAINS_STATE_DIR => (.*?)\s*$", text)
                if (
                    not text.startswith(f"{domain}/{identity} = {{")
                    or arguments is None
                    or [line.strip() for line in arguments[1].splitlines() if line.strip()]
                    != plist["ProgramArguments"]
                    or values
                    != {
                        "path": str(path),
                        "program": plist["ProgramArguments"][0],
                        "working directory": plist["WorkingDirectory"],
                    }
                    or state != [plist["EnvironmentVariables"]["BRAINS_STATE_DIR"]]
                ):
                    raise EvidenceFailure("loaded launchd definition differs")
    if (registered or local is not None) and (expected is None or local is None):
        raise EvidenceFailure("preexisting or unaccounted native definition")
    return {
        "label": identity,
        "definition": expected if local is not None else None,
        "registered": registered,
    }


def _expected_native_definition(plan: dict[str, Any]) -> dict[str, Any]:
    rendered = _run(
        plan["executable"],
        [
            "service",
            "install",
            "--dry-run",
            "--label",
            plan["label"],
            "--gateway-port",
            str(plan["gateway_port"]),
            "--mcp-port",
            str(plan["mcp_port"]),
        ],
    )
    platform_slug, key = {
        "Windows": ("windows", "xml"),
        "Darwin": ("macos", "plist"),
        "Linux": ("linux", "unit"),
    }[platform.system()]
    identity = native_service_identity(platform_slug, plan["label"])
    if (
        rendered.get("action") != "would-install"
        or rendered.get("platform") != platform_slug
        or rendered.get("label") != identity
        or any(
            not isinstance(rendered.get(field), str) or not rendered[field]
            for field in ("definition", "command", key)
        )
    ):
        raise EvidenceFailure("expected native definition is unavailable")
    executable = Path(plan["executable"]).resolve(strict=True)
    return {
        "label": identity,
        "definition_path": rendered["definition"],
        "content": rendered[key],
        "command": rendered["command"],
        "executable": str(executable),
        "executable_sha256": _digest(executable),
        "state_dir": str(_evidence_root() / "state"),
    }


def _assert_native_ownership(
    plan: dict[str, Any], *, absent: bool = False, allow_absent: bool = False
) -> bool:
    expected = plan["native_definition"]
    _assert_plain_path(Path(expected["executable"]))
    platform_slug = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform.system()]
    if (
        Path(plan["executable"]).resolve(strict=True) != Path(expected["executable"])
        or _digest(Path(expected["executable"])) != expected["executable_sha256"]
        or str(_evidence_root() / "state") != expected["state_dir"]
        or Path(os.environ.get("BRAINS_STATE_DIR", "")).resolve() != _evidence_root() / "state"
        or not str(plan["label"]).startswith("brains-serve-all-evidence-")
        or expected["label"] != native_service_identity(platform_slug, plan["label"])
    ):
        raise EvidenceFailure("native cleanup executable or state identity differs")
    _assert_plain_path(Path(expected["definition_path"]))
    observed = _native_observation(plan["label"], expected)
    if (
        set(observed) != {"label", "definition", "registered"}
        or observed["label"] != expected["label"]
    ):
        raise EvidenceFailure("native ownership identity differs")
    if observed["definition"] is None:
        if absent or allow_absent:
            return False
        raise EvidenceFailure("owned native definition is absent")
    if absent or observed["definition"] != expected:
        raise EvidenceFailure("native definition is not owned by this journey")
    return True


def _uninstall_owned(plan: dict[str, Any]) -> None:
    if not _assert_native_ownership(plan, allow_absent=True):
        return
    observed = _native_observation(plan["label"], plan["native_definition"])
    if observed["registered"]:
        _run(plan["executable"], ["service", "uninstall", "--label", plan["label"]])
    else:
        # A failed install can leave only its exact file. The native backend's
        # uninstall cannot succeed for an unloaded job, so remove this file only
        # after separately proving no process/listener and no registration.
        _wait_removed(plan["executable"], plan["label"])
        _assert_native_ownership(plan)
        if _native_observation(plan["label"], plan["native_definition"])["registered"]:
            raise EvidenceFailure("native registration changed during cleanup")
        Path(plan["native_definition"]["definition_path"]).unlink()
    _wait_removed(plan["executable"], plan["label"])
    _assert_native_ownership(plan, absent=True)


def _remove_runtime(plan: dict[str, Any]) -> None:
    """Delete a confined, quiescent journey tree, not an arbitrary runtime path."""
    root = _evidence_root()
    owner = plan["runtime_owner"]
    marker = root / "journey-owner.json"
    _assert_plain_path(marker)
    if (
        json.loads(marker.read_text(encoding="utf-8")) != owner
        or owner["root"] != str(root)
        or owner["journey"] != plan["journey"]["journey_id"]
        or (root.stat().st_dev, root.stat().st_ino) != (owner["device"], owner["inode"])
    ):
        raise EvidenceFailure("runtime ownership marker differs")
    _assert_native_ownership(plan, absent=True)
    _wait_removed(plan["executable"], plan["label"])
    # These are the exact mutable files emitted by SQLite, service/common.py and
    # supervisor.py. Everything else must match the post-setup inventory.
    mutable = {
        "state/brains.db",
        "state/brains.db-wal",
        "state/brains.db-shm",
        "state/brains.db-journal",
        "state/sessions/service.pid",
        "state/sessions/service.log",
        "state/sessions/service.log.1",
        "state/sessions/service.log.2",
        "state/sessions/service.log.3",
        "state/sessions/service.out.log",
        "state/sessions/service.err.log",
        "state/service/endpoints.json",
    }
    directories = {"workspace", "state", "state/sessions", "state/service"}
    inventory = plan["runtime_inventory"]
    files: list[Path] = []
    folders: list[Path] = []
    for path in root.rglob("*"):
        _assert_plain_path(path)
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            if relative not in directories:
                raise EvidenceFailure("unexpected runtime directory")
            folders.append(path)
        elif path.is_file() and path.stat().st_nlink == 1:
            if relative in {"journey-plan.json", "journey-owner.json"}:
                pass
            elif relative not in mutable and inventory.get(relative) != {
                "size": path.stat().st_size,
                "sha256": _digest(path),
            }:
                raise EvidenceFailure("unexpected or changed runtime file")
            files.append(path)
        else:
            raise EvidenceFailure("unexpected runtime resource")
    for relative in inventory:
        if relative not in mutable and not (root / relative).is_file():
            raise EvidenceFailure("inventoried runtime file disappeared")
    disk_plan = json.loads(_plan_path().read_text(encoding="utf-8"))
    if disk_plan != plan:
        raise EvidenceFailure("runtime plan changed before removal")
    # Validate the entire inventory before unlinking anything. Recheck identity
    # immediately before action; this is a disposable single-writer account,
    # not an atomic fence against a hostile concurrent user.
    _assert_native_ownership(plan, absent=True)
    for path in files:
        _assert_plain_path(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise EvidenceFailure("runtime file changed at removal")
        relative = path.relative_to(root).as_posix()
        if (
            relative == "journey-owner.json"
            and json.loads(path.read_text(encoding="utf-8")) != owner
        ):
            raise EvidenceFailure("runtime ownership marker changed at removal")
        if relative == "journey-plan.json" and json.loads(path.read_text(encoding="utf-8")) != plan:
            raise EvidenceFailure("runtime plan changed at removal")
        if (
            relative in inventory
            and relative not in mutable
            and inventory[relative]
            != {
                "size": path.stat().st_size,
                "sha256": _digest(path),
            }
        ):
            raise EvidenceFailure("runtime file changed at removal")
        path.unlink()
    for path in sorted(folders, key=lambda item: len(item.parts), reverse=True):
        path.rmdir()
    root.rmdir()


def _rollback(context: dict[str, Any]) -> dict[str, Any]:
    """Use only this invocation's trusted context, never load an unsealed plan."""
    plan = context["plan"]
    result: dict[str, Any] = {}
    try:
        if context.get("native_armed"):
            _uninstall_owned(plan)
            _wait_removed(plan["executable"], plan["label"])
            _assert_native_ownership(plan, absent=True)
            result["native_removed"] = True
    except Exception as exc:  # noqa: BLE001 - cleanup failures remain visible, content-free
        result["native_error_type"] = type(exc).__name__
        _diagnose(
            exc, phase=context.get("phase", "unknown"), stage="cleanup-native", context=context
        )
    try:
        if "config_snapshot" in context:
            _remove_synthetic_config(
                plan["adapter"],
                context["config_snapshot"],
                directories=context["config_directories"],
                original=plan["original_snapshot"],
            )
            result["configuration_removed"] = True
    except Exception as exc:  # noqa: BLE001 - independent of native cleanup
        result["configuration_error_type"] = type(exc).__name__
        _diagnose(
            exc,
            phase=context.get("phase", "unknown"),
            stage="cleanup-configuration",
            context=context,
        )
    result["runtime_root_removed"] = False
    if result.get("native_removed") and result.get("configuration_removed"):
        try:
            _remove_runtime(plan)
            result["runtime_root_removed"] = True
        except Exception as exc:  # noqa: BLE001 - retain uncertain partial effects
            result["runtime_error_type"] = type(exc).__name__
            _diagnose(
                exc, phase=context.get("phase", "unknown"), stage="cleanup-runtime", context=context
            )
    return result


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
    if not isinstance(record, dict):
        raise EvidenceFailure("prior normal-cycle evidence is not provenance-bound")
    required_fields = {
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
    if record.get("phase") == "verify":
        required_fields.add("prepare_record_sha256")
    if (
        set(record) != required_fields
        or record.get("schema") != "brains.native-service-evidence.v1"
        or record.get("phase") not in {"prepare", "verify"}
        or record.get("passed") is not True
        or record.get("provenance", {}).get("binding_sha256") != binding
        or not isinstance(record.get("plan_core_sha256"), str)
        or not SHA256_RE.fullmatch(record["plan_core_sha256"])
        or (
            record["phase"] == "verify"
            and (
                not isinstance(record["prepare_record_sha256"], str)
                or not SHA256_RE.fullmatch(record["prepare_record_sha256"])
            )
        )
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
    rollback_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not guard_completed:
        _guard("prepare")
    if not SHA1_RE.fullmatch(candidate.casefold()):
        raise EvidenceFailure("candidate must be a full Git commit SHA")
    label = f"brains-serve-all-evidence-{candidate[:8].lower()}"
    platform_slug = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform.system()]
    observed = _native_observation(label)
    if observed != {
        "label": native_service_identity(platform_slug, label),
        "definition": None,
        "registered": False,
    }:
        raise EvidenceFailure("native identity and definition are not positively absent")
    root = _evidence_root()
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    gateway_port, mcp_port = _port(), _port()
    while mcp_port == gateway_port:
        mcp_port = _port()
    original_snapshot = _config_snapshot(adapter)
    if original_snapshot and (adapter != "claude-code" or adapter in original_snapshot):
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
        "native_definition": {},
        "runtime_owner": {},
        "runtime_inventory": {},
        "config_directories": [],
    }
    # Probe capability/absence before wiring or any native mutation. Do not use
    # status(installed=False): manager errors and disabled units look absent.
    plan["native_definition"] = _expected_native_definition(plan)
    _assert_native_ownership(plan, absent=True)
    plan["runtime_owner"] = {
        "root": str(root),
        "device": root.stat().st_dev,
        "inode": root.stat().st_ino,
        "journey": journey["journey_id"],
        "nonce": secrets.token_hex(32),
    }
    with (root / "journey-owner.json").open("x", encoding="utf-8") as stream:
        json.dump(plan["runtime_owner"], stream)
    if rollback_context is not None:
        rollback_context["plan"] = plan
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
    plan["runtime_inventory"] = {
        key.removeprefix("runtime/"): value
        for key, value in snapshot_files((("runtime", root),)).items()
        if key not in {"runtime/journey-plan.json", "runtime/journey-owner.json"}
    }
    state_key = _evidence_root() / "state" / "admin-key"
    if not state_key.is_file():
        raise EvidenceFailure("synthetic state admin key is absent")
    key = state_key.read_text(encoding="utf-8").strip()
    wire_env = {**os.environ, "BRAINS_MCP_BEARER_TOKEN": key}
    path = _config_path(adapter)
    _assert_plain_path(path)
    if _config_root(adapter).exists():
        raise EvidenceFailure("client home appeared before seeding")
    _seed(path, adapter)
    plan["baseline_snapshot"] = _config_snapshot(adapter)
    plan["config_directories"] = _config_directories(adapter)
    if rollback_context is not None:
        rollback_context["config_snapshot"] = copy.deepcopy(plan["baseline_snapshot"])
        rollback_context["config_directories"] = list(plan["config_directories"])
    _write_plan(plan)
    _check_backup_collision(adapter)
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
    plan["config_directories"] = _config_directories(adapter)
    if rollback_context is not None:
        rollback_context["config_snapshot"] = copy.deepcopy(plan["wired_snapshot"])
        rollback_context["config_directories"] = list(plan["config_directories"])
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
    # Status must probe this journey's ports even if install fails before the
    # backend persists readiness. This file is private and initially absent.
    endpoints = root / "state" / "service" / "endpoints.json"
    endpoints.parent.mkdir(parents=True, exist_ok=True)
    with endpoints.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "schema_version": 1,
                "gateway_host": "127.0.0.1",
                "gateway_port": gateway_port,
                "mcp_port": mcp_port,
                "service_label": label,
            },
            stream,
        )
    _assert_native_ownership(plan, absent=True)
    if rollback_context is not None:
        rollback_context["native_armed"] = True
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
    _assert_native_ownership(plan)
    _run(executable, ["service", "stop", "--label", label])
    _assert_native_ownership(plan)
    stopped = _wait_stopped(plan)
    if platform.system() == "Darwin":
        # The exact retained plist was proved above; launchd status reports only
        # whether the job is loaded, not whether the definition is installed.
        stopped["installed"] = True
    _record(plan, "stopped", stopped)
    _assert_native_ownership(plan)
    _run(executable, ["service", "start", "--label", label])
    started = _status_evidence(_wait_healthy(executable, label), label)
    started_incarnation = _ready_incarnation(started)
    if started_incarnation[0] == installed_incarnation[0]:
        raise EvidenceFailure("native start reused the installed process identity")
    _record(plan, "started", started)
    _assert_native_ownership(plan)
    _run(executable, ["service", "restart", "--label", label])
    restarted = _status_evidence(_wait_healthy(executable, label), label)
    restarted_incarnation = _ready_incarnation(restarted)
    if restarted_incarnation[0] == started_incarnation[0]:
        raise EvidenceFailure("native restart reused the prior process identity")
    _record(plan, "restarted", restarted)
    old_pid = restarted_incarnation[0]
    _assert_native_ownership(plan)
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
    rollback_context: dict[str, Any] | None = None,
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
    _assert_native_ownership(plan)
    if rollback_context is not None:
        rollback_context.update(
            plan=copy.deepcopy(plan),
            diagnostic_steps=plan["steps"],
            native_armed=True,
            config_snapshot=copy.deepcopy(plan["wired_snapshot"]),
            config_directories=list(plan["config_directories"]),
        )
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
    if _config_directories(adapter) != plan["config_directories"]:
        raise EvidenceFailure("client configuration directories changed across the boundary")
    service_log = _evidence_root() / "state" / "sessions" / "service.log"
    service_log_text = (
        service_log.read_text(encoding="utf-8", errors="replace") if service_log.is_file() else ""
    )
    if "starting" not in service_log_text:
        raise EvidenceFailure("bounded supervisor lifecycle log evidence is absent")
    _check_backup_collision(adapter)
    _run(executable, ["unwire", "--tool", adapter, "--no-rules"])
    restored_snapshot = _config_snapshot(adapter)
    managed_backups = account_managed_backups(
        plan["baseline_snapshot"], plan["wired_snapshot"], restored_snapshot
    )
    if rollback_context is not None:
        rollback_context["config_snapshot"] = copy.deepcopy(restored_snapshot)
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
    _uninstall_owned(plan)
    removed = _wait_removed(executable, label)
    removed_evidence = _status_evidence(removed, label)
    if removed_evidence["installed"] or any(removed_evidence["listeners"].values()):
        raise EvidenceFailure("native identity or listener survived teardown")
    _assert_native_ownership(plan, absent=True)
    _remove_synthetic_config(
        adapter,
        restored_snapshot,
        directories=plan["config_directories"],
        original=plan["original_snapshot"],
    )
    if rollback_context is not None:
        rollback_context["config_snapshot"] = copy.deepcopy(plan["original_snapshot"])
        rollback_context["config_directories"] = []
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
    trusted_plan: dict[str, Any],
    expected_executable: Path,
    completed_restoration: dict[str, Any] | None = None,
    diagnostic_outcomes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform bounded cleanup and return only directly measured evidence."""
    root = _evidence_root()
    plan_path = _plan_path()
    if not root.exists():
        raise EvidenceFailure("native runtime root is absent before cleanup")
    if not plan_path.is_file():
        raise EvidenceFailure("native operational plan is absent")
    try:
        _assert_plain_path(plan_path)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        _validated_plan_digest(plan)
        if plan != trusted_plan:
            raise EvidenceFailure("cleanup plan differs from validated evidence")
        executable = str(plan.get("executable", ""))
        label = str(plan.get("label", ""))
        adapter = str(plan.get("adapter", ""))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceFailure("native operational plan is unreadable") from exc
    if not executable or adapter not in TOOLS:
        raise EvidenceFailure("native operational plan cleanup identity differs")
    resolved_executable = Path(executable).resolve(strict=True)
    if resolved_executable != expected_executable.resolve(strict=True):
        raise EvidenceFailure("cleanup executable differs from current provenance")
    baseline = plan.get("baseline_snapshot")
    wired = plan.get("wired_snapshot")
    if not isinstance(baseline, dict) or not isinstance(wired, dict):
        raise EvidenceFailure("native cleanup snapshots are invalid")
    # Native teardown must not depend on unwiring or a drifted client home.
    _uninstall_owned(plan)
    removed = _status_evidence(_wait_removed(str(resolved_executable), label), label)
    _assert_native_ownership(plan, absent=True)
    if diagnostic_outcomes is not None:
        diagnostic_outcomes["native_removed"] = True
    if completed_restoration is None:
        if _config_snapshot(adapter) != wired:
            raise EvidenceFailure("managed client configuration changed before cleanup")
        if _config_directories(adapter) != plan["config_directories"]:
            raise EvidenceFailure("client configuration directories changed before cleanup")
        _check_backup_collision(adapter)
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
        restored = plan["original_snapshot"]
        if _config_snapshot(adapter) != restored:
            raise EvidenceFailure("client home changed after completed restoration")
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
    _remove_synthetic_config(
        adapter,
        restored,
        directories=[] if completed_restoration is not None else plan["config_directories"],
        original=plan["original_snapshot"],
    )
    initial_restored = _config_snapshot(adapter) == plan["original_snapshot"]
    if not initial_restored:
        raise EvidenceFailure("native cleanup did not restore the initial client home")
    if diagnostic_outcomes is not None:
        diagnostic_outcomes["configuration_removed"] = True
    result = {
        "final_status": removed,
        **restoration,
        "initial_client_home_restored": initial_restored,
        "definition_removed": removed["installed"] is False,
        "listeners_removed": removed["listeners"] == {"gateway": False, "mcp": False},
    }
    _remove_runtime(plan)
    result["runtime_root_removed"] = not root.exists()
    if diagnostic_outcomes is not None:
        diagnostic_outcomes["runtime_root_removed"] = result["runtime_root_removed"]
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
    except Exception as exc:  # noqa: BLE001 - never overwrite or echo stale-output details
        _diagnose(exc, phase=args.phase, stage="output-preflight")
        return 1
    executable_name = "brains-ai.exe" if os.name == "nt" else "brains-ai"
    executable_path = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin") / executable_name
    executable = str(executable_path)
    result: dict[str, Any] = {"phase": args.phase}
    rollback_context: dict[str, Any] = {}
    cleanup_outcomes: dict[str, Any] = {}
    stage = "guard"
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
        stage = "runtime-tools"
        runtime_tools, controlled_path = explicit_runtime_tools(
            os.environ.get("BRAINS_NATIVE_TOOL_PATHS", "{}"),
            required=_required_runtime_tools(args.adapter),
            prepend_paths=(Path(sys.executable).parent,),
        )
        os.environ["PATH"] = controlled_path
        stage = "provenance"
        provenance = create_provenance(
            candidate=args.candidate,
            repo=args.repo,
            wheel=args.wheel,
            package_manifest=args.package_manifest,
            executable=executable_path,
            git_executable=args.git_executable,
            runtime_tools=runtime_tools,
        )
        stage = "lifecycle"
        if args.phase == "cleanup":
            stage = "cleanup-binding"
            _guard("cleanup")
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
                if (
                    plan.get("candidate") != args.candidate.lower()
                    or plan.get("adapter") != args.adapter
                    or plan.get("provenance", {}).get("binding_sha256")
                    != provenance["binding_sha256"]
                    or plan.get("steps") != prior["record"]["steps"]
                ):
                    raise EvidenceFailure("prepared cleanup evidence chain differs")
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
                    or plan["plan_core_sha256"] != prepared["plan_core_sha256"]
                    or plan["steps"] != prepared["steps"]
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
            stage = "cleanup"
            cleanup_evidence = cleanup(
                trusted_plan=plan,
                expected_executable=executable_path,
                completed_restoration=completed_restoration,
                diagnostic_outcomes=cleanup_outcomes,
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
                rollback_context=rollback_context,
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
                rollback_context=rollback_context,
            )
        else:
            result = prepare(
                executable,
                args.candidate,
                args.adapter,
                provenance,
                guard_completed=True,
                rollback_context=rollback_context,
            )
    except Exception as exc:  # noqa: BLE001 - artifact exposes type only
        _diagnose(
            exc,
            phase=args.phase,
            stage=stage,
            context=rollback_context,
            outcomes=cleanup_outcomes if args.phase == "cleanup" else None,
        )
        if rollback_context:
            rollback_context["phase"] = args.phase
            result["failure_cleanup"] = _rollback(rollback_context)
            _diagnose(
                None,
                phase=args.phase,
                stage="rollback-outcome",
                context=rollback_context,
                outcomes=result["failure_cleanup"],
            )
        result.update({"passed": False, "error_type": type(exc).__name__})
        passed = False
    else:
        passed = args.phase != "cleanup" or result["cleanup"]["runtime_root_removed"] is True
        if not passed:
            _diagnose(
                EvidenceFailure("native cleanup runtime removal is incomplete"),
                phase=args.phase,
                stage="cleanup",
            )
    result["passed"] = passed
    try:
        assert_sanitized(
            result,
            (str(Path.home()), os.environ.get("USERPROFILE", ""), getpass.getuser()),
        )
        _write_result(args.output, result, passed=passed)
    except Exception as exc:  # noqa: BLE001 - export failures must not leak record contents
        _diagnose(exc, phase=args.phase, stage="result-export", context=rollback_context)
        return 1
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

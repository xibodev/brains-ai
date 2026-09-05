"""Fail closed unless native evidence is complete, bound, and upload-safe."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from native_evidence import SHA1_RE, canonical_sha256, expected_tool_filenames, file_sha256

from brains.service.common import native_service_identity

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HOST_PATH_RE = re.compile(r"(?:^[A-Za-z]:[\\/]|[/\\](?:Users|home)[/\\])")
ADAPTERS = {"copilot-cli", "claude-code", "codex", "opencode"}
MANAGER_TOOLS = {
    "task-scheduler": {"powershell", "schtasks", "taskkill"},
    "launchd": {"launchctl", "ps", "sysctl"},
    "systemd-user": {"ps", "systemctl"},
}
MANAGER_PLATFORMS = {
    "task-scheduler": {"Windows", "windows"},
    "launchd": {"Darwin", "macos"},
    "systemd-user": {"Linux", "linux"},
}
INSTALL_STEPS = ("provenance", "harness", "manager-definition", "wire", "restoration")
SERVICE_PREPARE_STEPS = (
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
)
SERVICE_STEPS = (*SERVICE_PREPARE_STEPS, "boundary-verified", "configuration-restored", "teardown")


class VerificationFailure(RuntimeError):
    pass


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _walk_strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _walk_strings(nested)]
    return []


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise VerificationFailure(f"{label} schema differs")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise VerificationFailure(f"{label} hash differs")
    return value


def _positive(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise VerificationFailure(f"{label} is not positive")
    return value


def _bool(value: Any, expected: bool, label: str) -> None:
    if value is not expected:
        raise VerificationFailure(f"{label} differs")


def _verify_provenance(
    provenance: dict[str, Any], candidate: str, *, kind: str, manager: str, adapter: str
) -> str:
    _exact(
        provenance,
        {"schema", "binding_sha256", "source", "package", "distribution", "runtime_tools"},
        "provenance",
    )
    binding = _sha(provenance["binding_sha256"], "binding")
    bound = {key: provenance[key] for key in ("source", "package", "distribution", "runtime_tools")}
    if provenance["schema"] != "brains.native-provenance.v1" or binding != canonical_sha256(bound):
        raise VerificationFailure("provenance binding differs")
    source = _exact(bound["source"], {"commit", "tree", "git_sha256"}, "source")
    if source["commit"] != candidate or not SHA1_RE.fullmatch(str(source["commit"])):
        raise VerificationFailure("candidate differs")
    if not isinstance(source["tree"], str) or not SHA1_RE.fullmatch(source["tree"]):
        raise VerificationFailure("tree hash differs")
    _sha(source["git_sha256"], "Git executable")
    package = _exact(
        bound["package"],
        {
            "schema",
            "candidate",
            "source_tree",
            "wheel_filename",
            "wheel_sha256",
            "wheel_record_sha256",
            "wheel_archive_metadata_sha256",
            "wheel_archive_wheel_sha256",
            "builder_git_sha256",
            "manifest_sha256",
        },
        "package provenance",
    )
    if (
        package["schema"] != "brains-native-wakeup-package-provenance/v1"
        or package["candidate"] != source["commit"]
        or package["source_tree"] != source["tree"]
        or not isinstance(package["wheel_filename"], str)
        or Path(package["wheel_filename"]).name != package["wheel_filename"]
    ):
        raise VerificationFailure("package provenance identity differs")
    for key in package:
        if key.endswith("_sha256"):
            _sha(package[key], f"package {key}")
    distribution = _exact(bound["distribution"], {"wheel", "installed"}, "distribution")
    wheel = _exact(distribution["wheel"], {"sha256", "size", "payload_manifest_sha256"}, "wheel")
    _sha(wheel["sha256"], "wheel")
    if wheel["sha256"] != package["wheel_sha256"]:
        raise VerificationFailure("runtime wheel differs from package provenance")
    _sha(wheel["payload_manifest_sha256"], "wheel payload")
    _positive(wheel["size"], "wheel size")
    installed = _exact(
        distribution["installed"],
        {
            "name",
            "version",
            "manifest_sha256",
            "metadata_sha256",
            "direct_url_sha256",
            "record_hashes_verified",
            "executable_sha256",
            "interpreter_sha256",
            "console_entry_point",
        },
        "installed distribution",
    )
    if installed["name"] != "brains-ai" or installed["console_entry_point"] != "brains.cli.app:app":
        raise VerificationFailure("installed identity differs")
    if not isinstance(installed["version"], str) or not installed["version"]:
        raise VerificationFailure("installed version is absent")
    for key in (
        "manifest_sha256",
        "metadata_sha256",
        "direct_url_sha256",
        "executable_sha256",
        "interpreter_sha256",
    ):
        _sha(installed[key], f"installed {key}")
    _positive(installed["record_hashes_verified"], "RECORD count")
    expected = set(MANAGER_TOOLS[manager]) if kind == "service" else set()
    if adapter == "opencode":
        expected.update(("node", "opencode"))
    tools = _exact(bound["runtime_tools"], expected, "runtime tools")
    for name, raw in tools.items():
        tool = _exact(raw, {"executable", "sha256"}, f"tool {name}")
        executable = tool["executable"]
        if (
            not isinstance(executable, str)
            or Path(executable).name != executable
            or executable.casefold() not in expected_tool_filenames(name)
        ):
            raise VerificationFailure("tool identity differs")
        _sha(tool["sha256"], f"tool {name}")
    return binding


def _journey(
    raw: Any, *, candidate: str, matrix: dict[str, Any], provenance_sha256: str
) -> dict[str, Any]:
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
    journey = _exact(raw, keys, "journey")
    bound = {key: journey[key] for key in keys - {"schema", "binding_sha256"}}
    if (
        journey["schema"] != "brains.native-service-journey.v1"
        or journey["candidate"] != candidate
        or any(journey[key] != matrix[key] for key in ("manager", "python", "adapter", "transport"))
        or journey["provenance_sha256"] != provenance_sha256
    ):
        raise VerificationFailure("journey identity differs")
    _sha(journey["journey_id"], "journey id")
    if journey["binding_sha256"] != canonical_sha256(bound):
        raise VerificationFailure("journey binding differs")
    return journey


def _claim(evidence: Any) -> None:
    claim = _exact(
        evidence,
        {"candidate_bound", "wheel_bound", "installed_distribution_bound", "executable_bound"},
        "provenance evidence",
    )
    for key in claim:
        _bool(claim[key], True, key)


def _hashes(evidence: dict[str, Any], *keys: str) -> None:
    for key in keys:
        _sha(evidence[key], key)


def _status(
    evidence: Any, manager: str, *, active: bool, installed: bool, extras: set[str] | None = None
) -> dict[str, Any]:
    keys = {
        "manager",
        "platform",
        "label",
        "state",
        "installed",
        "healthy",
        "runtime_classification",
        "owned_process",
        "listeners",
        "mcp_protocol_ready",
        *(extras or set()),
    }
    status = _exact(evidence, keys, "service status")
    if status["manager"] != manager or status["installed"] is not installed:
        raise VerificationFailure("manager/install status differs")
    platform_slug = {"task-scheduler": "windows", "launchd": "macos", "systemd-user": "linux"}[
        manager
    ]
    if (
        status["platform"] != platform_slug
        or not isinstance(status["label"], str)
        or not status["label"]
        or not isinstance(status["state"], str)
        or not status["state"]
        or status["runtime_classification"] != ("installed-owned-ready" if active else "stopped")
    ):
        raise VerificationFailure("service status identity or classification differs")
    process = _exact(
        status["owned_process"], {"pid", "confidence", "start_marker_sha256"}, "owned process"
    )
    listeners = _exact(status["listeners"], {"gateway", "mcp"}, "listeners")
    _bool(status["healthy"], active, "health")
    _bool(listeners["gateway"], active, "gateway listener")
    _bool(listeners["mcp"], active, "MCP listener")
    _bool(status["mcp_protocol_ready"], active, "MCP readiness")
    if active:
        _positive(process["pid"], "PID")
        if process["confidence"] != "verified":
            raise VerificationFailure("process confidence differs")
        _sha(process["start_marker_sha256"], "start marker")
    elif process != {"pid": None, "confidence": "absent", "start_marker_sha256": None}:
        raise VerificationFailure("inactive process evidence differs")
    return status


def _verify_install(step: str, evidence: Any, manager: str, adapter: str) -> None:
    if step == "provenance":
        return _claim(evidence)
    if step == "harness":
        keys = {"adapter", "binary_required_for_wire"}
        if adapter == "opencode":
            keys.update(("version", "executable_sha256"))
        row = _exact(evidence, keys, "harness")
        if row["adapter"] != adapter:
            raise VerificationFailure("harness adapter differs")
        _bool(row["binary_required_for_wire"], adapter == "opencode", "harness binary")
        if adapter == "opencode":
            if row["version"] != "1.18.25":
                raise VerificationFailure("OpenCode version differs")
            _sha(row["executable_sha256"], "OpenCode executable")
        return
    if step == "manager-definition":
        row = _exact(
            evidence,
            {
                "manager",
                "platform",
                "native_execution",
                "identity",
                "gateway_port",
                "mcp_port",
                "command_sha256",
                "definition_sha256",
                "autostart",
                "restart_on_failure",
            },
            "manager definition",
        )
        platform_slug = {
            "task-scheduler": "windows",
            "launchd": "macos",
            "systemd-user": "linux",
        }[manager]
        if (
            row["manager"] != manager
            or row["platform"] != platform_slug
            or row["identity"] != native_service_identity(platform_slug, "brains-serve-all")
            or row["gateway_port"] == row["mcp_port"]
            or {row["gateway_port"], row["mcp_port"]} & {9876, 9877}
        ):
            raise VerificationFailure("manager definition differs")
        _positive(row["gateway_port"], "gateway port")
        _positive(row["mcp_port"], "MCP port")
        _sha(row["command_sha256"], "manager command")
        _sha(row["definition_sha256"], "manager definition")
        _bool(row["native_execution"], False, "native execution")
        _bool(row["autostart"], True, "autostart")
        return _bool(row["restart_on_failure"], True, "restart policy")
    if step == "wire":
        row = _exact(
            evidence,
            {"adapter", "protocol", "endpoint", "baseline_config_sha256", "wired_config_sha256"},
            "wire",
        )
        endpoint = _exact(row["endpoint"], {"host", "port", "path"}, "endpoint")
        port = _positive(endpoint["port"], "endpoint port")
        if (
            row["adapter"] != adapter
            or row["protocol"] != "streamable-http"
            or endpoint["host"] != "loopback"
            or endpoint["path"] != "/mcp"
            or port > 65535
            or port in {9876, 9877}
        ):
            raise VerificationFailure("wire endpoint differs")
        _hashes(row, "baseline_config_sha256", "wired_config_sha256")
        if row["baseline_config_sha256"] == row["wired_config_sha256"]:
            raise VerificationFailure("wire was a no-op")
        return
    row = _exact(
        evidence,
        {
            "baseline_config_sha256",
            "restored_config_sha256",
            "primary_configuration_restored",
            "managed_backup_count",
            "managed_backup_manifest_sha256",
            "initial_home_restored",
            "setup_idempotent",
            "setup_state_sha256",
        },
        "restoration",
    )
    _hashes(
        row,
        "baseline_config_sha256",
        "restored_config_sha256",
        "managed_backup_manifest_sha256",
        "setup_state_sha256",
    )
    if (
        not isinstance(row["managed_backup_count"], int)
        or isinstance(row["managed_backup_count"], bool)
        or row["managed_backup_count"] < 0
    ):
        raise VerificationFailure("backup count differs")
    for key in ("primary_configuration_restored", "initial_home_restored", "setup_idempotent"):
        _bool(row[key], True, key)


def _verify_service(step: str, evidence: Any, manager: str, adapter: str) -> None:
    if step == "provenance":
        return _claim(evidence)
    if step == "manager-identity":
        row = _exact(evidence, {"manager", "label", "platform"}, "manager identity")
        if (
            row["manager"] != manager
            or row["platform"] not in MANAGER_PLATFORMS[manager]
            or not isinstance(row["label"], str)
            or not row["label"].startswith("brains-serve-all-evidence-")
        ):
            raise VerificationFailure("manager identity differs")
        return
    if step == "endpoint-contract":
        row = _exact(
            evidence,
            {"host", "gateway_port", "mcp_port", "mcp_path", "transport"},
            "endpoint contract",
        )
        gateway, mcp = (
            _positive(row["gateway_port"], "gateway port"),
            _positive(row["mcp_port"], "MCP port"),
        )
        if (
            row["host"] != "loopback"
            or row["mcp_path"] != "/mcp"
            or row["transport"] != "streamable-http"
            or gateway == mcp
            or max(gateway, mcp) > 65535
            or {gateway, mcp} & {9876, 9877}
        ):
            raise VerificationFailure("endpoint contract differs")
        return
    if step == "adapter-wired":
        row = _exact(
            evidence,
            {"adapter", "transport", "baseline_config_sha256", "wired_config_sha256"},
            "adapter wire",
        )
        if row["adapter"] != adapter or row["transport"] != "streamable-http":
            raise VerificationFailure("adapter wire differs")
        _hashes(row, "baseline_config_sha256", "wired_config_sha256")
        if row["baseline_config_sha256"] == row["wired_config_sha256"]:
            raise VerificationFailure("adapter wire was a no-op")
        return
    if step in {"installed", "started", "restarted", "manager-recovered-owned-process"}:
        _status(evidence, manager, active=True, installed=True)
        return
    if step == "stopped":
        _status(evidence, manager, active=False, installed=True)
        return
    if step == "boundary-prepared":
        row = _exact(
            evidence,
            {"boot_marker_sha256", "login_transition_attestation"},
            "prepared boundary",
        )
        _sha(row["boot_marker_sha256"], "boot marker")
        if row["login_transition_attestation"] is not None:
            raise VerificationFailure("unsupported login attestation was supplied")
        return
    if step == "boundary-verified":
        row = _status(
            evidence,
            manager,
            active=True,
            installed=True,
            extras={
                "boot_changed",
                "prepared_boot_marker_sha256",
                "observed_boot_marker_sha256",
                "login_transition_attestation",
            },
        )
        if not isinstance(row["boot_changed"], bool):
            raise VerificationFailure("boot boundary differs")
        if row["login_transition_attestation"] is not None:
            raise VerificationFailure("unsupported login attestation was supplied")
        prepared = _sha(row["prepared_boot_marker_sha256"], "prepared boot marker")
        observed = _sha(row["observed_boot_marker_sha256"], "observed boot marker")
        if row["boot_changed"] is not (prepared != observed):
            raise VerificationFailure("boot boundary derivation differs")
        return
    if step == "configuration-restored":
        row = _exact(
            evidence,
            {
                "baseline_config_sha256",
                "restored_config_sha256",
                "primary_configuration_restored",
                "managed_backup_count",
                "managed_backup_manifest_sha256",
            },
            "configuration restoration",
        )
        _hashes(
            row,
            "baseline_config_sha256",
            "restored_config_sha256",
            "managed_backup_manifest_sha256",
        )
        _bool(row["primary_configuration_restored"], True, "primary restoration")
        if (
            not isinstance(row["managed_backup_count"], int)
            or isinstance(row["managed_backup_count"], bool)
            or row["managed_backup_count"] < 0
        ):
            raise VerificationFailure("backup count differs")
        return
    row = _status(
        evidence,
        manager,
        active=False,
        installed=False,
        extras={
            "definition_removed",
            "listeners_removed",
            "initial_client_home_restored",
            "service_log_sha256",
            "service_log_line_count",
        },
    )
    for key in ("definition_removed", "listeners_removed", "initial_client_home_restored"):
        _bool(row[key], True, key)
    _sha(row["service_log_sha256"], "service log")
    _positive(row["service_log_line_count"], "service log lines")


def _steps(
    steps: Any, required: tuple[str, ...], *, binding: str, kind: str, manager: str, adapter: str
) -> dict[str, dict[str, Any]]:
    if not isinstance(steps, list) or len(steps) != len(required):
        raise VerificationFailure("step sequence differs")
    indexed = {}
    for index, (raw, name) in enumerate(zip(steps, required, strict=True), start=1):
        row = _exact(raw, {"sequence", "step", "passed", "provenance_sha256", "evidence"}, "step")
        if (
            row["sequence"] != index
            or row["step"] != name
            or row["passed"] is not True
            or row["provenance_sha256"] != binding
        ):
            raise VerificationFailure("step binding differs")
        (_verify_install if kind == "installation" else _verify_service)(
            name, row["evidence"], manager, adapter
        )
        indexed[name] = row["evidence"]
    return indexed


def verify_record(
    path: Path, *, kind: str, candidate: str, manager: str, python: str, adapter: str
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VerificationFailure("input is not regular")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationFailure("JSON is unreadable") from exc
    if not isinstance(result, dict) or manager not in MANAGER_TOOLS or adapter not in ADAPTERS:
        raise VerificationFailure("root schema differs")
    matrix = {
        "manager": manager,
        "python": python,
        "adapter": adapter,
        "transport": "streamable-http",
    }
    if result.get("matrix") != matrix:
        raise VerificationFailure("matrix differs")
    provenance = result.get("provenance")
    if not isinstance(provenance, dict):
        raise VerificationFailure("provenance absent")
    binding = _verify_provenance(provenance, candidate, kind=kind, manager=manager, adapter=adapter)
    journey = (
        _journey(
            result.get("journey"), candidate=candidate, matrix=matrix, provenance_sha256=binding
        )
        if kind == "service"
        else None
    )
    if kind == "service":
        _sha(result.get("plan_core_sha256"), "operational plan")
    _bool(result.get("passed"), True, "result")
    if kind == "installation":
        _exact(result, {"schema", "passed", "matrix", "provenance", "steps"}, "installation result")
        if result["schema"] != "brains.native-installation-evidence.v1":
            raise VerificationFailure("schema differs")
        indexed = _steps(
            result["steps"],
            INSTALL_STEPS,
            binding=binding,
            kind=kind,
            manager=manager,
            adapter=adapter,
        )
        if (
            indexed["wire"]["baseline_config_sha256"]
            != indexed["restoration"]["baseline_config_sha256"]
        ):
            raise VerificationFailure("configuration evidence differs")
    elif result.get("phase") == "cleanup":
        _exact(
            result,
            {
                "schema",
                "phase",
                "passed",
                "matrix",
                "provenance",
                "journey",
                "plan_core_sha256",
                "cleanup",
            },
            "cleanup result",
        )
        if result["schema"] != "brains.native-service-evidence.v1":
            raise VerificationFailure("schema differs")
        cleanup = _exact(
            result["cleanup"],
            {
                "final_status",
                "baseline_config_sha256",
                "restored_config_sha256",
                "primary_configuration_restored",
                "managed_backup_count",
                "managed_backup_manifest_sha256",
                "initial_client_home_restored",
                "definition_removed",
                "listeners_removed",
                "runtime_root_removed",
                "prepared_binding_matched",
                "prior_normal_record_sha256",
                "prepare_record_sha256",
            },
            "cleanup",
        )
        final_status = _status(cleanup["final_status"], manager, active=False, installed=False)
        platform_slug = {
            "task-scheduler": "windows",
            "launchd": "macos",
            "systemd-user": "linux",
        }[manager]
        expected_label = native_service_identity(
            platform_slug, f"brains-serve-all-evidence-{candidate[:8]}"
        )
        if final_status["label"] != expected_label:
            raise VerificationFailure("cleanup status label differs")
        _hashes(
            cleanup,
            "baseline_config_sha256",
            "restored_config_sha256",
            "managed_backup_manifest_sha256",
        )
        if (
            not isinstance(cleanup["managed_backup_count"], int)
            or isinstance(cleanup["managed_backup_count"], bool)
            or cleanup["managed_backup_count"] < 0
        ):
            raise VerificationFailure("cleanup backup count differs")
        for key in (
            "primary_configuration_restored",
            "initial_client_home_restored",
            "definition_removed",
            "listeners_removed",
            "runtime_root_removed",
        ):
            _bool(cleanup[key], True, key)
        if not isinstance(cleanup["prepared_binding_matched"], bool):
            raise VerificationFailure("prepared binding differs")
        _sha(cleanup["prior_normal_record_sha256"], "prior record")
        _sha(cleanup["prepare_record_sha256"], "prepare record")
    else:
        normal_keys = {
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
        if result.get("phase") == "verify":
            normal_keys.add("prepare_record_sha256")
        _exact(
            result,
            normal_keys,
            "service result",
        )
        if result["schema"] != "brains.native-service-evidence.v1" or result["phase"] not in {
            "prepare",
            "verify",
        }:
            raise VerificationFailure("service phase differs")
        if result["phase"] == "verify":
            _sha(result["prepare_record_sha256"], "prepare record")
        required = SERVICE_PREPARE_STEPS if result["phase"] == "prepare" else SERVICE_STEPS
        indexed = _steps(
            result["steps"], required, binding=binding, kind=kind, manager=manager, adapter=adapter
        )
        platform_slug = {
            "task-scheduler": "windows",
            "launchd": "macos",
            "systemd-user": "linux",
        }[manager]
        expected_native_label = native_service_identity(
            platform_slug, indexed["manager-identity"]["label"]
        )
        status_steps = [
            "installed",
            "stopped",
            "started",
            "restarted",
            "manager-recovered-owned-process",
        ]
        if result["phase"] == "verify":
            status_steps.extend(("boundary-verified", "teardown"))
        if any(indexed[name]["label"] != expected_native_label for name in status_steps):
            raise VerificationFailure("status label differs from requested native identity")
        boundary = _exact(
            result["boundary"],
            {
                "boot_changed",
                "prepared_boot_marker_sha256",
                "observed_boot_marker_sha256",
                "login_transition_attestation",
            }
            if result["phase"] == "verify"
            else {"boot_changed", "login_transition_attestation"},
            "boundary",
        )
        if not isinstance(boundary["boot_changed"], bool):
            raise VerificationFailure("boundary differs")
        if boundary["login_transition_attestation"] is not None:
            raise VerificationFailure("unsupported login attestation was supplied")
        active = ["installed", "started", "restarted", "manager-recovered-owned-process"]
        if result["phase"] == "verify":
            active.append("boundary-verified")
            verified = indexed["boundary-verified"]
            if boundary != {key: verified[key] for key in boundary}:
                raise VerificationFailure("boundary evidence differs")
            if boundary["boot_changed"] is not True:
                raise VerificationFailure("machine-observed boot boundary is absent")
            if (
                indexed["adapter-wired"]["baseline_config_sha256"]
                != indexed["configuration-restored"]["baseline_config_sha256"]
            ):
                raise VerificationFailure("configuration evidence differs")
        elif boundary != {"boot_changed": False, "login_transition_attestation": None}:
            raise VerificationFailure("prepared boundary differs")
        pids = [indexed[name]["owned_process"]["pid"] for name in active]
        markers = [indexed[name]["owned_process"]["start_marker_sha256"] for name in active]
        if len(set(pids)) != len(pids) or len(set(markers)) != len(markers):
            raise VerificationFailure("process incarnation was reused")
    if any(HOST_PATH_RE.search(value) for value in _walk_strings(result)):
        raise VerificationFailure("host path leaked")
    if kind == "service" and result["journey"] != journey:
        raise VerificationFailure("journey record differs")
    return result


def verify_records(
    paths: list[Path],
    *,
    kind: str,
    candidate: str,
    manager: str,
    python: str,
    adapter: str,
) -> list[dict[str, Any]]:
    records = [
        verify_record(
            path,
            kind=kind,
            candidate=candidate,
            manager=manager,
            python=python,
            adapter=adapter,
        )
        for path in paths
    ]
    if len({record["provenance"]["binding_sha256"] for record in records}) != 1:
        raise VerificationFailure("provenance differs")
    if kind == "installation" and len(records) != 1:
        raise VerificationFailure("installation record set differs")
    if kind == "service":
        by_phase = {
            record["phase"]: (path, record) for path, record in zip(paths, records, strict=True)
        }
        if len(by_phase) != len(records) or set(by_phase) not in (
            {"prepare", "cleanup"},
            {"prepare", "verify", "cleanup"},
        ):
            raise VerificationFailure("service record set differs")
        prepare_path, prepare = by_phase["prepare"]
        cleanup_path, cleanup = by_phase["cleanup"]
        del cleanup_path
        link = cleanup["cleanup"]
        if any(record["journey"] != prepare["journey"] for record in records):
            raise VerificationFailure("cleanup journey differs")
        if any(record["plan_core_sha256"] != prepare["plan_core_sha256"] for record in records):
            raise VerificationFailure("cleanup operational plan differs")
        prepare_sha256 = file_sha256(prepare_path)
        if link["prepare_record_sha256"] != prepare_sha256:
            raise VerificationFailure("cleanup prepare-record link differs")
        if "verify" in by_phase:
            verify_path, verified = by_phase["verify"]
            if (
                verified["prepare_record_sha256"] != prepare_sha256
                or verified["steps"][: len(prepare["steps"])] != prepare["steps"]
                or link["prior_normal_record_sha256"] != file_sha256(verify_path)
                or link["prepared_binding_matched"] is not False
            ):
                raise VerificationFailure("completed lifecycle chain differs")
        elif (
            link["prior_normal_record_sha256"] != prepare_sha256
            or link["prepared_binding_matched"] is not True
        ):
            raise VerificationFailure("cleanup prepared-state link differs")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("installation", "service"), required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--manager", choices=tuple(MANAGER_TOOLS), required=True)
    parser.add_argument("--python", choices=("3.11", "3.12"), required=True)
    parser.add_argument("--adapter", choices=tuple(sorted(ADAPTERS)), required=True)
    parser.add_argument("--input", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        if not SHA1_RE.fullmatch(args.candidate):
            raise VerificationFailure("candidate differs")
        verify_records(
            args.input,
            kind=args.kind,
            candidate=args.candidate,
            manager=args.manager,
            python=args.python,
            adapter=args.adapter,
        )
    except Exception:  # noqa: BLE001 - verifier emits no artifact content
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

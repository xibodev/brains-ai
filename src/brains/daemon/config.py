"""Daemon configuration (WS1 §7).

Precedence (highest → lowest): **CLI flag > env (`BRAINS_DAEMON_*`) > config file
(`~/.brains/daemon.json`) > hub-returned defaults > built-in defaults** — mirrors
how :mod:`brains.config` layers settings.

Per-tool overrides (path / model / extra args / enabled) come from the config
file's ``tools`` map and/or env ``BRAINS_DAEMON_TOOL_<TOOL>_{PATH,MODEL,ARGS,ENABLED}``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Built-in defaults (the hub register response overrides the timing knobs).
DEFAULT_HEARTBEAT_S = 15
DEFAULT_ASSIGNMENTS_POLL_S = 3
DEFAULT_DETECT_S = 300
DEFAULT_TTL_S = 45
DEFAULT_MAX_CONCURRENT = 20


@dataclass
class ToolOverride:
    path: str | None = None
    model: str | None = None
    args: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class DaemonConfig:
    hub_url: str = "http://127.0.0.1:9876"
    operator_key: str = ""
    machine_id: str = ""
    machine_label: str | None = None
    org_id: int | None = None
    verify_tls: bool = True
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_S
    assignments_poll_s: int = DEFAULT_ASSIGNMENTS_POLL_S
    detect_interval_s: int = DEFAULT_DETECT_S
    ttl_s: int = DEFAULT_TTL_S
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    working_root: str | None = None
    keep_artifacts: bool = False
    tools: dict[str, ToolOverride] = field(default_factory=dict)

    def tool_override(self, tool: str) -> ToolOverride:
        return self.tools.get(tool, ToolOverride())

    def tool_enabled(self, tool: str) -> bool:
        return self.tool_override(tool).enabled


def _config_path() -> Path:
    override = os.environ.get("BRAINS_DAEMON_CONFIG")
    if override:
        return Path(override)
    try:
        from brains.api.admin_key import state_dir

        return state_dir() / "daemon.json"
    except Exception:
        return Path.home() / ".brains" / "daemon.json"


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _coerce_args(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        return shlex.split(value)
    return []


def _load_file(path: Path) -> dict:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _tools_from_file(raw: dict) -> dict[str, ToolOverride]:
    out: dict[str, ToolOverride] = {}
    for tool, spec in (raw.get("tools") or {}).items():
        if not isinstance(spec, dict):
            continue
        out[tool] = ToolOverride(
            path=spec.get("path"),
            model=spec.get("model"),
            args=_coerce_args(spec.get("args")),
            enabled=_coerce_bool(spec.get("enabled"), True),
        )
    return out


def _apply_tool_env(tools: dict[str, ToolOverride]) -> dict[str, ToolOverride]:
    """Merge ``BRAINS_DAEMON_TOOL_<TOOL>_{PATH,MODEL,ARGS,ENABLED}`` overrides."""
    prefix = "BRAINS_DAEMON_TOOL_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        if "_" not in rest:
            continue
        tool_part, _, attr = rest.rpartition("_")
        tool = tool_part.lower().replace("_", "-")
        attr = attr.lower()
        ov = tools.setdefault(tool, ToolOverride())
        if attr == "path":
            ov.path = value
        elif attr == "model":
            ov.model = value
        elif attr == "args":
            ov.args = _coerce_args(value)
        elif attr == "enabled":
            ov.enabled = _coerce_bool(value, True)
    return tools


def load_config(**overrides: Any) -> DaemonConfig:
    """Build a :class:`DaemonConfig` from file + env + explicit ``overrides``
    (CLI flags). ``None`` overrides are ignored so flags only win when set."""
    raw = _load_file(_config_path())
    hub = raw.get("hub", {}) if isinstance(raw.get("hub"), dict) else {}
    intervals = raw.get("intervals", {}) if isinstance(raw.get("intervals"), dict) else {}
    limits = raw.get("limits", {}) if isinstance(raw.get("limits"), dict) else {}
    runtime = raw.get("runtime", {}) if isinstance(raw.get("runtime"), dict) else {}

    cfg = DaemonConfig()

    # --- file layer ---
    cfg.hub_url = hub.get("url", cfg.hub_url)
    cfg.operator_key = hub.get("operator_key", cfg.operator_key)
    cfg.org_id = hub.get("org_id", cfg.org_id)
    cfg.verify_tls = _coerce_bool(hub.get("verify_tls"), cfg.verify_tls)
    cfg.heartbeat_interval_s = int(intervals.get("heartbeat_s", cfg.heartbeat_interval_s))
    cfg.assignments_poll_s = int(intervals.get("assignments_poll_s", cfg.assignments_poll_s))
    cfg.detect_interval_s = int(intervals.get("detect_s", cfg.detect_interval_s))
    cfg.ttl_s = int(intervals.get("ttl_s", cfg.ttl_s))
    cfg.max_concurrent = int(limits.get("max_concurrent", cfg.max_concurrent))
    cfg.working_root = runtime.get("working_root", cfg.working_root)
    cfg.keep_artifacts = _coerce_bool(runtime.get("keep_artifacts"), cfg.keep_artifacts)
    cfg.machine_label = raw.get("machine_label", cfg.machine_label)
    cfg.tools = _tools_from_file(raw)

    # --- env layer (BRAINS_DAEMON_*) ---
    env = os.environ
    cfg.hub_url = env.get("BRAINS_DAEMON_HUB_URL", cfg.hub_url)
    cfg.operator_key = env.get("BRAINS_DAEMON_OPERATOR_KEY", cfg.operator_key)
    if env.get("BRAINS_DAEMON_MACHINE_LABEL"):
        cfg.machine_label = env["BRAINS_DAEMON_MACHINE_LABEL"]
    if env.get("BRAINS_DAEMON_WORKING_ROOT"):
        cfg.working_root = env["BRAINS_DAEMON_WORKING_ROOT"]
    if env.get("BRAINS_DAEMON_MAX_CONCURRENT"):
        cfg.max_concurrent = int(env["BRAINS_DAEMON_MAX_CONCURRENT"])
    if env.get("BRAINS_DAEMON_HEARTBEAT_S"):
        cfg.heartbeat_interval_s = int(env["BRAINS_DAEMON_HEARTBEAT_S"])
    if env.get("BRAINS_DAEMON_ASSIGNMENTS_POLL_S"):
        cfg.assignments_poll_s = int(env["BRAINS_DAEMON_ASSIGNMENTS_POLL_S"])
    if env.get("BRAINS_DAEMON_ORG_ID"):
        with contextlib.suppress(ValueError):
            cfg.org_id = int(env["BRAINS_DAEMON_ORG_ID"])
    cfg.tools = _apply_tool_env(cfg.tools)

    # --- explicit overrides (CLI flags) ---
    for key, value in overrides.items():
        if value is None:
            continue
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    # --- derived defaults ---
    if not cfg.machine_id:
        try:
            from brains.control.sessions import current_machine_id

            cfg.machine_id = current_machine_id()
        except Exception:
            cfg.machine_id = "unknown-machine"
    if not cfg.machine_label:
        import platform

        cfg.machine_label = platform.node() or None
    if not cfg.working_root:
        cfg.working_root = os.getcwd()
    return cfg

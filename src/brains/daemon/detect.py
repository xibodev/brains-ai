"""CLI detection (WS1 §1).

Discover installed coding CLIs on PATH, map each to a ``registered_tools`` upsert
plus one ``runtimes`` row per (tool, machine). Resolution reuses the SAME resolver
the gate trusts (``shutil.which``) so "what the detector found" == "what the shim
will exec".

The static ``DETECTORS`` table mirrors ``runner._build_tool_argv``'s tool
vocabulary (``copilot | claude | codex``); the daemon additionally probes any tool
the local registry already knows (``list_registered_tools``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from brains.daemon.config import DaemonConfig, ToolOverride

# tool → (default binary on PATH, display name, version-probe argv tail)
DETECTORS: dict[str, tuple[str, str, list[str]]] = {
    "copilot": ("copilot", "Copilot CLI", ["--version"]),
    "claude": ("claude", "Claude Code", ["--version"]),
    "codex": ("codex", "Codex CLI", ["--version"]),
}

# Best-effort static model hints when a tool can't be probed for a model list.
STATIC_MODELS: dict[str, list[str]] = {
    "copilot": ["claude-opus-4.8", "gpt-5.4"],
    "claude": ["claude-opus-4.8", "claude-sonnet-4.6"],
    "codex": ["gpt-5.4", "gpt-5.3-codex"],
}


def _normalize_os() -> str:
    plat = sys.platform
    if plat.startswith("linux"):
        return "linux"
    if plat == "darwin":
        return "darwin"
    if plat.startswith("win"):
        return "win32"
    return plat


def _resolve_binary(tool: str, default_binary: str, override: ToolOverride) -> str | None:
    """Resolve a tool's binary: an explicit override path wins, else PATH lookup."""
    if override.path:
        return override.path
    return shutil.which(default_binary)


def _probe_version(binary: str, tail: list[str]) -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603 - local read-only --version probe (never gated)
            [binary, *tail],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    return out.splitlines()[0].strip() if out else None


def _known_tools(config: DaemonConfig) -> dict[str, tuple[str, str, list[str]]]:
    """Union of the static DETECTORS with anything the local tool registry knows."""
    table = dict(DETECTORS)
    try:
        from brains.control.tool_registry import list_registered_tools

        for row in list_registered_tools():
            name = row["name"]
            if name not in table:
                cli = row.get("cli_command") or name
                binary = cli.split()[0] if cli else name
                table[name] = (binary, row.get("display_name") or name, ["--version"])
    except Exception:
        pass
    return table


def detect_tools(config: DaemonConfig | None = None) -> list[dict]:
    """Return one capability descriptor per *installed* tool (enabled + on PATH).

    Each entry:
    ``{tool, display_name, binary, version, capabilities}`` where ``capabilities``
    is the JSON-able dict written to both ``registered_tools.capabilities`` and
    ``runtimes.capabilities`` (WS1 §1.2).
    """
    if config is None:
        config = DaemonConfig()
    found: list[dict] = []
    os_name = _normalize_os()
    for tool, (default_binary, display_name, tail) in _known_tools(config).items():
        if not config.tool_enabled(tool):
            continue
        override = config.tool_override(tool)
        binary = _resolve_binary(tool, default_binary, override)
        if not binary:
            continue
        version = _probe_version(binary, tail)
        capabilities = {
            "tool": tool,
            "version": version,
            "binary": binary,
            "os": os_name,
            "models": STATIC_MODELS.get(tool, []),
            "flags": {"headless": True, "allow_all": True},
        }
        if override.model:
            capabilities["default_model"] = override.model
        found.append(
            {
                "tool": tool,
                "display_name": display_name,
                "binary": binary,
                "version": version,
                "capabilities": capabilities,
            }
        )
    return found


def register_local_tools(config: DaemonConfig | None = None) -> list[dict]:
    """Upsert each detected tool into ``registered_tools`` via the EXISTING
    ``tool_registry.register_tool`` (WS1 §1.2 step 2) so the ``runtimes.tool`` FK
    holds locally. Idempotent."""
    import json

    detected = detect_tools(config)
    try:
        from brains.control.tool_registry import register_tool

        for entry in detected:
            register_tool(
                name=entry["tool"],
                display_name=entry["display_name"],
                cli_command=entry["binary"],
                capabilities=json.dumps(entry["capabilities"]),
                notes="auto-registered by brains daemon detection",
                verify=True,
            )
    except Exception:
        pass
    return detected

"""Generate and validate the shipped core advertisement inventory."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from brains.capabilities import (  # noqa: E402
    CORE_MCP_TOOLS,
    WITHDRAWN_CLI_COMMANDS,
    WITHDRAWN_CLI_GROUPS,
    withdrawn_http_path,
)


def inventory() -> dict[str, object]:
    from brains.cli.app import app as cli
    from brains.main import app as http
    from brains.mcp.server import TOOL_REGISTRY
    from brains.wire import RULE_BODY

    commands = sorted(command.name for command in cli.registered_commands if command.name)
    groups = sorted(group.name for group in cli.registered_groups if group.name)
    routes = sorted(http.openapi()["paths"])
    mounted_paths = sorted(
        path for route in http.routes if (path := getattr(route, "path", ""))
    )
    app_source = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    browser_routes = sorted(re.findall(r'<Route\s+path="([^"]+)"', app_source))
    config_source = (ROOT / "frontend/src/screens/Config.tsx").read_text(encoding="utf-8")
    config_sections = sorted(re.findall(r'key:\s*"([^"]+)"', config_source))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = sorted(project["project"].get("optional-dependencies", {}))
    return {
        "cli_commands": commands,
        "cli_groups": groups,
        "mcp_tools": sorted(TOOL_REGISTRY),
        "http_routes": routes,
        "mounted_paths": mounted_paths,
        "browser_routes": browser_routes,
        "config_sections": config_sections,
        "extras": extras,
        "wire_rule": RULE_BODY,
    }


def violations(snapshot: dict[str, object]) -> list[str]:
    errors: list[str] = []
    commands = set(snapshot["cli_commands"])
    groups = set(snapshot["cli_groups"])
    mcp_tools = set(snapshot["mcp_tools"])
    routes = list(snapshot["http_routes"])
    mounted_paths = list(snapshot["mounted_paths"])
    browser_routes = list(snapshot["browser_routes"])
    config_sections = set(snapshot["config_sections"])
    extras = set(snapshot["extras"])
    wire_rule = str(snapshot["wire_rule"]).lower()
    if overlap := commands & WITHDRAWN_CLI_COMMANDS:
        errors.append(f"withdrawn CLI commands advertised: {sorted(overlap)}")
    if overlap := groups & WITHDRAWN_CLI_GROUPS:
        errors.append(f"withdrawn CLI groups advertised: {sorted(overlap)}")
    if extra := mcp_tools - CORE_MCP_TOOLS:
        errors.append(f"non-core MCP tools advertised: {sorted(extra)}")
    if blocked := [path for path in routes if withdrawn_http_path(path)]:
        errors.append(f"withdrawn HTTP routes advertised: {blocked}")
    if blocked := [path for path in mounted_paths if path.startswith(("/dashboard", "/static/brains"))]:
        errors.append(f"withdrawn HTTP mounts advertised: {blocked}")
    if blocked := [
        path for path in browser_routes
        if path.startswith(("/labs", "/sessions", "/personas", "/pods", "/projects", "/issues", "/automation", "/runtimes", "/onboarding", "/settings"))
    ]:
        errors.append(f"withdrawn browser routes advertised: {blocked}")
    if blocked := config_sections & {"providers", "models", "integrations", "secrets", "email"}:
        errors.append(f"withdrawn configuration sections advertised: {sorted(blocked)}")
    for required in ("/health", "/v1/operator/workspaces", "/v1/asks"):
        if required not in routes:
            errors.append(f"required core HTTP route missing: {required}")
    if extras != {"dev"}:
        errors.append(f"runtime extras advertised: {sorted(extras - {'dev'})}")
    for term in ("search_semantic", "graph_query", "graph_neighbors"):
        if term in wire_rule:
            errors.append(f"wire guidance advertises {term}")
    return errors


def main() -> int:
    snapshot = inventory()
    errors = violations(snapshot)
    snapshot["violations"] = errors
    print(json.dumps(snapshot, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

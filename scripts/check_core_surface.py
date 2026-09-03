"""Generate and validate the shipped core advertisement inventory."""

from __future__ import annotations

import inspect
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WITHDRAWN_FRONTEND_API_TOKENS = (
    "/admin/configuration/",
    "/autopilots",
    "/config/providers/",
    "/config/summary",
    "/issues",
    "/onboarding/",
    "/operator/mailboxes/smtp",
    "/personas",
    "/pods",
    "/projects",
    "/runtimes",
    "/sessions/spawn",
)

from brains.capabilities import (  # noqa: E402
    CORE_MCP_TOOLS,
    WITHDRAWN_CLI_COMMANDS,
    WITHDRAWN_CLI_GROUPS,
    withdrawn_http_path,
)


def inventory() -> dict[str, object]:
    from brains.cli.app import app as cli
    from brains.events.topics import ORG_CHANNELS, parse_topic
    from brains.extras import EXTRAS
    from brains.install import VALID_FEATURES
    from brains.main import app as http
    from brains.mcp.server import TOOL_REGISTRY
    from brains.wire import RULE_BODY

    def command_tree(typer_app, prefix: str = "") -> list[str]:
        found = [
            f"{prefix}{command.name}" for command in typer_app.registered_commands if command.name
        ]
        for group in typer_app.registered_groups:
            if group.name and group.typer_instance is not None:
                group_path = f"{prefix}{group.name}"
                found.append(group_path)
                found.extend(command_tree(group.typer_instance, f"{group_path} "))
        return found

    cli_tree = sorted(command_tree(cli))
    commands = sorted(command.name for command in cli.registered_commands if command.name)
    groups = sorted(group.name for group in cli.registered_groups if group.name)
    routes = sorted(http.openapi()["paths"])
    mounted_paths = sorted(path for route in http.routes if (path := getattr(route, "path", "")))
    app_source = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    browser_routes = sorted(re.findall(r'<Route\s+path="([^"]+)"', app_source))
    config_source = (ROOT / "frontend/src/screens/Config.tsx").read_text(encoding="utf-8")
    config_sections = sorted(re.findall(r'key:\s*"([^"]+)"', config_source))
    frontend_api_source = (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    bundle_root = ROOT / "src/brains/web/spa"
    frontend_bundle = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(bundle_root.rglob("*"))
        if path.is_file() and path.suffix in {".html", ".js", ".css"}
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = sorted(project["project"].get("optional-dependencies", {}))
    mcp_contracts = {
        name: {
            "signature": str(inspect.signature(handler)),
            "description": inspect.getdoc(handler) or "",
        }
        for name, handler in TOOL_REGISTRY.items()
    }
    return {
        "cli_commands": commands,
        "cli_groups": groups,
        "cli_tree": cli_tree,
        "mcp_tools": sorted(TOOL_REGISTRY),
        "mcp_contracts": mcp_contracts,
        "http_routes": routes,
        "mounted_paths": mounted_paths,
        "browser_routes": browser_routes,
        "config_sections": config_sections,
        "withdrawn_frontend_api_tokens": sorted(
            token
            for token in WITHDRAWN_FRONTEND_API_TOKENS
            if token in frontend_api_source or token in frontend_bundle
        ),
        "extras": extras,
        "runtime_extra_registry": sorted(EXTRAS),
        "install_features": sorted(VALID_FEATURES),
        "wire_rule": RULE_BODY,
        "realtime_org_channels": sorted(ORG_CHANNELS),
        "withdrawn_realtime_topics_accepted": sorted(
            topic
            for topic in (
                "org/1/issues",
                "org/1/projects",
                "org/1/personas",
                "org/1/pods",
                "org/1/automation",
                "issue/ABC-1",
                "machine/box/control",
                "runtime/1/status",
            )
            if parse_topic(topic) is not None
        ),
        "legacy_browser_source": sorted(
            path.relative_to(ROOT).as_posix()
            for path in (
                ROOT / "src/brains/dashboard",
                ROOT / "src/brains/web/static",
                ROOT / "src/brains/web/templates/dashboard",
                ROOT / "src/brains/admin/service.py",
                ROOT / "src/brains/admin/ui.py",
                ROOT / "src/brains/web/filters.py",
                ROOT / "src/brains/web/icons.py",
            )
            if path.is_file() or (path.is_dir() and any(child.is_file() for child in path.rglob("*")))
        ),
    }


def violations(snapshot: dict[str, object]) -> list[str]:
    errors: list[str] = []
    commands = set(snapshot["cli_commands"])
    groups = set(snapshot["cli_groups"])
    cli_tree = set(snapshot["cli_tree"])
    mcp_tools = set(snapshot["mcp_tools"])
    routes = list(snapshot["http_routes"])
    mounted_paths = list(snapshot["mounted_paths"])
    browser_routes = list(snapshot["browser_routes"])
    config_sections = set(snapshot["config_sections"])
    withdrawn_frontend_api_tokens = list(snapshot["withdrawn_frontend_api_tokens"])
    extras = set(snapshot["extras"])
    runtime_extra_registry = set(snapshot["runtime_extra_registry"])
    install_features = set(snapshot["install_features"])
    wire_rule = str(snapshot["wire_rule"]).lower()
    realtime_org_channels = set(snapshot["realtime_org_channels"])
    withdrawn_realtime_topics = list(snapshot["withdrawn_realtime_topics_accepted"])
    legacy_browser_source = list(snapshot["legacy_browser_source"])
    if overlap := commands & WITHDRAWN_CLI_COMMANDS:
        errors.append(f"withdrawn CLI commands advertised: {sorted(overlap)}")
    if overlap := groups & WITHDRAWN_CLI_GROUPS:
        errors.append(f"withdrawn CLI groups advertised: {sorted(overlap)}")
    blocked_segments = WITHDRAWN_CLI_COMMANDS | WITHDRAWN_CLI_GROUPS
    if blocked := sorted(path for path in cli_tree if set(path.split()) & blocked_segments):
        errors.append(f"withdrawn nested CLI paths advertised: {blocked}")
    if extra := mcp_tools - CORE_MCP_TOOLS:
        errors.append(f"non-core MCP tools advertised: {sorted(extra)}")
    forbidden_help_contract = (
        "execution_mode",
        "retry_timeout_ms",
        "after_message_id",
        "ephemeral",
        "whatsapp",
        "telegram",
        "slack",
        "subscribed-topic",
        "messaging bridge",
    )
    for name in ("ask_human", "inbox_wait", "file_help_request", "release_help_request"):
        contract = snapshot["mcp_contracts"].get(name, {})
        advertised = f"{contract.get('signature', '')} {contract.get('description', '')}".lower()
        if terms := sorted(term for term in forbidden_help_contract if term in advertised):
            errors.append(f"withdrawn MCP contract advertised by {name}: {terms}")
    for name, contract in snapshot["mcp_contracts"].items():
        description = str(contract.get("description", "")).lower()
        if terms := sorted(
            term
            for term in ("postgres", "ephemeral", "whatsapp", "telegram", "slack")
            if term in description
        ):
            errors.append(f"withdrawn capability described by MCP tool {name}: {terms}")
    if blocked := [path for path in routes if withdrawn_http_path(path)]:
        errors.append(f"withdrawn HTTP routes advertised: {blocked}")
    if blocked := [
        path for path in mounted_paths if path.startswith(("/dashboard", "/static/brains"))
    ]:
        errors.append(f"withdrawn HTTP mounts advertised: {blocked}")
    if blocked := [
        path
        for path in browser_routes
        if path.startswith(
            (
                "/labs",
                "/sessions",
                "/personas",
                "/pods",
                "/projects",
                "/issues",
                "/automation",
                "/runtimes",
                "/onboarding",
                "/settings",
            )
        )
    ]:
        errors.append(f"withdrawn browser routes advertised: {blocked}")
    if blocked := config_sections & {"providers", "models", "integrations", "secrets", "email"}:
        errors.append(f"withdrawn configuration sections advertised: {sorted(blocked)}")
    if withdrawn_frontend_api_tokens:
        errors.append(f"withdrawn frontend API paths shipped: {withdrawn_frontend_api_tokens}")
    for required in ("/health", "/v1/operator/workspaces", "/v1/asks"):
        if required not in routes:
            errors.append(f"required core HTTP route missing: {required}")
    if extras != {"dev"}:
        errors.append(f"runtime extras advertised: {sorted(extras - {'dev'})}")
    if runtime_extra_registry:
        errors.append(f"runtime extra registry advertises: {sorted(runtime_extra_registry)}")
    if install_features:
        errors.append(f"feature installer advertises: {sorted(install_features)}")
    if realtime_org_channels != {"inbox", "sessions"}:
        errors.append(f"non-core realtime Org channels advertised: {sorted(realtime_org_channels)}")
    if withdrawn_realtime_topics:
        errors.append(f"withdrawn realtime topics accepted: {withdrawn_realtime_topics}")
    if legacy_browser_source:
        errors.append(f"deleted legacy browser source remains: {legacy_browser_source}")
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

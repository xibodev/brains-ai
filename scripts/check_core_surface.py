"""Generate and validate the shipped core advertisement inventory."""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/product/CORE_SURFACE.json"
sys.path.insert(0, str(ROOT / "src"))

HTTP_METHODS = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}

FRONTEND_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".css")
WITHDRAWN_SPA_PREFIXES = (
    "/automation",
    "/issues",
    "/labs",
    "/onboarding",
    "/personas",
    "/pods",
    "/projects",
    "/runtimes",
    "/sessions",
    "/settings",
)
CORE_SPA_TARGET_PREFIXES = (
    "/act",
    "/command-center",
    "/config",
    "/coordination",
    "/governance",
    "/inbox",
    "/operations",
    "/workspaces",
)
CORE_FRONTEND_MODULES = frozenset(
    {
        "App.tsx",
        "api/client.ts",
        "api/types.ts",
        "components/AppShell.tsx",
        "components/CommandPalette.tsx",
        "components/EmptyState.tsx",
        "components/EyebrowLabel.tsx",
        "components/MailboxWorkspace.tsx",
        "components/MasterDetail.tsx",
        "components/OperatorPrimitives.tsx",
        "components/Sidebar.tsx",
        "components/SoftCard.tsx",
        "components/StatusPill.tsx",
        "components/Toast.tsx",
        "components/TopBar.tsx",
        "components/format.ts",
        "components/sessionScope.ts",
        "main.tsx",
        "realtime/client.ts",
        "realtime/protocol.ts",
        "realtime/useRealtime.ts",
        "screens/Act.tsx",
        "screens/CommandCenter.tsx",
        "screens/Config.tsx",
        "screens/Governance.tsx",
        "screens/Operations.tsx",
        "screens/OperatorCoordination.tsx",
        "screens/ScreenHead.tsx",
        "screens/Workspaces.tsx",
        "store/OperatorContext.tsx",
        "store/useAsync.ts",
        "styles/app.css",
        "styles/tokens.css",
    }
)

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


def _configuration_keys() -> tuple[list[str], list[str], list[str]]:
    """Read the positive public configuration contract without loading settings."""

    tree = ast.parse((ROOT / "src/brains/control/configuration.py").read_text(encoding="utf-8"))
    readable: set[str] = set()
    summary_writable: set[str] = set()
    accepted_writable: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_EDITABLE"
            and isinstance(node.value, ast.Dict)
        ):
            accepted_writable.update(
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
        if not isinstance(node, ast.Dict):
            continue
        values: dict[str, ast.expr] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                values[key.value] = value
        public_key = values.get("key")
        editable = values.get("editable")
        if isinstance(public_key, ast.Constant) and isinstance(public_key.value, str):
            readable.add(public_key.value)
            if isinstance(editable, ast.Constant) and editable.value is True:
                summary_writable.add(public_key.value)
    if not readable or not summary_writable or not accepted_writable:
        raise RuntimeError("public configuration manifest could not be extracted")
    return sorted(readable), sorted(summary_writable), sorted(accepted_writable)


def _all_opt_in_env() -> dict[str, str]:
    from brains.experimental import EXPERIMENTAL_GATES

    return {
        **{name: "1" for name in EXPERIMENTAL_GATES},
        "BRAINS_MCP_TOOLS": "all",
    }


def _experimental_gate_inventory() -> dict[str, str]:
    from brains.experimental import EXPERIMENTAL_GATES

    return dict(sorted(EXPERIMENTAL_GATES.items()))


def _environment_names() -> list[str]:
    names: set[str] = set()
    for root in (ROOT / "src", ROOT / "frontend/src"):
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".ts", ".tsx"}:
                names.update(re.findall(r"\bBRAINS_[A-Z][A-Z0-9_]+\b", path.read_text(encoding="utf-8")))
    return sorted(names)


def _documented_ids() -> dict[str, object]:
    canonical = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "docs/product/PRODUCT_BRIEF.md",
            "docs/product/FEATURE_CONTRACT.md",
            "docs/product/PERSONAS_AND_JOURNEYS.md",
            "docs/product/USER_OUTCOME_SPEC.md",
            "docs/product/TRACEABILITY.md",
            "docs/product/BACKLOG.md",
        )
    }
    feature = canonical["docs/product/FEATURE_CONTRACT.md"]
    journeys = canonical["docs/product/PERSONAS_AND_JOURNEYS.md"]
    backlog = canonical["docs/product/BACKLOG.md"]
    stable_id = re.compile(
        r"\b(?:BL-P[0-3]-\d+|AC-(?:F|B)\d+-\d+|F(?:10|\d)|B\d+|J(?:1[01]|\d)|P\d+|O\d+)\b"
    )
    return {
        "features": sorted(set(re.findall(r"^### (F\d+)\b", feature, re.MULTILINE))),
        "boundaries": sorted(set(re.findall(r"^### (B\d+)\b", feature, re.MULTILINE))),
        "acceptance": sorted(set(re.findall(r"\b(AC-(?:F|B)\d+-\d+)\b", feature))),
        "journeys": sorted(set(re.findall(r"^### (J\d+)\b", journeys, re.MULTILINE))),
        "backlog": sorted(set(re.findall(r"^### (BL-P\d+-\d+)\b", backlog, re.MULTILINE))),
        "by_file": {
            path: sorted(set(stable_id.findall(source)))
            for path, source in sorted(canonical.items())
        },
    }


def _wire_inventory() -> dict[str, object]:
    from brains.mcp.transport import MCP_MODE_SSE, MCP_MODE_STDIO, MCP_MODE_STREAMABLE_HTTP
    from brains.wire import ADAPTERS, RULE_BODY, build_wire_context, wire

    rendered: dict[str, object] = {}
    token_name = "BRAINS_MCP_BEARER_TOKEN"
    previous_token = os.environ.get(token_name)
    os.environ[token_name] = "<redacted>"
    try:
        for name, adapter in sorted(ADAPTERS.items()):
            transports = [MCP_MODE_STREAMABLE_HTTP, MCP_MODE_STDIO]
            if adapter.supports_sse:
                transports.append(MCP_MODE_SSE)
            entries: dict[str, object] = {}
            for transport in transports:
                with tempfile.TemporaryDirectory(prefix=f"brains-wire-{name}-") as temporary:
                    home = Path(temporary)
                    context = build_wire_context(
                        transport=transport,
                        port=9877,
                        api_key=("" if transport == MCP_MODE_STDIO else "<redacted>"),
                        python="python",
                        db_url="sqlite:////surface/brains.db",
                    )
                    report = wire(
                        home,
                        context,
                        tools=[name],
                        rules=True,
                        force=True,
                        dry_run=False,
                    )
                    tool_reports = report.get("tools", [])
                    if not report.get("ok") or len(tool_reports) != 1:
                        raise RuntimeError(f"wire adapter {name} failed isolated generation")
                    tool_report = tool_reports[0]
                    config_path = adapter.mcp_path(home)
                    instruction_path = adapter.instr_path(home)
                    entries[transport] = {
                        "url": context.url if transport != MCP_MODE_STDIO else None,
                        "config_content": config_path.read_text(encoding="utf-8"),
                        "instruction_content": instruction_path.read_text(encoding="utf-8"),
                        "mcp_action": tool_report["mcp"].get("action"),
                        "rule_action": tool_report["rule"].get("action"),
                    }
            canonical_home = Path("/surface-home")
            rendered[name] = {
                "format": adapter.mcp_format,
                "mcp_path": adapter.mcp_path(canonical_home).relative_to(canonical_home).as_posix(),
                "instruction_path": adapter.instr_path(canonical_home)
                .relative_to(canonical_home)
                .as_posix(),
                "json_servers_key": adapter.json_servers_key,
                "mailbox_notification_mode": adapter.mailbox_notification_mode,
                "transports": entries,
            }
    finally:
        if previous_token is None:
            os.environ.pop(token_name, None)
        else:
            os.environ[token_name] = previous_token
    return {
        "adapters": rendered,
        "rule_sha256": hashlib.sha256(RULE_BODY.encode("utf-8")).hexdigest(),
    }


def _spa_navigation_inventory(
    source_root: Path | None = None,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    source_root = source_root or (ROOT / "frontend/src")
    inventory_root = ROOT if source_root == ROOT / "frontend/src" else source_root.parents[1]
    patterns = (
        ("route", re.compile(r'<Route\b[^>]*\bpath\s*=\s*(?P<q>["\'])(?P<target>.*?)(?P=q)')),
        ("redirect", re.compile(r'<Navigate\b[^>]*\bto\s*=\s*(?P<q>["\'])(?P<target>.*?)(?P=q)')),
        ("link", re.compile(r'<(?:Link|NavLink)\b[^>]*\bto\s*=\s*(?P<q>["\'])(?P<target>.*?)(?P=q)')),
        ("anchor", re.compile(r'<a\b[^>]*\bhref\s*=\s*(?P<q>["\'])(?P<target>.*?)(?P=q)')),
        ("navigate", re.compile(r'\bnavigate\(\s*(?P<q>["\'`])(?P<target>.*?)(?P=q)')),
        ("location", re.compile(r'\blocation\.(?:assign|replace)\(\s*(?P<q>["\'`])(?P<target>.*?)(?P=q)')),
        ("location-href", re.compile(r'\blocation\.href\s*=\s*(?P<q>["\'`])(?P<target>.*?)(?P=q)')),
        ("command-target", re.compile(r'\bto:\s*(?P<q>["\'`])(?P<target>.*?)(?P=q)')),
    )
    sites: list[dict[str, object]] = []
    hashes: dict[str, str] = {}
    for path in sorted(
        candidate
        for candidate in source_root.rglob("*")
        if candidate.is_file() and candidate.suffix in {".ts", ".tsx"}
    ):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(inventory_root).as_posix()
        hashes[relative] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        for kind, pattern in patterns:
            for match in pattern.finditer(source):
                sites.append(
                    {
                        "file": relative,
                        "kind": kind,
                        "line": source.count("\n", 0, match.start()) + 1,
                        "target": match.group("target"),
                    }
                )
    return sorted(
        sites,
        key=lambda item: (
            str(item["file"]),
            cast(int, item["line"]),
            str(item["kind"]),
        ),
    ), hashes


def _resolve_frontend_import(source_root: Path, importer: Path, specifier: str) -> Path | None:
    """Resolve a static relative frontend import without invoking a bundler."""

    if not specifier.startswith("."):
        return None
    unresolved = importer.parent / specifier.split("?", 1)[0].split("#", 1)[0]
    candidates = [unresolved]
    if unresolved.suffix not in FRONTEND_EXTENSIONS:
        candidates.extend(unresolved.with_suffix(suffix) for suffix in FRONTEND_EXTENSIONS)
        candidates.extend(unresolved / f"index{suffix}" for suffix in FRONTEND_EXTENSIONS)
    root = source_root.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    raise RuntimeError(f"relative frontend import could not be resolved: {specifier}")


def _frontend_reachability(
    source_root: Path | None = None,
) -> tuple[list[str], dict[str, list[str]], list[dict[str, object]]]:
    """Inventory the import graph and navigation reachable from the actual SPA entry."""

    root = source_root or (ROOT / "frontend/src")
    entry = root / "main.tsx"
    if not entry.is_file():
        raise RuntimeError("frontend entry main.tsx is unavailable")
    import_pattern = re.compile(
        r"(?:\b(?:import|export)\s+(?:type\s+)?(?:[^;\n]*?\s+from\s+)?|\bimport\s*\(\s*)"
        r"(?P<q>[\"'])(?P<target>\.[^\"']+)(?P=q)"
    )
    pending = [entry.resolve()]
    reachable: set[Path] = set()
    graph: dict[str, list[str]] = {}
    while pending:
        path = pending.pop()
        if path in reachable:
            continue
        reachable.add(path)
        source = path.read_text(encoding="utf-8")
        dependencies: list[Path] = []
        for match in import_pattern.finditer(source):
            dependency = _resolve_frontend_import(root, path, match.group("target"))
            if dependency is not None:
                dependencies.append(dependency)
                if dependency not in reachable:
                    pending.append(dependency)
        relative = path.relative_to(root).as_posix()
        graph[relative] = sorted(
            dependency.relative_to(root).as_posix() for dependency in set(dependencies)
        )
    modules = sorted(path.relative_to(root).as_posix() for path in reachable)
    if "App.tsx" not in modules:
        raise RuntimeError("frontend entry does not reach App.tsx")
    all_sites, _hashes = _spa_navigation_inventory(root)
    prefix = "frontend/src/"
    reachable_files = {f"{prefix}{module}" for module in modules}
    sites = [site for site in all_sites if site["file"] in reachable_files]
    return modules, dict(sorted(graph.items())), sites


def inventory() -> dict[str, object]:
    import typer

    from brains.cli.app import app as cli
    from brains.events.topics import ORG_CHANNELS, parse_topic
    from brains.extras import EXTRAS
    from brains.install import VALID_FEATURES
    from brains.main import app as http
    from brains.mcp.server import TOOL_PREFIX, TOOL_REGISTRY, list_tools
    from brains.wire import RULE_BODY

    resolved_cli = typer.main.get_command(cli)
    if not isinstance(getattr(resolved_cli, "commands", None), dict):
        raise RuntimeError("CLI command inventory is not a group")
    cli_tree: list[str] = []
    cli_groups: list[str] = []
    cli_nodes: dict[str, object] = {}
    callback_paths: dict[str, list[str]] = {}

    def parameter_contract(parameter: Any) -> dict[str, object]:
        spellings = [
            *list(getattr(parameter, "opts", [])),
            *list(getattr(parameter, "secondary_opts", [])),
        ]
        default = getattr(parameter, "default", None)
        if callable(default):
            default_contract: object = {"callable": getattr(default, "__qualname__", "callable")}
        elif isinstance(default, bool | int | float | str) or default is None:
            default_contract = default
        elif isinstance(default, list | tuple) and all(
            isinstance(value, bool | int | float | str) or value is None for value in default
        ):
            default_contract = list(default)
        else:
            default_contract = {"type": type(default).__name__}
        return {
            "name": parameter.name,
            "spellings": spellings,
            "type": getattr(parameter.type, "name", type(parameter.type).__name__),
            "required": bool(parameter.required),
            "default": default_contract,
            "show_default": getattr(parameter, "show_default", None),
            "multiple": bool(getattr(parameter, "multiple", False)),
            "nargs": int(getattr(parameter, "nargs", 1)),
            "is_flag": bool(getattr(parameter, "is_flag", False)),
        }

    def command_contract(command: Any, identity: str, kind: str) -> dict[str, object]:
        return {
            "callback": identity,
            "hidden": bool(command.hidden),
            "kind": kind,
            "parameters": [parameter_contract(parameter) for parameter in command.params],
        }

    def collect_commands(group: Any, prefix: str = "") -> None:
        for name, command in group.commands.items():
            path = f"{prefix}{name}"
            cli_tree.append(path)
            is_group = isinstance(getattr(command, "commands", None), dict)
            if is_group:
                cli_groups.append(path)
            callback = command.callback
            identity = (
                f"{callback.__module__}.{callback.__qualname__}" if callback is not None else "<none>"
            )
            if callback is not None:
                callback_paths.setdefault(identity, []).append(path)
            cli_nodes[path] = command_contract(
                command, identity, "group" if is_group else "command"
            )
            if is_group:
                collect_commands(command, f"{path} ")

    collect_commands(resolved_cli)
    root_commands = sorted(
        name
        for name, command in resolved_cli.commands.items()
        if not isinstance(getattr(command, "commands", None), dict)
    )
    root_groups = sorted(
        name
        for name, command in resolved_cli.commands.items()
        if isinstance(getattr(command, "commands", None), dict)
    )
    routes = sorted(http.openapi()["paths"])
    mounted_paths = sorted(path for route in http.routes if (path := getattr(route, "path", "")))
    app_source = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    browser_routes = sorted(re.findall(r'<Route\s+path="([^"]+)"', app_source))
    sidebar_source = (ROOT / "frontend/src/components/Sidebar.tsx").read_text(encoding="utf-8")
    palette_source = (ROOT / "frontend/src/components/CommandPalette.tsx").read_text(
        encoding="utf-8"
    )
    sidebar_routes = sorted(set(re.findall(r'\bto:\s*"([^"]+)"', sidebar_source)))
    palette_routes = sorted(set(re.findall(r'\bto:\s*"([^"]+)"', palette_source)))
    palette_dynamic_routes = sorted(
        set(re.findall(r"\bto:\s*`([^`?]+)\?", palette_source))
    )
    browser_redirects = sorted(set(re.findall(r'<Navigate\s+to="([^"]+)"', app_source)))
    sidebar_imperative_routes = sorted(
        set(re.findall(r'\bnavigate\("([^"]+)"', sidebar_source))
    )
    spa_navigation_sites, frontend_source_hashes = _spa_navigation_inventory()
    (
        frontend_reachable_modules,
        frontend_import_graph,
        reachable_spa_navigation_sites,
    ) = _frontend_reachability()
    frontend_dormant_modules = sorted(
        source.removeprefix("frontend/src/")
        for source in frontend_source_hashes
        if source.removeprefix("frontend/src/") not in frontend_reachable_modules
    )
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
    read_keys, summary_write_keys, accepted_write_keys = _configuration_keys()
    environment_names = _environment_names()
    mcp_contracts = {
        name: {
            "signature": str(inspect.signature(handler)),
            "description": inspect.getdoc(handler) or "",
        }
        for name, handler in TOOL_REGISTRY.items()
    }
    return {
        "cli_commands": root_commands,
        "cli_groups": root_groups,
        "cli_tree": sorted(cli_tree),
        "cli_group_tree": sorted(cli_groups),
        "cli_nodes": dict(sorted(cli_nodes.items())),
        "cli_root_callback": (
            f"{resolved_cli.callback.__module__}.{resolved_cli.callback.__qualname__}"
            if resolved_cli.callback is not None
            else "<none>"
        ),
        "cli_root": command_contract(
            resolved_cli,
            (
                f"{resolved_cli.callback.__module__}.{resolved_cli.callback.__qualname__}"
                if resolved_cli.callback is not None
                else "<none>"
            ),
            "group",
        ),
        "cli_callbacks": {key: sorted(value) for key, value in sorted(callback_paths.items())},
        "cli_aliases": sorted(
            sorted(paths) for paths in callback_paths.values() if len(paths) > 1
        ),
        "mcp_tools": sorted(TOOL_REGISTRY),
        "mcp_tool_prefix": TOOL_PREFIX,
        "mcp_advertised_tools": sorted(list_tools()),
        "mcp_contracts": mcp_contracts,
        "http_routes": routes,
        "mounted_paths": mounted_paths,
        "http_operations": sorted(
            f"{method.upper()} {path}"
            for path, operations in http.openapi()["paths"].items()
            for method in operations
            if method.upper() in HTTP_METHODS
        ),
        "browser_routes": browser_routes,
        "browser_redirects": browser_redirects,
        "sidebar_routes": sidebar_routes,
        "sidebar_imperative_routes": sidebar_imperative_routes,
        "palette_routes": palette_routes,
        "palette_dynamic_routes": palette_dynamic_routes,
        "spa_contract_sha256": {
            "app": hashlib.sha256(app_source.encode("utf-8")).hexdigest(),
            "sidebar": hashlib.sha256(sidebar_source.encode("utf-8")).hexdigest(),
            "palette": hashlib.sha256(palette_source.encode("utf-8")).hexdigest(),
        },
        "spa_navigation_sites": spa_navigation_sites,
        "reachable_spa_navigation_sites": reachable_spa_navigation_sites,
        "frontend_reachable_modules": frontend_reachable_modules,
        "frontend_dormant_modules": frontend_dormant_modules,
        "frontend_import_graph": frontend_import_graph,
        "frontend_source_sha256": frontend_source_hashes,
        "config_sections": config_sections,
        "config_read_keys": read_keys,
        "config_summary_write_keys": summary_write_keys,
        "config_write_keys": accepted_write_keys,
        "environment_names": environment_names,
        "all_opt_in_environment_names": sorted(_all_opt_in_env()),
        "experimental_gates": _experimental_gate_inventory(),
        "withdrawn_frontend_api_tokens": sorted(
            token
            for token in WITHDRAWN_FRONTEND_API_TOKENS
            if token in frontend_api_source or token in frontend_bundle
        ),
        "extras": extras,
        "runtime_extra_registry": sorted(EXTRAS),
        "install_features": sorted(VALID_FEATURES),
        "package": {
            "entry_points": project["project"].get("scripts", {}),
            "dependencies": sorted(project["project"].get("dependencies", [])),
            "optional_dependencies": {
                key: sorted(value)
                for key, value in sorted(project["project"].get("optional-dependencies", {}).items())
            },
            "package_data": project.get("tool", {}).get("setuptools", {}).get("package-data", {}),
        },
        "wire": _wire_inventory(),
        "documented_ids": _documented_ids(),
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


def violations(snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    commands = set(snapshot["cli_commands"])
    groups = set(snapshot["cli_groups"])
    cli_tree = set(snapshot["cli_tree"])
    mcp_tools = set(snapshot["mcp_tools"])
    routes = list(snapshot["http_routes"])
    mounted_paths = list(snapshot["mounted_paths"])
    browser_routes = list(snapshot["browser_routes"])
    config_sections = set(snapshot["config_sections"])
    summary_write_keys = set(snapshot["config_summary_write_keys"])
    accepted_write_keys = set(snapshot["config_write_keys"])
    withdrawn_frontend_api_tokens = list(snapshot["withdrawn_frontend_api_tokens"])
    extras = set(snapshot["extras"])
    runtime_extra_registry = set(snapshot["runtime_extra_registry"])
    install_features = set(snapshot["install_features"])
    wire_rule = str(snapshot["wire_rule"]).lower()
    realtime_org_channels = set(snapshot["realtime_org_channels"])
    withdrawn_realtime_topics = list(snapshot["withdrawn_realtime_topics_accepted"])
    legacy_browser_source = list(snapshot["legacy_browser_source"])
    reachable_modules = set(snapshot["frontend_reachable_modules"])
    reachable_navigation = list(snapshot["reachable_spa_navigation_sites"])
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
    if blocked_sections := config_sections & {"providers", "models", "integrations", "secrets", "email"}:
        errors.append(f"withdrawn configuration sections advertised: {sorted(blocked_sections)}")
    if summary_write_keys != accepted_write_keys:
        errors.append("configuration summary and accepted write keys differ")
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
    if missing_modules := CORE_FRONTEND_MODULES - reachable_modules:
        errors.append(f"required core frontend modules unreachable: {sorted(missing_modules)}")
    if extra_modules := reachable_modules - CORE_FRONTEND_MODULES:
        errors.append(f"unknown or frozen frontend modules reachable: {sorted(extra_modules)}")
    for site in reachable_navigation:
        target = str(site.get("target", ""))
        route = target.split("?", 1)[0].split("#", 1)[0]
        if route == "*":
            continue
        if route.startswith(WITHDRAWN_SPA_PREFIXES):
            errors.append(f"frozen SPA target reachable: {route}")
            continue
        if not any(route == prefix or route.startswith(f"{prefix}/") for prefix in CORE_SPA_TARGET_PREFIXES):
            errors.append(f"unknown SPA target reachable: {route or '<empty>'}")
    for term in ("search_semantic", "graph_query", "graph_neighbors"):
        if term in wire_rule:
            errors.append(f"wire guidance advertises {term}")
    return errors


def _exact_differences(expected: Any, actual: Any, path: str = "surface") -> list[str]:
    """Return bounded, non-secret differences from the reviewed manifest."""

    if isinstance(expected, dict) and isinstance(actual, dict):
        errors: list[str] = []
        expected_keys = set(expected)
        actual_keys = set(actual)
        if missing := sorted(expected_keys - actual_keys):
            errors.append(f"{path}: missing key count={len(missing)}")
        if extra := sorted(actual_keys - expected_keys):
            errors.append(f"{path}: unexpected key count={len(extra)}")
        for key in sorted(expected_keys & actual_keys):
            errors.extend(_exact_differences(expected[key], actual[key], f"{path}.{key}"))
        return errors
    if expected != actual:
        if isinstance(expected, list) and isinstance(actual, list):
            try:
                missing = sorted(set(expected) - set(actual))
                extra = sorted(set(actual) - set(expected))
            except TypeError:
                missing, extra = [], []
            if missing or extra:
                return [f"{path}: missing count={len(missing)} unexpected count={len(extra)}"]
            return [f"{path}: ordered content differs"]
        return [f"{path}: reviewed value changed"]
    return []


def manifest_violations(
    actual: dict[str, object], expected: dict[str, object]
) -> list[str]:
    errors: list[str] = []
    if expected.get("schema_version") != 1:
        errors.append("core surface manifest schema is missing or unsupported")
        return errors
    if actual.get("schema_version") != 1:
        errors.append("generated core surface inventory schema is missing or unsupported")
        return errors
    expected_modes = expected.get("modes")
    actual_modes = actual.get("modes")
    if not isinstance(expected_modes, dict) or not isinstance(actual_modes, dict):
        return ["core surface mode inventory is malformed"]
    for mode in ("normal", "all_opt_in"):
        expected_snapshot = expected_modes.get(mode)
        actual_snapshot = actual_modes.get(mode)
        if not isinstance(expected_snapshot, dict) or not isinstance(actual_snapshot, dict):
            errors.append(f"{mode}: inventory is unavailable")
            continue
        errors.extend(f"{mode}: {error}" for error in violations(actual_snapshot))
        errors.extend(_exact_differences(expected_snapshot, actual_snapshot, mode))
    return errors


def _isolated_inventory(*, all_opt_in: bool) -> dict[str, Any]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("BRAINS_")
    }
    with tempfile.TemporaryDirectory(prefix="brains-core-surface-") as isolated_home:
        env.update(
            {
                "HOME": isolated_home,
                "USERPROFILE": isolated_home,
                "XDG_CONFIG_HOME": isolated_home,
                "BRAINS_DB_URL": "sqlite:///:memory:",
                "BRAINS_RUNTIME_OVERLAY": str(Path(isolated_home) / "runtime.yaml"),
            }
        )
        if all_opt_in:
            env.update(_all_opt_in_env())
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--snapshot-only"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    if completed.returncode != 0:
        raise RuntimeError("isolated core surface inventory failed")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("isolated core surface inventory was malformed")
    return payload


def full_inventory() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "modes": {
            "normal": _isolated_inventory(all_opt_in=False),
            "all_opt_in": _isolated_inventory(all_opt_in=True),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="Write the reviewed positive manifest; never used by CI.",
    )
    args = parser.parse_args(argv)
    try:
        if args.snapshot_only:
            print(json.dumps(inventory(), sort_keys=True))
            return 0
        actual = full_inventory()
        if args.write_manifest:
            unsafe = [
                f"{mode}: {error}"
                for mode, snapshot in actual["modes"].items()
                for error in violations(snapshot)
            ]
            if unsafe:
                raise RuntimeError("generated inventory violates the core boundary")
            existing: dict[str, object] = {}
            if MANIFEST.exists():
                loaded = json.loads(MANIFEST.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            actual.update(
                {key: value for key, value in existing.items() if key not in {"schema_version", "modes"}}
            )
            MANIFEST.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"wrote reviewed core surface manifest: {MANIFEST.relative_to(ROOT)}")
            return 0
        expected = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if not isinstance(expected, dict):
            raise RuntimeError("core surface manifest must be an object")
        errors = manifest_violations(actual, expected)
        if errors:
            print(json.dumps({"violations": errors}, indent=2, sort_keys=True))
            return 1
        print("reviewed core surface matches normal and all-opt-in inventories")
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "violations": [
                        f"core surface inventory failed closed ({type(exc).__name__})"
                    ]
                },
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

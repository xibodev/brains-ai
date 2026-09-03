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
from enum import Enum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/product/CORE_SURFACE.json"
sys.path.insert(0, str(ROOT / "src"))

HTTP_METHODS = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}

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
CORE_ROUTE_GUARD_SHA256 = "7cc4382abdf32681404f3e0b68cf16c7fc781d2d4d66de229c8cc72354120225"
SPA_AST_HELPER_SHA256 = "0ea1c5dd9ccfe700c41b8b78ea92e78838e901ccae52aa9d64ebbe181dd7c473"
CORE_WIRE_RULE_SHA256 = "9ad047867401f064dae31480ba75a7a57a87bddb66bfbe05fbd4494ca39caeff"
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
        "components/useDialogFocus.ts",
        "coreRoutes.ts",
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
        "screens/NotFound.tsx",
        "screens/ScreenHead.tsx",
        "screens/Workspaces.tsx",
        "store/OperatorContext.tsx",
        "store/useAsync.ts",
        "styles/app.css",
        "styles/tokens.css",
    }
)
CORE_WIRE_ADAPTERS = {
    "claude-code": (".claude.json", ".claude/CLAUDE.md", {"sse", "stdio", "streamable-http"}),
    "codex": (".codex/config.toml", ".codex/AGENTS.md", {"stdio", "streamable-http"}),
    "copilot-cli": (
        ".copilot/mcp-config.json",
        ".copilot/copilot-instructions.md",
        {"sse", "stdio", "streamable-http"},
    ),
    "opencode": (
        ".config/opencode/opencode.json",
        ".config/opencode/AGENTS.md",
        {"sse", "stdio", "streamable-http"},
    ),
}
CORE_WIRE_METADATA = {
    "claude-code": ("json", "mcpServers", "pull"),
    "codex": ("toml", "mcpServers", "pull"),
    "copilot-cli": ("json", "mcpServers", "pull"),
    "opencode": ("json", "mcp", "pull"),
}
PARSER_INSTALL_HINT = "run `npm ci --ignore-scripts` in frontend/"


class ParserDependencyError(RuntimeError):
    """The lockfile-declared TypeScript parser has not been installed."""


def _require_frontend_parser() -> None:
    parser = ROOT / "frontend/node_modules/typescript/lib/typescript.js"
    if not parser.is_file():
        raise ParserDependencyError(
            f"TypeScript AST parser dependency is unavailable; {PARSER_INSTALL_HINT}"
        )


def _expected_wire_config(adapter: str, transport: str) -> dict[str, object]:
    url = {
        "sse": "http://127.0.0.1:9877/sse",
        "streamable-http": "http://127.0.0.1:9877/mcp",
    }.get(transport)
    if transport == "stdio":
        process = ["python", "-m", "brains.mcp.server", "--mode", "stdio"]
        if adapter == "opencode":
            server: dict[str, object] = {
                "type": "local",
                "command": process,
                "environment": {"BRAINS_DB_URL": "sqlite:////surface/brains.db"},
                "enabled": True,
                "_brains_managed": True,
            }
            return {"mcp": {"brains": server}}
        server = {
            "command": process[0],
            "args": process[1:],
            "env": {"BRAINS_DB_URL": "sqlite:////surface/brains.db"},
        }
        if adapter == "claude-code":
            server = {"type": "stdio", **server, "_brains_managed": True}
        elif adapter == "copilot-cli":
            server["_brains_managed"] = True
        return {"mcp_servers" if adapter == "codex" else "mcpServers": {"brains": server}}
    if adapter == "codex":
        return {
            "mcp_servers": {
                "brains": {
                    "url": url,
                    "bearer_token_env_var": "BRAINS_MCP_BEARER_TOKEN",
                }
            }
        }
    server = {
        "type": "remote" if adapter == "opencode" else ("sse" if transport == "sse" else "http"),
        "url": url,
        "headers": {"Authorization": "Bearer <redacted>"},
    }
    if adapter == "opencode":
        server.update({"oauth": False, "enabled": True})
    server["_brains_managed"] = True
    return {"mcp" if adapter == "opencode" else "mcpServers": {"brains": server}}


def _parse_wire_config(adapter: str, content: str) -> dict[str, object]:
    parsed = tomllib.loads(content) if adapter == "codex" else json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("wire config root is not a mapping")
    return parsed


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

CANONICAL_PRODUCT_DOCS = (
    "docs/product/PRODUCT_BRIEF.md",
    "docs/product/FEATURE_CONTRACT.md",
    "docs/product/PERSONAS_AND_JOURNEYS.md",
    "docs/product/USER_OUTCOME_SPEC.md",
    "docs/product/TRACEABILITY.md",
    "docs/product/BACKLOG.md",
)
WITHDRAWN_DOC_SURFACE_TERMS = frozenset(
    {
        "code graph",
        "dashboard",
        "embedding search",
        "external bridge",
        "github delivery",
        "graph query",
        "labs",
        "model proxy",
        "model routing",
        "provider routing",
        "public defect relay",
        "recurring jobs",
        "semantic search",
        "slack bridge",
        "smtp delivery",
        "telegram bridge",
        "webhook delivery",
        "whatsapp bridge",
    }
)
DOC_NEGATIVE_CONTEXT = re.compile(
    r"\b(?:absence|absent|cannot|compatibility|deleted|deletion|deprecated|does not|"
    r"do not|frozen|historical|legacy|never|no supported|not supported|prevents?|"
    r"removed|retained only|unavailable|unsupported|withdrawn|without)\b",
    re.IGNORECASE,
)
DOC_POSITIVE_CONTEXT = re.compile(
    r"\b(?:advertised|available|call|configure|enable|invoke|launch|navigate|open|"
    r"provides?|run|ships?|supported|use|visit)\b",
    re.IGNORECASE,
)
DOC_PATH = re.compile(r"(?<![:/A-Za-z0-9])/[A-Za-z0-9_{}][A-Za-z0-9_{}./:-]*")


def _canonical_doc_advertisements(canonical: dict[str, str]) -> list[str]:
    """Find actionable frozen-surface claims in canonical product documentation."""

    findings: set[str] = set()
    withdrawn_commands = WITHDRAWN_CLI_COMMANDS | WITHDRAWN_CLI_GROUPS
    withdrawn_code_names = {
        *WITHDRAWN_DOC_SURFACE_TERMS,
        *(name.replace("-", " ") for name in withdrawn_commands),
    }
    for relative, source in sorted(canonical.items()):
        in_fence = False
        for line_number, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if not stripped or stripped.startswith("<!--"):
                continue
            negative = DOC_NEGATIVE_CONTEXT.search(stripped) is not None
            positive = DOC_POSITIVE_CONTEXT.search(stripped) is not None
            code_tokens = re.findall(r"`([^`\n]+)`", stripped)

            for command in re.findall(r"\bbrains-ai\s+([a-z][a-z0-9-]*)", stripped):
                inline_command = any(
                    re.search(rf"\bbrains-ai\s+{re.escape(command)}\b", token)
                    for token in code_tokens
                )
                if command in withdrawn_commands and (
                    in_fence or inline_command or (positive and not negative)
                ):
                    findings.add(f"{relative}:{line_number}:cli:{command}")

            links = set(re.findall(r"\]\((/[^)\s]+)", stripped))
            paths = {raw_path.rstrip(":"): False for raw_path in DOC_PATH.findall(stripped)}
            for link in links:
                paths[link.split("?", 1)[0].split("#", 1)[0]] = True
            for path, is_link in paths.items():
                if not (in_fence or is_link or (positive and not negative)):
                    continue
                if path.startswith(("/dashboard", *WITHDRAWN_SPA_PREFIXES)) or withdrawn_http_path(
                    path
                ):
                    findings.add(f"{relative}:{line_number}:path:{path}")

            if negative:
                continue
            if in_fence or positive:
                for token in code_tokens:
                    normalized = token.strip().lower().replace("-", " ")
                    if normalized in withdrawn_code_names:
                        findings.add(f"{relative}:{line_number}:surface:{normalized}")

            if positive:
                lowered = stripped.lower().replace("-", " ")
                for name in WITHDRAWN_DOC_SURFACE_TERMS:
                    if re.search(rf"\b{re.escape(name)}\b", lowered):
                        findings.add(f"{relative}:{line_number}:surface:{name}")
    return sorted(findings)


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
                names.update(
                    re.findall(r"\bBRAINS_[A-Z][A-Z0-9_]+\b", path.read_text(encoding="utf-8"))
                )
    return sorted(names)


def _documented_ids(root: Path = ROOT) -> dict[str, object]:
    canonical = {path: (root / path).read_text(encoding="utf-8") for path in CANONICAL_PRODUCT_DOCS}
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
        "forbidden_advertisements": _canonical_doc_advertisements(canonical),
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


def _frontend_source_hashes() -> dict[str, str]:
    """Hash all retained TS/TSX source separately from AST-proven reachability."""

    source_root = ROOT / "frontend/src"
    hashes: dict[str, str] = {}
    for path in sorted(
        candidate
        for candidate in source_root.rglob("*")
        if candidate.is_file() and candidate.suffix in {".ts", ".tsx"}
    ):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT).as_posix()
        hashes[relative] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return hashes


def _frontend_reachability(
    source_root: Path | None = None,
) -> tuple[list[str], dict[str, list[str]], list[dict[str, object]]]:
    """Invoke the checked TypeScript AST inventory rooted at the actual SPA entry."""

    root = (source_root or (ROOT / "frontend/src")).resolve()
    helper = ROOT / "scripts/inventory_spa_surface.mjs"
    _require_frontend_parser()
    completed = subprocess.run(
        ["node", str(helper), str(root)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError("TypeScript AST surface inventory failed closed")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("TypeScript AST surface inventory was malformed")
    modules = payload.get("modules")
    graph = payload.get("graph")
    navigation = payload.get("navigation")
    if (
        not isinstance(modules, list)
        or not isinstance(graph, dict)
        or not isinstance(navigation, list)
    ):
        raise RuntimeError("TypeScript AST surface inventory was incomplete")
    return modules, graph, navigation


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

    def callable_identity(value: Any) -> str | None:
        if value is None:
            return None
        target = getattr(value, "__func__", value)
        module = getattr(target, "__module__", type(target).__module__)
        name = getattr(target, "__qualname__", type(target).__qualname__)
        return f"{module}.{name}"

    def normalized_value(value: Any) -> object:
        if isinstance(value, Enum):
            return normalized_value(value.value)
        if isinstance(value, Path):
            return value.as_posix()
        if callable(value):
            return {"callable": callable_identity(value)}
        if isinstance(value, bool | int | float | str) or value is None:
            return value
        if isinstance(value, list | tuple):
            return [normalized_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): normalized_value(item) for key, item in sorted(value.items())}
        return {"type": type(value).__name__}

    def type_contract(parameter_type: Any) -> dict[str, object]:
        return {
            "class": type(parameter_type).__name__,
            "name": getattr(parameter_type, "name", type(parameter_type).__name__),
            "choices": normalized_value(getattr(parameter_type, "choices", None)),
            "case_sensitive": normalized_value(getattr(parameter_type, "case_sensitive", None)),
            "min": normalized_value(getattr(parameter_type, "min", None)),
            "max": normalized_value(getattr(parameter_type, "max", None)),
            "min_open": normalized_value(getattr(parameter_type, "min_open", None)),
            "max_open": normalized_value(getattr(parameter_type, "max_open", None)),
            "clamp": normalized_value(getattr(parameter_type, "clamp", None)),
            "path_type": normalized_value(getattr(parameter_type, "path_type", None)),
            "exists": normalized_value(getattr(parameter_type, "exists", None)),
            "file_okay": normalized_value(getattr(parameter_type, "file_okay", None)),
            "dir_okay": normalized_value(getattr(parameter_type, "dir_okay", None)),
            "writable": normalized_value(getattr(parameter_type, "writable", None)),
            "readable": normalized_value(getattr(parameter_type, "readable", None)),
            "resolve_path": normalized_value(getattr(parameter_type, "resolve_path", None)),
            "allow_dash": normalized_value(getattr(parameter_type, "allow_dash", None)),
            "formats": normalized_value(getattr(parameter_type, "formats", None)),
            "types": [
                type_contract(nested) for nested in list(getattr(parameter_type, "types", []))
            ],
        }

    def parameter_contract(parameter: Any) -> dict[str, object]:
        spellings = [
            *list(getattr(parameter, "opts", [])),
            *list(getattr(parameter, "secondary_opts", [])),
        ]
        parameter_type = parameter.type
        return {
            "kind": type(parameter).__name__,
            "name": parameter.name,
            "spellings": spellings,
            "type": type_contract(parameter_type),
            "required": bool(parameter.required),
            "default": normalized_value(getattr(parameter, "default", None)),
            "help": normalized_value(getattr(parameter, "help", None)),
            "metavar": normalized_value(getattr(parameter, "metavar", None)),
            "envvar": normalized_value(getattr(parameter, "envvar", None)),
            "prompt": normalized_value(getattr(parameter, "prompt", None)),
            "prompt_required": normalized_value(getattr(parameter, "prompt_required", None)),
            "hide_input": normalized_value(getattr(parameter, "hide_input", None)),
            "confirmation_prompt": normalized_value(
                getattr(parameter, "confirmation_prompt", None)
            ),
            "expose_value": bool(getattr(parameter, "expose_value", True)),
            "is_eager": bool(getattr(parameter, "is_eager", False)),
            "flag_value": normalized_value(getattr(parameter, "flag_value", None)),
            "count": bool(getattr(parameter, "count", False)),
            "allow_from_autoenv": bool(getattr(parameter, "allow_from_autoenv", True)),
            "show_default": normalized_value(getattr(parameter, "show_default", None)),
            "show_choices": normalized_value(getattr(parameter, "show_choices", None)),
            "show_envvar": normalized_value(getattr(parameter, "show_envvar", None)),
            "hidden": bool(getattr(parameter, "hidden", False)),
            "multiple": bool(getattr(parameter, "multiple", False)),
            "nargs": int(getattr(parameter, "nargs", 1)),
            "is_flag": bool(getattr(parameter, "is_flag", False)),
            "is_bool_flag": bool(getattr(parameter, "is_bool_flag", False)),
            "callback": callable_identity(getattr(parameter, "callback", None)),
            "shell_complete": callable_identity(getattr(parameter, "shell_complete", None)),
            "autocompletion": callable_identity(getattr(parameter, "autocompletion", None)),
            "deprecated": normalized_value(getattr(parameter, "deprecated", None)),
            "deprecation": normalized_value(getattr(parameter, "deprecation", None)),
            "deprecation_help": normalized_value(getattr(parameter, "deprecation_help", None)),
            "rich_help_panel": normalized_value(getattr(parameter, "rich_help_panel", None)),
        }

    def command_contract(command: Any, identity: str, kind: str) -> dict[str, object]:
        return {
            "callback": identity,
            "name": normalized_value(getattr(command, "name", None)),
            "hidden": bool(command.hidden),
            "kind": kind,
            "help": normalized_value(getattr(command, "help", None)),
            "short_help": normalized_value(getattr(command, "short_help", None)),
            "epilog": normalized_value(getattr(command, "epilog", None)),
            "deprecated": normalized_value(getattr(command, "deprecated", None)),
            "deprecation": normalized_value(getattr(command, "deprecation", None)),
            "deprecation_help": normalized_value(getattr(command, "deprecation_help", None)),
            "options_metavar": normalized_value(getattr(command, "options_metavar", None)),
            "subcommand_metavar": normalized_value(getattr(command, "subcommand_metavar", None)),
            "context_settings": normalized_value(getattr(command, "context_settings", None)),
            "result_callback": callable_identity(getattr(command, "_result_callback", None)),
            "rich_help_panel": normalized_value(getattr(command, "rich_help_panel", None)),
            "allow_extra_args": bool(getattr(command, "allow_extra_args", False)),
            "allow_interspersed_args": bool(getattr(command, "allow_interspersed_args", True)),
            "ignore_unknown_options": bool(getattr(command, "ignore_unknown_options", False)),
            "context_class": callable_identity(getattr(command, "context_class", None)),
            "shell_complete": callable_identity(getattr(command, "shell_complete", None)),
            "command_class": callable_identity(getattr(command, "command_class", None)),
            "group_class": callable_identity(getattr(command, "group_class", None)),
            "add_help_option": bool(getattr(command, "add_help_option", True)),
            "no_args_is_help": bool(getattr(command, "no_args_is_help", False)),
            "invoke_without_command": bool(getattr(command, "invoke_without_command", False)),
            "chain": bool(getattr(command, "chain", False)),
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
                f"{callback.__module__}.{callback.__qualname__}"
                if callback is not None
                else "<none>"
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
    palette_dynamic_routes = sorted(set(re.findall(r"\bto:\s*`([^`?]+)\?", palette_source)))
    browser_redirects = sorted(set(re.findall(r'<Navigate\s+to="([^"]+)"', app_source)))
    sidebar_imperative_routes = sorted(set(re.findall(r'\bnavigate\("([^"]+)"', sidebar_source)))
    frontend_source_hashes = _frontend_source_hashes()
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
        "cli_aliases": sorted(sorted(paths) for paths in callback_paths.values() if len(paths) > 1),
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
        "reachable_spa_navigation_sites": reachable_spa_navigation_sites,
        "frontend_reachable_modules": frontend_reachable_modules,
        "frontend_dormant_modules": frontend_dormant_modules,
        "frontend_import_graph": frontend_import_graph,
        "spa_ast_helper_sha256": hashlib.sha256(
            (ROOT / "scripts/inventory_spa_surface.mjs").read_bytes()
        ).hexdigest(),
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
                for key, value in sorted(
                    project["project"].get("optional-dependencies", {}).items()
                )
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
            if path.is_file()
            or (path.is_dir() and any(child.is_file() for child in path.rglob("*")))
        ),
    }


def violations(snapshot: dict[str, Any]) -> list[str]:
    from brains.wire import MD_END, MD_START

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
    wire_rule_body = str(snapshot["wire_rule"])
    wire_rule = wire_rule_body.lower()
    realtime_org_channels = set(snapshot["realtime_org_channels"])
    withdrawn_realtime_topics = list(snapshot["withdrawn_realtime_topics_accepted"])
    legacy_browser_source = list(snapshot["legacy_browser_source"])
    reachable_modules = set(snapshot["frontend_reachable_modules"])
    reachable_navigation = list(snapshot["reachable_spa_navigation_sites"])
    frontend_hashes = snapshot["frontend_source_sha256"]
    ast_helper_hash = snapshot["spa_ast_helper_sha256"]
    wire = snapshot["wire"]
    wire_adapters = wire.get("adapters", {}) if isinstance(wire, dict) else {}
    wire_rule_hash = wire.get("rule_sha256") if isinstance(wire, dict) else None
    documented = snapshot.get("documented_ids")
    documented_advertisements = (
        documented.get("forbidden_advertisements") if isinstance(documented, dict) else None
    )
    if overlap := commands & WITHDRAWN_CLI_COMMANDS:
        errors.append(f"withdrawn CLI commands advertised: {sorted(overlap)}")
    if not isinstance(documented_advertisements, list):
        errors.append("canonical documentation advertisement inventory is malformed")
    elif documented_advertisements:
        errors.append(
            f"canonical documentation advertises frozen surfaces: {documented_advertisements}"
        )
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
    if blocked_sections := config_sections & {
        "providers",
        "models",
        "integrations",
        "secrets",
        "email",
    }:
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
    if (
        not isinstance(frontend_hashes, dict)
        or frontend_hashes.get("frontend/src/coreRoutes.ts") != CORE_ROUTE_GUARD_SHA256
    ):
        errors.append("runtime core-route guard differs from the reviewed semantic boundary")
    if ast_helper_hash != SPA_AST_HELPER_SHA256:
        errors.append("TypeScript AST helper differs from the reviewed semantic analyzer")
    for site in reachable_navigation:
        target = str(site.get("target", ""))
        route = target.split("?", 1)[0].split("#", 1)[0]
        if route in {"*", "@core-route-guard", "@history-delta"}:
            continue
        if route.startswith(WITHDRAWN_SPA_PREFIXES):
            errors.append(f"frozen SPA target reachable: {route}")
            continue
        if not any(
            route == prefix or route.startswith(f"{prefix}/") for prefix in CORE_SPA_TARGET_PREFIXES
        ):
            errors.append(f"unknown SPA target reachable: {route or '<empty>'}")
    if set(wire_adapters) != set(CORE_WIRE_ADAPTERS):
        errors.append("wire adapter set differs from the supported core adapters")
    if (
        wire_rule_hash != CORE_WIRE_RULE_SHA256
        or hashlib.sha256(wire_rule_body.encode("utf-8")).hexdigest() != CORE_WIRE_RULE_SHA256
    ):
        errors.append("wire guidance differs from the reviewed core guidance")
    expected_instruction = f"{MD_START}\n{wire_rule_body.rstrip()}\n{MD_END}\n"
    for name, (mcp_path, instruction_path, expected_transports) in CORE_WIRE_ADAPTERS.items():
        adapter = wire_adapters.get(name)
        if not isinstance(adapter, dict):
            continue
        expected_format, expected_servers_key, expected_notification = CORE_WIRE_METADATA[name]
        if (
            adapter.get("format") != expected_format
            or adapter.get("json_servers_key") != expected_servers_key
            or adapter.get("mailbox_notification_mode") != expected_notification
        ):
            errors.append(f"wire adapter {name} metadata differs from the supported contract")
        if (
            adapter.get("mcp_path") != mcp_path
            or adapter.get("instruction_path") != instruction_path
        ):
            errors.append(f"wire adapter {name} uses a noncanonical managed path")
        transports = adapter.get("transports", {})
        if not isinstance(transports, dict) or set(transports) != expected_transports:
            errors.append(f"wire adapter {name} exposes incorrect transports")
            continue
        for transport_name, contract in transports.items():
            if not isinstance(contract, dict):
                errors.append(f"wire adapter {name}/{transport_name} contract is malformed")
                continue
            expected_url = {
                "sse": "http://127.0.0.1:9877/sse",
                "stdio": None,
                "streamable-http": "http://127.0.0.1:9877/mcp",
            }[transport_name]
            if contract.get("url") != expected_url:
                errors.append(f"wire adapter {name}/{transport_name} uses an incorrect URL")
            if contract.get("mcp_action") != "create" or contract.get("rule_action") != "create":
                errors.append(f"wire adapter {name}/{transport_name} action is not atomic creation")
            config_content = contract.get("config_content")
            instruction_content = contract.get("instruction_content")
            if not isinstance(config_content, str):
                errors.append(f"wire adapter {name}/{transport_name} config content is invalid")
            else:
                try:
                    parsed_config = _parse_wire_config(name, config_content)
                except (json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError):
                    errors.append(f"wire adapter {name}/{transport_name} config is malformed")
                else:
                    if parsed_config != _expected_wire_config(name, transport_name):
                        errors.append(
                            f"wire adapter {name}/{transport_name} config structure is not exact"
                        )
            if instruction_content != expected_instruction:
                errors.append(f"wire adapter {name}/{transport_name} instructions are not exact")
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


def manifest_violations(actual: dict[str, object], expected: dict[str, object]) -> list[str]:
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
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("BRAINS_")}
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
        _require_frontend_parser()
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
                {
                    key: value
                    for key, value in existing.items()
                    if key not in {"schema_version", "modes"}
                }
            )
            MANIFEST.write_text(
                json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
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
    except ParserDependencyError:
        print(
            json.dumps(
                {
                    "violations": [
                        f"TypeScript AST parser dependency is unavailable; {PARSER_INSTALL_HINT}"
                    ]
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {"violations": [f"core surface inventory failed closed ({type(exc).__name__})"]},
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

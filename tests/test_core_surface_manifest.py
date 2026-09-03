from __future__ import annotations

import copy
import json
import subprocess

import pytest

from scripts import check_core_surface


def _manifest() -> dict[str, object]:
    return json.loads(check_core_surface.MANIFEST.read_text(encoding="utf-8"))


def _mutate_path(mapping: dict[str, object], dotted: str) -> None:
    parts = dotted.split(".")
    target = mapping
    for part in parts[:-1]:
        value = target[part]
        assert isinstance(value, dict)
        target = value
    leaf = parts[-1]
    current = target[leaf]
    if isinstance(current, bool):
        target[leaf] = not current
    elif isinstance(current, list):
        target[leaf] = [*current, "changed"]
    else:
        target[leaf] = "changed"


def test_spa_ast_helper_executes_directly_against_repository() -> None:
    helper = check_core_surface.ROOT / "scripts/inventory_spa_surface.mjs"
    source = check_core_surface.ROOT / "frontend/src"
    completed = subprocess.run(
        ["node", str(helper), str(source)],
        cwd=check_core_surface.ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert "main.tsx" in payload["modules"]
    assert "App.tsx" in payload["modules"]
    assert payload["graph"]["main.tsx"]
    assert payload["navigation"]


def test_spa_ast_helper_fails_clearly_without_declared_parser(
    tmp_path, monkeypatch, capsys
) -> None:
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    monkeypatch.setattr(check_core_surface, "ROOT", tmp_path)

    with pytest.raises(RuntimeError, match=r"npm ci --ignore-scripts"):
        check_core_surface._frontend_reachability(source)

    assert check_core_surface.main([]) == 1
    assert "npm ci --ignore-scripts" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("section", "rogue"),
    [
        ("cli_tree", "rogue-command"),
        ("mcp_tools", "rogue_tool"),
        ("http_operations", "POST /v1/rogue"),
        ("browser_routes", "/rogue"),
        ("sidebar_routes", "/rogue"),
        ("palette_routes", "/rogue"),
        ("config_read_keys", "rogue.value"),
        ("config_summary_write_keys", "rogue.value"),
        ("config_write_keys", "rogue.value"),
        ("environment_names", "BRAINS_ROGUE_SWITCH"),
        ("extras", "rogue"),
    ],
)
def test_positive_manifest_rejects_rogue_surface(section: str, rogue: str) -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    modes = actual["modes"]
    assert isinstance(modes, dict)
    normal = modes["normal"]
    assert isinstance(normal, dict)
    values = normal[section]
    assert isinstance(values, list)
    values.append(rogue)

    assert check_core_surface.manifest_violations(actual, expected)


@pytest.mark.parametrize("mode", ["normal", "all_opt_in"])
def test_positive_manifest_rejects_rogue_package_and_documented_surface(mode: str) -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    modes = actual["modes"]
    assert isinstance(modes, dict)
    snapshot = modes[mode]
    assert isinstance(snapshot, dict)
    package = snapshot["package"]
    assert isinstance(package, dict)
    scripts = package["entry_points"]
    assert isinstance(scripts, dict)
    scripts["rogue"] = "rogue:main"
    optional = package["optional_dependencies"]
    assert isinstance(optional, dict)
    optional["rogue"] = ["rogue"]
    documented = snapshot["documented_ids"]
    assert isinstance(documented, dict)
    backlog = documented["backlog"]
    assert isinstance(backlog, list)
    backlog.append("BL-P9-99")

    errors = check_core_surface.manifest_violations(actual, expected)
    assert any("entry_points" in error for error in errors)
    assert any("optional_dependencies" in error for error in errors)
    assert any("documented_ids.backlog" in error for error in errors)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Operators can use the Labs dashboard.\n", "surface:labs"),
        ("```console\nbrains-ai graph-query\n```\n", "cli:graph-query"),
        ("[Open Labs](/labs)\n", "path:/labs"),
        ("The `brains-ai run` command is available.\n", "cli:run"),
        ("Navigate to `/v1/issues` to file work.\n", "path:/v1/issues"),
    ],
)
def test_canonical_docs_reject_frozen_advertisements(source: str, expected: str) -> None:
    findings = check_core_surface._canonical_doc_advertisements(
        {"docs/product/PRODUCT_BRIEF.md": source}
    )
    assert any(finding.endswith(expected) for finding in findings)


def test_canonical_docs_allow_explicit_boundary_prose() -> None:
    source = "\n".join(
        [
            "Historical `/labs` and the dashboard were deleted.",
            "Semantic search is frozen and not supported.",
            "The graph query capability is frozen and not supported.",
            "The retained GitHub delivery code is compatibility-only.",
        ]
    )
    assert not check_core_surface._canonical_doc_advertisements(
        {"docs/product/TRACEABILITY.md": source}
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "Historical compatibility only: `brains-ai graph-query` must not be used.\n",
            "cli:graph-query",
        ),
        (
            "Withdrawn compatibility link: [old Labs](/labs?from=history).\n",
            "path:/labs",
        ),
        (
            "Historical compatibility example:\n```console\nbrains-ai graph-query\n```\n",
            "cli:graph-query",
        ),
    ],
)
def test_actionable_docs_syntax_is_never_exempted_by_boundary_prose(
    source: str, expected: str
) -> None:
    findings = check_core_surface._canonical_doc_advertisements(
        {"docs/product/TRACEABILITY.md": source}
    )
    assert any(finding.endswith(expected) for finding in findings)


def test_noncanonical_docs_do_not_define_product_advertisements(tmp_path) -> None:
    for relative in check_core_surface.CANONICAL_PRODUCT_DOCS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Canonical\n", encoding="utf-8")
    history = tmp_path / "docs" / "history.md"
    history.write_text("Use the Labs dashboard.\n", encoding="utf-8")

    documented = check_core_surface._documented_ids(tmp_path)
    assert documented["forbidden_advertisements"] == []


def test_semantic_docs_rejection_survives_manifest_regeneration() -> None:
    normal = copy.deepcopy(_manifest()["modes"]["normal"])
    normal["documented_ids"]["forbidden_advertisements"] = [
        "docs/product/PRODUCT_BRIEF.md:1:path:/labs"
    ]

    assert any(
        "documentation advertises frozen surfaces" in error
        for error in check_core_surface.violations(normal)
    )


def test_positive_manifest_rejects_changed_harness_rendering() -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    modes = actual["modes"]
    assert isinstance(modes, dict)
    normal = modes["normal"]
    assert isinstance(normal, dict)
    wire = normal["wire"]
    assert isinstance(wire, dict)
    adapters = wire["adapters"]
    assert isinstance(adapters, dict)
    adapters["rogue"] = {"format": "json"}

    aliases = normal["cli_aliases"]
    assert isinstance(aliases, list)
    aliases.append(["rogue", "rogue-alias"])

    errors = check_core_surface.manifest_violations(actual, expected)
    assert any("wire.adapters" in error for error in errors)
    assert any("cli_aliases" in error for error in errors)


def test_positive_manifest_rejects_deep_surface_changes() -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    modes = actual["modes"]
    assert isinstance(modes, dict)
    normal = modes["normal"]
    assert isinstance(normal, dict)
    normal["http_routes"].append("/v1/rogue")
    normal["mounted_paths"].append("/v1/rogue")
    normal["palette_dynamic_routes"].append("/rogue")
    normal["spa_contract_sha256"]["app"] = "0" * 64
    normal["package"]["dependencies"].append("rogue>=1")
    first_tool = next(iter(normal["mcp_contracts"]))
    normal["mcp_contracts"][first_tool]["description"] = "changed"
    normal["wire"]["rule_sha256"] = "0" * 64

    errors = check_core_surface.manifest_violations(actual, expected)
    for section in (
        "http_routes",
        "mounted_paths",
        "palette_dynamic_routes",
        "spa_contract_sha256.app",
        "package.dependencies",
        "mcp_contracts",
        "wire.rule_sha256",
    ):
        assert any(section in error for error in errors)


def test_positive_manifest_rejects_cli_contract_mutations() -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    normal = actual["modes"]["normal"]
    normal["cli_groups"].append("rogue")
    normal["cli_callbacks"]["rogue.callback"] = ["rogue"]
    normal["cli_root"]["parameters"].append({"name": "rogue"})
    normal["cli_nodes"]["serve"]["parameters"].append({"name": "activator"})

    errors = check_core_surface.manifest_violations(actual, expected)
    for section in ("cli_groups", "cli_callbacks", "cli_root.parameters", "cli_nodes.serve"):
        assert any(section in error for error in errors)


@pytest.mark.parametrize(
    "field",
    [
        "kind",
        "name",
        "spellings",
        "type.class",
        "type.name",
        "type.choices",
        "type.case_sensitive",
        "type.min",
        "type.max",
        "type.min_open",
        "type.max_open",
        "type.clamp",
        "type.path_type",
        "type.exists",
        "type.file_okay",
        "type.dir_okay",
        "type.writable",
        "type.readable",
        "type.resolve_path",
        "type.allow_dash",
        "type.formats",
        "type.types",
        "required",
        "default",
        "help",
        "metavar",
        "envvar",
        "prompt",
        "prompt_required",
        "hide_input",
        "confirmation_prompt",
        "expose_value",
        "is_eager",
        "flag_value",
        "count",
        "allow_from_autoenv",
        "show_default",
        "show_choices",
        "show_envvar",
        "hidden",
        "multiple",
        "nargs",
        "is_flag",
        "is_bool_flag",
        "callback",
        "shell_complete",
        "autocompletion",
        "deprecated",
        "deprecation",
        "deprecation_help",
        "rich_help_panel",
    ],
)
def test_positive_manifest_rejects_every_cli_parameter_field(field: str) -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    parameter = actual["modes"]["normal"]["cli_nodes"]["service logs"]["parameters"][0]
    _mutate_path(parameter, field)

    errors = check_core_surface.manifest_violations(actual, expected)
    assert any("cli_nodes.service logs.parameters" in error for error in errors)


@pytest.mark.parametrize(
    "field",
    [
        "callback",
        "name",
        "hidden",
        "kind",
        "help",
        "short_help",
        "epilog",
        "deprecated",
        "deprecation",
        "deprecation_help",
        "options_metavar",
        "subcommand_metavar",
        "context_settings",
        "result_callback",
        "rich_help_panel",
        "allow_extra_args",
        "allow_interspersed_args",
        "ignore_unknown_options",
        "context_class",
        "shell_complete",
        "command_class",
        "group_class",
        "add_help_option",
        "no_args_is_help",
        "invoke_without_command",
        "chain",
    ],
)
def test_positive_manifest_rejects_cli_root_contract_fields(field: str) -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    _mutate_path(actual["modes"]["normal"]["cli_root"], field)

    errors = check_core_surface.manifest_violations(actual, expected)
    assert any(f"cli_root.{field}" in error for error in errors)


def test_manifest_records_supported_secondary_option_spellings() -> None:
    manifest = _manifest()
    normal = manifest["modes"]["normal"]
    root_spellings = {
        spelling
        for parameter in normal["cli_root"]["parameters"]
        for spelling in parameter["spellings"]
    }
    log_spellings = {
        spelling
        for parameter in normal["cli_nodes"]["service logs"]["parameters"]
        for spelling in parameter["spellings"]
    }
    assert {"--version", "-V"} <= root_spellings
    assert {"--lines", "-n"} <= log_spellings


def test_positive_manifest_rejects_advertisement_and_activation_mutations() -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    normal = actual["modes"]["normal"]
    normal["mcp_tool_prefix"] = "rogue_"
    normal["mcp_advertised_tools"].append("rogue_tool")
    normal["config_sections"].append("rogue")
    normal["extras"].append("rogue")
    normal["install_features"].append("rogue")
    all_opt = actual["modes"]["all_opt_in"]
    all_opt["all_opt_in_environment_names"].append("BRAINS_ROGUE_ACTIVATOR")

    errors = check_core_surface.manifest_violations(actual, expected)
    for section in (
        "mcp_tool_prefix",
        "mcp_advertised_tools",
        "config_sections",
        "extras",
        "install_features",
        "all_opt_in_environment_names",
    ):
        assert any(section in error for error in errors)


def test_positive_manifest_rejects_navigation_and_wire_file_mutations() -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    normal = actual["modes"]["normal"]
    normal["browser_redirects"].append("/rogue")
    normal["reachable_spa_navigation_sites"].append(
        {"file": "frontend/src/Rogue.tsx", "kind": "link", "line": 1, "target": "/rogue"}
    )
    adapter = next(iter(normal["wire"]["adapters"].values()))
    transport = next(iter(adapter["transports"].values()))
    transport["config_content"] = transport["config_content"].replace("brains", "rogue", 1)
    transport["instruction_content"] = "missing sentinels"

    errors = check_core_surface.manifest_violations(actual, expected)
    assert any("browser_redirects" in error for error in errors)
    assert any("reachable_spa_navigation_sites" in error for error in errors)
    assert any("config_content" in error for error in errors)
    assert any("instruction_content" in error for error in errors)


def test_positive_manifest_rejects_frontend_source_hash_mutation() -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    hashes = actual["modes"]["normal"]["frontend_source_sha256"]
    first_source = next(iter(hashes))
    hashes[first_source] = "0" * 64
    normal = actual["modes"]["normal"]
    normal["spa_ast_helper_sha256"] = "0" * 64

    errors = check_core_surface.manifest_violations(actual, expected)
    assert any("frontend_source_sha256" in error for error in errors)
    assert any("spa_ast_helper_sha256" in error for error in errors)


def test_reachability_rejects_rendered_retained_labs_component(tmp_path) -> None:
    source = tmp_path / "frontend/src"
    (source / "screens").mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App";\n<App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(
        'import { LabsHome } from "./screens/Labs";\n'
        "export function App() { return <LabsHome />; }\n"
        '<a href="/labs">Labs</a>;\n',
        encoding="utf-8",
    )
    (source / "screens/Labs.tsx").write_text(
        "export function LabsHome() { return <div>Labs</div>; }\n", encoding="utf-8"
    )

    modules, graph, sites = check_core_surface._frontend_reachability(source)
    expected = _manifest()
    actual = copy.deepcopy(expected)
    normal = actual["modes"]["normal"]
    normal["frontend_reachable_modules"] = modules
    normal["frontend_import_graph"] = graph
    normal["reachable_spa_navigation_sites"] = sites

    errors = check_core_surface.violations(normal)
    assert any("screens/Labs.tsx" in error for error in errors)
    assert any("frozen SPA target reachable: /labs" in error for error in errors)


@pytest.mark.parametrize(
    "app_source",
    [
        'export function App() { return <Route path={"/labs"} />; }\n',
        'export function App() { return <Route path={"/" + "labs"} />; }\n',
        'import { LabsHome as Core } from "./screens/Labs"; export function App() { return <Core />; }\n',
        'const Core = import("./screens/Labs"); export function App() { return <div>{Core}</div>; }\n',
    ],
)
def test_ast_reachability_rejects_expression_alias_and_dynamic_import_bypasses(
    tmp_path, app_source: str
) -> None:
    source = tmp_path / "frontend/src"
    (source / "screens").mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(app_source, encoding="utf-8")
    (source / "screens/Labs.tsx").write_text(
        "export const LabsHome = () => <div />;\n", encoding="utf-8"
    )

    modules, graph, sites = check_core_surface._frontend_reachability(source)
    normal = copy.deepcopy(_manifest()["modes"]["normal"])
    normal["frontend_reachable_modules"] = modules
    normal["frontend_import_graph"] = graph
    normal["reachable_spa_navigation_sites"] = sites
    assert check_core_surface.violations(normal)


def test_reachability_rejects_unknown_target_without_manifest_comparison() -> None:
    normal = copy.deepcopy(_manifest()["modes"]["normal"])
    normal["reachable_spa_navigation_sites"].append(
        {"file": "frontend/src/App.tsx", "kind": "route", "line": 1, "target": "/rogue"}
    )

    assert any(
        "unknown SPA target reachable: /rogue" in error
        for error in check_core_surface.violations(normal)
    )


def test_reachability_accepts_non_advertising_history_navigation() -> None:
    normal = copy.deepcopy(_manifest()["modes"]["normal"])
    normal["reachable_spa_navigation_sites"].append(
        {
            "file": "frontend/src/screens/NotFound.tsx",
            "kind": "history",
            "line": 1,
            "target": "@history-delta",
        }
    )

    assert not any("SPA target" in error for error in check_core_surface.violations(normal))


def test_ast_reachability_fails_closed_on_unresolved_route_target(tmp_path) -> None:
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(
        "export function App() { return <Route path={window.location.pathname} />; }\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="failed closed"):
        check_core_surface._frontend_reachability(source)


@pytest.mark.parametrize(
    "app_source",
    [
        'import * as Router from "react-router-dom"; '
        'export function App() { return <Router.Route path="/labs" />; }\n',
        'import * as Router from "react-router-dom"; const RR = Router; '
        'export function App() { return <RR.Route path="/labs" />; }\n',
        'import { redirect as go } from "react-router-dom"; '
        'export function App() { go("/labs"); return <div />; }\n',
        'import * as Router from "react-router-dom"; const { redirect: go } = Router; '
        'export function App() { go("/labs"); return <div />; }\n',
        'export function App() { window.location.assign("/labs"); return <div />; }\n',
        'export function App() { window.location.replace("/labs"); return <div />; }\n',
        'export function App() { window.location.href = "/labs"; return <div />; }\n',
        'export function App() { window.location.pathname = "/labs"; return <div />; }\n',
        'export function App() { window.location = "/labs"; return <div />; }\n',
        'export function App() { location = "/labs"; return <div />; }\n',
        "const destination = window.location; "
        'export function App() { destination.assign("/labs"); return <div />; }\n',
    ],
)
def test_ast_reachability_detects_namespace_router_and_window_location(
    tmp_path, app_source: str
) -> None:
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(app_source, encoding="utf-8")

    _modules, _graph, sites = check_core_surface._frontend_reachability(source)
    assert any(site["target"] == "/labs" for site in sites)


def test_ast_reachability_fails_closed_on_unknown_namespace_router_sink(tmp_path) -> None:
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(
        'import * as Router from "react-router-dom"; '
        'export function App() { return <Router.Form to="/labs" />; }\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="failed closed"):
        check_core_surface._frontend_reachability(source)


def test_ast_reachability_fails_closed_on_compound_location_write(tmp_path) -> None:
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(
        'export function App() { window.location.pathname += "/labs"; return <div />; }\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="failed closed"):
        check_core_surface._frontend_reachability(source)


def test_ast_reachability_follows_configured_alias_and_reexport(tmp_path) -> None:
    source = tmp_path / "frontend/src"
    (source / "screens").mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","baseUrl":".",'
        '"paths":{"@/*":["src/*"]},"jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(
        'import { LabsHome as Core } from "@/barrel"; export const App = () => <Core />;\n',
        encoding="utf-8",
    )
    (source / "barrel.ts").write_text(
        'export { LabsHome } from "./screens/Labs";\n', encoding="utf-8"
    )
    (source / "screens/Labs.tsx").write_text(
        "export const LabsHome = () => <div />;\n", encoding="utf-8"
    )

    modules, _graph, _sites = check_core_surface._frontend_reachability(source)
    assert "barrel.ts" in modules
    assert "screens/Labs.tsx" in modules


def test_ast_reachability_fails_closed_on_nonliteral_dynamic_import(tmp_path) -> None:
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(
        'const target = "./screens/Labs"; const loaded = import(target); '
        "export const App = () => <div>{String(loaded)}</div>;\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="failed closed"):
        check_core_surface._frontend_reachability(source)


@pytest.mark.parametrize(
    "field",
    [
        "format",
        "mcp_path",
        "instruction_path",
        "json_servers_key",
        "mailbox_notification_mode",
    ],
)
def test_positive_manifest_rejects_every_wire_adapter_field(field: str) -> None:
    expected = _manifest()
    adapters = expected["modes"]["normal"]["wire"]["adapters"]
    for adapter_name in adapters:
        actual = copy.deepcopy(expected)
        actual_adapter = actual["modes"]["normal"]["wire"]["adapters"][adapter_name]
        actual_adapter[field] = "changed"
        errors = check_core_surface.manifest_violations(actual, expected)
        assert any(f"wire.adapters.{adapter_name}.{field}" in error for error in errors)


@pytest.mark.parametrize(
    "field",
    ["url", "config_content", "instruction_content", "mcp_action", "rule_action"],
)
def test_positive_manifest_rejects_every_wire_transport_field(field: str) -> None:
    expected = _manifest()
    adapters = expected["modes"]["normal"]["wire"]["adapters"]
    for adapter_name, adapter in adapters.items():
        for transport_name in adapter["transports"]:
            actual = copy.deepcopy(expected)
            transport = actual["modes"]["normal"]["wire"]["adapters"][adapter_name]["transports"][
                transport_name
            ]
            transport[field] = "changed"
            errors = check_core_surface.manifest_violations(actual, expected)
            assert any(
                f"wire.adapters.{adapter_name}.transports.{transport_name}.{field}" in error
                for error in errors
            )


def test_wire_semantics_reject_drift_without_manifest_comparison() -> None:
    baseline = _manifest()["modes"]["normal"]
    adapters = baseline["wire"]["adapters"]
    for adapter_name, adapter in adapters.items():
        for field in ("mcp_path", "instruction_path"):
            snapshot = copy.deepcopy(baseline)
            snapshot["wire"]["adapters"][adapter_name][field] = "rogue"
            assert check_core_surface.violations(snapshot)
        for transport_name in adapter["transports"]:
            mutations = {
                "url": "http://127.0.0.1:9877/rogue",
                "mcp_action": "skip",
                "rule_action": "skip",
                "config_content": "brains",
                "instruction_content": "brains:wire:start\nbrains:wire:end",
            }
            for field, replacement in mutations.items():
                snapshot = copy.deepcopy(baseline)
                snapshot["wire"]["adapters"][adapter_name]["transports"][transport_name][field] = (
                    replacement
                )
                assert check_core_surface.violations(snapshot)


def _wire_config_with_rogue_server(adapter_name: str, content: str) -> str:
    if adapter_name == "codex":
        return f'{content}\n[mcp_servers.rogue]\ncommand = "rogue"\n'
    parsed = json.loads(content)
    servers_key = "mcp" if adapter_name == "opencode" else "mcpServers"
    parsed[servers_key]["rogue"] = {"command": "rogue"}
    return json.dumps(parsed)


def _wire_config_with_rogue_process(adapter_name: str, content: str) -> str:
    if adapter_name == "codex":
        return content.replace('command = "python"', 'command = "rogue"')
    parsed = json.loads(content)
    servers_key = "mcp" if adapter_name == "opencode" else "mcpServers"
    server = parsed[servers_key]["brains"]
    if adapter_name == "opencode":
        server["command"][0] = "rogue"
    else:
        server["command"] = "rogue"
    return json.dumps(parsed)


def test_wire_semantics_reject_same_shape_regenerated_bypasses() -> None:
    baseline = _manifest()["modes"]["normal"]
    for adapter_name, adapter in baseline["wire"]["adapters"].items():
        for transport_name, transport in adapter["transports"].items():
            rogue_server = copy.deepcopy(baseline)
            rogue_server["wire"]["adapters"][adapter_name]["transports"][transport_name][
                "config_content"
            ] = _wire_config_with_rogue_server(adapter_name, transport["config_content"])
            assert any(
                "config structure is not exact" in error
                for error in check_core_surface.violations(rogue_server)
            )

            rogue_instruction = copy.deepcopy(baseline)
            rogue_instruction["wire"]["adapters"][adapter_name]["transports"][transport_name][
                "instruction_content"
            ] = f"rogue guidance\n{transport['instruction_content']}appended rogue guidance\n"
            assert any(
                "instructions are not exact" in error
                for error in check_core_surface.violations(rogue_instruction)
            )

            if transport_name == "stdio":
                rogue_process = copy.deepcopy(baseline)
                rogue_process["wire"]["adapters"][adapter_name]["transports"][transport_name][
                    "config_content"
                ] = _wire_config_with_rogue_process(adapter_name, transport["config_content"])
                assert any(
                    "config structure is not exact" in error
                    for error in check_core_surface.violations(rogue_process)
                )


def test_wire_inventory_covers_every_adapter_transport_and_managed_file() -> None:
    normal = _manifest()["modes"]["normal"]
    adapters = normal["wire"]["adapters"]
    expected = {
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
    assert set(adapters) == set(expected)
    for name, adapter in adapters.items():
        expected_mcp, expected_instruction, expected_transports = expected[name]
        assert adapter["mcp_path"] == expected_mcp
        assert adapter["instruction_path"] == expected_instruction
        transports = adapter["transports"]
        assert set(transports) == expected_transports
        assert transports["stdio"]["url"] is None
        assert transports["streamable-http"]["url"] == "http://127.0.0.1:9877/mcp"
        if "sse" in transports:
            assert transports["sse"]["url"] == "http://127.0.0.1:9877/sse"
        for transport in transports.values():
            assert transport["mcp_action"] == "create"
            assert transport["rule_action"] == "create"
            assert "brains:wire:start" in transport["instruction_content"]
            assert "brains:wire:end" in transport["instruction_content"]
            assert "brains" in transport["config_content"].lower()
            if transport["url"] is not None:
                assert transport["url"] in transport["config_content"]


def test_checker_fails_closed_without_disclosing_inventory_error(monkeypatch, capsys) -> None:
    def fail() -> dict[str, object]:
        raise RuntimeError("sensitive inventory detail")

    monkeypatch.setattr(check_core_surface, "full_inventory", fail)
    assert check_core_surface.main([]) == 1
    output = capsys.readouterr().out
    assert "failed closed (RuntimeError)" in output
    assert "sensitive inventory detail" not in output


def test_manifest_rejects_auto_named_cli_surface_removal() -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    modes = actual["modes"]
    assert isinstance(modes, dict)
    normal = modes["normal"]
    assert isinstance(normal, dict)
    nodes = normal["cli_nodes"]
    assert isinstance(nodes, dict)
    assert "serve" in nodes
    del nodes["serve"]

    assert check_core_surface.manifest_violations(actual, expected)


def test_configuration_inventory_reads_actual_acceptance_map(tmp_path, monkeypatch) -> None:
    source = tmp_path / "src/brains/control"
    source.mkdir(parents=True)
    (source / "configuration.py").write_text(
        '_EDITABLE: dict[str, tuple[str, str]] = {"visible": ("a", "restart"), '
        '"accepted_only": ("b", "restart")}\n'
        'FIELDS = [{"key": "visible", "editable": True}]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(check_core_surface, "ROOT", tmp_path)

    readable, summary_writable, accepted_writable = check_core_surface._configuration_keys()
    assert readable == ["visible"]
    assert summary_writable == ["visible"]
    assert accepted_writable == ["accepted_only", "visible"]

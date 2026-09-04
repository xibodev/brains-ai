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


def test_spa_ast_helper_semantic_hash_normalizes_line_endings(tmp_path) -> None:
    helper = tmp_path / "helper.mjs"
    helper.write_bytes(b'const route = "/";\nexport { route };\n')
    lf_hash = check_core_surface._semantic_text_sha256(helper)

    helper.write_bytes(b'const route = "/";\r\nexport { route };\r\n')
    assert check_core_surface._semantic_text_sha256(helper) == lf_hash

    helper.write_bytes(b'const route = "/labs";\r\nexport { route };\r\n')
    assert check_core_surface._semantic_text_sha256(helper) != lf_hash


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


@pytest.mark.parametrize("kind", ["source", "sdist", "wheel"])
@pytest.mark.parametrize("mutation", ["missing", "extra", "stale"])
def test_manifest_rejects_distribution_inventory_drift(kind: str, mutation: str) -> None:
    expected = _manifest()
    expected["distribution"] = {
        "source": ["src/brains/__init__.py"],
        "sdist": ["pyproject.toml"],
        "wheel": ["brains/__init__.py"],
    }
    actual = copy.deepcopy(expected)
    expected_members = expected["distribution"][kind]
    actual_members = actual["distribution"][kind]
    assert isinstance(expected_members, list) and expected_members
    assert isinstance(actual_members, list)
    if mutation == "missing":
        actual_members.pop()
    elif mutation == "extra":
        actual_members.append("rogue/member")
        actual_members.sort()
    else:
        expected_members[-1] = "stale/reviewed-member"
        expected_members.sort()

    errors = check_core_surface.manifest_violations(actual, expected)
    assert any(f"distribution.{kind}" in error for error in errors)


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        {},
        {"source": ["src/brains/__init__.py"], "sdist": ["pyproject.toml"]},
        {
            "source": ["src/brains/__init__.py"],
            "sdist": ["pyproject.toml"],
            "wheel": ["brains/__init__.py"],
            "extra": ["rogue/member"],
        },
        {"source": [], "sdist": ["pyproject.toml"], "wheel": ["brains/__init__.py"]},
        {
            "source": ["src/brains/z.py", "src/brains/a.py"],
            "sdist": ["pyproject.toml"],
            "wheel": ["brains/__init__.py"],
        },
        {
            "source": ["src/brains/__init__.py", "src/brains/__init__.py"],
            "sdist": ["pyproject.toml"],
            "wheel": ["brains/__init__.py"],
        },
        {
            "source": ["src/brains/__init__.py"],
            "sdist": ["pyproject.toml"],
            "wheel": [1],
        },
    ],
)
def test_manifest_rejects_malformed_distribution_inventory(malformed) -> None:
    expected = _manifest()
    expected["distribution"] = {
        "source": ["src/brains/__init__.py"],
        "sdist": ["pyproject.toml"],
        "wheel": ["brains/__init__.py"],
    }
    actual = copy.deepcopy(expected)
    if malformed is None:
        actual.pop("distribution", None)
    else:
        actual["distribution"] = malformed

    errors = check_core_surface.manifest_violations(actual, expected)
    assert any("distribution" in error for error in errors)


def test_manifest_generation_requires_fresh_wheel_and_sdist(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / "CORE_SURFACE.json"
    original = '{"preserved": true}\n'
    manifest.write_text(original, encoding="utf-8")
    monkeypatch.setattr(check_core_surface, "MANIFEST", manifest)
    monkeypatch.setattr(check_core_surface, "_require_frontend_parser", lambda: None)

    assert check_core_surface.main(["--write-manifest"]) == 1
    assert "failed closed" in capsys.readouterr().out
    assert manifest.read_text(encoding="utf-8") == original


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


def test_negative_doc_clause_does_not_exempt_mixed_positive_claim() -> None:
    findings = check_core_surface._canonical_doc_advertisements(
        {
            "docs/product/TRACEABILITY.md": (
                "Labs is unavailable, but users can use the dashboard.\n"
            )
        }
    )

    assert any(finding.endswith("surface:dashboard") for finding in findings)


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

    with pytest.raises(RuntimeError, match="failed closed"):
        check_core_surface._frontend_reachability(source)


@pytest.mark.parametrize(
    "app_source",
    [
        'import { Route } from "react-router-dom"; export function App() { return <Route path={"/labs"} />; }\n',
        'import { Route } from "react-router-dom"; export function App() { return <Route path={"/" + "labs"} />; }\n',
        'window.history.pushState({}, "", "/labs"); export function App() { return <div />; }\n',
        'const h = globalThis["history"]; h.replaceState(null, "", "/labs"); export function App() { return <div />; }\n',
        'const push = window.history.pushState; push({}, "", "/labs"); export function App() { return <div />; }\n',
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

    with pytest.raises(RuntimeError, match="failed closed"):
        check_core_surface._frontend_reachability(source)


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
        'import { Route } from "react-router-dom"; export function App() { return <Route path={window.location.pathname} />; }\n',
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

    with pytest.raises(RuntimeError, match="failed closed"):
        check_core_surface._frontend_reachability(source)


def _synthetic_spa_sites(tmp_path, app_source: str):
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(app_source, encoding="utf-8")
    return check_core_surface._frontend_reachability(source)[2]


@pytest.mark.parametrize(
    "app_source",
    [
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); const go = navigate; go("/labs"); export const App = () => <div />;\n',
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); const { go } = { go: navigate }; go("/labs"); export const App = () => <div />;\n',
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); const [go] = [navigate]; go("/labs"); export const App = () => <div />;\n',
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); let go; go = navigate; go("/labs"); export const App = () => <div />;\n',
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); let go; [go] = [navigate]; go("/labs"); export const App = () => <div />;\n',
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); let go; ({ go } = { go: navigate }); go("/labs"); export const App = () => <div />;\n',
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); const first = navigate; const second = first; second("/labs"); export const App = () => <div />;\n',
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); const { navigate: go } = { navigate }; go("/labs"); export const App = () => <div />;\n',
    ],
)
def test_ast_reachability_closes_navigate_callback_aliases(tmp_path, app_source: str) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


@pytest.mark.parametrize(
    "app_source",
    [
        'window.open("/labs"); export const App = () => <div />;\n',
        'window["open"]("/labs"); export const App = () => <div />;\n',
        'open("/labs"); export const App = () => <div />;\n',
        'const go = window.open; go("/labs"); export const App = () => <div />;\n',
        'const { open: go } = window; go("/labs"); export const App = () => <div />;\n',
        'let go; go = globalThis["open"]; go("/labs"); export const App = () => <div />;\n',
        'const first = window.open; const second = first; second("/labs"); export const App = () => <div />;\n',
    ],
)
def test_ast_reachability_covers_window_open_and_aliases(tmp_path, app_source: str) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


@pytest.mark.parametrize(
    "app_source",
    [
        'document.location = "/labs"; export const App = () => <div />;\n',
        'document["location"].href = "/labs"; export const App = () => <div />;\n',
        'document.location["assign"]("/labs"); export const App = () => <div />;\n',
        'window["location"] = "/labs"; export const App = () => <div />;\n',
        'window["location"]["replace"]("/labs"); export const App = () => <div />;\n',
        'const loc = window.location; loc.assign("/labs"); export const App = () => <div />;\n',
        'const { assign: go } = window.location; go("/labs"); export const App = () => <div />;\n',
        'const { location: loc } = window; loc["replace"]("/labs"); export const App = () => <div />;\n',
        'let loc; loc = globalThis["location"]; loc.assign("/labs"); export const App = () => <div />;\n',
        'let go; ({ replace: go } = document.location); go("/labs"); export const App = () => <div />;\n',
        'const first = window.location.assign; const second = first; second("/labs"); export const App = () => <div />;\n',
    ],
)
def test_ast_reachability_covers_location_dot_and_bracket_forms(tmp_path, app_source: str) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


@pytest.mark.parametrize(
    "app_source",
    [
        'export const App = () => <form action="/labs" />;\n',
        'export const App = () => <button formAction="/labs" />;\n',
        'const form = document.createElement("form"); form.action = "/labs"; export const App = () => <div />;\n',
        'const button = document.querySelector("button"); button["formAction"] = "/labs"; export const App = () => <div />;\n',
        'document.forms[0]["action"] = "/labs"; export const App = () => <div />;\n',
    ],
)
def test_ast_reachability_covers_form_navigation_activators(tmp_path, app_source: str) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


@pytest.mark.parametrize(
    "app_source",
    [
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); const go = navigate; go(target); export const App = () => <div />;\n',
        "const go = window.open; go(target); export const App = () => <div />;\n",
        'document["location"]["assign"](target); export const App = () => <div />;\n',
        "const { assign: go } = window.location; go(target); export const App = () => <div />;\n",
        'let loc; loc = globalThis["location"]; loc.replace(target); export const App = () => <div />;\n',
        "export const App = () => <form action={target} />;\n",
        'const form = document.createElement("form"); form["action"] = target; export const App = () => <div />;\n',
    ],
)
def test_ast_reachability_fails_closed_for_every_dynamic_activator(
    tmp_path, app_source: str
) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


def test_ast_reachability_ignores_non_navigation_members_and_custom_attributes(tmp_path) -> None:
    sites = _synthetic_spa_sites(
        tmp_path,
        'const service = { open: (_target: string) => {} }; service.open("/labs"); '
        "const model = { location: { assign: (_target: string) => {} } }; "
        'model.location.assign("/labs"); const config = { action: "save", formAction: "submit" }; '
        'config.action = "save"; config["formAction"] = "submit"; '
        "const Widget = (_props: object) => <div />; export const App = () => "
        '<Widget action="/labs" formAction="/labs" />;\n',
    )
    assert not any(site["target"] == "/labs" for site in sites)


@pytest.mark.parametrize(
    "app_source",
    [
        'const w = self; const d = w.document; const loc = d.location; loc.assign("/labs"); export const App = () => <div />;\n',
        'const w = top; const { open: go } = w; go("/labs"); export const App = () => <div />;\n',
        'const w = parent; w["location"].href = "/labs"; export const App = () => <div />;\n',
        'const d = window.document; d.location.replace("/labs"); export const App = () => <div />;\n',
        'import { Link } from "react-router-dom"; const X = Link; export const App = () => <X to="/labs" />;\n',
        'import * as Router from "react-router-dom"; export const App = () => <Router.Form action="/labs" />;\n',
        'import { Link } from "react-router-dom"; export const App = () => <Link {...{to: "/labs"}} />;\n',
        'import React from "react"; import { Link } from "react-router-dom"; export const App = () => React.createElement(Link, {to: "/labs"});\n',
        'import { createBrowserRouter } from "react-router-dom"; createBrowserRouter([{path: "/labs", element: <div />}]); export const App = () => <div />;\n',
        'import { useRoutes } from "react-router-dom"; export const App = () => useRoutes([{path: "/labs", element: <div />}]);\n',
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); ({go: navigate}).go("/labs"); export const App = () => <div />;\n',
        'const form = document.createElement("form"); form.setAttribute("action", "/labs"); export const App = () => <div />;\n',
        'const button = document.querySelector("button"); button["setAttribute"]("formaction", "/labs"); export const App = () => <div />;\n',
    ],
)
def test_ast_reachability_uses_symbol_provenance_for_aliases_and_activators(
    tmp_path, app_source: str
) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


def test_ast_reachability_follows_reexported_router_component_symbol(tmp_path) -> None:
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "router.ts").write_text(
        'export { Link as ProductLink } from "react-router-dom";\n', encoding="utf-8"
    )
    (source / "App.tsx").write_text(
        'import { ProductLink } from "./router"; export const App = () => <ProductLink to="/labs" />;\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="failed closed"):
        check_core_surface._frontend_reachability(source)


@pytest.mark.parametrize(
    "app_source",
    [
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); pass(navigate); export const App = () => <div />;\n',
        'import { useNavigate } from "react-router-dom"; const navigate = useNavigate(); const bound = navigate.bind(null); bound("/labs"); export const App = () => <div />;\n',
        'const key = "open"; window[key]("/labs"); export const App = () => <div />;\n',
        'const form = document.createElement("form"); form.setAttribute(name, "/labs"); export const App = () => <div />;\n',
        'import { Link } from "react-router-dom"; export const App = () => <Link {...props} />;\n',
        'import { createBrowserRouter } from "react-router-dom"; createBrowserRouter(routes); export const App = () => <div />;\n',
        'open(flag ? "/labs" : target); export const App = () => <div />;\n',
        "open(`/labs/${target}`); export const App = () => <div />;\n",
    ],
)
def test_ast_reachability_fails_closed_on_capability_escape_and_dynamic_joins(
    tmp_path, app_source: str
) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


def test_ast_reachability_tracks_alias_kills_and_lexical_shadowing(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(
            tmp_path / "ignored",
            'const local = (_target: string) => {}; let go = window.open; go = local; go("/labs"); '
            "function nested(open: (_target: string) => void, location: {assign: (_target: string) => void}) {"
            ' open("/labs"); location.assign("/labs"); } '
            "const history = {pushState: (_a: object, _b: string, _c: string) => {}}; "
            'history.pushState({}, "", "/labs"); export const App = () => <div />;\n',
        )

    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(
            tmp_path / "detected",
            "const local = (_target: string) => {}; let go = local; go = window.open; "
            'function nested() { go("/labs"); } export const App = () => <div />;\n',
        )


@pytest.mark.parametrize(
    "app_source",
    [
        'let go = window.open; if (flag) go = (_target: string) => {}; go("/labs"); export const App = () => <div />;\n',
        'let go = window.open; go = maybe; go("/labs"); export const App = () => <div />;\n',
    ],
)
def test_ast_reachability_fails_closed_on_conditional_or_unknown_alias_kill(
    tmp_path, app_source: str
) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


def test_ast_reachability_records_dom_submit_activation_without_inventing_target(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(
            tmp_path,
            'const form = document.createElement("form"); form.requestSubmit(); export const App = () => <div />;\n',
        )


def test_ast_reachability_fails_closed_on_import_meta_glob(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(
            tmp_path,
            'const modules = import.meta.glob("./screens/*.tsx"); export const App = () => <div />;\n',
        )


@pytest.mark.parametrize(
    "app_source",
    [
        'const local = (_target: string) => {}; const go = flag ? window.open : local; go("/labs"); export const App = () => <div />;\n',
        'const local = (_target: string) => {}; let go = local; go ||= window.open; go("/labs"); export const App = () => <div />;\n',
        'const local = (_target: string) => {}; let go = window.open; go &&= local; go("/labs"); export const App = () => <div />;\n',
        'const local = (_target: string) => {}; let go = window.open; go ??= local; go("/labs"); export const App = () => <div />;\n',
        "const box = {go: window.open}; export const App = () => <div />;\n",
        "const slots: unknown[] = []; slots[0] = window.open; export const App = () => <div />;\n",
        "function factory() { return window.open; } export const App = () => <div />;\n",
        "const factory = () => window.open; export const App = () => <div />;\n",
        "let go = window.open; go += local; export const App = () => <div />;\n",
    ],
)
def test_ast_reachability_denies_unmodeled_capability_flow(tmp_path, app_source: str) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


@pytest.mark.parametrize(
    "app_source",
    [
        'import { createBrowserRouter } from "react-router-dom"; createBrowserRouter([{path: "/", children: [{path: "/labs", element: <div />}]}]); export const App = () => <div />;\n',
        'import React from "react"; export const App = () => React.createElement("a", {href: "/labs"});\n',
        'import { createElement as h } from "react"; export const App = () => h("form", {action: "/labs"});\n',
        'import * as React from "react"; export const App = () => React.createElement("button", {...{formAction: "/labs"}});\n',
        'import { createElement as h } from "react"; import { Link } from "react-router-dom"; export const App = () => h(Link, {...{to: "/labs"}});\n',
    ],
)
def test_ast_reachability_covers_nested_routes_and_create_element_forms(
    tmp_path, app_source: str
) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


@pytest.mark.parametrize(
    "app_source",
    [
        'import React from "react"; export const App = () => React.createElement("a", props);\n',
        'import { createElement as h } from "react"; import { Link } from "react-router-dom"; export const App = () => h(Link, {...props});\n',
    ],
)
def test_ast_reachability_fails_closed_on_dynamic_create_element_props(
    tmp_path, app_source: str
) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _synthetic_spa_sites(tmp_path, app_source)


def test_ast_reachability_ignores_non_navigation_create_element(tmp_path) -> None:
    sites = _synthetic_spa_sites(
        tmp_path,
        'import React from "react"; export const App = () => React.createElement("div", {title: "/labs"});\n',
    )
    assert not any(site["target"] == "/labs" for site in sites)


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


def test_ast_reachability_fails_closed_on_dynamic_history_state_target(tmp_path) -> None:
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(
        "export function App() { "
        'window.history.pushState({}, "", window.location.pathname); '
        "return <div />; }\n",
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
        "mailbox_wakeup_mode",
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
            if name == "opencode":
                plugin = transport["lifecycle_plugin"]
                assert plugin["path"] == ".config/opencode/plugins/brains-lifecycle.js"
                assert plugin["manifest_path"] == (
                    ".config/opencode/plugins/brains-lifecycle.sha256"
                )
                assert plugin["verified_version"] == "1.18.25"
                assert plugin["content"].startswith("// brains:opencode-lifecycle:v1")
                assert plugin["manifest_content"].endswith("\n")


@pytest.mark.parametrize("mutation", ["extra-content", "extra-slot"])
def test_manifest_rejects_opencode_lifecycle_plan_mutation(mutation: str) -> None:
    expected = _manifest()
    actual = copy.deepcopy(expected)
    plugin = actual["modes"]["normal"]["wire"]["adapters"]["opencode"]["transports"]["stdio"][
        "lifecycle_plugin"
    ]
    if mutation == "extra-content":
        plugin["content"] += "\nprocess.env.UNREVIEWED\n"
    else:
        plugin["unreviewed_runtime_slot"] = "value"

    errors = check_core_surface.manifest_violations(actual, expected)
    assert any("lifecycle_plugin" in error for error in errors)


def test_checker_fails_closed_without_disclosing_inventory_error(
    tmp_path, monkeypatch, capsys
) -> None:
    def fail(_dist) -> dict[str, object]:
        raise RuntimeError("sensitive inventory detail")

    monkeypatch.setattr(check_core_surface, "full_inventory", fail)
    assert check_core_surface.main(["--dist", str(tmp_path)]) == 1
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


_FINITE_APP = """
import { BrowserRouter, Route, Routes } from "react-router-dom";
import "./coreRoutes";
import "./Consumer";
export function App() {
  return <BrowserRouter><Routes><Route element={<div />}>
    <Route index element={<div />} />
    <Route path="/command-center" element={<div />} />
    <Route path="/workspaces" element={<div />} />
    <Route path="/workspaces/:slug" element={<div />} />
    <Route path="/coordination" element={<div />} />
    <Route path="/governance" element={<div />} />
    <Route path="/operations" element={<div />} />
    <Route path="/operations/config" element={<div />} />
    <Route path="/operations/config/:section" element={<div />} />
    <Route path="/act" element={<div />} />
    <Route path="/inbox" element={<div />} />
    <Route path="/config" element={<div />} />
    <Route path="*" element={<div />} />
  </Route></Routes></BrowserRouter>;
}
"""


def _finite_boundary_fixture(
    tmp_path,
    consumer: str = "export const Consumer = () => null;\n",
    *,
    app: str = _FINITE_APP,
    boundary_mutation=None,
):
    source = tmp_path / "frontend/src"
    source.mkdir(parents=True)
    (tmp_path / "frontend/tsconfig.json").write_text(
        '{"compilerOptions":{"moduleResolution":"bundler","jsx":"react-jsx"}}',
        encoding="utf-8",
    )
    (source / "main.tsx").write_text('import { App } from "./App"; <App />;\n', encoding="utf-8")
    (source / "App.tsx").write_text(app, encoding="utf-8")
    (source / "Consumer.tsx").write_text(consumer, encoding="utf-8")
    boundary = (check_core_surface.ROOT / "frontend/src/coreRoutes.tsx").read_text(encoding="utf-8")
    if boundary_mutation is not None:
        boundary = boundary_mutation(boundary)
    (source / "coreRoutes.tsx").write_text(boundary, encoding="utf-8")
    return check_core_surface._frontend_reachability(source)


@pytest.mark.parametrize(
    "consumer",
    [
        'import { useNavigate as acquire } from "react-router-dom"; void acquire;\n',
        'import * as Router from "react-router-dom"; void Router;\n',
        'export { Link as ProductLink } from "react-router-dom";\n',
        'const Router = require("react-router-dom"); void Router;\n',
        'const moduleName = "react-router-dom"; const Router = require(moduleName); void Router;\n',
        'void import("react-router-dom");\n',
        "const go = window.open; class Holder { go = window.open; } void go; void Holder;\n",
        "const go = await window.open; void go;\n",
        "const { open: go = () => undefined } = window; void go;\n",
        'const loc = window["location"]; void loc;\n',
        'const form = document.createElement("form"); form.requestSubmit();\n',
        'const tag = "a"; const node = document.createElement(tag); node.setAttribute("href", "/labs");\n',
        'const node = document.createElement("div"); node.setAttribute(name, "/labs");\n',
        'const node = document.createElement("div"); node.innerHTML = `<a href="/labs">x</a>`;\n',
        'document.body.insertAdjacentHTML("beforeend", `<form action="/labs"></form>`);\n',
        'document.write(`<meta http-equiv="refresh" content="0;/labs">`);\n',
        'eval(`location.href="/labs"`);\n',
        'const modules = import.meta.glob("./*.tsx"); void modules;\n',
        'export const Consumer = () => <a href="/" {...{href: "/labs"}}>x</a>;\n',
        "export const Consumer = () => <form {...props} onSubmit={() => undefined} />;\n",
        'export const Consumer = () => <iframe src="/labs" />;\n',
        'export const Consumer = () => <object data="/labs" />;\n',
        'export const Consumer = () => <embed src="/labs" />;\n',
        'export const Consumer = () => <base href="/labs" />;\n',
        'export const Consumer = () => <meta httpEquiv="refresh" content="0;/labs" />;\n',
        'import React from "react"; export const Consumer = () => React.createElement("a", {href: "/labs"});\n',
        'import React from "react"; const link = <a />; export const Consumer = () => React.cloneElement(link, {href: "/labs"});\n',
    ],
)
def test_finite_navigation_boundary_denies_acquisition_and_activation(tmp_path, consumer) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _finite_boundary_fixture(tmp_path, consumer)


def test_finite_navigation_boundary_preserves_non_navigation_false_positive_controls(
    tmp_path,
) -> None:
    _finite_boundary_fixture(
        tmp_path,
        """
import React from "react";
import { Outlet, useLocation, useParams, useSearchParams } from "react-router-dom";
const service = { open: (_target: string) => undefined };
const model = { location: { assign: (_target: string) => undefined } };
const record = { innerHTML: "data" };
const Widget = (_props: {href?: string; to?: string; action?: string}) => <div />;
service.open("/labs"); model.location.assign("/labs"); record.innerHTML = "safe";
window.setTimeout(() => undefined, 1); document.addEventListener("ready", () => undefined);
export const Consumer = () => <><Outlet /><form onSubmit={() => undefined}><button>Save</button><input /></form><Widget {...{href: "/labs", to: "/labs", action: "/labs"}} />{React.createElement("div", {title: "/labs"})}{String(useLocation())}{String(useParams())}{String(useSearchParams())}</>;
""",
    )


@pytest.mark.parametrize(
    "app_mutation",
    [
        lambda source: source.replace('path="/act"', 'path={"/act"}'),
        lambda source: source.replace('path="/act"', '{...{path: "/labs"}}'),
        lambda source: source.replace('path="/act"', 'path="/labs"'),
        lambda source: source.replace(
            "return <BrowserRouter>",
            'React.createElement(Route, {path: "/labs", element: <div />}); return <BrowserRouter>',
        ).replace(
            'import { BrowserRouter, Route, Routes } from "react-router-dom";',
            'import React from "react"; import { BrowserRouter, Route, Routes } from "react-router-dom";',
        ),
        lambda source: source.replace(
            "import { BrowserRouter, Route, Routes }",
            "import { BrowserRouter, Route as ProductRoute, Routes }",
        ),
    ],
)
def test_finite_navigation_boundary_rejects_route_declaration_bypasses(
    tmp_path, app_mutation
) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _finite_boundary_fixture(tmp_path, app=app_mutation(_FINITE_APP))


@pytest.mark.parametrize(
    "boundary_mutation",
    [
        lambda source: source + "\nexport const rawNavigate = useNavigate;\n",
        lambda source: source.replace(
            "<NavLink {...props} to={coreHref(to)} />",
            "<NavLink to={coreHref(to)} {...props} />",
        ),
        lambda source: source.replace(
            '<a {...props} href={safe} target="_blank" rel="noopener noreferrer" />',
            '<a href={safe} {...props} target="_blank" rel="noopener noreferrer" />',
        ),
        lambda source: source.replace("navigate(coreHref(candidate));", "navigate(candidate);"),
    ],
)
def test_finite_navigation_boundary_rejects_raw_exports_and_override_order(
    tmp_path, boundary_mutation
) -> None:
    with pytest.raises(RuntimeError, match="failed closed"):
        _finite_boundary_fixture(tmp_path, boundary_mutation=boundary_mutation)

from __future__ import annotations

import copy
import json

import pytest

from scripts import check_core_surface


def _manifest() -> dict[str, object]:
    return json.loads(check_core_surface.MANIFEST.read_text(encoding="utf-8"))


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

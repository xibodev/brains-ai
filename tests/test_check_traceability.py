"""Deliberate-failure regression fixtures for the generated traceability gate.

``scripts/check_traceability.py`` derives every surface inventory from source.
These tests prove each derivation actually fails when the tree drifts, rather
than passing because a token happened to appear somewhere: every case mutates
real inputs (a copied repository fixture or an explicit in-memory inventory) and
asserts the specific error the checker must produce.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_traceability.py"
_SPEC = importlib.util.spec_from_file_location("check_traceability", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
checker = importlib.util.module_from_spec(_SPEC)
sys.modules["check_traceability"] = checker
_SPEC.loader.exec_module(checker)

#: Everything ``check_repository`` reads from the tree. The migration registry
#: and the SQLAlchemy metadata come from the installed package, so the copied
#: baseline and migration files keep those checks meaningful.
FIXTURE_PATHS = (
    "docs/product/PRODUCT_BRIEF.md",
    "docs/QUALITY_GATES.md",
    "README.md",
    "scripts/check_traceability.py",
    "frontend/src",
    "src/brains/storage/baseline",
    "src/brains/storage/sql_migrations",
    "tests/e2e/specs",
    "tests/test_acceptance_brains.py",
)


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    for relative in FIXTURE_PATHS:
        source = ROOT / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    return root


def _errors(root: Path) -> list[str]:
    return checker.check_repository(root)


def _edit(root: Path, relative: str, old: str, new: str) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture drift: {old!r} absent from {relative}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _assert_reports(errors: list[str], fragment: str) -> None:
    assert any(fragment in error for error in errors), (fragment, errors)


# --------------------------------------------------------------------------
# clean run
# --------------------------------------------------------------------------


def test_repository_generated_traceability_contract() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(ROOT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "generated traceability contract passed" in result.stdout


def test_untouched_fixture_tree_is_clean(tmp_path: Path) -> None:
    assert _errors(_fixture_root(tmp_path)) == []


# --------------------------------------------------------------------------
# SPA routes
# --------------------------------------------------------------------------


def test_duplicate_spa_route_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route path="/command-center" element={<CommandCenter />} />',
        '<Route path="/command-center" element={<CommandCenter />} />\n'
        '                  <Route path="/command-center" element={<CommandCenter />} />',
    )
    _assert_reports(_errors(root), "duplicate declared route /app/command-center")


def test_route_parameter_no_component_reads_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route path="/workspaces/:slug" element={<Workspaces />} />',
        '<Route path="/workspaces/:slug/:tab" element={<Workspaces />} />',
    )
    _assert_reports(
        _errors(root),
        "route /app/workspaces/:slug/:tab declares :tab, which Workspaces never reads",
    )


def test_redirect_to_an_undeclared_route_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route path="/command-center" element={<CommandCenter />} />',
        '<Route path="/command-center" element={<Navigate to="/dashboard" replace />} />',
    )
    _assert_reports(_errors(root), "redirects to /app/dashboard, which is not declared")


def test_route_component_that_is_not_imported_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route path="/command-center" element={<CommandCenter />} />',
        '<Route path="/command-center" element={<Mailbox />} />',
    )
    _assert_reports(_errors(root), "names unimported component Mailbox")


def test_route_component_module_that_does_not_exist_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        'import { CommandCenter } from "./screens/CommandCenter";',
        'import { CommandCenter } from "./screens/CommandCenterScreen";',
    )
    _assert_reports(
        _errors(root),
        "component CommandCenter for route /app/command-center resolves to no module",
    )


def test_route_allowlist_drift_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A route in the allowlist that App.tsx no longer declares must be reported."""

    root = _fixture_root(tmp_path)
    monkeypatch.setattr(
        checker,
        "REQUIRED_SPA_ROUTES",
        (*checker.REQUIRED_SPA_ROUTES, "/app/retired"),
    )
    _assert_reports(
        _errors(root),
        "check_docs REQUIRED_SPA_ROUTES lists /app/retired, which is not declared",
    )


# --------------------------------------------------------------------------
# API client vs server
# --------------------------------------------------------------------------


def test_client_call_without_a_server_route_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/api/client.ts",
        'readiness: () => request<ReadinessReport>("/admin/readiness")',
        'listReports: () => request<unknown>("/reports"),\n'
        '  readiness: () => request<ReadinessReport>("/admin/readiness")',
    )
    _assert_reports(_errors(root), "client: GET /v1/reports has no mounted server route")


def test_client_call_with_a_non_literal_path_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/api/client.ts",
        'readiness: () => request<ReadinessReport>("/admin/readiness")',
        "listDynamic: (target: string) => request<unknown>(target),\n"
        '  readiness: () => request<ReadinessReport>("/admin/readiness")',
    )
    _assert_reports(
        _errors(root),
        "does not use a literal path, so no server route can be matched",
    )


def test_request_definition_is_not_read_as_a_call(tmp_path: Path) -> None:
    inventory = checker.collect_client_calls(_fixture_root(tmp_path))
    assert inventory.unmatchable == ()
    assert len(inventory.calls) > 40


def test_client_call_with_the_wrong_method_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/api/client.ts",
        'readiness: () => request<ReadinessReport>("/admin/readiness")',
        'readiness: () =>\n    request<ReadinessReport>("/admin/readiness", { method: "PUT" })',
    )
    _assert_reports(_errors(root), "client: PUT /v1/admin/readiness has no mounted server route")


def test_client_paths_match_parameterised_server_routes() -> None:
    inventory = checker.ClientCallInventory(
        calls=(checker.ClientCall(method="GET", path="/v1/orgs/{}/members"),),
        unmatchable=(),
    )
    routes = (checker.ServerRoute(method="GET", path="/v1/orgs/{org}/members"),)
    assert checker.check_client_server(inventory, routes) == []


def test_client_query_string_helper_is_not_part_of_the_path(tmp_path: Path) -> None:
    inventory = checker.collect_client_calls(_fixture_root(tmp_path))
    assert checker.ClientCall(method="GET", path="/v1/operator/mailboxes/inbox") in inventory.calls
    assert not any("qs(" in call.path for call in inventory.calls)


def test_stale_client_allowlist_entry_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "CLIENT_CALL_ALLOWLIST", {("GET", "/v1/ghost"): "gone"})
    errors = checker.check_client_server(checker.ClientCallInventory(calls=(), unmatchable=()), ())
    _assert_reports(errors, "CLIENT_CALL_ALLOWLIST entry GET /v1/ghost matched no call")


# --------------------------------------------------------------------------
# server families
# --------------------------------------------------------------------------


def test_copilot_alias_to_an_unmounted_route_fails() -> None:
    routes = (checker.ServerRoute(method="POST", path="/v1/chat/completions"),)
    _assert_reports(
        checker.check_copilot_aliases({"/chat": "/v1/chat"}, routes),
        "Copilot alias /chat rewrites to unmounted /v1/chat",
    )


# --------------------------------------------------------------------------
# entities and migrations
# --------------------------------------------------------------------------


def test_model_table_without_a_migration_fails() -> None:
    _assert_reports(
        checker.check_entities(frozenset({"orgs", "ghosts"}), {"sqlite": frozenset({"orgs"})}, {}),
        "model table ghosts is created by no baseline or migration",
    )


def test_orphan_provisioned_table_fails() -> None:
    _assert_reports(
        checker.check_entities(frozenset({"orgs"}), {"sqlite": frozenset({"orgs", "legacy"})}, {}),
        "table legacy is provisioned but has no SQLAlchemy model",
    )


def test_backend_baseline_asymmetry_fails() -> None:
    _assert_reports(
        checker.check_entities(
            frozenset({"orgs", "events"}),
            {"postgresql": frozenset({"orgs", "events"}), "sqlite": frozenset({"orgs"})},
            {},
        ),
        "baseline table events exists for one backend only",
    )


def test_dropped_baseline_table_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    path = root / "src/brains/storage/baseline/sqlite.sql"
    text = path.read_text(encoding="utf-8")
    start = text.index("CREATE TABLE IF NOT EXISTS operators")
    end = text.index(");", start) + 2
    path.write_text(text[:start] + text[end:], encoding="utf-8")
    errors = _errors(root)
    _assert_reports(errors, "baseline table operators exists for one backend only")


def test_create_table_mentions_in_prose_are_not_tables() -> None:
    text = "Idempotent: ``CREATE TABLE IF NOT EXISTS`` plus a sentinel.\nCREATE TABLE x (a int);"
    assert checker.collect_sql_tables(text) == frozenset({"x"})


def test_migration_file_missing_from_the_corpus_fails() -> None:
    _assert_reports(
        checker.check_migrations(("100_a",), ("100_a", "140_new"), ()),
        "140_new exists on disk but is absent from the corpus",
    )


def test_corpus_migration_without_a_file_fails() -> None:
    _assert_reports(
        checker.check_migrations(("100_a", "140_new"), ("100_a",), ()),
        "corpus ID 140_new has no file on disk",
    )


def test_new_migration_file_fails_the_repository_check(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "src/brains/storage/sql_migrations/140_orphan.sql").write_text(
        "CREATE TABLE IF NOT EXISTS orphan_rows (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    errors = _errors(root)
    _assert_reports(errors, "table orphan_rows is provisioned but has no SQLAlchemy model")


def test_missing_source_is_reported_not_skipped(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "frontend/src/App.tsx").unlink()
    with pytest.raises(checker.TraceabilityInputError):
        _errors(root)

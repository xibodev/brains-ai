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
    "docs/product/TRACEABILITY.md",
    "docs/product/FEATURE_CONTRACT.md",
    "docs/product/BACKLOG.md",
    "docs/product/USER_OUTCOME_SPEC.md",
    "docs/product/PERSONAS_AND_JOURNEYS.md",
    "docs/product/PRODUCT_BRIEF.md",
    "docs/QUALITY_GATES.md",
    "README.md",
    "scripts/check_docs.py",
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


def test_new_spa_route_without_documentation_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route path="/inbox" element={<Inbox />} />',
        '<Route path="/inbox" element={<Inbox />} />\n'
        '                <Route path="/reports" element={<Inbox />} />',
    )
    errors = _errors(root)
    _assert_reports(
        errors, "declared route /app/reports is missing from docs/product/TRACEABILITY.md"
    )
    _assert_reports(errors, "declared route /app/reports is missing from check_docs")


def test_documented_spa_route_that_no_longer_exists_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route path="/automation" element={<Automation />} />',
        "",
    )
    errors = _errors(root)
    _assert_reports(errors, "documents route /app/automation, which is not declared")


def test_duplicate_spa_route_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route path="/inbox" element={<Inbox />} />',
        '<Route path="/inbox" element={<Inbox />} />\n'
        '                <Route path="/inbox" element={<Inbox />} />',
    )
    _assert_reports(_errors(root), "duplicate declared route /app/inbox")


def test_route_parameter_no_component_reads_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route path="/automation" element={<Automation />} />',
        '<Route path="/automation/:tab" element={<Automation />} />',
    )
    _assert_reports(
        _errors(root),
        "route /app/automation/:tab declares :tab, which Automation never reads",
    )


def test_route_parameter_that_became_consumed_must_leave_the_allowlist(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/screens/Personas.tsx",
        "export function Personas()",
        "function _useSlug() {\n"
        "  const { slug } = useParams();\n"
        "  return slug;\n"
        "}\n\nexport function Personas()",
    )
    _assert_reports(
        _errors(root),
        "route /app/personas/:slug now consumes :slug; remove it from UNCONSUMED_ROUTE_PARAMS",
    )


def test_unconsumed_parameter_without_a_documented_gap_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "docs/product/TRACEABILITY.md",
        "| `/app/sessions/:id` | `Sessions` | F3, J7 | `:id` is not consumed to select the Session. |",
        "| `/app/sessions/:id` | `Sessions` | F3, J7 | None. |",
    )
    _assert_reports(
        _errors(root),
        "route /app/sessions/:id has an unconsumed :id that docs/product/TRACEABILITY.md "
        "does not record as a gap",
    )


def test_redirect_to_an_undeclared_route_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route index element={<Navigate to="/inbox" replace />} />',
        '<Route index element={<Navigate to="/dashboard" replace />} />',
    )
    _assert_reports(_errors(root), "redirects to /app/dashboard, which is not declared")


def test_route_component_that_is_not_imported_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        '<Route path="/inbox" element={<Inbox />} />',
        '<Route path="/inbox" element={<Mailbox />} />',
    )
    _assert_reports(_errors(root), "names unimported component Mailbox")


def test_route_component_module_that_does_not_exist_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/App.tsx",
        'import { Inbox } from "./screens/Inbox";',
        'import { Inbox } from "./screens/InboxScreen";',
    )
    _assert_reports(_errors(root), "component Inbox for route /app/inbox resolves to no module")


def test_check_docs_route_list_drift_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(root, "scripts/check_docs.py", '    "/app/automation",\n', "")
    _assert_reports(
        _errors(root),
        "declared route /app/automation is missing from check_docs REQUIRED_SPA_ROUTES",
    )


def test_stale_unconsumed_parameter_allowlist_entry_fails() -> None:
    routes = (checker.SpaRoute(path="/app/inbox", component="Inbox", redirect_to=None),)
    errors = checker.check_spa_routes(
        routes,
        {"/app/inbox": "| `/app/inbox` | `Inbox` |"},
        ("/app/inbox",),
        {"Inbox": "./screens/Inbox"},
        {},
    )
    _assert_reports(errors, "UNCONSUMED_ROUTE_PARAMS lists undeclared route /app/sessions/:id")


# --------------------------------------------------------------------------
# API client vs server
# --------------------------------------------------------------------------


def test_client_call_without_a_server_route_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/api/client.ts",
        'listOrgs: () => request<unknown>("/orgs")',
        'listReports: () => request<unknown>("/reports"),\n'
        '  listOrgs: () => request<unknown>("/orgs")',
    )
    _assert_reports(_errors(root), "client: GET /v1/reports has no mounted server route")


def test_client_call_with_a_non_literal_path_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "frontend/src/api/client.ts",
        'listOrgs: () => request<unknown>("/orgs")',
        "listDynamic: (target: string) => request<unknown>(target),\n"
        '  listOrgs: () => request<unknown>("/orgs")',
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
        "getOrg: (org: string | number) => request<Org>(`/orgs/${org}`)",
        'getOrg: (org: string | number) =>\n    request<Org>(`/orgs/${org}`, { method: "PUT" })',
    )
    _assert_reports(_errors(root), "client: PUT /v1/orgs/{} has no mounted server route")


def test_client_paths_match_parameterised_server_routes() -> None:
    inventory = checker.ClientCallInventory(
        calls=(checker.ClientCall(method="GET", path="/v1/orgs/{}/members"),),
        unmatchable=(),
    )
    routes = (checker.ServerRoute(method="GET", path="/v1/orgs/{org}/members"),)
    assert checker.check_client_server(inventory, routes) == []


def test_client_query_string_helper_is_not_part_of_the_path(tmp_path: Path) -> None:
    inventory = checker.collect_client_calls(_fixture_root(tmp_path))
    assert checker.ClientCall(method="GET", path="/v1/issues") in inventory.calls
    assert not any("qs(" in call.path for call in inventory.calls)


def test_stale_client_allowlist_entry_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "CLIENT_CALL_ALLOWLIST", {("GET", "/v1/ghost"): "gone"})
    errors = checker.check_client_server(checker.ClientCallInventory(calls=(), unmatchable=()), ())
    _assert_reports(errors, "CLIENT_CALL_ALLOWLIST entry GET /v1/ghost matched no call")


# --------------------------------------------------------------------------
# server families
# --------------------------------------------------------------------------


def test_undocumented_server_surface_fails() -> None:
    routes = (checker.ServerRoute(method="GET", path="/v2/experiments"),)
    _assert_reports(
        checker.check_server_families(routes, ("Health",)),
        "GET /v2/experiments belongs to no documented family",
    )


def test_documented_family_without_routes_fails() -> None:
    routes = (checker.ServerRoute(method="GET", path="/health"),)
    _assert_reports(
        checker.check_server_families(routes, ("Health", "Relay")),
        "documented family 'Relay' matches no mounted route",
    )


def test_family_missing_from_the_document_fails() -> None:
    routes = (checker.ServerRoute(method="POST", path="/relay/reply"),)
    _assert_reports(
        checker.check_server_families(routes, ("Health",)),
        "maps to family 'Relay', which docs/product/TRACEABILITY.md does not list",
    )


def test_removed_family_row_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "docs/product/TRACEABILITY.md",
        "| Relay | `POST /relay/reply`, `/relay/triage` | relay bearer or 503 when unset | B7 |\n",
        "",
    )
    _assert_reports(_errors(root), "maps to family 'Relay', which docs/product/TRACEABILITY.md")


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
        checker.check_migrations(("100_a",), ("100_a", "140_new"), (), frozenset({"100", "140"})),
        "140_new exists on disk but is absent from the corpus",
    )


def test_corpus_migration_without_a_file_fails() -> None:
    _assert_reports(
        checker.check_migrations(("100_a", "140_new"), ("100_a",), (), frozenset({"100", "140"})),
        "corpus ID 140_new has no file on disk",
    )


def test_undocumented_migration_fails() -> None:
    _assert_reports(
        checker.check_migrations(
            ("100_a", "140_new"), ("100_a", "140_new"), (), frozenset({"100"})
        ),
        "140_new is not recorded in the docs/product/TRACEABILITY.md data and migration mapping",
    )


def test_documented_migration_that_does_not_exist_fails() -> None:
    _assert_reports(
        checker.check_migrations(("100_a",), ("100_a",), (), frozenset({"100", "199"})),
        "records migration 199, which does not exist",
    )


def test_new_migration_file_fails_the_repository_check(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "src/brains/storage/sql_migrations/140_orphan.sql").write_text(
        "CREATE TABLE IF NOT EXISTS orphan_rows (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    errors = _errors(root)
    _assert_reports(errors, "table orphan_rows is provisioned but has no SQLAlchemy model")


def test_migration_document_drift_fails_the_repository_check(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(root, "docs/product/TRACEABILITY.md", "| 132 |", "| 199 |")
    errors = _errors(root)
    _assert_reports(errors, "132_realtime_events is not recorded")
    _assert_reports(errors, "records migration 199, which does not exist")


# --------------------------------------------------------------------------
# stable ID markers
# --------------------------------------------------------------------------


def test_unreferenced_journey_spec_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    source = root / "tests/e2e/specs/j07-sessions.spec.ts"
    (source.parent / "j05-projects.spec.ts").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    _assert_reports(_errors(root), "journey spec j05-projects.spec.ts is not recorded for J5")


def test_documented_journey_spec_that_is_absent_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "tests/e2e/specs/j04-pods.spec.ts").unlink()
    _assert_reports(_errors(root), "names j04-pods.spec.ts for J4, which is absent")


def test_spec_without_a_journey_id_fails() -> None:
    _assert_reports(
        checker.check_test_markers(
            {"?": ("smoke.spec.ts",)},
            {journey: () for journey in checker.REQUIRED_JOURNEY_IDS},
            {},
            frozenset(),
            [],
            frozenset(),
        ),
        "journey spec smoke.spec.ts does not encode a jNN journey ID",
    )


def test_acceptance_test_for_an_unknown_feature_fails() -> None:
    _assert_reports(
        checker.check_test_markers(
            {},
            {journey: () for journey in checker.REQUIRED_JOURNEY_IDS},
            {"F42": 1},
            frozenset(),
            [],
            frozenset(),
        ),
        "acceptance tests name unknown feature F42",
    )


def test_feature_losing_acceptance_coverage_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    path = root / "tests/test_acceptance_brains.py"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("def test_f5_", "def test_pods_"), encoding="utf-8")
    _assert_reports(_errors(root), "F5 has no acceptance test and no declared gap")


def test_closed_acceptance_gap_must_leave_the_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fixture_root(tmp_path)
    monkeypatch.setattr(
        checker,
        "ACCEPTANCE_COVERAGE_GAPS",
        {"F6": "stale test-only gap"},
    )
    _assert_reports(
        _errors(root),
        "F6 now has acceptance tests; remove it from ACCEPTANCE_COVERAGE_GAPS",
    )


def test_reference_to_an_undeclared_acceptance_criterion_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(root, "docs/product/TRACEABILITY.md", "AC-F5-01..04", "AC-F5-01..09")
    _assert_reports(
        _errors(root),
        "AC-F5-05 is referenced but not declared in docs/product/FEATURE_CONTRACT.md",
    )


def test_declared_acceptance_criterion_nobody_references_fails(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "docs/product/FEATURE_CONTRACT.md",
        "- AC-B7-04:",
        "- AC-B7-09: fabricated criterion.\n- AC-B7-04:",
    )
    _assert_reports(
        _errors(root),
        "AC-B7-09 is declared but referenced by no canonical document",
    )


def test_duplicate_acceptance_declaration_fails() -> None:
    _assert_reports(
        checker.check_test_markers(
            {},
            {journey: () for journey in checker.REQUIRED_JOURNEY_IDS},
            dict.fromkeys(checker.REQUIRED_FEATURE_IDS, 1),
            frozenset({"AC-F0-01"}),
            ["AC-F0-01", "AC-F0-01"],
            frozenset({"AC-F0-01"}),
        ),
        "duplicate acceptance criterion AC-F0-01",
    )


def test_acceptance_range_expansion() -> None:
    assert checker.expand_ac_references("AC-F0-01..03 and AC-B9-02") == frozenset(
        {"AC-F0-01", "AC-F0-02", "AC-F0-03", "AC-B9-02"}
    )


# --------------------------------------------------------------------------
# input integrity
# --------------------------------------------------------------------------


def test_missing_source_is_reported_not_skipped(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "frontend/src/App.tsx").unlink()
    with pytest.raises(checker.TraceabilityInputError):
        _errors(root)


def test_missing_traceability_section_is_reported(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _edit(
        root,
        "docs/product/TRACEABILITY.md",
        "## Modern SPA route inventory",
        "## Modern SPA routes",
    )
    with pytest.raises(checker.TraceabilityInputError):
        _errors(root)

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_docs.py"
SPEC = importlib.util.spec_from_file_location("check_docs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check_docs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_docs)
HEADER = """<!--
last_verified: 2026-08-01T08:04:38.943-06:00
verified_by: test
verification_basis: HEAD 1111111111111111111111111111111111111111; test fixture; deployment not verified
-->
"""


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _valid_tree(root: Path) -> None:
    for relative in check_docs.CANONICAL_DOCS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER, encoding="utf-8")

    links = "\n".join(f"- [doc]({path})" for path in check_docs.CANONICAL_DOCS[1:])
    (root / "README.md").write_text(HEADER + links, encoding="utf-8")

    ids = " ".join(check_docs.REQUIRED_IDS)
    acs = " ".join(prefix + "01" for prefix in check_docs.REQUIRED_AC_PREFIXES)
    routes = "\n".join(f"`{route}`" for route in check_docs.REQUIRED_SPA_ROUTES)
    (root / "docs/product/TRACEABILITY.md").write_text(
        HEADER + ids + "\n" + acs + "\n" + routes,
        encoding="utf-8",
    )
    outcome_rows = [
        "| ID | User promise | Minimal path | Code contract | Expected evidence contract | Current gap ownership | Acceptance anchors |",
        "|---|---|---|---|---|---|---|",
    ]
    for stable_id in check_docs.REQUIRED_OUTCOME_IDS:
        outcome_rows.append(
            f"| {stable_id} | promise | path | code | evidence | BL-P0-01 | AC-{stable_id}-01; J1 |"
        )
    outcomes = "\n".join(
        f"### {outcome_id} - fixture" for outcome_id in check_docs.REQUIRED_END_TO_END_OUTCOMES
    )
    outcome_markers = "\n".join(check_docs.REQUIRED_OUTCOME_MARKERS)
    (root / "docs/product/USER_OUTCOME_SPEC.md").write_text(
        HEADER + "\n".join(outcome_rows) + "\n" + outcomes + "\n" + outcome_markers,
        encoding="utf-8",
    )
    (root / "docs/product/BACKLOG.md").write_text(
        HEADER + "### BL-P0-01 - fixture\n\n- **Maps to:** F0-F10, B1-B9.\n",
        encoding="utf-8",
    )


def test_repository_documentation_contract() -> None:
    result = _run(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_checker_rejects_required_contract_failures(tmp_path: Path) -> None:
    case = tmp_path / "missing"
    _valid_tree(case)
    (case / check_docs.CANONICAL_DOCS[1]).unlink()
    assert "missing canonical document" in _run(case).stdout

    case = tmp_path / "freshness"
    _valid_tree(case)
    (case / "docs/ARCHITECTURE.md").write_text("# no header\n", encoding="utf-8")
    assert "missing HTML freshness header" in _run(case).stdout

    case = tmp_path / "readme"
    _valid_tree(case)
    readme = case / "README.md"
    readme.write_text(HEADER, encoding="utf-8")
    assert "missing canonical link" in _run(case).stdout

    case = tmp_path / "history"
    _valid_tree(case)
    (case / "CHANGELOG.md").write_text("# history\n", encoding="utf-8")
    assert "prohibited documentation/evidence path" in _run(case).stdout

    case = tmp_path / "traceability"
    _valid_tree(case)
    trace = case / "docs/product/TRACEABILITY.md"
    trace.write_text(
        trace.read_text(encoding="utf-8").replace("AC-B9-", "AC-BX-").replace("B9", "BX"),
        encoding="utf-8",
    )
    output = _run(case).stdout
    assert "missing stable ID B9" in output
    assert "missing acceptance ID prefix AC-B9-" in output

    case = tmp_path / "outcome-spec"
    _valid_tree(case)
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    lines = outcome.read_text(encoding="utf-8").splitlines()
    outcome.write_text(
        "\n".join(line for line in lines if not line.startswith("| B9 |")),
        encoding="utf-8",
    )
    assert "missing outcome row B9" in _run(case).stdout

    case = tmp_path / "duplicate-outcome"
    _valid_tree(case)
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8")
        + "\n| B9 | promise | path | code | evidence | BL-P0-01 | AC-B9-01; J1 |\n",
        encoding="utf-8",
    )
    assert "duplicate outcome row B9" in _run(case).stdout

    case = tmp_path / "bad-outcome-owner"
    _valid_tree(case)
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8").replace("BL-P0-01", "BL-P9-99", 1),
        encoding="utf-8",
    )
    assert "references unknown backlog ID BL-P9-99" in _run(case).stdout

    case = tmp_path / "mismatched-outcome-owner"
    _valid_tree(case)
    backlog = case / "docs/product/BACKLOG.md"
    backlog.write_text(
        backlog.read_text(encoding="utf-8").replace("F0-F10, B1-B9", "F1-F10, B1-B9"),
        encoding="utf-8",
    )
    assert "backlog ID BL-P0-01 does not map to F0" in _run(case).stdout

    case = tmp_path / "bad-outcome-anchor"
    _valid_tree(case)
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8").replace("AC-F0-01", "AC-FX-01", 1),
        encoding="utf-8",
    )
    assert "outcome row F0 missing acceptance anchor AC-F0-" in _run(case).stdout

    case = tmp_path / "empty-outcome-cell"
    _valid_tree(case)
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8").replace(
            "| F0 | promise | path | code | evidence |",
            "| F0 | promise | path |  | evidence |",
        ),
        encoding="utf-8",
    )
    assert "outcome row F0 has an empty contract cell" in _run(case).stdout

    case = tmp_path / "missing-end-to-end"
    _valid_tree(case)
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8").replace("### O7 - fixture", ""),
        encoding="utf-8",
    )
    assert "missing end-to-end outcome O7" in _run(case).stdout


def test_checker_requires_feature_backlogs_and_their_freshness(tmp_path: Path) -> None:
    case = tmp_path / "feature-backlogs"
    _valid_tree(case)
    active = case / "docs/product/ACTIVE_BACKLOG.md"
    active.unlink()
    assert "missing canonical document: docs/product/ACTIVE_BACKLOG.md" in _run(case).stdout

    _valid_tree(case)
    experimental = case / "docs/product/EXPERIMENTAL_BACKLOG.md"
    experimental.write_text("# missing freshness\n", encoding="utf-8")
    assert (
        "docs/product/EXPERIMENTAL_BACKLOG.md: missing HTML freshness header"
        in _run(case).stdout
    )


def test_checker_accepts_normalized_and_reference_style_readme_links(tmp_path: Path) -> None:
    case = tmp_path / "links"
    _valid_tree(case)
    definitions = []
    links = []
    for index, relative in enumerate(check_docs.CANONICAL_DOCS[1:], start=1):
        label = f"doc-{index}"
        links.append(f"- [canonical][{label}]")
        definitions.append(f"[{label}]: <{relative}#section>")
    (case / "README.md").write_text(
        HEADER + "\n".join(links + definitions),
        encoding="utf-8",
    )
    assert _run(case).returncode == 0


def test_checker_rejects_missing_document_references_in_source(tmp_path: Path) -> None:
    case = tmp_path / "source-reference"
    _valid_tree(case)
    source = case / "src/example.py"
    source.parent.mkdir(parents=True)
    source.write_text('"""See docs/removed-guide.md."""\n', encoding="utf-8")
    output = _run(case).stdout
    assert "reference to missing documentation: docs/removed-guide.md" in output


def test_checker_supports_brains_example_and_rejects_legacy_identity(tmp_path: Path) -> None:
    case = tmp_path / "identity"
    _valid_tree(case)
    example = case / "examples/brains.skill.md"
    example.parent.mkdir(parents=True)
    example.write_text(HEADER + "# Brains workflow\n", encoding="utf-8")
    assert _run(case).returncode == 0

    forbidden = "".join(("gar", "rison"))
    source = case / "src/identity.ts"
    source.parent.mkdir(parents=True)
    source.write_text(f'export const product = "{forbidden.title()}";\n', encoding="utf-8")
    assert "forbidden legacy product identity text" in _run(case).stdout


def test_checker_rejects_legacy_identity_paths(tmp_path: Path) -> None:
    forbidden = "".join(("gar", "rison"))

    case = tmp_path / "old-example"
    _valid_tree(case)
    old_example = case / "examples" / f"{forbidden}.skill.md"
    old_example.parent.mkdir(parents=True)
    old_example.write_text(HEADER, encoding="utf-8")
    assert "prohibited documentation/evidence path" in _run(case).stdout

    case = tmp_path / "old-acceptance"
    _valid_tree(case)
    old_test = case / "tests" / f"test_acceptance_{forbidden}.py"
    old_test.parent.mkdir(parents=True)
    old_test.write_text("# legacy identity path\n", encoding="utf-8")
    assert "prohibited documentation/evidence path" in _run(case).stdout

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
HEADER = ""


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

    for relative in check_docs.REQUIRED_SUPPORTING_DOCS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Supporting\n", encoding="utf-8")

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
        "| ID | User promise | Minimal path | Code contract | Expected evidence contract | Core backlog items | Acceptance anchors |",
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
        "### BL-P0-01 - Implement fixture behavior\n\n"
        + "- **Action:** Implement the fixture behavior.\n"
        + "- **Done when:** The fixture behavior is verified.\n"
        + "- **Maps to:** F0-F10, B1-B9.\n",
        encoding="utf-8",
    )
    (root / "docs/product/FROZEN_BACKLOG.md").write_text(
        "# Frozen backlog\n\n"
        + "Items may move only after `BACKLOG.md` is empty and a human explicitly "
        + "approves thawing it.\n",
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

    case = tmp_path / "supporting"
    _valid_tree(case)
    for relative in check_docs.REQUIRED_SUPPORTING_DOCS:
        path = case / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Supporting\n", encoding="utf-8")
    (case / check_docs.REQUIRED_SUPPORTING_DOCS[0]).unlink()
    assert "missing required supporting document" in _run(case).stdout

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

    case = tmp_path / "backlog-range-shorthand"
    _valid_tree(case)
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8").replace("BL-P0-01", "BL-P0-01..03", 1),
        encoding="utf-8",
    )
    assert (
        "backlog range shorthand BL-P0-01..03 is not allowed; list exact backlog IDs"
        in _run(case).stdout
    )

    case = tmp_path / "mismatched-outcome-owner"
    _valid_tree(case)
    backlog = case / "docs/product/BACKLOG.md"
    backlog.write_text(
        backlog.read_text(encoding="utf-8").replace("F0-F10, B1-B9", "F1-F10, B1-B9"),
        encoding="utf-8",
    )
    assert "backlog ID BL-P0-01 does not map to F0" in _run(case).stdout

    case = tmp_path / "completed-outcome"
    _valid_tree(case)
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8").replace(
            "| F0 | promise | path | code | evidence | BL-P0-01 |",
            "| F0 | promise | path | code | evidence | — |",
        ),
        encoding="utf-8",
    )
    assert _run(case).returncode == 0

    case = tmp_path / "orphan-backlog-item"
    _valid_tree(case)
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8").replace("BL-P0-01", "—"),
        encoding="utf-8",
    )
    assert (
        "backlog ID BL-P0-01 is not referenced by any Core backlog items cell" in _run(case).stdout
    )

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


def test_checker_rejects_retired_backlog_documents(tmp_path: Path) -> None:
    for name in ("ACTIVE_BACKLOG.md", "EXPERIMENTAL_BACKLOG.md"):
        case = tmp_path / name.lower()
        _valid_tree(case)
        retired = case / "docs/product" / name
        retired.write_text(HEADER, encoding="utf-8")
        assert f"prohibited documentation/evidence path: docs/product/{name}" in _run(case).stdout


def test_checker_rejects_non_actionable_backlog_items(tmp_path: Path) -> None:
    mutations = (
        (
            "history-section",
            "### BL-P0-01 - Implement fixture behavior",
            "## Implemented foundation\n\nCompleted narrative.\n\n"
            "### BL-P0-01 - Implement fixture behavior",
            "unexpected non-actionable section",
        ),
        (
            "heading",
            "### BL-P0-01 - Implement fixture behavior",
            "### BL-P0-01 - Fixture behavior",
            "heading must start with an actionable verb",
        ),
        (
            "missing-action",
            "- **Action:** Implement the fixture behavior.\n",
            "",
            "must contain exactly one **Action:** line",
        ),
        (
            "duplicate-action",
            "- **Action:** Implement the fixture behavior.\n",
            "- **Action:** Implement the fixture behavior.\n- **Action:** Implement it again.\n",
            "must contain exactly one **Action:** line",
        ),
        (
            "missing-done",
            "- **Done when:** The fixture behavior is verified.\n",
            "",
            "must contain exactly one **Done when:** line",
        ),
        (
            "missing-maps",
            "- **Maps to:** F0-F10, B1-B9.\n",
            "",
            "must contain exactly one **Maps to:** line",
        ),
    )
    for name, old, new, expected in mutations:
        case = tmp_path / name
        _valid_tree(case)
        backlog = case / "docs/product/BACKLOG.md"
        backlog.write_text(
            backlog.read_text(encoding="utf-8").replace(old, new),
            encoding="utf-8",
        )
        assert expected in _run(case).stdout


def test_checker_rejects_backlog_verification_history_metadata(tmp_path: Path) -> None:
    for name in ("BACKLOG.md", "FROZEN_BACKLOG.md"):
        case = tmp_path / name.lower()
        _valid_tree(case)
        backlog = case / "docs/product" / name
        backlog.write_text(
            "last_verified: synthetic\n" + backlog.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        assert "verification/history metadata is not allowed" in _run(case).stdout


def test_checker_rejects_stale_verification_metadata_across_public_surfaces(
    tmp_path: Path,
) -> None:
    for relative in (
        "notes.md",
        ".github/ISSUE_TEMPLATE/bug.yml",
        "examples/service.env.example",
    ):
        case = tmp_path / relative.replace("/", "-")
        _valid_tree(case)
        target = case / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("last_verified: synthetic\n", encoding="utf-8")
        output = _run(case).stdout
        assert "stale manual verification metadata is not allowed" in output


def test_checker_enforces_frozen_backlog_boundary(tmp_path: Path) -> None:
    case = tmp_path / "missing-thaw-rule"
    _valid_tree(case)
    frozen = case / "docs/product/FROZEN_BACKLOG.md"
    frozen.write_text(
        frozen.read_text(encoding="utf-8").replace(
            "only after `BACKLOG.md` is empty and a human explicitly approves thawing it",
            "only after a future decision",
        ),
        encoding="utf-8",
    )
    assert "missing empty-core and explicit-human thaw rule" in _run(case).stdout

    frozen_item = (
        "\n### BL-P1-99 - Implement frozen fixture behavior\n\n"
        "- **Action:** Implement the frozen fixture behavior.\n"
        "- **Done when:** The frozen fixture behavior is verified.\n"
        "- **Maps to:** F0.\n"
    )

    case = tmp_path / "duplicate-id"
    _valid_tree(case)
    frozen = case / "docs/product/FROZEN_BACKLOG.md"
    frozen.write_text(
        frozen.read_text(encoding="utf-8") + frozen_item.replace("BL-P1-99", "BL-P0-01"),
        encoding="utf-8",
    )
    assert "appears in both BACKLOG.md and FROZEN_BACKLOG.md" in _run(case).stdout

    case = tmp_path / "frozen-current-owner"
    _valid_tree(case)
    frozen = case / "docs/product/FROZEN_BACKLOG.md"
    frozen.write_text(
        frozen.read_text(encoding="utf-8") + frozen_item,
        encoding="utf-8",
    )
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8").replace("BL-P0-01", "BL-P1-99", 1),
        encoding="utf-8",
    )
    assert "references frozen backlog ID BL-P1-99" in _run(case).stdout


def test_checker_accepts_explicit_empty_core_backlog(tmp_path: Path) -> None:
    case = tmp_path / "empty-core"
    _valid_tree(case)
    backlog = case / "docs/product/BACKLOG.md"
    backlog.write_text("# Core backlog\n\nCore backlog is empty.\n", encoding="utf-8")
    outcome = case / "docs/product/USER_OUTCOME_SPEC.md"
    outcome.write_text(
        outcome.read_text(encoding="utf-8").replace("BL-P0-01", "—"),
        encoding="utf-8",
    )
    assert _run(case).returncode == 0


def test_checker_accepts_normalized_and_reference_style_readme_links(
    tmp_path: Path,
) -> None:
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


def test_checker_supports_brains_example(tmp_path: Path) -> None:
    case = tmp_path / "identity"
    _valid_tree(case)
    example = case / "examples/brains.skill.md"
    example.parent.mkdir(parents=True)
    example.write_text(HEADER + "# Brains workflow\n", encoding="utf-8")
    assert _run(case).returncode == 0

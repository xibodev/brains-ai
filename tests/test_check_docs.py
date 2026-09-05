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
    """The smallest tree the documentation contract accepts."""

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


def test_repository_documentation_contract() -> None:
    result = _run(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_checker_accepts_the_minimal_valid_tree(tmp_path: Path) -> None:
    case = tmp_path / "valid"
    _valid_tree(case)
    assert _run(case).returncode == 0


def test_checker_rejects_retired_backlog_documents(tmp_path: Path) -> None:
    for name in ("ACTIVE_BACKLOG.md", "EXPERIMENTAL_BACKLOG.md"):
        case = tmp_path / name.lower()
        _valid_tree(case)
        retired = case / "docs/product" / name
        retired.parent.mkdir(parents=True, exist_ok=True)
        retired.write_text(HEADER, encoding="utf-8")
        assert f"prohibited documentation/evidence path: docs/product/{name}" in _run(case).stdout


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

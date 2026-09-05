"""Validate the repository's canonical Brains documentation contract."""

from __future__ import annotations

import re
import subprocess
import sys
from os import walk
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DOCS = (
    "README.md",
    "docs/product/PRODUCT_BRIEF.md",
    "docs/GUIDE.md",
    "docs/MCP.md",
    "docs/ARCHITECTURE.md",
    "docs/OPERATIONS.md",
    "docs/QUALITY_GATES.md",
)

REQUIRED_SUPPORTING_DOCS = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/copilot-instructions.md",
)

EXACT_PROHIBITED = {
    "CHANGELOG.md",
    "docs/REMOTE_INSTALL.md",
    "docs/architecture.md",
    "docs/operations.md",
    "docs/pre-beta-roadmap.md",
    "docs/protocols.md",
    "docs/providers.md",
    "docs/roadmap.md",
    "docs/security.md",
    "docs/wiring.md",
    "docs/product/ACTIVE_BACKLOG.md",
    "docs/product/EXPERIMENTAL_BACKLOG.md",
    "docs/product/FEATURE_REGISTRY.md",
    "docs/product/KNOWN_LIMITATIONS.md",
    "docs/product/RELEASE_NOTES.md",
    "deploy/box/CUTOVER_CHECKLIST.md",
    "examples/copilot-instructions.md",
    "install/README.md",
    "docs/images/admin-copilot-device-login.png",
}
PROHIBITED_NAMES = {
    "changelog.md",
    "release_notes.md",
    "evidence.md",
    "cutover_checklist.md",
    "public_release_plan.md",
    "sidekick_build_status.md",
}

EXCLUDED_DIRS = {
    ".brains",
    ".brains-gate-bin",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".quality-run",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
}

VERIFICATION_METADATA_RE = re.compile(
    r"(?im)^\s*(?:last_verified|verified_by|verification_basis):\s*"
)
FIELD_PATTERNS = {
    "last_verified": re.compile(r"(?m)^\s*last_verified:\s*(\S+)\s*$"),
    "verified_by": re.compile(r"(?m)^\s*verified_by:\s*(.+?)\s*$"),
    "verification_basis": re.compile(r"(?m)^\s*verification_basis:\s*(.+?)\s*$"),
}
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
MARKDOWN_REFERENCE_RE = re.compile(r"\[(?P<text>[^\]]+)\]\[(?P<label>[^\]]*)\]")
MARKDOWN_REFERENCE_DEFINITION_RE = re.compile(
    r"(?m)^\s*\[(?P<label>[^\]]+)\]:\s*(?P<target><[^>]+>|\S+)"
)
SOURCE_DOC_REFERENCE_RE = re.compile(
    r"(?P<target>(?:docs|sandbox|deploy|examples|install)/"
    r"[A-Za-z0-9_./-]+\.md|CHANGELOG\.md|RELEASE_NOTES\.md|"
    r"PRODUCT-SPEC\.md|WS1-daemon-protocol\.md)"
)
SOURCE_REFERENCE_SUFFIXES = {
    ".html",
    ".js",
    ".json",
    ".mjs",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _iter_repo_files(root: Path):
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        result = None

    if result is not None and result.returncode == 0:
        for relative in result.stdout.split("\0"):
            if not relative:
                continue
            path = root / relative
            if path.is_file() and not any(part in EXCLUDED_DIRS for part in Path(relative).parts):
                yield path
        return

    for current, directories, files in walk(root):
        directories[:] = [name for name in directories if name not in EXCLUDED_DIRS]
        current_path = Path(current)
        for name in files:
            yield current_path / name


def _is_prohibited(relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    lower = normalized.lower()
    name = Path(normalized).name.lower()

    if normalized in EXACT_PROHIBITED:
        return True
    if lower.startswith("docs/decisions/") or lower.startswith("docs/battle-test/"):
        return True
    if lower.startswith("sandbox/collab/") and lower.endswith(".md"):
        return True
    if lower.startswith("sandbox/pivot/") and lower.endswith((".md", ".png")):
        return True
    if name in PROHIBITED_NAMES:
        return True
    if name.endswith("_saga.md") or name.endswith("-report.md"):
        return True
    return "roadmap" in name and name.endswith(".md")


def _normalize_markdown_target(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<"):
        closing = target.find(">")
        if closing != -1:
            target = target[1:closing]
    else:
        target = target.split(maxsplit=1)[0]
    return unquote(target.split("#", 1)[0])


def _markdown_targets(text: str) -> list[str]:
    references = {
        match.group("label").strip().casefold(): match.group("target")
        for match in MARKDOWN_REFERENCE_DEFINITION_RE.finditer(text)
    }
    targets = list(MARKDOWN_LINK_RE.findall(text))
    for match in MARKDOWN_REFERENCE_RE.finditer(text):
        label = (match.group("label") or match.group("text")).strip().casefold()
        target = references.get(label)
        if target:
            targets.append(target)
    return targets


def _link_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted(path for path in _iter_repo_files(root) if path.suffix.lower() == ".md"):
        relative = path.relative_to(root).as_posix()
        text = _read(path)
        for raw_target in _markdown_targets(text):
            target = raw_target.strip()
            if not target or target.startswith(("#", "/", "http://", "https://", "mailto:")):
                continue
            target = _normalize_markdown_target(target)
            if not target or " " in target:
                continue
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{relative}: link escapes repository: {raw_target}")
                continue
            if not resolved.exists():
                errors.append(f"{relative}: broken relative link: {raw_target}")
    return errors


def _source_reference_errors(root: Path) -> list[str]:
    errors: list[str] = []
    ignored = {
        "scripts/check_docs.py",
        "tests/test_check_docs.py",
    }
    for path in _iter_repo_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in ignored or path.suffix.lower() not in SOURCE_REFERENCE_SUFFIXES:
            continue
        try:
            text = _read(path)
        except UnicodeDecodeError:
            continue
        for match in SOURCE_DOC_REFERENCE_RE.finditer(text):
            raw_target = match.group("target")
            if "/" in raw_target:
                exists = (root / raw_target).is_file()
            else:
                exists = any(root.rglob(raw_target))
            if not exists:
                errors.append(f"{relative}: reference to missing documentation: {raw_target}")
    return errors


def _public_surface_errors(root: Path) -> list[str]:
    """Reject stale manual verification records anywhere users may read them."""
    errors: list[str] = []
    for path in _iter_repo_files(root):
        relative = path.relative_to(root).as_posix()
        lower = relative.lower()
        public_text = (
            path.suffix.lower() == ".md"
            or lower.startswith(".github/issue_template/")
            and path.suffix.lower() in {".yml", ".yaml"}
            or lower.endswith(".env.example")
        )
        if not public_text:
            continue
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError):
            continue
        if VERIFICATION_METADATA_RE.search(text):
            errors.append(f"{relative}: stale manual verification metadata is not allowed")
    return errors


def check_repository(root: Path = ROOT) -> list[str]:
    errors: list[str] = []

    for relative in CANONICAL_DOCS:
        if not (root / relative).is_file():
            errors.append(f"missing canonical document: {relative}")

    for relative in REQUIRED_SUPPORTING_DOCS:
        if not (root / relative).is_file():
            errors.append(f"missing required supporting document: {relative}")

    readme_path = root / "README.md"
    if readme_path.is_file():
        readme_targets = {
            _normalize_markdown_target(target) for target in _markdown_targets(_read(readme_path))
        }
        for relative in CANONICAL_DOCS[1:]:
            if relative not in readme_targets:
                errors.append(f"README.md: missing canonical link to {relative}")

    for path in _iter_repo_files(root):
        relative = path.relative_to(root).as_posix()
        if _is_prohibited(relative):
            errors.append(f"prohibited documentation/evidence path: {relative}")

    errors.extend(_link_errors(root))
    errors.extend(_source_reference_errors(root))
    errors.extend(_public_surface_errors(root))
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).resolve() if args else ROOT
    errors = check_repository(root)
    if errors:
        print("documentation contract failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("documentation contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

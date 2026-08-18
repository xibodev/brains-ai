"""Validate the repository's canonical Brains documentation contract."""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from os import walk
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_IDENTITY = "".join(("gar", "rison"))
IDENTITY_SCAN_EXCLUSIONS = {
    "scripts/check_docs.py",
    "tests/test_check_docs.py",
}

CANONICAL_DOCS = (
    "README.md",
    "docs/product/PRODUCT_BRIEF.md",
    "docs/product/FEATURE_CONTRACT.md",
    "docs/product/USER_OUTCOME_SPEC.md",
    "docs/product/PERSONAS_AND_JOURNEYS.md",
    "docs/product/TRACEABILITY.md",
    "docs/ARCHITECTURE.md",
    "docs/OPERATIONS.md",
    "docs/QUALITY_GATES.md",
    "docs/product/BACKLOG.md",
)

SUPPORTING_DOCS = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/copilot-instructions.md",
    "frontend/README.md",
    "docker/README.md",
    "services/wa-web/README.md",
    "tests/e2e/README.md",
    "sandbox/README.md",
    "sandbox/battle/README.md",
    "examples/brains.skill.md",
)

FRESHNESS_DOCS = CANONICAL_DOCS + SUPPORTING_DOCS

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
    "docs/product/FEATURE_REGISTRY.md",
    "docs/product/KNOWN_LIMITATIONS.md",
    "docs/product/RELEASE_NOTES.md",
    "deploy/box/CUTOVER_CHECKLIST.md",
    "examples/copilot-instructions.md",
    "install/README.md",
    "docs/images/admin-copilot-device-login.png",
    f"examples/{FORBIDDEN_IDENTITY}.skill.md",
    f"tests/test_acceptance_{FORBIDDEN_IDENTITY}.py",
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

REQUIRED_IDS = (
    *(f"F{i}" for i in range(11)),
    *(f"B{i}" for i in range(1, 10)),
    *(f"P{i}" for i in range(1, 8)),
    *(f"J{i}" for i in range(1, 12)),
)

REQUIRED_AC_PREFIXES = (
    *(f"AC-F{i}-" for i in range(11)),
    *(f"AC-B{i}-" for i in range(1, 10)),
)

REQUIRED_OUTCOME_IDS = (
    *(f"F{i}" for i in range(11)),
    *(f"B{i}" for i in range(1, 10)),
)

REQUIRED_OUTCOME_MARKERS = (
    "User promise",
    "Minimal path",
    "Code contract",
    "Expected evidence contract",
    "Current gap ownership",
    "Acceptance anchors",
)

REQUIRED_END_TO_END_OUTCOMES = tuple(f"O{i}" for i in range(1, 8))

OUTCOME_ROW_RE = re.compile(r"(?m)^\|\s*(F(?:10|[0-9])|B[1-9])\s*\|.*\|\s*$")
BACKLOG_REF_RE = re.compile(r"\bBL-P\d+-\d+\b")
BACKLOG_HEADING_RE = re.compile(r"(?m)^###\s+(BL-P[0-3]-\d+)\s+-")
BACKLOG_ITEM_RE = re.compile(
    r"(?ms)^###\s+(?P<id>BL-P[0-3]-\d+)\s+-.*?(?=^###\s+BL-P[0-3]-\d+\s+-|\Z)"
)
BACKLOG_MAPS_RE = re.compile(r"(?m)^- \*\*Maps to:\*\*\s*(?P<maps>.+)$")
OUTCOME_ID_RE = re.compile(r"\b(?:F(?:10|[0-9])|B[1-9])\b")
OUTCOME_RANGE_RE = re.compile(r"\b(?P<kind>[FB])(?P<start>\d+)-(?P=kind)(?P<end>\d+)\b")
JOURNEY_ID_RE = re.compile(r"\bJ(?:1[01]|[1-9])\b")

REQUIRED_SPA_ROUTES = (
    "/app",
    "/app/onboarding",
    "/app/inbox",
    "/app/sessions",
    "/app/sessions/:id",
    "/app/personas",
    "/app/personas/:slug",
    "/app/pods",
    "/app/pods/:slug",
    "/app/projects",
    "/app/projects/:code",
    "/app/issues",
    "/app/issues/:code",
    "/app/automation",
    "/app/runtimes",
    "/app/runtimes/:slug",
    "/app/config",
    "/app/config/:section",
    "/app/settings",
    "/app/settings/:section",
    "/app/*",
)

FIELD_PATTERNS = {
    "last_verified": re.compile(r"(?m)^\s*last_verified:\s*(\S+)\s*$"),
    "verified_by": re.compile(r"(?m)^\s*verified_by:\s*(.+?)\s*$"),
    "verification_basis": re.compile(r"(?m)^\s*verification_basis:\s*(.+?)\s*$"),
}
HTML_COMMENT_RE = re.compile(r"<!--(?P<body>.*?)-->", re.DOTALL)
HEAD_RE = re.compile(r"\bHEAD\s+[0-9a-f]{40}\b", re.IGNORECASE)
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


def _backlog_outcome_maps(text: str) -> dict[str, set[str]]:
    mappings: dict[str, set[str]] = {}
    for item in BACKLOG_ITEM_RE.finditer(text):
        map_match = BACKLOG_MAPS_RE.search(item.group(0))
        mapped_ids: set[str] = set()
        if map_match:
            maps = map_match.group("maps")
            mapped_ids.update(OUTCOME_ID_RE.findall(maps))
            for range_match in OUTCOME_RANGE_RE.finditer(maps):
                kind = range_match.group("kind")
                start = int(range_match.group("start"))
                end = int(range_match.group("end"))
                mapped_ids.update(f"{kind}{index}" for index in range(start, end + 1))
        mappings[item.group("id")] = mapped_ids
    return mappings


def _outcome_spec_errors(root: Path, path: Path) -> list[str]:
    text = _read(path)
    errors: list[str] = []
    rows: dict[str, list[list[str]]] = {stable_id: [] for stable_id in REQUIRED_OUTCOME_IDS}

    for match in OUTCOME_ROW_RE.finditer(text):
        cells = [cell.strip() for cell in match.group(0).strip().strip("|").split("|")]
        rows[match.group(1)].append(cells)

    backlog_path = root / "docs/product/BACKLOG.md"
    backlog_text = _read(backlog_path) if backlog_path.is_file() else ""
    backlog_ids = set(BACKLOG_HEADING_RE.findall(backlog_text))
    backlog_outcome_maps = _backlog_outcome_maps(backlog_text)

    for stable_id, matches in rows.items():
        if not matches:
            errors.append(f"USER_OUTCOME_SPEC.md: missing outcome row {stable_id}")
            continue
        if len(matches) > 1:
            errors.append(f"USER_OUTCOME_SPEC.md: duplicate outcome row {stable_id}")
            continue

        cells = matches[0]
        if len(cells) != 7:
            errors.append(
                f"USER_OUTCOME_SPEC.md: outcome row {stable_id} has {len(cells)} columns, expected 7"
            )
            continue
        if any(not cell for cell in cells[1:]):
            errors.append(
                f"USER_OUTCOME_SPEC.md: outcome row {stable_id} has an empty contract cell"
            )

        owners = set(BACKLOG_REF_RE.findall(cells[5]))
        if not owners:
            errors.append(f"USER_OUTCOME_SPEC.md: outcome row {stable_id} has no backlog owner")
        for owner in sorted(owners - backlog_ids):
            errors.append(
                f"USER_OUTCOME_SPEC.md: outcome row {stable_id} references unknown backlog ID {owner}"
            )
        for owner in sorted(owners & backlog_ids):
            if stable_id not in backlog_outcome_maps.get(owner, set()):
                errors.append(
                    f"USER_OUTCOME_SPEC.md: backlog ID {owner} does not map to {stable_id}"
                )

        expected_ac = f"AC-{stable_id}-"
        if expected_ac not in cells[6]:
            errors.append(
                f"USER_OUTCOME_SPEC.md: outcome row {stable_id} missing acceptance anchor {expected_ac}"
            )
        if JOURNEY_ID_RE.search(cells[6]) is None:
            errors.append(f"USER_OUTCOME_SPEC.md: outcome row {stable_id} has no journey anchor")

    for outcome_id in REQUIRED_END_TO_END_OUTCOMES:
        count = len(re.findall(rf"(?m)^###\s+{outcome_id}\s+-", text))
        if count == 0:
            errors.append(f"USER_OUTCOME_SPEC.md: missing end-to-end outcome {outcome_id}")
        elif count > 1:
            errors.append(f"USER_OUTCOME_SPEC.md: duplicate end-to-end outcome {outcome_id}")

    return errors


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


def _freshness_errors(path: Path, relative: str) -> list[str]:
    text = _read(path)
    header = None
    for match in HTML_COMMENT_RE.finditer(text):
        body = match.group("body")
        if "last_verified:" in body:
            header = body
            break
    if header is None:
        return [f"{relative}: missing HTML freshness header"]

    errors: list[str] = []
    values: dict[str, str] = {}
    for field, pattern in FIELD_PATTERNS.items():
        match = pattern.search(header)
        if match is None or not match.group(1).strip():
            errors.append(f"{relative}: freshness header missing {field}")
        else:
            values[field] = match.group(1).strip()

    last_verified = values.get("last_verified")
    if last_verified:
        try:
            parsed = datetime.fromisoformat(last_verified.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                errors.append(f"{relative}: last_verified must include a timezone")
        except ValueError:
            errors.append(f"{relative}: last_verified is not full ISO-8601")

    basis = values.get("verification_basis", "")
    if basis and not HEAD_RE.search(basis):
        errors.append(f"{relative}: verification_basis must include HEAD and a full SHA")
    if basis and "deployment not verified" not in basis.lower():
        errors.append(f"{relative}: verification_basis must state deployment not verified")
    return errors


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


def _identity_errors(root: Path) -> list[str]:
    errors: list[str] = []
    forbidden = FORBIDDEN_IDENTITY.casefold()
    for path in _iter_repo_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in IDENTITY_SCAN_EXCLUSIONS:
            continue
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError):
            continue
        if forbidden in text.casefold():
            errors.append(f"{relative}: forbidden legacy product identity text")
    return errors


def check_repository(root: Path = ROOT) -> list[str]:
    errors: list[str] = []

    for relative in CANONICAL_DOCS:
        if not (root / relative).is_file():
            errors.append(f"missing canonical document: {relative}")

    for relative in FRESHNESS_DOCS:
        path = root / relative
        if path.is_file():
            errors.extend(_freshness_errors(path, relative))

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

    trace_path = root / "docs/product/TRACEABILITY.md"
    if trace_path.is_file():
        trace = _read(trace_path)
        for stable_id in REQUIRED_IDS:
            if re.search(rf"(?<![A-Z0-9]){re.escape(stable_id)}(?![A-Z0-9])", trace) is None:
                errors.append(f"TRACEABILITY.md: missing stable ID {stable_id}")
        for prefix in REQUIRED_AC_PREFIXES:
            if prefix not in trace:
                errors.append(f"TRACEABILITY.md: missing acceptance ID prefix {prefix}")
        for route in REQUIRED_SPA_ROUTES:
            if f"`{route}`" not in trace:
                errors.append(f"TRACEABILITY.md: missing SPA route {route}")

    outcome_path = root / "docs/product/USER_OUTCOME_SPEC.md"
    if outcome_path.is_file():
        outcome = _read(outcome_path)
        for marker in REQUIRED_OUTCOME_MARKERS:
            if marker not in outcome:
                errors.append(f"USER_OUTCOME_SPEC.md: missing outcome marker {marker}")
        errors.extend(_outcome_spec_errors(root, outcome_path))

    errors.extend(_link_errors(root))
    errors.extend(_source_reference_errors(root))
    errors.extend(_identity_errors(root))
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

"""Assert the built wheel and sdist ship the files Brains needs at run time.

``uv build`` succeeds even when package data is missing, and importing the
package still works, so a wheel that omits the operator-console bundle, the
frozen baseline DDL or the SQL migrations looks healthy right up to the moment
an installed Brains cannot serve ``/app`` or create a database.

This gate opens the built artifacts and checks the payload directly.

Usage::

    python scripts/check_distribution.py [--dist DIST_DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/product/CORE_SURFACE.json"

#: Exact members every wheel must carry, relative to the package root.
REQUIRED_WHEEL_FILES = (
    "brains/web/spa/index.html",
    "brains/storage/baseline/sqlite.sql",
    "brains/storage/baseline/postgresql.sql",
)

#: Directories inside the wheel that must not be empty.
REQUIRED_WHEEL_TREES = (
    "brains/web/spa/assets/",
    "brains/web/templates/",
    "brains/storage/sql_migrations/",
)

FORBIDDEN_WHEEL_PREFIXES = (
    "brains/dashboard/",
    "brains/web/static/",
    "brains/web/templates/dashboard/",
)
FORBIDDEN_WHEEL_FILES = (
    "brains/admin/service.py",
    "brains/admin/ui.py",
    "brains/web/filters.py",
    "brains/web/icons.py",
    "brains/web/templates/admin/base.html",
    "brains/web/templates/admin/config.html",
    "brains/web/templates/admin/overview.html",
    "brains/web/templates/admin/secrets.html",
    "brains/web/templates/admin/test.html",
)

#: Paths every sdist must carry so the tree can be rebuilt from it.
REQUIRED_SDIST_FILES = (
    "pyproject.toml",
    "src/brains/web/spa/index.html",
    "src/brains/storage/baseline/sqlite.sql",
)


def _one(paths: list[Path], kind: str) -> tuple[Path | None, list[str]]:
    if not paths:
        return None, [f"no {kind} was built"]
    if len(paths) > 1:
        names = ", ".join(sorted(path.name for path in paths))
        return None, [f"expected exactly one {kind}, found: {names}"]
    return paths[0], []


def wheel_inventory(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(
            re.sub(r"^brains_ai-[^/]+\.dist-info/", "brains_ai.dist-info/", member)
            for member in archive.namelist()
            if not member.endswith("/")
        )


def sdist_inventory(path: Path) -> list[str]:
    with tarfile.open(path) as archive:
        members = archive.getmembers()
    if any(not member.isdir() and not member.isfile() for member in members):
        raise ValueError("sdist contains a non-regular member")
    names = [member.name for member in members if member.isfile()]
    if not names or any("/" not in name for name in names):
        raise ValueError("sdist file inventory has no single package root")
    roots = {name.split("/", 1)[0] for name in names}
    if len(roots) != 1 or not next(iter(roots)).startswith(("brains_ai-", "brains-ai-")):
        raise ValueError("sdist file inventory has no single package root")
    return sorted(name.split("/", 1)[1] for name in names)


def _manifest_inventory(kind: str) -> list[str]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    distribution = payload.get("distribution") if isinstance(payload, dict) else None
    expected = distribution.get(kind) if isinstance(distribution, dict) else None
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise ValueError(f"reviewed {kind} inventory is missing or malformed")
    return expected


def _exact_errors(path: Path, kind: str, actual: list[str]) -> list[str]:
    try:
        expected = _manifest_inventory(kind)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return [f"{path.name}: reviewed {kind} inventory is unavailable"]
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    missing = sum((expected_counts - actual_counts).values())
    extra = sum((actual_counts - expected_counts).values())
    if missing or extra:
        return [f"{path.name}: {kind} inventory differs; missing={missing} unexpected={extra}"]
    return []


def check_wheel(path: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for member in REQUIRED_WHEEL_FILES:
            if member not in names:
                errors.append(f"{path.name}: missing {member}")
        for tree in REQUIRED_WHEEL_TREES:
            if not any(name.startswith(tree) and not name.endswith("/") for name in names):
                errors.append(f"{path.name}: {tree} ships no files")
        for prefix in FORBIDDEN_WHEEL_PREFIXES:
            if any(name.startswith(prefix) for name in names):
                errors.append(f"{path.name}: ships deleted legacy tree {prefix}")
        for member in FORBIDDEN_WHEEL_FILES:
            if member in names:
                errors.append(f"{path.name}: ships deleted legacy file {member}")
        migrations = {name for name in names if name.startswith("brains/storage/sql_migrations/")}
        if not any(name.endswith(".sql") for name in migrations):
            errors.append(f"{path.name}: brains/storage/sql_migrations ships no .sql delta")
    errors.extend(_exact_errors(path, "wheel", wheel_inventory(path)))
    return errors


def check_sdist(path: Path) -> list[str]:
    errors: list[str] = []
    with tarfile.open(path) as archive:
        # Members are prefixed with `<name>-<version>/`.
        names = {name.split("/", 1)[-1] for name in archive.getnames() if "/" in name}
        for member in REQUIRED_SDIST_FILES:
            if member not in names:
                errors.append(f"{path.name}: missing {member}")
        for prefix in (
            "src/brains/dashboard/",
            "src/brains/web/static/",
            "src/brains/web/templates/dashboard/",
        ):
            if any(name.startswith(prefix) for name in names):
                errors.append(f"{path.name}: ships deleted legacy tree {prefix}")
        for member in (f"src/{name}" for name in FORBIDDEN_WHEEL_FILES):
            if member in names:
                errors.append(f"{path.name}: ships deleted legacy file {member}")
    try:
        members = sdist_inventory(path)
    except (OSError, ValueError, tarfile.TarError):
        errors.append(f"{path.name}: sdist inventory is malformed")
    else:
        errors.extend(_exact_errors(path, "sdist", members))
    return errors


def check_distribution(dist: Path) -> list[str]:
    wheel, errors = _one(sorted(dist.glob("*.whl")), "wheel")
    sdist, sdist_errors = _one(sorted(dist.glob("*.tar.gz")), "sdist")
    errors = [*errors, *sdist_errors]
    if wheel is not None:
        errors.extend(check_wheel(wheel))
    if sdist is not None:
        errors.extend(check_sdist(sdist))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default=str(ROOT / "dist"), help="Directory holding the build.")
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="Record exact normalized artifact members; never used by CI.",
    )
    args = parser.parse_args(argv)

    dist = Path(args.dist).resolve()
    if not dist.is_dir():
        print(f"distribution contract failed:\n- {dist} does not exist")
        return 1

    wheel, selection_errors = _one(sorted(dist.glob("*.whl")), "wheel")
    sdist, sdist_errors = _one(sorted(dist.glob("*.tar.gz")), "sdist")
    if args.write_manifest:
        errors = [*selection_errors, *sdist_errors]
        if errors or wheel is None or sdist is None:
            print("distribution contract failed:\n- " + "\n- ".join(errors))
            return 1
        try:
            payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            payload["distribution"] = {
                "wheel": wheel_inventory(wheel),
                "sdist": sdist_inventory(sdist),
            }
            MANIFEST.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            print("distribution contract failed:\n- core surface manifest is unavailable")
            return 1
        print("wrote reviewed wheel and sdist inventories")
        return 0

    errors = check_distribution(dist)
    if errors:
        print("distribution contract failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("distribution ships core runtime data and excludes the deleted legacy browser")
    return 0


if __name__ == "__main__":
    sys.exit(main())

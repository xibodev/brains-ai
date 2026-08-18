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
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Exact members every wheel must carry, relative to the package root.
REQUIRED_WHEEL_FILES = (
    "brains/web/spa/index.html",
    "brains/storage/baseline/sqlite.sql",
    "brains/storage/baseline/postgresql.sql",
)

#: Directories inside the wheel that must not be empty.
REQUIRED_WHEEL_TREES = (
    "brains/web/spa/assets/",
    "brains/web/static/",
    "brains/web/templates/",
    "brains/storage/sql_migrations/",
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
        migrations = {name for name in names if name.startswith("brains/storage/sql_migrations/")}
        if not any(name.endswith(".sql") for name in migrations):
            errors.append(f"{path.name}: brains/storage/sql_migrations ships no .sql delta")
    return errors


def check_sdist(path: Path) -> list[str]:
    errors: list[str] = []
    with tarfile.open(path) as archive:
        # Members are prefixed with `<name>-<version>/`.
        names = {name.split("/", 1)[-1] for name in archive.getnames() if "/" in name}
        for member in REQUIRED_SDIST_FILES:
            if member not in names:
                errors.append(f"{path.name}: missing {member}")
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
    args = parser.parse_args(argv)

    dist = Path(args.dist).resolve()
    if not dist.is_dir():
        print(f"distribution contract failed:\n- {dist} does not exist")
        return 1

    errors = check_distribution(dist)
    if errors:
        print("distribution contract failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("distribution ships the SPA bundle, baseline DDL and SQL migrations")
    return 0


if __name__ == "__main__":
    sys.exit(main())

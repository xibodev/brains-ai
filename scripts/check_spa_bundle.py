"""Rebuild the operator SPA and compare it with the committed bundle.

The wheel ships a pre-built bundle under ``src/brains/web/spa`` (pyproject
package-data). Nothing in the tree proves that bundle was produced from the
current ``frontend/src``, so a source change that is never rebuilt ships an
older console than the one that was reviewed.

This gate closes that hole (BL-P1-01). It builds the SPA into a scratch
directory, compares the result byte-for-byte with the committed bundle, and
removes the scratch directory again, so a normal local run leaves the worktree
exactly as it found it and never overwrites the tracked bundle.

Usage::

    python scripts/check_spa_bundle.py [--root ROOT] [--no-install] [--keep]
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIRNAME = "frontend"
BUNDLE_RELATIVE = "src/brains/web/spa"
#: Scratch build target. Ignored by git and removed after every run.
BUILD_DIRNAME = ".bundle-check"
VITE_ENTRY = "node_modules/vite/bin/vite.js"


def _npm() -> str:
    for candidate in ("npm.cmd", "npm") if os.name == "nt" else ("npm",):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise SystemExit("error: npm is not on PATH; the SPA bundle gate needs Node.js")


def _node() -> str:
    resolved = shutil.which("node")
    if not resolved:
        raise SystemExit("error: node is not on PATH; the SPA bundle gate needs Node.js")
    return resolved


def _run(command: list[str], cwd: Path) -> int:
    print(f"$ {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=str(cwd), check=False).returncode


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in sorted(root.rglob("*")) if path.is_file()}


def compare_trees(built: Path, committed: Path) -> list[str]:
    """Byte-compare two bundle directories and describe every difference."""

    differences: list[str] = []
    built_files = _relative_files(built)
    committed_files = _relative_files(committed)

    for relative in sorted(built_files - committed_files):
        differences.append(f"built but not committed: {relative}")
    for relative in sorted(committed_files - built_files):
        differences.append(f"committed but not built: {relative}")
    for relative in sorted(built_files & committed_files):
        if not filecmp.cmp(built / relative, committed / relative, shallow=False):
            differences.append(f"content differs: {relative}")
    return differences


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT), help="Repository root to check.")
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Skip `npm ci` and use the existing frontend/node_modules.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the scratch build directory (for inspecting a failure).",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    frontend = root / FRONTEND_DIRNAME
    committed = root / BUNDLE_RELATIVE
    build_dir = frontend / BUILD_DIRNAME

    if not (frontend / "package.json").is_file():
        print(f"error: {FRONTEND_DIRNAME}/package.json is missing")
        return 1
    if not (committed / "index.html").is_file():
        print(f"error: committed SPA bundle is missing at {BUNDLE_RELATIVE}/index.html")
        return 1

    if not args.no_install:
        # `npm ci` is the only install that is defined by the lockfile alone; it
        # refuses to run when package.json and package-lock.json disagree, which
        # is exactly the drift this gate must not paper over.
        if _run([_npm(), "ci"], frontend) != 0:
            print("error: `npm ci` failed in frontend/ (lockfile out of sync?)")
            return 1
    elif not (frontend / "node_modules").is_dir():
        print("error: --no-install was given but frontend/node_modules does not exist")
        return 1

    entry = frontend / VITE_ENTRY
    if not entry.is_file():
        print(f"error: vite is not installed at {FRONTEND_DIRNAME}/{VITE_ENTRY}")
        return 1

    if build_dir.exists():
        shutil.rmtree(build_dir)

    try:
        # Build into a scratch directory instead of the tracked output path, so
        # the committed bundle is never touched by the check itself.
        code = _run(
            [
                _node(),
                str(entry),
                "build",
                "--outDir",
                str(build_dir),
                "--emptyOutDir",
            ],
            frontend,
        )
        if code != 0:
            print("error: the SPA production build failed")
            return 1

        differences = compare_trees(build_dir, committed)
    finally:
        if not args.keep and build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)

    if differences:
        print("committed SPA bundle does not match a build of frontend/src:")
        for difference in differences:
            print(f"- {difference}")
        print(
            "run `cd frontend && npm ci && npm run build` and commit "
            f"{BUNDLE_RELATIVE} with the source change"
        )
        return 1

    print(f"committed SPA bundle matches a build of {FRONTEND_DIRNAME}/src")
    return 0


if __name__ == "__main__":
    sys.exit(main())

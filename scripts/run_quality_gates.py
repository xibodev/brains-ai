"""Run the blocking Brains quality gates locally, in CI order.

This is the exact local equivalent of `.github/workflows/ci.yml`. It runs the
same commands, in the same order, and stops reporting success the moment one of
them fails, so "it passes locally" and "it passes in CI" mean the same thing.

The Docker smoke and Playwright journey gates are not run here: both need an
environment this script cannot assume (a Docker daemon, browsers, an ephemeral
hub). They are listed as not run, together with any gate whose tool is missing
from this host, so a local run never implies more evidence than it produced.

Usage::

    python scripts/run_quality_gates.py            # every gate this host can run
    python scripts/run_quality_gates.py --fast     # skip the full pytest sweep
    python scripts/run_quality_gates.py --no-spa   # skip the Node gates
    python scripts/run_quality_gates.py --list     # print the commands only
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_PYTHON = "3.12"


@dataclass(frozen=True)
class Gate:
    name: str
    command: list[str]
    cwd: Path = ROOT


def _runner() -> list[str]:
    """The prefix that runs a command inside the project environment."""

    return (
        ["uv", "run", "--extra", "dev", "--python", CI_PYTHON, "--no-sync"]
        if shutil.which("uv")
        else []
    )


def _npm() -> str | None:
    for candidate in ("npm.cmd", "npm") if os.name == "nt" else ("npm",):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def gates(*, fast: bool, spa: bool) -> list[Gate]:
    runner = _runner()
    npm = _npm()

    def script(path: str) -> list[str]:
        return [*runner, "python", path] if runner else [sys.executable, path]

    def tool(name: str, *args: str) -> list[str]:
        if runner:
            return [*runner, name, *args]
        return [sys.executable, "-m", name, *args]

    plan: list[Gate] = []
    if npm is not None:
        plan.append(
            Gate(
                "checked TypeScript parser install",
                [npm, "ci", "--ignore-scripts"],
                cwd=ROOT / "frontend",
            )
        )
    plan.extend(
        [
            Gate("documentation contract", script("scripts/check_docs.py")),
            Gate("generated traceability contract", script("scripts/check_traceability.py")),
            Gate("core surface boundary", script("scripts/check_core_surface.py")),
            Gate("ruff lint", tool("ruff", "check", ".")),
            Gate("ruff format", tool("ruff", "format", "--check", ".")),
            Gate("mypy", tool("mypy")),
            Gate("acceptance tests", tool("pytest", "-q", "-m", "acceptance")),
        ]
    )
    if fast:
        plan.append(
            Gate(
                "contract self-tests",
                tool(
                    "pytest",
                    "-q",
                    "tests/test_check_docs.py",
                    "tests/test_check_traceability.py",
                    "tests/test_migration_contract.py",
                    "tests/test_baseline_schema_generation.py",
                ),
            )
        )
    else:
        plan.append(Gate("unit + integration tests", tool("pytest", "-q", "--maxfail=20")))

    if spa and npm is not None:
        plan.append(Gate("spa typecheck", [npm, "run", "typecheck"], cwd=ROOT / "frontend"))
        plan.append(
            Gate(
                "committed spa bundle",
                [*runner, "python", "scripts/check_spa_bundle.py", "--no-install"]
                if runner
                else [sys.executable, "scripts/check_spa_bundle.py", "--no-install"],
            )
        )

    # `uv build` is the packaging gate CI runs; without uv there is no
    # equivalent that uses only what this repository already declares, so the
    # gate is reported as unavailable rather than silently passed.
    if shutil.which("uv"):
        plan.append(Gate("build wheel + sdist", ["uv", "build"]))
        plan.append(Gate("distribution contents", script("scripts/check_distribution.py")))
    return plan


def _unavailable(plan: list[Gate], *, spa: bool) -> list[str]:
    missing = ["docker smoke", "Playwright E2E"]
    if spa and _npm() is None:
        missing.append("SPA typecheck/build/bundle (npm not on PATH)")
    if _npm() is None:
        missing.append("core-surface AST parser install (npm not on PATH)")
    if not shutil.which("uv"):
        missing.append("wheel/sdist build (uv not on PATH)")
    if not spa:
        missing.append("SPA gates (--no-spa)")
    if any(gate.name == "contract self-tests" for gate in plan):
        missing.append("full pytest sweep (--fast)")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="Skip the full pytest sweep.")
    parser.add_argument("--no-spa", action="store_true", help="Skip the Node/SPA gates.")
    parser.add_argument("--list", action="store_true", help="Print the commands and exit.")
    args = parser.parse_args(argv)

    plan = gates(fast=args.fast, spa=not args.no_spa)
    if args.list:
        for gate in plan:
            print(f"{gate.name}: {' '.join(gate.command)} (cwd={gate.cwd.name})")
        return 0

    if shutil.which("uv"):
        sync = ["uv", "sync", "--extra", "dev", "--python", CI_PYTHON]
        print(f"$ {' '.join(sync)}", flush=True)
        if subprocess.run(sync, cwd=str(ROOT), check=False).returncode != 0:
            print("failed to prepare the locked CI Python/dev environment")
            return 1

    if _npm() is None:
        print("npm is required to install the checked TypeScript parser before core-surface gates")
        return 1

    failures: list[str] = []
    for gate in plan:
        print(f"\n=== {gate.name} ===", flush=True)
        print(f"$ {' '.join(gate.command)}", flush=True)
        started = time.monotonic()
        code = subprocess.run(gate.command, cwd=str(gate.cwd), check=False).returncode
        elapsed = time.monotonic() - started
        if code == 0:
            print(f"--- {gate.name}: ok ({elapsed:.1f}s)")
        else:
            print(f"--- {gate.name}: FAILED (exit {code}, {elapsed:.1f}s)")
            failures.append(gate.name)

    print("\n=== summary ===")
    for gate in plan:
        print(f"{'FAIL' if gate.name in failures else 'ok  '}  {gate.name}")
    print(f"not run here: {', '.join(_unavailable(plan, spa=not args.no_spa))}")

    if failures:
        print(f"\n{len(failures)} gate(s) failed: {', '.join(failures)}")
        return 1
    print("\nevery gate this host ran passed; the ones listed above were not run")
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_quality_gates.py"
_SPEC = importlib.util.spec_from_file_location("brains_quality_gate_runner", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
run_quality_gates = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = run_quality_gates
_SPEC.loader.exec_module(run_quality_gates)


def test_uv_runner_selects_ci_python_and_dev_extra(monkeypatch) -> None:
    monkeypatch.setattr(run_quality_gates.shutil, "which", lambda name: name)

    assert run_quality_gates._runner() == [
        "uv",
        "run",
        "--extra",
        "dev",
        "--python",
        "3.12",
        "--no-sync",
    ]


def test_gate_commands_share_the_ci_runner(monkeypatch) -> None:
    monkeypatch.setattr(run_quality_gates.shutil, "which", lambda name: name)
    plan = run_quality_gates.gates(fast=True, spa=False)
    commands = {gate.name: gate.command for gate in plan}

    prefix = ["uv", "run", "--extra", "dev", "--python", "3.12", "--no-sync"]
    assert plan[0].name == "checked TypeScript parser install"
    assert plan[0].command[-2:] == ["ci", "--ignore-scripts"]
    assert commands["documentation contract"][: len(prefix)] == prefix
    assert commands["ruff lint"][: len(prefix)] == prefix
    assert commands["contract self-tests"][: len(prefix)] == prefix
    assert commands["core surface boundary"][: len(prefix)] == prefix
    assert commands["core surface boundary"][-2:] == ["--dist", "dist"]
    assert commands["distribution contents"][: len(prefix)] == prefix
    names = [gate.name for gate in plan]
    assert names.index("build wheel + sdist") < names.index("core surface boundary")
    assert names.index("core surface boundary") < names.index("distribution contents")


def test_main_syncs_once_before_running_gates(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr(run_quality_gates.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        run_quality_gates.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or Result(),
    )

    assert run_quality_gates.main(["--fast", "--no-spa"]) == 0
    assert calls[0] == ["uv", "sync", "--extra", "dev", "--python", "3.12"]
    assert calls[1][-2:] == ["ci", "--ignore-scripts"]
    assert all("--no-sync" in command for command in calls[2:8])


def test_package_ci_builds_fresh_artifacts_before_core_surface() -> None:
    workflow = (_PATH.parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    package_job = workflow.split("  package:\n", 1)[1].split("\n  native-installation-probe:\n", 1)[
        0
    ]

    setup = package_job.index("actions/setup-node@v4")
    pinned = package_job.index('node-version: "22"')
    install = package_job.index("npm ci --ignore-scripts")
    build = package_job.index("uv build")
    checker = package_job.index("python scripts/check_core_surface.py --dist dist")
    distribution = package_job.index("python scripts/check_distribution.py")
    assert setup < pinned < install < build < checker < distribution


def test_local_runner_fails_clearly_without_parser_installer(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def which(name: str) -> str | None:
        return None if name in {"npm", "npm.cmd"} else name

    monkeypatch.setattr(run_quality_gates.shutil, "which", which)
    monkeypatch.setattr(
        run_quality_gates.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or Result(),
    )

    assert run_quality_gates.main(["--fast", "--no-spa"]) == 1
    assert calls == [["uv", "sync", "--extra", "dev", "--python", "3.12"]]
    assert "npm is required to install the checked TypeScript parser" in capsys.readouterr().out

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
    assert commands["distribution contents"][: len(prefix)] == prefix


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


def test_docs_ci_installs_declared_parser_before_core_surface() -> None:
    workflow = (_PATH.parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    docs_job = workflow.split("  docs:\n", 1)[1].split("\n  lint:\n", 1)[0]

    setup = docs_job.index("actions/setup-node@v4")
    pinned = docs_job.index("node-version: 22")
    install = docs_job.index("npm ci --ignore-scripts")
    checker = docs_job.index("python scripts/check_core_surface.py")
    self_tests = docs_job.index("tests/test_core_surface_manifest.py")
    assert setup < pinned < install < checker < self_tests


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

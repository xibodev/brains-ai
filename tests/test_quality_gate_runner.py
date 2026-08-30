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
    assert all("--no-sync" in command for command in calls[1:7])

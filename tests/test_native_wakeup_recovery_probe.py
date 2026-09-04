"""Static safety contract for the native Claude wakeup recovery probe."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PROBE = ROOT / "scripts" / "probe_native_wakeup_recovery.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
COMMIT = "0" * 40


def _module():
    spec = importlib.util.spec_from_file_location("native_wakeup_probe_contract", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_uses_real_abrupt_process_boundaries_and_complete_matrix() -> None:
    probe = _module()
    source = PROBE.read_text(encoding="utf-8")

    assert probe.PHASES == ("prepared", "swapped", "validated", "metadata")
    assert probe.OPERATIONS == ("install", "remove")
    assert "os._exit(CRASH_EXIT_CODE)" in source
    assert "ReplaceFileW" in source
    assert "renamex_np(RENAME_SWAP)" in source
    assert '"displaced-settings.bin"' in source
    assert "Never relay child output" in source


def test_probe_child_environment_replaces_ambient_brains_and_home_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _module()
    for key in (
        "home",
        "state",
        "tmp",
    ):
        (tmp_path / key).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRAINS_DB_URL", "sqlite:///ambient-must-not-survive.db")
    monkeypatch.setenv("BRAINS_STATE_DIR", "ambient-must-not-survive")
    monkeypatch.setenv("BRAINS_UNRELATED_AMBIENT_VALUE", "must-not-survive")
    monkeypatch.setenv("HOME", "ambient-home-must-not-survive")
    monkeypatch.setenv("USERPROFILE", "ambient-home-must-not-survive")

    environment = probe._isolated_environment(tmp_path, COMMIT)

    assert environment["BRAINS_NATIVE_WAKEUP_PROBE_CANDIDATE"] == COMMIT
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["USERPROFILE"] == str(tmp_path / "home")
    assert environment["BRAINS_STATE_DIR"] == str(tmp_path / "state")
    assert environment["BRAINS_DB_URL"].endswith("/state/brains.db")
    assert "BRAINS_UNRELATED_AMBIENT_VALUE" not in environment
    assert all(
        not value.startswith("ambient-")
        for key, value in environment.items()
        if key in probe._ISOLATED_ENV_KEYS
    )
    with pytest.raises(RuntimeError, match="escaped"):
        probe._owned(tmp_path.parent / "outside", tmp_path)


def test_native_matrix_and_real_claude_probe_are_blocking() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    gate = workflow.split("\n  gate:\n", 1)[1]

    assert "python scripts/probe_native_wakeup_recovery.py" in workflow
    assert "runner.os == 'Windows' || runner.os == 'macOS'" in workflow
    assert '--candidate "${{ github.sha }}"' in workflow
    assert "--output native-wakeup-recovery.json" in workflow
    assert "if-no-files-found: error" in workflow
    assert "      - claude-wakeup-probe\n" in gate


def test_public_report_is_bounded_and_cannot_escape_or_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _module()
    monkeypatch.chdir(tmp_path)
    report = {
        "ok": True,
        "candidate": COMMIT,
        "platform": "synthetic",
        "scenarios": len(probe.PHASES) * len(probe.OPERATIONS),
    }

    probe._write_public_result("native-result.json", report)

    rendered = (tmp_path / "native-result.json").read_text(encoding="utf-8")
    assert json.loads(rendered) == report
    assert capsys.readouterr().out == rendered
    assert str(tmp_path) not in rendered
    assert "settings" not in rendered
    assert "S-1-" not in rendered
    with pytest.raises(RuntimeError, match="already exists"):
        probe._write_public_result("native-result.json", report)
    with pytest.raises(RuntimeError, match="escaped"):
        probe._write_public_result(str(tmp_path.parent / "outside.json"), report)

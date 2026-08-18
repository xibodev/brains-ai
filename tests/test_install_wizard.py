"""Tests for the `brains-ai features` subsystem wizard.

Covers the planner (pure function — easy to test) and the CLI surface
(via Typer's test runner). We deliberately do not exec real pip in tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from brains.cli.app import app
from brains.config import RUNTIME_OVERLAY_SCHEMA_VERSION
from brains.install import (
    SPECS,
    VALID_FEATURES,
    apply_plan,
    plan_changes,
    status_report,
)


@pytest.fixture
def fresh_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the runtime overlay at a tmp file and force-reload settings."""
    overlay = tmp_path / "brains.runtime.yaml"
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay))
    # Reload settings so the new overlay path takes effect for both the
    # config module's module-level ``settings`` and the wizard's read_overlay
    # (which calls _overlay_path -> settings.runtime_overlay).
    from brains import config as config_module

    config_module.reload_settings()
    yield overlay
    # Clean up: restore default settings for the next test
    monkeypatch.delenv("BRAINS_RUNTIME_OVERLAY", raising=False)
    config_module.reload_settings()


# --- Planner --------------------------------------------------------------


def test_specs_cover_every_extra_except_litellm_which_is_extra_only(
    fresh_overlay: Path,
) -> None:
    """Every subsystem we expose must have a planner spec.

    Postgres, telegram, slack, whatsapp, otel all set a config flag.
    LiteLLM is extra-only (no subsystems.litellm flag exists yet).
    """
    assert set(VALID_FEATURES) == {
        "telegram",
        "slack",
        "whatsapp",
        "postgres",
        "otel",
        "litellm",
    }
    assert SPECS["litellm"].config_flag is False
    for name in ("telegram", "slack", "whatsapp", "postgres", "otel"):
        assert SPECS[name].config_flag is True


def test_status_report_with_no_overlay(fresh_overlay: Path) -> None:
    report = status_report()
    assert report["schema_version"] == RUNTIME_OVERLAY_SCHEMA_VERSION
    feature_names = {row["feature"] for row in report["features"]}
    assert feature_names == set(VALID_FEATURES)
    # Lean-core defaults: nothing enabled in config.
    for row in report["features"]:
        if row["feature"] == "litellm":
            continue
        assert row["config_enabled"] is False


def test_plan_enable_telegram_from_clean_overlay(
    fresh_overlay: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force telegram extra to look missing so the plan recommends pip install.
    monkeypatch.setitem(sys.modules, "telegram", None)
    plan = plan_changes(enable=["telegram"])
    assert plan.features_to_enable == ["telegram"]
    assert plan.extras_to_install == ["telegram"]
    assert plan.overlay_updates == {"subsystems": {"bridges": {"telegram": {"enabled": True}}}}
    assert plan.pip_command is not None
    assert plan.pip_command[-1] == "brains-ai[telegram]"


def test_plan_features_replaces_current_set(
    fresh_overlay: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "telegram", None)
    monkeypatch.setitem(sys.modules, "slack_sdk", None)
    # Pre-seed overlay with telegram on, slack off.
    fresh_overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": RUNTIME_OVERLAY_SCHEMA_VERSION,
                "subsystems": {"bridges": {"telegram": {"enabled": True}}},
            }
        ),
        encoding="utf-8",
    )
    # Now ask for exactly slack — should disable telegram + enable slack.
    plan = plan_changes(features=["slack"])
    assert "slack" in plan.features_to_enable
    assert "telegram" in plan.features_to_disable
    assert plan.extras_to_install == ["slack"]


def test_plan_no_op_when_already_enabled(
    fresh_overlay: Path,
) -> None:
    # Pre-seed otel on. WhatsApp probe is empty so its extra always shows
    # as installed; use whatsapp here so installed_extras() reports True.
    fresh_overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": RUNTIME_OVERLAY_SCHEMA_VERSION,
                "subsystems": {"bridges": {"whatsapp": {"enabled": True}}},
            }
        ),
        encoding="utf-8",
    )
    plan = plan_changes(enable=["whatsapp"])
    assert plan.features_to_enable == []
    assert plan.skipped_no_change == ["whatsapp"]
    assert not plan.has_changes


def test_plan_disable_already_off_is_no_op(fresh_overlay: Path) -> None:
    plan = plan_changes(disable=["slack"])
    assert plan.skipped_no_change == ["slack"]
    assert not plan.has_changes


def test_plan_unknown_feature_raises(fresh_overlay: Path) -> None:
    with pytest.raises(KeyError):
        plan_changes(enable=["not-a-real-feature"])


def test_apply_plan_writes_overlay_but_skips_pip_by_default(
    fresh_overlay: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "slack_sdk", None)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("pip should not be invoked when run_pip=False")

    monkeypatch.setattr("brains.install.subprocess.run", _fail_if_called)

    plan = plan_changes(enable=["slack"])
    result = apply_plan(plan, run_pip=False)
    assert result["overlay_written"] is True
    assert result["pip_executed"] is False
    assert result["pip_command"] is not None
    # Overlay file actually rewritten with the new flag.
    written = yaml.safe_load(fresh_overlay.read_text(encoding="utf-8"))
    assert written["subsystems"]["bridges"]["slack"]["enabled"] is True


def test_apply_plan_runs_pip_when_consented(
    fresh_overlay: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "slack_sdk", None)
    calls: list[list[str]] = []

    class _Done:
        returncode = 0

    def _record(cmd, check):
        calls.append(cmd)
        return _Done()

    monkeypatch.setattr("brains.install.subprocess.run", _record)
    plan = plan_changes(enable=["slack"])
    result = apply_plan(plan, run_pip=True)
    assert result["pip_executed"] is True
    assert result["pip_returncode"] == 0
    assert len(calls) == 1
    assert calls[0][-1] == "brains-ai[slack]"


# --- CLI surface (via Typer test runner) ----------------------------------


def test_cli_features_status(fresh_overlay: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["features", "--status"])
    assert result.exit_code == 0, result.output
    assert "schema_version" in result.output
    assert "features" in result.output


def test_cli_features_yes_writes_overlay(
    fresh_overlay: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "telegram", None)

    def _fail(*args, **kwargs):
        raise AssertionError("pip should not run without --run-pip")

    monkeypatch.setattr("brains.install.subprocess.run", _fail)
    runner = CliRunner()
    result = runner.invoke(app, ["features", "--features", "telegram", "--yes"])
    assert result.exit_code == 0, result.output
    payload = yaml.safe_load(fresh_overlay.read_text(encoding="utf-8"))
    assert payload["subsystems"]["bridges"]["telegram"]["enabled"] is True


def test_cli_features_dry_run_writes_nothing(
    fresh_overlay: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "telegram", None)
    runner = CliRunner()
    result = runner.invoke(app, ["features", "--enable", "telegram", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not fresh_overlay.exists()


def test_cli_features_and_enable_are_mutually_exclusive(
    fresh_overlay: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["features", "--features", "slack", "--enable", "telegram", "--yes"],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_cli_features_unknown_feature_rejected(
    fresh_overlay: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["features", "--features", "nope", "--yes"])
    assert result.exit_code != 0
    assert "Unknown features" in result.output

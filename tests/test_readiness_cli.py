"""CLI tests for readiness, queue-health, and recovery administration (B8)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from brains.cli.app import app


def test_readiness_cli_prints_status_and_components():
    result = CliRunner().invoke(app, ["readiness"])
    payload = json.loads(result.output)
    assert payload["status"] in ("ready", "degraded")
    assert set(payload["components"]) == {
        "storage",
        "sqlite_integrity",
        "mcp_protocol",
        "queue",
        "durable_mail",
        "recovery_policy",
    }
    # Exit code mirrors the overall verdict so it composes in scripts.
    assert result.exit_code == (0 if payload["status"] == "ready" else 1)


def test_readiness_cli_never_prints_a_raw_exception(monkeypatch):
    import brains.storage.migrations as migrations_module

    monkeypatch.setattr(
        migrations_module,
        "migration_status",
        lambda: (_ for _ in ()).throw(RuntimeError("leaked-secret-xyz")),
    )
    result = CliRunner().invoke(app, ["readiness"])
    assert "leaked-secret-xyz" not in result.output


def test_queue_health_status_cli_reports_summary_and_diagnosis():
    result = CliRunner().invoke(app, ["queue-health", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "summary" in payload and "diagnosis" in payload
    assert "families" in payload["summary"]


def test_queue_health_repair_cli_defaults_to_dry_run():
    result = CliRunner().invoke(app, ["queue-health", "repair"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is False
    assert payload["unresolved_work_preserved"] is True


def test_queue_health_repair_cli_apply_flag_actually_runs():
    result = CliRunner().invoke(app, ["queue-health", "repair", "--apply"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is True
    assert all("applied_rows" in action for action in payload["actions"])


def test_recovery_policy_cli_reports_completeness():
    result = CliRunner().invoke(app, ["recovery-policy"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "ready" in payload
    assert "missing_fields" in payload["policy"]


def test_recovery_drill_refuses_missing_candidate_without_exposing_path(tmp_path, monkeypatch):
    from brains.config import settings

    candidate = tmp_path / "private-candidate-name.tar.gz"
    monkeypatch.setattr(settings, "backup_candidate_path", str(candidate), raising=False)
    result = CliRunner().invoke(app, ["recovery-drill"])
    assert result.exit_code == 1
    assert "candidate-unavailable" in result.output
    assert str(candidate) not in result.output

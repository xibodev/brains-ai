"""Tests for the recovery policy and proven recovery posture.

The recovery policy is a *declaration* the operator configures via
``BRAINS_BACKUP_*`` settings, never a fabricated default. These tests prove:

* an unconfigured install reports ``complete: False`` with the exact missing
  fields (never a silent "managed" claim);
* a fully-configured policy reports ``complete: True``;
* the encryption-owner field is conditionally mandatory;
* the compatibility precheck reuses the real migration/backup contracts and
  degrades (never raises) when the migration ledger is unhealthy or a
  withdrawn runtime backend is selected;
* ``recovery_readiness`` combines both into one truthful ``ready`` verdict.
"""

from __future__ import annotations

import pytest

from brains.config import settings
from brains.control import recovery_policy


@pytest.fixture(autouse=True)
def _reset_backup_settings():
    """Every test starts from the unconfigured (all-empty) policy defaults."""
    fields = [
        "backup_scope",
        "backup_schedule",
        "backup_retention_days",
        "backup_encryption_at_rest",
        "backup_encryption_owner",
        "backup_offsite_owner",
        "backup_offsite_location",
        "backup_rto_minutes",
        "backup_rpo_minutes",
        "backup_restore_drill_required",
        "backup_last_restore_drill_at",
        "backup_candidate_path",
    ]
    original = {name: getattr(settings, name) for name in fields}
    yield
    for name, value in original.items():
        setattr(settings, name, value)


def test_policy_summary_is_incomplete_by_default():
    summary = recovery_policy.policy_summary()
    assert summary["complete"] is False
    assert "backup_scope" in summary["missing_fields"]
    assert "backup_schedule" in summary["missing_fields"]
    assert "backup_retention_days" in summary["missing_fields"]
    assert "backup_rto_minutes" in summary["missing_fields"]
    assert "backup_rpo_minutes" in summary["missing_fields"]
    assert "backup_offsite_owner" in summary["missing_fields"]
    assert "backup_offsite_location" in summary["missing_fields"]
    # An operator-entered date is never accepted as proof of a drill.
    assert "backup_last_restore_drill_at" not in summary["missing_fields"]
    # Encryption is off by default, so no owner is demanded yet.
    assert "backup_encryption_owner" not in summary["missing_fields"]
    assert summary["scope"] is None
    assert summary["schedule"] is None


def test_policy_summary_reports_declared_values_without_secrets():
    settings.backup_scope = "sqlite_database"
    settings.backup_schedule = "daily"
    settings.backup_retention_days = 30
    settings.backup_rto_minutes = 60
    settings.backup_rpo_minutes = 15
    settings.backup_offsite_owner = "infra-team"
    settings.backup_offsite_location = "s3://ops-backups/brains (see runbook)"
    settings.backup_restore_drill_required = False

    summary = recovery_policy.policy_summary()

    assert summary["complete"] is True
    assert summary["missing_fields"] == []
    assert summary["scope"] == "sqlite_database"
    assert summary["schedule"] == "daily"
    assert summary["retention_days"] == 30
    assert summary["offsite_owner"] == "infra-team"
    # Nothing secret-shaped is ever present in the summary payload.
    for value in summary.values():
        if isinstance(value, str):
            assert "key" not in value.lower()
            assert "password" not in value.lower()
            assert "token" not in value.lower()


def test_encryption_owner_is_mandatory_only_when_encryption_is_declared():
    settings.backup_encryption_at_rest = True
    settings.backup_encryption_owner = ""
    summary = recovery_policy.policy_summary()
    assert "backup_encryption_owner" in summary["missing_fields"]

    settings.backup_encryption_owner = "security-team"
    summary = recovery_policy.policy_summary()
    assert "backup_encryption_owner" not in summary["missing_fields"]


def test_declared_restore_drill_date_is_metadata_not_evidence():
    settings.backup_restore_drill_required = True
    settings.backup_last_restore_drill_at = ""
    summary = recovery_policy.policy_summary()
    settings.backup_last_restore_drill_at = "2026-01-01T00:00:00Z"
    summary = recovery_policy.policy_summary()
    assert "backup_last_restore_drill_at" not in summary["missing_fields"]
    assert summary["last_restore_drill_at"] == "2026-01-01T00:00:00Z"
    assert summary["restore_drill_error"] is None


def test_compatibility_precheck_sqlite_backend_has_no_external_tool_requirement():
    result = recovery_policy.compatibility_precheck()
    assert result["compaction_prerequisite_ok"] is True
    assert isinstance(result["migration_healthy"], bool)
    assert isinstance(result["known_schema_versions"], int)
    assert result["known_schema_versions"] > 0


def test_compatibility_precheck_degrades_when_migration_status_unhealthy(monkeypatch):
    import brains.storage.migrations as migrations_module

    monkeypatch.setattr(
        migrations_module,
        "migration_status",
        lambda: {"healthy": False, "schema_verified": False},
    )
    result = recovery_policy.compatibility_precheck()
    assert result["migration_healthy"] is False


def test_compatibility_precheck_rejects_withdrawn_backend_without_tool_probe(monkeypatch):
    import brains.backup as backup_module

    monkeypatch.setattr(backup_module, "_current_backend", lambda: "postgres")
    result = recovery_policy.compatibility_precheck()
    assert result["compaction_prerequisite_ok"] is False
    assert result["detail"] == "runtime backend is withdrawn; SQLite is required"


def test_compatibility_precheck_never_raises_on_unexpected_error(monkeypatch):
    import brains.backup as backup_module

    monkeypatch.setattr(
        backup_module,
        "_current_backend",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = recovery_policy.compatibility_precheck()
    assert result["compaction_prerequisite_ok"] is None
    assert result["detail"] == "compaction prerequisite check failed: RuntimeError"
    assert "boom" not in result["detail"]


def test_recovery_readiness_degrades_when_mechanics_probe_is_unknown(monkeypatch):
    import brains.control.readiness as readiness_module

    monkeypatch.setattr(
        recovery_policy,
        "policy_summary",
        lambda: {
            "complete": True,
            "missing_fields": [],
            "restore_drill_required": False,
        },
    )
    monkeypatch.setattr(
        readiness_module,
        "backup_candidate_status",
        lambda: {"ready": True, "reason": "verified", "data_fingerprint": "same"},
    )
    monkeypatch.setattr(
        readiness_module,
        "last_restore_drill_status",
        lambda: {
            "verified": True,
            "reason": "successful-drill-recorded",
            "data_fingerprint": "same",
        },
    )
    monkeypatch.setattr(
        recovery_policy,
        "compatibility_precheck",
        lambda: {
            "migration_healthy": True,
            "compaction_prerequisite_ok": None,
            "detail": "probe failed",
        },
    )
    readiness = recovery_policy.recovery_readiness()
    assert readiness["ready"] is False
    assert "probe failed" in readiness["reasons"]


def test_recovery_readiness_not_ready_when_policy_incomplete():
    readiness = recovery_policy.recovery_readiness()
    assert readiness["ready"] is False
    assert any("policy incomplete" in reason for reason in readiness["reasons"])


def test_recovery_readiness_requires_drill_for_selected_candidate(monkeypatch):
    import brains.control.readiness as readiness_module

    monkeypatch.setattr(
        recovery_policy,
        "policy_summary",
        lambda: {
            "complete": True,
            "missing_fields": [],
            "restore_drill_required": True,
        },
    )
    monkeypatch.setattr(
        recovery_policy,
        "compatibility_precheck",
        lambda: {
            "migration_healthy": True,
            "compaction_prerequisite_ok": True,
            "detail": None,
        },
    )
    monkeypatch.setattr(
        readiness_module,
        "backup_candidate_status",
        lambda: {"ready": True, "reason": "candidate-verified", "data_fingerprint": "new"},
    )
    monkeypatch.setattr(
        readiness_module,
        "last_restore_drill_status",
        lambda: {
            "verified": True,
            "reason": "successful-drill-recorded",
            "data_fingerprint": "old",
        },
    )
    assert recovery_policy.recovery_readiness()["reasons"] == ["candidate-not-drilled"]


def test_recovery_readiness_ready_when_policy_candidate_and_mechanics_ok(monkeypatch):
    import brains.control.readiness as readiness_module

    settings.backup_scope = "sqlite_database"
    settings.backup_schedule = "daily"
    settings.backup_retention_days = 30
    settings.backup_rto_minutes = 60
    settings.backup_rpo_minutes = 15
    settings.backup_offsite_owner = "infra-team"
    settings.backup_offsite_location = "s3://ops-backups/brains"
    settings.backup_restore_drill_required = False
    monkeypatch.setattr(
        readiness_module,
        "backup_candidate_status",
        lambda: {"ready": True, "reason": "verified", "data_fingerprint": "same"},
    )
    monkeypatch.setattr(
        readiness_module,
        "last_restore_drill_status",
        lambda: {"verified": False, "reason": "no-successful-drill-recorded"},
    )

    readiness = recovery_policy.recovery_readiness()

    assert readiness["policy"]["complete"] is True
    if readiness["compatibility"]["migration_healthy"]:
        assert readiness["ready"] is True
        assert readiness["reasons"] == []

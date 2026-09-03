"""Recovery policy and proven recovery posture.

Brains does not run a backup scheduler itself: an install's *managed
recovery* is a declared policy (scope, schedule, retention, encryption
expectation/owner, RTO/RPO, an offsite owner/location pointer, and a restore
drill requirement) that an external scheduler invoking ``brains-ai backup``
is expected to honour. This module is the single place that:

* reports the policy back, redacted, with an honest completeness verdict
  (:func:`policy_summary`) - an install that configured nothing gets
  ``complete: False`` and the exact missing fields, never a fabricated
  "managed" claim;
* reuses :mod:`brains.backup` and :mod:`brains.storage.migrations` to prove
  the SQLite mechanics and configured restore candidate a real recovery uses;
* combines both into the single ``ready`` / ``degraded`` verdict the
  readiness surface (B8) reports (:func:`recovery_readiness`).

Every value surfaced here is a pointer, description, boolean, or number the
operator explicitly configured via ``BRAINS_BACKUP_*`` env vars - never a
secret. The offsite location/owner fields are descriptions ("S3 bucket
`ops-backups`, owned by infra-team"), not credentials; the credential itself
lives in that store's own secret management, never in brains config.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from brains.config import settings

#: Fields that are mandatory regardless of how the policy is configured.
_ALWAYS_MANDATORY: tuple[str, ...] = (
    "backup_scope",
    "backup_schedule",
    "backup_retention_days",
    "backup_rto_minutes",
    "backup_rpo_minutes",
    "backup_offsite_owner",
    "backup_offsite_location",
)


def _declared_restore_drill_error() -> str | None:
    value = settings.backup_last_restore_drill_at
    if not value:
        return "missing"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "must be an ISO-8601 timestamp"
    if parsed.tzinfo is None:
        return "must include a timezone"
    if parsed.astimezone(UTC) > datetime.now(UTC):
        return "must not be in the future"
    return None


def _missing_fields() -> list[str]:
    """Every mandatory recovery-policy field that is still unset.

    ``backup_encryption_owner`` is mandatory only when the operator has
    committed to encryption at rest. Drill evidence is operational truth,
    not a policy field, and is evaluated separately from this declaration.
    """
    missing: list[str] = []
    if not settings.backup_scope:
        missing.append("backup_scope")
    if not settings.backup_schedule:
        missing.append("backup_schedule")
    if settings.backup_retention_days <= 0:
        missing.append("backup_retention_days")
    if settings.backup_rto_minutes <= 0:
        missing.append("backup_rto_minutes")
    if settings.backup_rpo_minutes <= 0:
        missing.append("backup_rpo_minutes")
    if not settings.backup_offsite_owner:
        missing.append("backup_offsite_owner")
    if not settings.backup_offsite_location:
        missing.append("backup_offsite_location")
    if settings.backup_encryption_at_rest and not settings.backup_encryption_owner:
        missing.append("backup_encryption_owner")
    return missing


def policy_summary() -> dict[str, Any]:
    """Redacted recovery policy: every declared value plus completeness.

    ``complete`` is ``True`` only when every mandatory field (see
    :func:`_missing_fields`) is set. Unset string/int fields are reported as
    ``None`` rather than as their empty/zero sentinel, so a JSON consumer
    never has to know the sentinel convention.
    """
    missing = _missing_fields()
    return {
        "scope": settings.backup_scope or None,
        "schedule": settings.backup_schedule or None,
        "retention_days": settings.backup_retention_days or None,
        "encryption_at_rest": settings.backup_encryption_at_rest,
        "encryption_owner": settings.backup_encryption_owner or None,
        "offsite_owner": settings.backup_offsite_owner or None,
        "offsite_location": settings.backup_offsite_location or None,
        "rto_minutes": settings.backup_rto_minutes or None,
        "rpo_minutes": settings.backup_rpo_minutes or None,
        "restore_drill_required": settings.backup_restore_drill_required,
        "last_restore_drill_at": settings.backup_last_restore_drill_at or None,
        "restore_drill_error": (
            _declared_restore_drill_error() if settings.backup_restore_drill_required else None
        ),
        "complete": not missing,
        "missing_fields": missing,
    }


def compatibility_precheck() -> dict[str, Any]:
    """Prove the mechanics a managed policy depends on, without running one.

    Reuses migration status and the shipped SQLite backup implementation.
    Alternate runtime backends are withdrawn and never probed or advertised.
    """
    from brains.storage.migrations import (
        current_schema_versions,
        known_migration_ids,
        migration_status,
    )

    status = migration_status()
    result: dict[str, Any] = {
        "migration_healthy": bool(status.get("healthy")) and bool(status.get("schema_verified")),
        "known_schema_versions": len(known_migration_ids()),
        "applied_schema_versions": len(current_schema_versions()),
        "compaction_prerequisite_ok": None,
        "detail": None,
    }
    try:
        from brains.backup import _current_backend

        backend = _current_backend()
        if backend == "sqlite":
            # This build always takes a live ``sqlite3.Connection.backup()``
            # image (see ``brains.backup._sqlite_backup_image``); no external
            # tool is required, so the prerequisite is met by definition.
            result["compaction_prerequisite_ok"] = True
        else:
            result["compaction_prerequisite_ok"] = False
            result["detail"] = "runtime backend is withdrawn; SQLite is required"
    except Exception as exc:  # pragma: no cover - defensive; never raise from a probe
        result["detail"] = f"compaction prerequisite check failed: {type(exc).__name__}"
    return result


def recovery_readiness() -> dict[str, Any]:
    """Combined policy-completeness + mechanics verdict for readiness (B8).

    ``ready`` is ``True`` only when the declared policy is complete AND the
    mechanics precheck passed, a compatible candidate is verified, and any
    required drill has a successful audit record. Operator-entered timestamps
    are declarations only and never become drill evidence.
    """
    policy = policy_summary()
    compat = compatibility_precheck()
    from brains.control.readiness import backup_candidate_status, last_restore_drill_status

    candidate = backup_candidate_status()
    last_drill = last_restore_drill_status()
    reasons: list[str] = []
    if not policy["complete"]:
        reasons.append("recovery policy incomplete: missing " + ", ".join(policy["missing_fields"]))
    if not compat["migration_healthy"]:
        reasons.append("storage/migration state is not healthy")
    if compat["compaction_prerequisite_ok"] is not True:
        reasons.append(compat.get("detail") or "compaction prerequisite failed")
    if not candidate["ready"]:
        reasons.append(candidate["reason"])
    if policy["restore_drill_required"] and not last_drill["verified"]:
        reasons.append(last_drill["reason"])
    elif policy["restore_drill_required"] and candidate.get("data_fingerprint") != last_drill.get(
        "data_fingerprint"
    ):
        reasons.append("candidate-not-drilled")
    return {
        "ready": not reasons,
        "policy": policy,
        "compatibility": compat,
        "candidate": candidate,
        "last_drill": last_drill,
        "reasons": reasons,
    }


__all__ = [
    "compatibility_precheck",
    "policy_summary",
    "recovery_readiness",
]

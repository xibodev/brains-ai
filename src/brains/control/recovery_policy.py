"""Recovery policy — BL-P1-09.

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
  the *mechanics* a real backup would depend on actually work - schema
  compatibility, migration health, and (for Postgres) the required CLI
  tools - without ever running a real backup or inventing a schedule
  (:func:`compatibility_precheck`);
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


def _restore_drill_error() -> str | None:
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

    ``backup_encryption_owner`` is only mandatory when the operator has
    committed to encryption at rest; ``backup_last_restore_drill_at`` is only
    mandatory when a drill is required (the default). Both default to the
    stricter "required" position so an install cannot silently opt out by
    doing nothing.
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
    if settings.backup_restore_drill_required and _restore_drill_error() is not None:
        missing.append("backup_last_restore_drill_at")
    return missing


def policy_summary() -> dict[str, Any]:
    """Redacted recovery policy: every declared value plus completeness.

    ``complete`` is ``True`` only when every mandatory field (see
    :func:`_missing_fields`) is set. Unset string/int fields are reported as
    ``None`` rather than as their empty/zero sentinel, so a JSON consumer
    never has to know the sentinel convention.
    """
    missing = _missing_fields()
    drill_error = _restore_drill_error() if settings.backup_restore_drill_required else None
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
        "restore_drill_error": drill_error,
        "complete": not missing,
        "missing_fields": missing,
    }


def compatibility_precheck() -> dict[str, Any]:
    """Prove the mechanics a managed policy depends on, without running one.

    Reuses the existing migration/backup contracts instead of inventing a new
    check: :func:`brains.storage.migrations.migration_status` for schema
    health, and, for Postgres, :mod:`brains.backup`'s own ``pg_dump``/
    ``pg_restore`` tool-presence check (:func:`brains.backup._require_tool`) -
    the same gate a real backup/restore would hit.
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
        from brains.backup import BackupToolUnavailable, _current_backend, _require_tool

        backend = _current_backend()
        if backend == "sqlite":
            # This build always takes a live ``sqlite3.Connection.backup()``
            # image (see ``brains.backup._sqlite_backup_image``); no external
            # tool is required, so the prerequisite is met by definition.
            result["compaction_prerequisite_ok"] = True
        elif backend == "postgres":
            try:
                _require_tool("pg_dump")
                _require_tool("pg_restore")
                result["compaction_prerequisite_ok"] = True
            except BackupToolUnavailable as exc:
                result["compaction_prerequisite_ok"] = False
                result["detail"] = str(exc)
        else:
            result["detail"] = f"unknown storage backend {backend!r}"
    except Exception as exc:  # pragma: no cover - defensive; never raise from a probe
        result["detail"] = f"compaction prerequisite check failed: {type(exc).__name__}"
    return result


def recovery_readiness() -> dict[str, Any]:
    """Combined policy-completeness + mechanics verdict for readiness (B8).

    ``ready`` is ``True`` only when the declared policy is complete AND the
    mechanics precheck passed. An install that configured nothing, or whose
    storage/migration state is unhealthy, or whose Postgres backend is
    missing ``pg_dump``/``pg_restore``, is reported not-ready with the exact
    reasons - never silently upgraded to "managed".
    """
    policy = policy_summary()
    compat = compatibility_precheck()
    reasons: list[str] = []
    if not policy["complete"]:
        reasons.append("recovery policy incomplete: missing " + ", ".join(policy["missing_fields"]))
    if not compat["migration_healthy"]:
        reasons.append("storage/migration state is not healthy")
    if compat["compaction_prerequisite_ok"] is not True:
        reasons.append(compat.get("detail") or "compaction prerequisite failed")
    return {
        "ready": not reasons,
        "policy": policy,
        "compatibility": compat,
        "reasons": reasons,
    }


__all__ = [
    "compatibility_precheck",
    "policy_summary",
    "recovery_readiness",
]

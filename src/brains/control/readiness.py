"""Bounded, secret-free readiness probes for the supported local topology."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from brains.config import settings


def sqlite_integrity_status() -> dict[str, Any]:
    """Run real SQLite quick, full integrity, and foreign-key checks."""
    from brains.storage.integrity import (
        UnsupportedDatabaseError,
        open_database,
        resolve_sqlite_path,
    )

    try:
        database = resolve_sqlite_path()
        if not database.is_file():
            return {"ready": False, "reason": "database-unavailable"}
        with open_database(database, read_only=True) as connection:
            quick = all(str(row[0]) == "ok" for row in connection.execute("PRAGMA quick_check"))
            full = all(str(row[0]) == "ok" for row in connection.execute("PRAGMA integrity_check"))
            foreign_keys = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
    except UnsupportedDatabaseError:
        return {"ready": False, "reason": "unsupported-runtime-backend"}
    except (OSError, sqlite3.DatabaseError):
        return {"ready": False, "reason": "sqlite-check-failed"}
    except Exception:
        return {"ready": False, "reason": "sqlite-probe-failed"}

    if not quick:
        reason = "quick-check-failed"
    elif not full:
        reason = "integrity-check-failed"
    elif foreign_keys:
        reason = "foreign-key-violations"
    else:
        reason = "checks-succeeded"
    return {
        "ready": reason == "checks-succeeded",
        "reason": reason,
        "quick_check_ok": quick,
        "integrity_check_ok": full,
        "foreign_key_violations": foreign_keys,
    }


def backup_candidate_status(candidate: str | Path | None = None) -> dict[str, Any]:
    """Validate the configured restore candidate without exposing its path."""
    from brains.backup import BackupError, verify_backup

    raw = str(candidate or settings.backup_candidate_path or "").strip()
    if not raw:
        return {"ready": False, "configured": False, "reason": "candidate-not-configured"}
    try:
        archive = Path(raw).expanduser()
        if not archive.is_file():
            return {"ready": False, "configured": True, "reason": "candidate-unavailable"}
        verification = verify_backup(archive)
    except (BackupError, OSError, ValueError):
        return {"ready": False, "configured": True, "reason": "candidate-unreadable"}
    if not verification.ok:
        compatibility = verification.checks.get("schema_compatibility") or {}
        reason = (
            "candidate-schema-incompatible"
            if compatibility.get("unknown_migrations")
            else "candidate-verification-failed"
        )
        return {"ready": False, "configured": True, "reason": reason}
    return {
        "ready": True,
        "configured": True,
        "reason": "candidate-verified",
        "backend": verification.backend,
        "data_fingerprint": verification.checks.get("data_sha256"),
    }


def last_restore_drill_status() -> dict[str, Any]:
    """Report only a successfully audited isolated recovery drill."""
    try:
        from brains.audit import assert_chain_intact, list_entries

        assert_chain_intact()
        rows = list_entries(limit=1000, action_prefix="admin.recovery_drill")
    except Exception:
        return {"verified": False, "reason": "drill-evidence-unavailable", "at": None}
    completed = next(
        (
            row
            for row in rows
            if row.get("action") == "admin.recovery_drill"
            and (row.get("payload") or {}).get("candidate_verified") is True
            and (row.get("payload") or {}).get("restore_verified") is True
            and (row.get("payload") or {}).get("rollback_verified") is True
            and isinstance((row.get("payload") or {}).get("data_fingerprint"), str)
        ),
        None,
    )
    if completed is None:
        return {"verified": False, "reason": "no-successful-drill-recorded", "at": None}
    return {
        "verified": True,
        "reason": "successful-drill-recorded",
        "at": completed.get("created_at"),
        "data_fingerprint": completed["payload"]["data_fingerprint"],
    }


def mcp_protocol_readiness() -> dict[str, Any]:
    """Probe the configured authenticated Streamable HTTP MCP lifecycle."""
    try:
        from brains.service.common import mcp_protocol_status, read_service_config

        configured = read_service_config()
        report = mcp_protocol_status(
            str(configured["gateway_host"]), int(configured["mcp_port"]), timeout=1.0
        )
    except Exception:
        return {"ready": False, "stage": "probe", "reason": "probe-failed"}
    bounded = {
        "ready": bool(report.get("ready")),
        "stage": str(report.get("stage") or "probe"),
        "reason": str(report.get("reason") or "probe-failed"),
    }
    if isinstance(report.get("tool_count"), int):
        bounded["tool_count"] = report["tool_count"]
    if isinstance(report.get("status_code"), int):
        bounded["status_code"] = report["status_code"]
    return bounded


def perform_restore_drill(candidate: str | Path) -> dict[str, Any]:
    """Restore a candidate and prove rollback capture in disposable state."""
    from brains.backup import restore_backup, verify_backup

    with TemporaryDirectory(prefix="brains-restore-drill-") as directory:
        target = Path(directory) / "restored.sqlite"
        target_url = f"sqlite:///{target.as_posix()}"
        restore_backup(candidate, target_url=target_url)
        result = restore_backup(candidate, target_url=target_url)
        rollback_verified = bool(
            result.rollback_archive_path and verify_backup(result.rollback_archive_path).ok
        )
        ready = result.candidate_verified and result.post_restore_verified and rollback_verified
        return {
            "ready": ready,
            "reason": "restore-drill-succeeded" if ready else "restore-drill-failed",
            "backend": result.backend,
            "rollback_verified": rollback_verified,
        }


__all__ = [
    "backup_candidate_status",
    "last_restore_drill_status",
    "mcp_protocol_readiness",
    "perform_restore_drill",
    "sqlite_integrity_status",
]

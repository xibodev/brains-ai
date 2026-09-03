"""Operator-facing operational projections shared by HTTP and CLI adapters."""

from __future__ import annotations

from typing import Any


def readiness_report() -> dict[str, Any]:
    """Return the protected ready/degraded contract without exposing raw errors."""
    components: dict[str, dict[str, Any]] = {}

    try:
        from brains.storage.migrations import migration_status

        status = migration_status()
        healthy = bool(status.get("healthy")) and bool(status.get("schema_verified"))
        components["storage"] = {
            "state": "ready" if healthy else "degraded",
            "detail": {
                "backend": status.get("backend"),
                "healthy": status.get("healthy"),
                "schema_verified": status.get("schema_verified"),
                "pending": len(status.get("pending") or []),
                "failed": len(status.get("failed") or []),
            },
        }
    except Exception as exc:  # pragma: no cover - readiness must remain bounded
        components["storage"] = {"state": "degraded", "detail": {"error": type(exc).__name__}}

    from brains.control.readiness import (
        mcp_protocol_readiness,
        sqlite_integrity_status,
    )

    try:
        sqlite_health = sqlite_integrity_status()
        components["sqlite_integrity"] = {
            "state": "ready" if sqlite_health["ready"] else "degraded",
            "detail": sqlite_health,
        }
    except Exception as exc:  # pragma: no cover - readiness must remain bounded
        components["sqlite_integrity"] = {
            "state": "degraded",
            "detail": {"error": type(exc).__name__},
        }

    try:
        mcp_health = mcp_protocol_readiness()
        components["mcp_protocol"] = {
            "state": "ready" if mcp_health["ready"] else "degraded",
            "detail": mcp_health,
        }
    except Exception as exc:  # pragma: no cover - readiness must remain bounded
        components["mcp_protocol"] = {
            "state": "degraded",
            "detail": {"error": type(exc).__name__},
        }

    try:
        from brains.control.queue_health import summarize

        queue = summarize()
        stale_total = sum(family["stale_or_expired"] for family in queue["families"].values())
        components["queue"] = {
            "state": "ready" if stale_total == 0 else "degraded",
            "detail": {
                "stale_or_expired_total": stale_total,
                "families": len(queue["families"]),
            },
        }
    except Exception as exc:  # pragma: no cover - readiness must remain bounded
        components["queue"] = {"state": "degraded", "detail": {"error": type(exc).__name__}}

    try:
        from brains.control.mailbox_observability import mailbox_health_report

        mailbox = mailbox_health_report()
        components["durable_mail"] = {
            "state": mailbox["state"],
            "detail": mailbox,
        }
    except Exception as exc:  # pragma: no cover - readiness must remain bounded
        components["durable_mail"] = {
            "state": "degraded",
            "detail": {"error": type(exc).__name__},
        }

    try:
        from brains.control.recovery_policy import recovery_readiness

        recovery = recovery_readiness()
        components["recovery_policy"] = {
            "state": "ready" if recovery["ready"] else "degraded",
            "detail": {
                "complete": recovery["policy"]["complete"],
                "missing_fields": recovery["policy"]["missing_fields"],
                "candidate": recovery["candidate"],
                "last_drill": recovery["last_drill"],
                "reasons": recovery["reasons"],
            },
        }
    except Exception as exc:  # pragma: no cover - readiness must remain bounded
        components["recovery_policy"] = {
            "state": "degraded",
            "detail": {"error": type(exc).__name__},
        }

    overall = "ready" if all(row["state"] == "ready" for row in components.values()) else "degraded"
    return {"status": overall, "components": components}


def operations_snapshot() -> dict[str, Any]:
    """Install-admin projection for the Operations screen.

    Read-only. Host mutations require separate typed contracts and are reported
    as capabilities rather than executed here.
    """
    from brains import service
    from brains.control.queue_health import diagnose, summarize
    from brains.control.recovery_policy import recovery_readiness

    return {
        "readiness": readiness_report(),
        "queue": {"summary": summarize(), "diagnosis": diagnose()},
        "recovery": recovery_readiness(),
        "service": service.status(),
    }


__all__ = ["operations_snapshot", "readiness_report"]

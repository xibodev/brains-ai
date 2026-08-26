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
        from brains.control.runtimes import count_stale, list_runtimes
        from brains.mcp.server import _runtime_stale_ttl_seconds

        ttl = _runtime_stale_ttl_seconds()
        runtimes = list_runtimes()
        stale = count_stale(ttl)
        components["runtime_lifecycle"] = {
            "state": "ready" if stale == 0 else "degraded",
            "detail": {
                "total": len(runtimes),
                "online": len([row for row in runtimes if row.get("status") == "online"]),
                "stale_pending_sweep": stale,
                "stale_sweep_ttl_seconds": ttl,
            },
        }
    except Exception as exc:  # pragma: no cover - readiness must remain bounded
        components["runtime_lifecycle"] = {
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
    from brains.control.operators import list_operators
    from brains.control.queue_health import diagnose, summarize
    from brains.control.recovery_policy import recovery_readiness
    from brains.control.runtimes import list_runtimes
    from brains.control.tool_registry import list_registered_tools
    from brains.experimental import ui_labs_enabled

    return {
        "readiness": readiness_report(),
        "queue": {"summary": summarize(), "diagnosis": diagnose()},
        "recovery": recovery_readiness(),
        "service": service.status(),
        "runtimes": list_runtimes(),
        "tools": list_registered_tools(verify_now=False),
        "operators": list_operators(),
        "labs_enabled": ui_labs_enabled(),
    }


__all__ = ["operations_snapshot", "readiness_report"]

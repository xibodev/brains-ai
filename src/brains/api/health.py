from fastapi import APIRouter

from brains.bridges import enabled_bridges
from brains.config import RUNTIME_OVERLAY_SCHEMA_VERSION, settings
from brains.extras import installed_extras
from brains.observability import status as otel_status

router = APIRouter()


@router.get("/health")
def health() -> dict[str, object]:
    """Liveness probe + lightweight subsystem-inventory snapshot.

    Returns the install state of every declared pip extra and the names of
    every enabled bridge. Used by ``brains health`` (CLI), the dashboard
    overview, and uptime monitors. Intentionally returns 200 even when an
    extra is missing — health reports state; it does not gate on it.
    """
    otel_snapshot = otel_status(settings)
    return {
        "status": "ok",
        "schema_version": RUNTIME_OVERLAY_SCHEMA_VERSION,
        "extras": installed_extras(),
        "subsystems": {
            "storage_backend": settings.subsystems.storage.backend,
            "bridges_enabled": enabled_bridges(settings),
            "otel_enabled": settings.subsystems.otel.enabled,
            "otel": {
                "enabled": otel_snapshot.enabled,
                "configured": otel_snapshot.configured,
                "endpoint": otel_snapshot.endpoint,
                "service_name": otel_snapshot.service_name,
                "detail": otel_snapshot.detail,
            },
        },
    }

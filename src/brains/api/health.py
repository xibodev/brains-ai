from fastapi import APIRouter

from brains.config import RUNTIME_OVERLAY_SCHEMA_VERSION

router = APIRouter()


@router.get("/health")
def health() -> dict[str, object]:
    """Process liveness only; readiness is a separate protected probe."""
    return {
        "status": "ok",
        "schema_version": RUNTIME_OVERLAY_SCHEMA_VERSION,
    }

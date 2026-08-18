from fastapi.testclient import TestClient

from brains.config import RUNTIME_OVERLAY_SCHEMA_VERSION
from brains.main import app


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


def test_health_reports_extras_and_subsystems() -> None:
    body = TestClient(app).get("/health").json()
    assert body["schema_version"] == RUNTIME_OVERLAY_SCHEMA_VERSION
    extras = body["extras"]
    # The lean-core install can run without any extras, but every declared
    # extra must appear in the inventory so the dashboard / wizard can
    # render a complete checklist.
    for name in ("litellm", "postgres", "telegram", "slack", "whatsapp", "whatsapp_web", "otel"):
        assert name in extras
        assert isinstance(extras[name], bool)
    subs = body["subsystems"]
    assert subs["storage_backend"] in {"sqlite", "postgres"}
    assert isinstance(subs["bridges_enabled"], list)
    assert isinstance(subs["otel_enabled"], bool)

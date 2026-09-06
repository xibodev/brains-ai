from fastapi.testclient import TestClient

from brains.config import RUNTIME_OVERLAY_SCHEMA_VERSION
from brains.main import app


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


def test_health_does_not_advertise_withdrawn_extras_or_subsystems() -> None:
    body = TestClient(app).get("/health").json()
    assert body["schema_version"] == RUNTIME_OVERLAY_SCHEMA_VERSION
    assert set(body) == {"status", "schema_version"}

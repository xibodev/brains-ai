import uuid

from fastapi.testclient import TestClient

from brains.main import app


def test_auth_is_enforced_on_v1_routes():
    response = TestClient(app).get("/v1/models")
    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["type"] == "unauthorized_error"


def test_validation_errors_are_standardized(auth_headers):
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "brains-auto", "messages": "invalid"},
    )
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["type"] == "invalid_request_error"


def test_trace_payload_redaction(auth_headers, monkeypatch):
    marker = f"safe-visible-marker-{uuid.uuid4()}"
    secret = "super-secret-value"
    captured = {}

    def fake_write_trace(_: str, payload: str):
        captured["payload"] = payload

    monkeypatch.setattr("brains.api.openai.write_trace", fake_write_trace)
    monkeypatch.setattr("brains.api.openai.write_route", lambda *_: None)

    response = TestClient(app).post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "brains-auto",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "probe",
                        "parameters": {"note": marker, "api_key": secret},
                    },
                }
            ],
        },
    )
    assert response.status_code == 200

    assert marker in captured["payload"]
    assert secret not in captured["payload"]
    assert "[REDACTED]" in captured["payload"]

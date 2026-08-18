from fastapi.testclient import TestClient

from brains.main import app


def test_responses_wrapper(auth_headers):
    client = TestClient(app)
    response = client.post(
        "/v1/responses",
        headers=auth_headers,
        json={"model": "brains-auto", "input": "hello"},
    )
    assert response.status_code == 200


def test_anthropic_messages_wrapper(auth_headers):
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        headers=auth_headers,
        json={"model": "brains-auto", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert response.json()["type"] == "message"

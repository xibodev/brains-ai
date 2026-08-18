from fastapi.testclient import TestClient

from brains.main import app


def test_models(auth_headers):
    assert TestClient(app).get("/v1/models", headers=auth_headers).status_code == 200


def test_chat(auth_headers):
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "brains-auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200


def test_stream(auth_headers):
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "brains-auto",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert response.status_code == 200


def _capturing_route(captured: dict):
    class _Cap:
        def complete(self, model, messages, **kwargs):
            captured["messages"] = messages
            return {
                "id": "x",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }

    return {"provider": _Cap(), "provider_name": "cap", "model": "m", "tier": "default"}


def test_gateway_preamble_prepended_to_provider(monkeypatch, auth_headers):
    """When gateway_preamble is set, the provider sees it as the first system message."""
    from brains.api import openai as openai_mod
    from brains.config import settings

    captured: dict = {}
    monkeypatch.setattr(openai_mod, "select_model", lambda _c, **_kw: _capturing_route(captured))
    monkeypatch.setattr(settings, "gateway_preamble", "ALWAYS CONSULT BRAINS FIRST")

    resp = TestClient(app).post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert captured["messages"][0] == {
        "role": "system",
        "content": "ALWAYS CONSULT BRAINS FIRST",
    }
    assert captured["messages"][-1]["content"] == "hi"


def test_no_preamble_when_unset(monkeypatch, auth_headers):
    """Default (empty) preamble must leave the provider messages untouched."""
    from brains.api import openai as openai_mod
    from brains.config import settings

    captured: dict = {}
    monkeypatch.setattr(openai_mod, "select_model", lambda _c, **_kw: _capturing_route(captured))
    monkeypatch.setattr(settings, "gateway_preamble", "")

    resp = TestClient(app).post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert all(m["role"] != "system" for m in captured["messages"])
    assert captured["messages"][0]["content"] == "hi"


def test_chat_response_omits_brains_block_by_default(monkeypatch, auth_headers):
    """Default OpenAI wire MUST NOT carry the non-standard
    ``response.brains`` debug block. That block leaked classifier
    internals + workspace plan (active_handoffs, active_claims,
    available_tasks, internal directive language like
    'ignore active workspace claims before editing') to every API
    client and was inconsistent with the Anthropic facade which
    never carried it. Off-by-default; opt-in via
    ``router.expose_classifier_in_response``."""
    from brains.api import openai as openai_mod

    captured: dict = {}
    monkeypatch.setattr(openai_mod, "select_model", lambda _c, **_kw: _capturing_route(captured))

    resp = TestClient(app).post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "brains" not in body, (
        f"response leaks internal brains debug payload: {body.get('brains')!r}"
    )


def test_chat_response_includes_brains_block_when_opted_in(monkeypatch, auth_headers):
    """When the operator opts in via
    ``settings.router.expose_classifier_in_response=True``, the
    ``response.brains`` debug block is restored (with classification
    + plan) for dashboards/clients that already consume it."""
    from brains.api import openai as openai_mod
    from brains.config import settings

    captured: dict = {}
    monkeypatch.setattr(openai_mod, "select_model", lambda _c, **_kw: _capturing_route(captured))
    monkeypatch.setattr(settings.router, "expose_classifier_in_response", True)

    resp = TestClient(app).post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "brains" in body
    assert "classification" in body["brains"]
    assert "plan" in body["brains"]


def test_provider_invocation_error_does_not_leak_provider_name(monkeypatch, auth_headers):
    """Client-facing 502 must NOT include the upstream provider name or
    raw exception text — both can carry internal URLs, model ids, or
    auth-shaped strings. Operator gets the full picture via the log."""
    from brains.api import openai as openai_mod
    from brains.providers.registry import ProviderInvocationError

    class _Boom:
        def complete(self, *_a, **_kw):
            raise ProviderInvocationError(
                "401 from https://api.github.com/copilot_internal/v2/token: leaked-secret-xyz"
            )

    monkeypatch.setattr(
        openai_mod,
        "select_model",
        lambda _c, **_kw: {
            "provider": _Boom(),
            "provider_name": "github_copilot",
            "model": "claude-haiku-4.5",
            "tier": "default",
        },
    )

    resp = TestClient(app).post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    body = resp.json()
    detail = str(body).lower()
    assert "github_copilot" not in detail
    assert "leaked-secret-xyz" not in detail
    assert "api.github.com" not in detail

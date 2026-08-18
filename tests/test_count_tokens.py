"""Tests for ``POST /v1/messages/count_tokens`` — Anthropic gateway spec.

count_tokens is the cheap pre-flight every Claude Code client makes
to size a prompt. The contract: returns ``{"input_tokens": int}``,
never calls upstream, requires auth.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from brains.main import app


def test_count_tokens_requires_auth():
    response = TestClient(app).post(
        "/v1/messages/count_tokens",
        json={"model": "claude-sonnet-4.5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code in (401, 403)


def test_count_tokens_returns_input_tokens(auth_headers):
    response = TestClient(app).post(
        "/v1/messages/count_tokens",
        headers=auth_headers,
        json={
            "model": "claude-sonnet-4.5",
            "messages": [{"role": "user", "content": "hello world"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"input_tokens"}
    assert isinstance(body["input_tokens"], int)
    assert body["input_tokens"] >= 1


def test_count_tokens_walks_typed_content_blocks(auth_headers):
    response = TestClient(app).post(
        "/v1/messages/count_tokens",
        headers=auth_headers,
        json={
            "model": "claude-sonnet-4.5",
            "system": [
                {"type": "text", "text": "you are helpful"},
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "part one "},
                        {"type": "text", "text": "part two"},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["input_tokens"] >= 1


def test_count_tokens_accepts_max_tokens_field(auth_headers):
    """max_tokens is meaningless for count_tokens but Claude Code may
    still send it (same payload shape as /v1/messages). We must
    tolerate it without 422."""
    response = TestClient(app).post(
        "/v1/messages/count_tokens",
        headers=auth_headers,
        json={
            "model": "claude-sonnet-4.5",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200


def test_count_tokens_empty_messages_returns_zero(auth_headers):
    response = TestClient(app).post(
        "/v1/messages/count_tokens",
        headers=auth_headers,
        json={"model": "claude-sonnet-4.5", "messages": []},
    )
    assert response.status_code == 200
    assert response.json()["input_tokens"] == 0


def test_count_tokens_never_calls_provider(auth_headers, monkeypatch):
    """Sanity: count_tokens must not round-trip to any provider."""
    from brains.api import anthropic as anthropic_mod

    # If the endpoint accidentally routed via select_model, calling it
    # would blow up — count_tokens is supposed to be local-only.
    monkeypatch.setattr(anthropic_mod, "select_model", lambda *a, **kw: 1 / 0)

    response = TestClient(app).post(
        "/v1/messages/count_tokens",
        headers=auth_headers,
        json={"model": "claude-sonnet-4.5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200


def test_count_tokens_uses_tiktoken_when_available(auth_headers, monkeypatch):
    """When ``_load_tiktoken_encoder`` returns an encoder, the count
    goes through ``encoder.encode``. We patch the single seam so we
    don't have to mangle sys.modules (which leaks across tests)."""
    from brains.api import anthropic as anthropic_mod

    class FakeEncoder:
        def encode(self, text: str) -> list[int]:
            # Deterministic — 1 token per whitespace-separated word.
            return text.split()

    monkeypatch.setattr(anthropic_mod, "_load_tiktoken_encoder", lambda: FakeEncoder())

    response = TestClient(app).post(
        "/v1/messages/count_tokens",
        headers=auth_headers,
        json={
            "model": "claude-sonnet-4.5",
            "messages": [{"role": "user", "content": "one two three four"}],
        },
    )
    assert response.status_code == 200
    # 4 words → 4 tokens with the fake encoder.
    assert response.json()["input_tokens"] == 4


def test_count_tokens_falls_back_to_heuristic_when_tiktoken_missing(auth_headers, monkeypatch):
    """If ``_load_tiktoken_encoder`` returns ``None``, count uses the
    ``ceil(len(text)/4)`` heuristic."""
    from brains.api import anthropic as anthropic_mod

    monkeypatch.setattr(anthropic_mod, "_load_tiktoken_encoder", lambda: None)

    response = TestClient(app).post(
        "/v1/messages/count_tokens",
        headers=auth_headers,
        json={
            "model": "claude-sonnet-4.5",
            "messages": [{"role": "user", "content": "abcdefgh"}],
        },
    )
    assert response.status_code == 200
    # "abcdefgh" = 8 chars → ceil(8/4) = 2 tokens.
    assert response.json()["input_tokens"] == 2


def test_count_tokens_falls_back_when_encoder_raises(auth_headers, monkeypatch):
    """A runtime failure inside ``encoder.encode`` must also degrade
    gracefully to the heuristic."""
    from brains.api import anthropic as anthropic_mod

    class BrokenEncoder:
        def encode(self, text: str):
            raise RuntimeError("encoder went sideways")

    monkeypatch.setattr(anthropic_mod, "_load_tiktoken_encoder", lambda: BrokenEncoder())

    response = TestClient(app).post(
        "/v1/messages/count_tokens",
        headers=auth_headers,
        json={
            "model": "claude-sonnet-4.5",
            "messages": [{"role": "user", "content": "abcd"}],
        },
    )
    assert response.status_code == 200
    # "abcd" = 4 chars → ceil(4/4) = 1.
    assert response.json()["input_tokens"] == 1

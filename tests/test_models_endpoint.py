"""HTTP tests for the upgraded ``GET /v1/models`` endpoint.

The endpoint used to return a single stub entry; it now serves a real
LLM-gateway discovery surface (brains tier aliases in both dialects +
union of every enabled provider's ``list_models()``).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from brains.main import app


def test_models_endpoint_requires_auth():
    response = TestClient(app).get("/v1/models")
    assert response.status_code in (401, 403)


def test_models_endpoint_returns_openai_shape(auth_headers):
    response = TestClient(app).get("/v1/models", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    for entry in body["data"]:
        assert "id" in entry and isinstance(entry["id"], str)
        assert entry.get("object") == "model"
        assert "owned_by" in entry


def test_models_endpoint_exposes_brains_tier_aliases_in_both_dialects(auth_headers):
    body = TestClient(app).get("/v1/models", headers=auth_headers).json()
    ids = {row["id"] for row in body["data"]}
    expected = {
        # slash form — canonical going forward
        "brains/auto",
        "brains/cheap",
        "brains/default",
        "brains/strong",
        "brains/deep",
        # hyphen form — legacy alias, kept for back-compat
        "brains-auto",
        "brains-cheap",
        "brains-default",
        "brains-strong",
        "brains-deep",
        # claude-prefixed for Claude Code's discovery filter
        "claude-brains-cheap",
        "claude-brains-default",
        "claude-brains-strong",
        "claude-brains-deep",
    }
    missing = expected - ids
    assert not missing, f"missing tier aliases: {sorted(missing)}"
    for alias in expected:
        entry = next(r for r in body["data"] if r["id"] == alias)
        assert entry["owned_by"] == "brains"
        assert "display_name" in entry


def test_models_endpoint_hides_brains_auto_when_routing_disabled(auth_headers, monkeypatch):
    """``brains/auto`` is the only alias gated by ``router.enabled`` —
    listing it when routing is off would advertise an alias that 404s.
    Tier aliases (``brains/cheap`` etc.) keep showing up because they
    don't need the classifier."""
    from brains.api import models as models_mod
    from brains.config import RouterConfig

    monkeypatch.setattr(models_mod.settings, "router", RouterConfig(enabled=False))
    body = TestClient(app).get("/v1/models", headers=auth_headers).json()
    ids = {row["id"] for row in body["data"]}
    assert "brains/auto" not in ids
    assert "brains-auto" not in ids
    # Tier aliases unaffected:
    assert "brains/cheap" in ids
    assert "brains-cheap" in ids


def test_models_endpoint_includes_provider_catalog_entries(auth_headers):
    """Every CONFIGURED real provider's catalog appears in the union,
    namespaced as ``<provider>/<model>`` — independent of tier wiring —
    so real model ids are discoverable and directly usable. Stub
    providers (``echo``) are excluded; they're dev scaffolding, not a
    real model source."""
    from brains.providers.ollama import OllamaProvider

    fake_catalog = [{"id": "llama3.1:8b"}, {"id": "qwen2.5-coder:7b"}]
    with patch.object(OllamaProvider, "list_models", return_value=fake_catalog):
        body = TestClient(app).get("/v1/models", headers=auth_headers).json()
    ids = {row["id"] for row in body["data"]}
    # Real provider catalog appears, namespaced, regardless of tiers.
    for model_id in ("ollama/llama3.1:8b", "ollama/qwen2.5-coder:7b"):
        assert model_id in ids
        entry = next(r for r in body["data"] if r["id"] == model_id)
        assert entry["owned_by"] == "ollama"
    # Stub (echo) is NOT advertised as a real model source.
    assert not any(rid.startswith("echo/") for rid in ids)


def test_models_endpoint_swallows_provider_failures(auth_headers):
    """If a provider's ``list_models()`` raises, the endpoint still
    200s with everything else intact (tier aliases + other providers)."""
    from brains.providers.echo import EchoProvider

    with patch.object(EchoProvider, "list_models", side_effect=RuntimeError("catalog down")):
        response = TestClient(app).get("/v1/models", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    ids = {row["id"] for row in body["data"]}
    # Tier aliases still present:
    assert "brains-auto" in ids and "claude-brains-strong" in ids
    # Echo catalog entries are absent (provider raised):
    assert "echo/echo-default" not in ids
    assert "echo-default" not in ids


def test_models_endpoint_deduplicates_ids(auth_headers):
    body = TestClient(app).get("/v1/models", headers=auth_headers).json()
    ids = [row["id"] for row in body["data"]]
    assert len(ids) == len(set(ids)), "duplicate ids in /v1/models response"


def test_models_endpoint_swallows_get_provider_failures(auth_headers, monkeypatch):
    """A provider whose ``get_provider()`` itself raises (missing creds,
    unknown name, missing extra) must not 500 the endpoint."""
    from brains.api import models as models_mod

    def boom(name: str):
        raise RuntimeError(f"can't build provider {name!r}")

    monkeypatch.setattr(models_mod, "get_provider", boom)
    response = TestClient(app).get("/v1/models", headers=auth_headers)
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["data"]}
    # Tier aliases are always present even when no catalog can be loaded.
    assert "brains-auto" in ids


@pytest.mark.parametrize("alias", ["claude-brains-strong", "claude-brains-default"])
def test_claude_prefixed_aliases_match_claude_code_filter(auth_headers, alias):
    """Claude Code's gateway discovery filters /v1/models for ids
    matching ``claude*`` / ``anthropic*``. Verify the claude-prefixed
    siblings of every brains tier alias are present so the picker
    populates correctly."""
    body = TestClient(app).get("/v1/models", headers=auth_headers).json()
    ids = {row["id"] for row in body["data"]}
    assert alias in ids
    assert alias.startswith("claude-")


def test_tier_display_name_does_not_leak_provider_or_model(auth_headers):
    """``display_name`` on tier rows must NOT echo the operator's
    pinned provider+model wiring. That mapping is operator-internal
    and only belongs on the auth-gated ``/admin/api/config`` view;
    every API key holder hits ``/v1/models``, so leaking the wiring
    here is a recon vector. Verified for every tier alias."""
    body = TestClient(app).get("/v1/models", headers=auth_headers).json()
    from brains.config import settings

    pinned_models = {route.model for route in settings.models.values()}
    pinned_providers = {route.provider for route in settings.models.values()}
    for row in body["data"]:
        if row.get("owned_by") != "brains":
            continue
        label = row.get("display_name", "")
        for model in pinned_models:
            assert model not in label, f"display_name leaks pinned model: {label!r}"
        for provider in pinned_providers:
            # ``provider`` strings like ``echo`` are also fine to appear
            # in unrelated text; we only fail on the specific "via
            # <provider>" leak pattern.
            assert f"via {provider}" not in label, f"display_name leaks provider wiring: {label!r}"


def test_models_endpoint_exposes_reasoning_levels(auth_headers):
    """A provider that reports ``reasoning_effort`` levels (Copilot's
    GPT-5 family) surfaces them under ``capabilities`` so a client can
    discover a model's thinking levels from ``/v1/models`` alone — no
    second, provider-specific call needed."""
    from brains.providers.github_copilot import GitHubCopilotProvider

    fake = [
        {
            "id": "gpt-5.4",
            "vendor": "azure-openai",
            "label": None,
            "capabilities": {
                "reasoning_effort": ["none", "low", "medium", "high", "xhigh"],
                "vision": True,
                "context_window": 264000,
            },
        }
    ]
    with patch.object(GitHubCopilotProvider, "list_models", return_value=fake):
        body = TestClient(app).get("/v1/models", headers=auth_headers).json()
    entry = next((r for r in body["data"] if r["id"] == "github_copilot/gpt-5.4"), None)
    assert entry is not None
    assert entry["capabilities"]["reasoning_effort"] == ["none", "low", "medium", "high", "xhigh"]
    assert entry["capabilities"]["context_window"] == 264000

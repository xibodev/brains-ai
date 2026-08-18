"""Tests for ``Provider.list_models()`` across every shipped provider.

Each provider's catalog endpoint is mocked so the test suite doesn't need
network or upstream services. The contract under test is uniform:

- Returns a list of ``{"id": str, "vendor": str | None, "label": str | None}``.
- Returns ``[]`` on transport failure, parse failure, or 4xx/5xx — never raises.
- Never reflects upstream payloads with malformed shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from brains.providers.echo import EchoProvider
from brains.providers.github_copilot import GitHubCopilotProvider
from brains.providers.ollama import OllamaProvider
from brains.providers.openai_compatible import OpenAICompatibleProvider


def _mock_response(*, status_code: int = 200, json_body: object = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = json_body
    response.text = ""
    return response


def _assert_well_formed(rows: list[dict]) -> None:
    for row in rows:
        assert "id" in row and isinstance(row["id"], str) and row["id"].strip()
        assert "vendor" in row
        assert "label" in row


# ---- EchoProvider ---------------------------------------------------------


def test_echo_list_models_returns_four_tiers():
    rows = EchoProvider().list_models()
    assert [r["id"] for r in rows] == [
        "echo-small",
        "echo-default",
        "echo-strong",
        "echo-deep",
    ]
    _assert_well_formed(rows)


# ---- OpenAICompatibleProvider --------------------------------------------


def test_openai_compatible_list_models_parses_data_array():
    body = {
        "object": "list",
        "data": [
            {"id": "gpt-4o", "owned_by": "openai"},
            {"id": "gpt-4o-mini", "owned_by": "openai"},
            {"name": "llama-3", "vendor": "meta"},  # alternative shape
        ],
    }
    with patch("brains.providers.openai_compatible.httpx.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        rows = OpenAICompatibleProvider().list_models()
    ids = [r["id"] for r in rows]
    assert "gpt-4o" in ids and "gpt-4o-mini" in ids and "llama-3" in ids
    _assert_well_formed(rows)


def test_openai_compatible_list_models_empty_on_4xx():
    with patch("brains.providers.openai_compatible.httpx.get") as mock_get:
        mock_get.return_value = _mock_response(status_code=403, json_body=None)
        assert OpenAICompatibleProvider().list_models() == []


def test_openai_compatible_list_models_empty_on_transport_failure():
    with patch("brains.providers.openai_compatible.httpx.get") as mock_get:
        mock_get.side_effect = httpx.ConnectError("boom")
        assert OpenAICompatibleProvider().list_models() == []


def test_openai_compatible_list_models_skips_malformed_rows():
    body = {"data": [{"id": "ok"}, "not a dict", {"vendor": "missing-id"}, {"id": ""}]}
    with patch("brains.providers.openai_compatible.httpx.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        rows = OpenAICompatibleProvider().list_models()
    assert [r["id"] for r in rows] == ["ok"]


# ---- OllamaProvider ------------------------------------------------------


def test_ollama_list_models_parses_tags_endpoint():
    body = {
        "models": [
            {"name": "llama3:8b", "details": {"family": "llama"}},
            {"name": "qwen2.5-coder", "details": {"family": "qwen2.5"}},
        ]
    }
    with patch("brains.providers.ollama.httpx.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        rows = OllamaProvider().list_models()
    assert [r["id"] for r in rows] == ["llama3:8b", "qwen2.5-coder"]
    assert rows[0]["vendor"] == "llama"
    _assert_well_formed(rows)


def test_ollama_list_models_empty_when_daemon_unreachable():
    with patch("brains.providers.ollama.httpx.get") as mock_get:
        mock_get.side_effect = httpx.ConnectError("ollama is down")
        assert OllamaProvider().list_models() == []


def test_ollama_list_models_empty_on_garbage_json():
    with patch("brains.providers.ollama.httpx.get") as mock_get:
        bad = _mock_response(json_body=None)
        bad.json.side_effect = ValueError("nope")
        mock_get.return_value = bad
        assert OllamaProvider().list_models() == []


# ---- GitHubCopilotProvider -----------------------------------------------


def test_github_copilot_list_models_returns_empty_when_unauthed():
    """No login → the provider can't resolve a session → catalog is empty
    (instead of raising). The UI falls back to free-text entry."""
    with patch.object(GitHubCopilotProvider, "_session") as mock_session:
        from brains.providers.openai_compatible import ProviderInvocationError

        mock_session.side_effect = ProviderInvocationError("not logged in")
        assert GitHubCopilotProvider().list_models() == []


def test_github_copilot_list_models_parses_data_array_when_authed():
    from brains.auth.copilot import CopilotSession

    fake_session = CopilotSession(
        token="ghs_fake", chat_base_url="https://api.example", expires_at=9_999_999_999
    )
    body = {
        "data": [
            {"id": "gpt-4o", "vendor": "OpenAI"},
            {"id": "claude-3.5-sonnet", "vendor": "Anthropic"},
        ]
    }
    with (
        patch.object(GitHubCopilotProvider, "_session", return_value=fake_session),
        patch("brains.providers.github_copilot.httpx.get") as mock_get,
    ):
        mock_get.return_value = _mock_response(json_body=body)
        rows = GitHubCopilotProvider().list_models()
    assert [r["id"] for r in rows] == ["gpt-4o", "claude-3.5-sonnet"]
    assert rows[0]["vendor"] == "OpenAI"
    _assert_well_formed(rows)


def test_github_copilot_list_models_retries_once_on_401():
    from brains.auth.copilot import CopilotSession

    s1 = CopilotSession(token="stale", chat_base_url="https://api.example", expires_at=0)
    s2 = CopilotSession(token="fresh", chat_base_url="https://api.example", expires_at=9999)
    with (
        patch.object(GitHubCopilotProvider, "_session", side_effect=[s1, s2]) as mock_session,
        patch("brains.providers.github_copilot.httpx.get") as mock_get,
    ):
        mock_get.side_effect = [
            _mock_response(status_code=401, json_body={}),
            _mock_response(json_body={"data": [{"id": "gpt-4o"}]}),
        ]
        rows = GitHubCopilotProvider().list_models()
    assert [r["id"] for r in rows] == ["gpt-4o"]
    assert mock_session.call_count == 2  # second call is the force_refresh

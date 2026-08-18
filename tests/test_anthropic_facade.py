"""Tests for the Anthropic Messages API facade."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from brains.api.anthropic_translate import (
    anthropic_messages_to_openai,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai,
    openai_response_to_anthropic,
    openai_stream_to_anthropic_sse,
)
from brains.main import app

# ----------------------------- translator unit tests ----------------------------- #


def test_system_string_becomes_system_message():
    out = anthropic_messages_to_openai(
        [{"role": "user", "content": "hi"}], system="you are helpful"
    )
    assert out[0] == {"role": "system", "content": "you are helpful"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_system_list_of_text_blocks_is_concatenated():
    out = anthropic_messages_to_openai(
        [{"role": "user", "content": "hi"}],
        system=[
            {"type": "text", "text": "part one"},
            {"type": "text", "text": "part two"},
        ],
    )
    assert out[0]["content"] == "part one\n\npart two"


def test_user_text_blocks_are_concatenated():
    out = anthropic_messages_to_openai(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                ],
            }
        ]
    )
    assert out == [{"role": "user", "content": "hello world"}]


def test_assistant_tool_use_becomes_tool_calls():
    out = anthropic_messages_to_openai(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "calling the tool"},
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "search",
                        "input": {"q": "brains"},
                    },
                ],
            }
        ]
    )
    assert len(out) == 1
    msg = out[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "calling the tool"
    assert msg["tool_calls"][0]["id"] == "toolu_abc"
    assert msg["tool_calls"][0]["function"]["name"] == "search"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"q": "brains"}


def test_user_tool_result_becomes_tool_message():
    out = anthropic_messages_to_openai(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": "result text",
                    }
                ],
            }
        ]
    )
    assert out == [{"role": "tool", "tool_call_id": "toolu_abc", "content": "result text"}]


def test_tools_translation_wraps_input_schema_as_parameters():
    out = anthropic_tools_to_openai(
        [
            {
                "name": "search",
                "description": "search the web",
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
        ]
    )
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "search the web",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    ]


def test_tool_choice_translation_maps_all_cases():
    assert anthropic_tool_choice_to_openai({"type": "auto"}) == "auto"
    assert anthropic_tool_choice_to_openai({"type": "any"}) == "required"
    assert anthropic_tool_choice_to_openai({"type": "none"}) == "none"
    assert anthropic_tool_choice_to_openai({"type": "tool", "name": "search"}) == {
        "type": "function",
        "function": {"name": "search"},
    }
    assert anthropic_tool_choice_to_openai(None) is None


def test_response_translation_emits_text_and_usage():
    out = openai_response_to_anthropic(
        {
            "id": "chatcmpl-abc",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello there"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
        model="claude-3-sonnet",
    )
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["model"] == "claude-3-sonnet"
    assert out["content"] == [{"type": "text", "text": "hello there"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 5, "output_tokens": 3}


def test_response_translation_emits_tool_use_block():
    out = openai_response_to_anthropic(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"q":"brains"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        model="claude-3-sonnet",
    )
    assert out["stop_reason"] == "tool_use"
    assert len(out["content"]) == 1
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "call_1"
    assert block["name"] == "search"
    assert block["input"] == {"q": "brains"}


# ----------------------------- streaming translator ----------------------------- #


def _parse_sse_events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        if not block:
            continue
        event_name = None
        data_line = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_line = line[len("data: ") :]
        if event_name and data_line is not None:
            events.append((event_name, json.loads(data_line)))
    return events


def test_stream_text_emits_full_event_sequence():
    chunks = [
        json.dumps(
            {
                "id": "chatcmpl-x",
                "choices": [{"index": 0, "delta": {"content": "hello "}, "finish_reason": None}],
            }
        ),
        json.dumps(
            {
                "id": "chatcmpl-x",
                "choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": None}],
            }
        ),
        json.dumps(
            {
                "id": "chatcmpl-x",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        ),
    ]
    sse = "".join(openai_stream_to_anthropic_sse(chunks, model="claude-x"))
    events = _parse_sse_events(sse)
    types = [e[0] for e in events]
    assert types[0] == "message_start"
    assert "content_block_start" in types
    assert "content_block_delta" in types
    assert "content_block_stop" in types
    assert types[-2:] == ["message_delta", "message_stop"]

    deltas = [e[1] for e in events if e[0] == "content_block_delta"]
    assert "".join(d["delta"]["text"] for d in deltas) == "hello world"

    final = next(e[1] for e in events if e[0] == "message_delta")
    assert final["delta"]["stop_reason"] == "end_turn"


def test_stream_tool_use_emits_input_json_delta():
    chunks = [
        json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": '{"q":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"brains"}'}}]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
    ]
    sse = "".join(openai_stream_to_anthropic_sse(chunks, model="claude-x"))
    events = _parse_sse_events(sse)

    starts = [e[1] for e in events if e[0] == "content_block_start"]
    assert starts[0]["content_block"]["type"] == "tool_use"
    assert starts[0]["content_block"]["id"] == "call_1"
    assert starts[0]["content_block"]["name"] == "search"

    deltas = [e[1] for e in events if e[0] == "content_block_delta"]
    assert all(d["delta"]["type"] == "input_json_delta" for d in deltas)
    assert "".join(d["delta"]["partial_json"] for d in deltas) == '{"q":"brains"}'

    final = next(e[1] for e in events if e[0] == "message_delta")
    assert final["delta"]["stop_reason"] == "tool_use"


# ----------------------------- HTTP integration tests ----------------------------- #


def test_messages_non_stream_returns_anthropic_shape(auth_headers):
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        headers=auth_headers,
        json={
            # Use a real catalog id (echo provider in tests) so the
            # resolver hits a direct match. Faithful-proxy contract:
            # response.model is the upstream id, not whatever alias
            # the client originally sent.
            "model": "echo-default",
            "max_tokens": 100,
            "system": "you are helpful",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "echo-default"
    assert isinstance(body["content"], list)
    assert body["content"][0]["type"] == "text"
    # Echo provider: response should contain the user text we sent.
    assert "hello" in body["content"][0]["text"]
    assert body["stop_reason"] == "end_turn"
    assert "input_tokens" in body["usage"]


def test_messages_stream_returns_sse_event_stream(auth_headers):
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        headers=auth_headers,
        json={
            "model": "echo-default",
            "stream": True,
            "messages": [{"role": "user", "content": "stream me"}],
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(response.text)
    types = [e[0] for e in events]
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    assert "content_block_start" in types
    assert "content_block_delta" in types


def test_messages_response_model_is_upstream_id_not_alias(auth_headers):
    """Faithful-proxy contract: the ``model`` field in the response
    MUST carry the actual upstream id that served the call, never the
    alias the client originally sent. Regression for the
    ``response.model = brains-auto`` wire-level lie."""
    client = TestClient(app)
    # brains/cheap is pinned to echo-small in the default test config.
    response = client.post(
        "/v1/messages",
        headers=auth_headers,
        json={
            "model": "brains/cheap",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    # NEVER the alias the client sent.
    assert body["model"] != "brains/cheap"
    assert body["model"] != "brains-cheap"
    # ALWAYS the resolved upstream id.
    assert body["model"] == "echo-small"


def test_messages_stream_message_start_advertises_upstream_model(auth_headers):
    """Streaming counterpart: the ``message_start`` event must carry
    the resolved upstream model id, not the alias. Was a leak before
    the faithful-proxy rewrite."""
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        headers=auth_headers,
        json={
            "model": "brains/cheap",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    message_start = next(payload for ev, payload in events if ev == "message_start")
    model_advertised = message_start["message"]["model"]
    assert model_advertised != "brains/cheap"
    assert model_advertised != "brains-cheap"
    assert model_advertised == "echo-small"


def test_messages_provider_invocation_error_does_not_leak_provider_name(monkeypatch, auth_headers):
    """Anthropic 502 must NOT include the upstream provider name or
    raw exception text. Both can carry internal URLs, model ids, or
    auth-shaped strings. Mirror of the OpenAI-side leak test."""
    from brains.api import anthropic as anthropic_mod
    from brains.providers.registry import ProviderInvocationError

    class _Boom:
        def complete(self, *_a, **_kw):
            raise ProviderInvocationError(
                "401 from https://api.github.com/copilot_internal/v2/token: leaked-secret-abc"
            )

    monkeypatch.setattr(
        anthropic_mod,
        "select_model",
        lambda _c, **_kw: {
            "provider": _Boom(),
            "provider_name": "github_copilot",
            "model": "claude-haiku-4.5",
            "tier": "default",
        },
    )

    resp = TestClient(app).post(
        "/v1/messages",
        headers=auth_headers,
        json={
            "model": "claude-haiku-4.5",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 502
    detail = str(resp.json()).lower()
    assert "github_copilot" not in detail
    assert "leaked-secret-abc" not in detail
    assert "api.github.com" not in detail

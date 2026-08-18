"""Translation layer between the Anthropic Messages API and OpenAI Chat Completions.

The brains gateway speaks OpenAI Chat Completions natively. This module is the
single round-tripper that lets Claude Code, the Anthropic CLI, and any other
Anthropic-shaped client talk to brains as if it were Anthropic.

Direction map:

    Request  : Anthropic Messages  --to_openai_*-->  OpenAI Chat Completion
    Response : OpenAI Chat result  --from_openai_*->  Anthropic Messages
    Stream   : OpenAI SSE chunks   --openai_stream_to_anthropic_sse--> Anthropic SSE events

Spec references:
- Anthropic Messages: https://docs.anthropic.com/en/api/messages
- Anthropic streaming events: https://docs.anthropic.com/en/api/messages-streaming
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Iterator
from typing import Any

# ----------------------------- Request: A -> OAI ----------------------------- #


def _system_to_text(system: str | list[dict[str, Any]] | None) -> str | None:
    """Flatten Anthropic ``system`` (string or list of text blocks) to a string."""
    if system is None:
        return None
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n\n".join(parts) if parts else None
    return None


def _blocks_to_openai_assistant_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse an Anthropic assistant ``content`` list into one OpenAI message.

    Anthropic assistant messages can interleave ``text`` and ``tool_use``
    blocks. OpenAI puts text in ``content`` and tool calls in ``tool_calls``
    on the same message, so we collect both.
    """
    text_chunks: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                text_chunks.append(text)
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        # OpenAI expects ``arguments`` as a JSON string.
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    msg: dict[str, Any] = {"role": "assistant"}
    msg["content"] = "".join(text_chunks) if text_chunks else None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _tool_result_content_to_text(content: Any) -> str:
    """Normalise a ``tool_result.content`` value into a single string for OpenAI's ``tool`` role."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content)


def _user_blocks_to_openai_messages(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split an Anthropic user ``content`` list into the OpenAI message(s) it implies.

    Anthropic packs both plain user text and ``tool_result`` replies under
    the same user role. In OpenAI, tool replies live in their own
    ``{role: "tool"}`` message tied to ``tool_call_id``.
    """
    out: list[dict[str, Any]] = []
    text_chunks: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                text_chunks.append(text)
        elif btype == "tool_result":
            # Flush any accumulated user text first so message order is preserved.
            if text_chunks:
                out.append({"role": "user", "content": "".join(text_chunks)})
                text_chunks = []
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": _tool_result_content_to_text(block.get("content")),
                }
            )
        # image / other blocks are silently dropped — brains is a router, not a vision pipeline.
    if text_chunks:
        out.append({"role": "user", "content": "".join(text_chunks)})
    return out


def anthropic_messages_to_openai(
    messages: list[dict[str, Any]],
    system: str | list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convert Anthropic Messages ``messages`` + ``system`` into an OpenAI message list."""
    out: list[dict[str, Any]] = []
    system_text = _system_to_text(system)
    if system_text:
        out.append({"role": "system", "content": system_text})
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant":
            if isinstance(content, str):
                out.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                out.append(_blocks_to_openai_assistant_message(content))
        elif role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                out.extend(_user_blocks_to_openai_messages(content))
        # system on a per-message basis is rare in Anthropic shape; honour it anyway.
        elif role == "system" and isinstance(content, str):
            out.append({"role": "system", "content": content})
    return out


def anthropic_tools_to_openai(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Convert Anthropic ``tools`` list (``{name, description, input_schema}``) to OpenAI shape."""
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": str(name),
                    "description": tool.get("description") or "",
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return out or None


def anthropic_tool_choice_to_openai(choice: dict[str, Any] | None) -> Any:
    """Convert Anthropic ``tool_choice`` to OpenAI ``tool_choice``.

    Anthropic shapes:
        {"type": "auto"}                                -> OpenAI "auto"
        {"type": "any"}                                 -> OpenAI "required"
        {"type": "tool", "name": "X"}                   -> OpenAI {"type":"function","function":{"name":"X"}}
        {"type": "none"} (newer)                        -> OpenAI "none"
    """
    if not isinstance(choice, dict):
        return None
    ctype = choice.get("type")
    if ctype == "auto":
        return "auto"
    if ctype == "any":
        return "required"
    if ctype == "none":
        return "none"
    if ctype == "tool":
        name = choice.get("name")
        if name:
            return {"type": "function", "function": {"name": str(name)}}
    return None


# ----------------------------- Response: OAI -> A ---------------------------- #

# OpenAI ``finish_reason`` -> Anthropic ``stop_reason`` map. Anything outside
# this table falls back to ``end_turn`` so a novel upstream value never crashes
# the response shape.
_FINISH_TO_STOP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def openai_finish_reason_to_anthropic(reason: str | None) -> str:
    if reason is None:
        return "end_turn"
    return _FINISH_TO_STOP.get(reason, "end_turn")


def _anthropic_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _anthropic_tool_use_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:24]}"


def _usage_from_openai(usage: dict[str, Any] | None) -> dict[str, int]:
    """Map OpenAI ``usage`` to Anthropic ``usage``. Missing fields default to 0."""
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
    }


def openai_response_to_anthropic(response: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert a non-streaming OpenAI Chat Completion into an Anthropic Message."""
    choices = response.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {} or {}
    if not isinstance(message, dict):
        message = {}

    content_blocks: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        if not isinstance(fn, dict):
            continue
        try:
            tool_input = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            tool_input = {"_raw_arguments": fn.get("arguments")}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or _anthropic_tool_use_id()),
                "name": str(fn.get("name") or ""),
                "input": tool_input,
            }
        )

    return {
        "id": str(response.get("id") or _anthropic_message_id()),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": openai_finish_reason_to_anthropic(
            choice.get("finish_reason") if isinstance(choice, dict) else None
        ),
        "stop_sequence": None,
        "usage": _usage_from_openai(response.get("usage")),
    }


# ----------------------------- Streaming: OAI -> A --------------------------- #


def _sse(event: str, data: dict[str, Any]) -> str:
    """Format an Anthropic SSE event. Each event is two lines + blank."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class _StreamState:
    """Mutable state threaded through the OpenAI->Anthropic stream translator."""

    __slots__ = (
        "message_id",
        "model",
        "started",
        "text_index",
        "text_open",
        "tool_blocks",
        "tool_index_by_oai",
        "next_block_index",
        "stop_reason",
        "input_tokens",
        "output_tokens",
    )

    def __init__(self, model: str) -> None:
        self.message_id = _anthropic_message_id()
        self.model = model
        self.started = False
        self.text_index: int | None = None
        self.text_open: bool = False
        self.tool_blocks: dict[int, dict[str, Any]] = {}
        self.tool_index_by_oai: dict[int, int] = {}
        self.next_block_index = 0
        self.stop_reason: str = "end_turn"
        self.input_tokens: int = 0
        self.output_tokens: int = 0

    def reserve_block_index(self) -> int:
        idx = self.next_block_index
        self.next_block_index += 1
        return idx


def _emit_message_start(state: _StreamState) -> str:
    return _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": state.message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": state.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": state.input_tokens, "output_tokens": 0},
            },
        },
    )


def _open_text_block(state: _StreamState) -> Iterator[str]:
    if state.text_open:
        return
    if state.text_index is None:
        state.text_index = state.reserve_block_index()
    state.text_open = True
    yield _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": state.text_index,
            "content_block": {"type": "text", "text": ""},
        },
    )


def _close_text_block(state: _StreamState) -> Iterator[str]:
    if not state.text_open or state.text_index is None:
        return
    yield _sse(
        "content_block_stop",
        {"type": "content_block_stop", "index": state.text_index},
    )
    state.text_open = False


def _open_tool_block(state: _StreamState, oai_index: int, tool_id: str, name: str) -> Iterator[str]:
    if oai_index in state.tool_index_by_oai:
        return
    block_index = state.reserve_block_index()
    state.tool_index_by_oai[oai_index] = block_index
    state.tool_blocks[block_index] = {"id": tool_id, "name": name, "args": ""}
    yield _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": block_index,
            "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
        },
    )


def _emit_tool_partial(state: _StreamState, oai_index: int, partial: str) -> Iterator[str]:
    block_index = state.tool_index_by_oai.get(oai_index)
    if block_index is None:
        return
    state.tool_blocks[block_index]["args"] += partial
    yield _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": block_index,
            "delta": {"type": "input_json_delta", "partial_json": partial},
        },
    )


def _close_all_tool_blocks(state: _StreamState) -> Iterator[str]:
    for block_index in list(state.tool_blocks.keys()):
        yield _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": block_index},
        )


def openai_stream_to_anthropic_sse(chunks: Iterable[str], model: str) -> Iterator[str]:
    """Translate an OpenAI Chat Completions SSE chunk stream to Anthropic SSE events.

    Each input ``chunks`` element is the JSON payload of one ``data:`` line
    (the gateway strips the ``data: `` prefix in ``brains.api.openai.chat``
    before yielding to us). Output is fully-formed Anthropic SSE event
    blocks (``event: X\\ndata: {...}\\n\\n``) ready to write to the wire.
    """
    state = _StreamState(model)
    saw_any_delta = False

    for raw in chunks:
        if not raw:
            continue
        try:
            chunk = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(chunk, dict):
            continue

        # Some providers ship usage on the final chunk; capture it.
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            state.input_tokens = int(usage.get("prompt_tokens") or state.input_tokens)
            state.output_tokens = int(usage.get("completion_tokens") or state.output_tokens)

        choices = chunk.get("choices") or []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            finish_reason = choice.get("finish_reason")

            if not state.started:
                state.started = True
                yield _emit_message_start(state)
                yield _sse("ping", {"type": "ping"})

            text_piece = delta.get("content")
            if isinstance(text_piece, str) and text_piece:
                saw_any_delta = True
                yield from _open_text_block(state)
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": state.text_index,
                        "delta": {"type": "text_delta", "text": text_piece},
                    },
                )
                state.output_tokens += max(1, len(text_piece) // 4)

            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                oai_index = int(tc.get("index") or 0)
                fn = tc.get("function") or {}
                # First chunk for this tool index carries id + name.
                if oai_index not in state.tool_index_by_oai:
                    tool_id = str(tc.get("id") or _anthropic_tool_use_id())
                    name = str(fn.get("name") or "")
                    yield from _open_tool_block(state, oai_index, tool_id, name)
                args_piece = fn.get("arguments")
                if isinstance(args_piece, str) and args_piece:
                    saw_any_delta = True
                    yield from _emit_tool_partial(state, oai_index, args_piece)

            if finish_reason:
                state.stop_reason = openai_finish_reason_to_anthropic(finish_reason)

    # If the provider never sent a single delta (e.g. echo on empty input),
    # emit a no-op text block so clients see a complete frame sequence.
    if not state.started:
        yield _emit_message_start(state)

    if state.text_open:
        yield from _close_text_block(state)
    yield from _close_all_tool_blocks(state)

    # If we saw tool calls but no explicit finish_reason, default to tool_use.
    if state.tool_blocks and state.stop_reason == "end_turn" and not saw_any_delta:
        state.stop_reason = "tool_use"

    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": state.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": state.output_tokens},
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})


# Stable timestamp helper exposed for tests.
def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "anthropic_messages_to_openai",
    "anthropic_tool_choice_to_openai",
    "anthropic_tools_to_openai",
    "openai_finish_reason_to_anthropic",
    "openai_response_to_anthropic",
    "openai_stream_to_anthropic_sse",
]

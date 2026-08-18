from typing import Any

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model: str = "brains-auto"
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    metadata: dict[str, Any] | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    # Reasoning models (e.g. Copilot's GPT-5 family) accept a thinking
    # level here — "none"/"low"/"medium"/"high"/"xhigh". The accepted set
    # per model is reported under capabilities.reasoning_effort in
    # GET /v1/models. Forwarded verbatim to providers that support it.
    reasoning_effort: str | None = None


class ResponsesRequest(BaseModel):
    model: str = "brains-auto"
    input: Any
    stream: bool = False


class AnthropicMessagesRequest(BaseModel):
    """Anthropic Messages API request.

    Mirrors https://docs.anthropic.com/en/api/messages so brains can act
    as a drop-in endpoint for Claude Code, the Anthropic CLI, and any
    other Anthropic-shaped client. Fields outside the official schema
    are tolerated via Pydantic's default extra="ignore" so a richer
    upstream payload doesn't 422 us.
    """

    model: str = "brains-auto"
    # ``messages`` is intentionally loose: each item is the raw Anthropic
    # message object whose ``content`` may be a string OR a list of typed
    # content blocks (``text`` / ``tool_use`` / ``tool_result`` / ``image``).
    # The translator in brains.api.anthropic_translate handles both shapes.
    messages: list[dict[str, Any]] = Field(default_factory=list)
    max_tokens: int | None = None
    system: str | list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    metadata: dict[str, Any] | None = None


class CountTokensRequest(BaseModel):
    """Anthropic ``POST /v1/messages/count_tokens`` body.

    Same shape as :class:`AnthropicMessagesRequest` minus the required
    ``max_tokens`` semantics — count_tokens never generates output, so
    pinning a token budget is meaningless. We accept the field for
    payload-shape compatibility and ignore it.
    """

    model: str = "brains-auto"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    system: str | list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    max_tokens: int | None = None
    metadata: dict[str, Any] | None = None

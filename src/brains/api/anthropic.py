"""Anthropic Messages API facade.

Surfaces the brains gateway as a drop-in Anthropic endpoint so Claude Code,
the Anthropic CLI, and any other Anthropic-shaped client can talk to brains
with nothing more than ``ANTHROPIC_BASE_URL=http://<gateway>``.

Implementation strategy: this module owns the full Anthropic request +
response shape (system, tools, tool_use/tool_result blocks, streaming SSE)
but delegates the actual provider call to the same router + provider stack
that powers ``/v1/chat/completions``. Translation happens at the edges in
:mod:`brains.api.anthropic_translate`.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from brains.api.anthropic_translate import (
    anthropic_messages_to_openai,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai,
    openai_response_to_anthropic,
    openai_stream_to_anthropic_sse,
)
from brains.api.auth import require_api_key
from brains.api.openai import session_attribution
from brains.api.schemas import AnthropicMessagesRequest, CountTokensRequest
from brains.config import settings
from brains.context.planner import plan
from brains.gateway.normalizer import redact_payload
from brains.providers.registry import ProviderConfigError, ProviderInvocationError
from brains.router.classifier import classify
from brains.router.model_router import ModelNotFoundError, select_model
from brains.router.savings import record_usage, record_usage_from_response
from brains.storage.repositories import write_route, write_trace

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

logger = logging.getLogger(__name__)


def _provider_error_payload(provider_name: str, exc: Exception) -> dict:
    # Client-facing payload deliberately does NOT name the upstream
    # provider or echo its raw exception text; both can carry internal
    # URLs, model ids, or auth-shaped strings. The operator gets the
    # full picture via the logger.warning at every call site.
    logger.warning("provider failure on /v1/messages: provider=%s error=%s", provider_name, exc)
    return {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": "Upstream provider request failed.",
        },
    }


@router.post("/messages")
def messages(req: AnthropicMessagesRequest, request: Request):
    """Anthropic Messages — full streaming + tool-use facade."""
    payload = req.model_dump()
    safe_payload = redact_payload(payload)

    openai_messages = anthropic_messages_to_openai(req.messages, req.system)
    classification = classify(openai_messages, req.model)

    provider_messages = openai_messages
    if settings.gateway_preamble:
        provider_messages = [
            {"role": "system", "content": settings.gateway_preamble}
        ] + openai_messages

    try:
        route = select_model(classification, requested_model=req.model)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderConfigError as exc:
        logger.warning("provider config error during route on /v1/messages: %s", exc)
        raise HTTPException(
            status_code=500, detail="Gateway is not configured for the requested model."
        ) from exc

    planned = plan(
        classification,
        messages=openai_messages,
        workspace_path=(req.metadata or {}).get("workspace_path") if req.metadata else None,
    )

    provider = route["provider"]
    provider_name = (
        route.get("provider_name") or provider.__class__.__name__.replace("Provider", "").lower()
    )
    model = route["model"]

    # Stash routing decision for the optional dump middleware
    # (brains.observability.dump). Off by default; only read when
    # ``BRAINS_DUMP_DIR`` is set.
    request.state.brains_route = {
        "task_type": classification.task_type,
        "tier": route["tier"],
        "provider": provider_name,
        "model": model,
        "requested_model": req.model,
        "simulated": bool(route.get("is_stub")),
        "strategy": planned["strategy"],
    }
    try:
        write_route(
            classification.task_type,
            route["tier"],
            provider=provider_name,
            model=model,
            strategy=planned["strategy"],
            policy={
                "require_approval_for_deep": settings.control.require_approval_for_deep,
                "require_approval_for_external_docs": settings.control.require_approval_for_external_docs,
                "require_approval_for_large_scans": settings.control.require_approval_for_large_scans,
            },
        )
    except TypeError:
        write_route(classification.task_type, route["tier"])
    write_trace(classification.task_type, json.dumps(safe_payload))

    provider_kwargs: dict = {}
    if req.temperature is not None:
        provider_kwargs["temperature"] = req.temperature
    if req.max_tokens is not None:
        provider_kwargs["max_tokens"] = req.max_tokens
    if req.top_p is not None:
        provider_kwargs["top_p"] = req.top_p
    if req.stop_sequences:
        provider_kwargs["stop"] = req.stop_sequences
    if req.metadata:
        provider_kwargs["metadata"] = req.metadata

    oai_tools = anthropic_tools_to_openai(req.tools)
    if oai_tools:
        provider_kwargs["tools"] = oai_tools
    oai_tool_choice = anthropic_tool_choice_to_openai(req.tool_choice)
    if oai_tool_choice is not None:
        provider_kwargs["tool_choice"] = oai_tool_choice

    if req.stream:
        attributed_session = session_attribution(request)

        def stream_gen():
            usage_acc: dict = {"prompt_tokens": 0, "completion_tokens": 0}

            def _peek_usage(chunks):
                """Pass-through wrapper that extracts ``usage`` from
                each OpenAI chunk before handing it to the SSE
                translator. Echo provider and providers that don't
                emit usage leave the accumulator at zero."""
                for chunk in chunks:
                    try:
                        obj = json.loads(chunk)
                        usage = obj.get("usage") if isinstance(obj, dict) else None
                        if isinstance(usage, dict):
                            usage_acc["prompt_tokens"] = int(
                                usage.get("prompt_tokens")
                                or usage.get("input_tokens")
                                or usage_acc["prompt_tokens"]
                            )
                            usage_acc["completion_tokens"] = int(
                                usage.get("completion_tokens")
                                or usage.get("output_tokens")
                                or usage_acc["completion_tokens"]
                            )
                    except (TypeError, ValueError):
                        pass
                    yield chunk

            try:
                iterator = provider.stream(model, provider_messages, **provider_kwargs)
            except (ProviderInvocationError, ProviderConfigError) as exc:
                logger.warning("anthropic.stream provider failed: %s", exc)
                yield (
                    "event: error\n"
                    f"data: {json.dumps(_provider_error_payload(provider_name, exc))}\n\n"
                )
                return
            try:
                # Pass the resolved upstream model (not ``req.model``)
                # so message_start events advertise the real model that
                # served the request, not whichever alias the client
                # originally sent. Mirrors the non-streaming path at
                # the bottom of ``messages``.
                yield from openai_stream_to_anthropic_sse(_peek_usage(iterator), model)
            except (ProviderInvocationError, ProviderConfigError) as exc:
                logger.warning("anthropic.stream translator failed: %s", exc)
                yield (
                    "event: error\n"
                    f"data: {json.dumps(_provider_error_payload(provider_name, exc))}\n\n"
                )
            record_usage(
                endpoint="anthropic.messages",
                requested_model=req.model,
                routed_model=model,
                provider=provider_name,
                input_tokens=usage_acc["prompt_tokens"],
                output_tokens=usage_acc["completion_tokens"],
                task_type=classification.task_type,
                session_id=attributed_session,
            )

        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    try:
        response = provider.complete(model, provider_messages, **provider_kwargs)
    except ProviderConfigError as exc:
        logger.warning(
            "provider config error on /v1/messages: provider=%s error=%s", provider_name, exc
        )
        raise HTTPException(status_code=500, detail="Upstream provider is misconfigured.") from exc
    except ProviderInvocationError as exc:
        logger.warning(
            "provider invocation error on /v1/messages: provider=%s error=%s", provider_name, exc
        )
        raise HTTPException(status_code=502, detail="Upstream provider request failed.") from exc

    record_usage_from_response(
        endpoint="anthropic.messages",
        requested_model=req.model,
        routed_model=model,
        provider=provider_name,
        provider_response=response,
        task_type=classification.task_type,
        session_id=session_attribution(request),
    )
    # Faithful response: the ``model`` field on the wire must be the
    # actual upstream model that served the call, not the alias the
    # client requested. Anything else lies to telemetry and any client
    # that branches on response.model (model-specific prompt tuning,
    # cost attribution, etc.).
    return openai_response_to_anthropic(response, model)


# --- count_tokens -------------------------------------------------------
#
# Anthropic's gateway spec requires every well-behaved gateway to expose
# ``POST /v1/messages/count_tokens`` so clients can size prompts without
# paying a generation round-trip. The whole point is that it's CHEAP:
# we count locally and never call any upstream provider. ``tiktoken``
# gives a tight Claude-shaped approximation when available; otherwise
# we use the ``len(text) // 4`` heuristic that Anthropic's own SDKs
# fall back to.


def _flatten_text_for_tokens(req: CountTokensRequest) -> str:
    """Concatenate every text fragment in the request payload.

    Walks ``system`` (string or list of typed blocks), every message's
    ``content`` (string or list of typed blocks), and any string-valued
    fields inside tool definitions. Non-text blocks (images, tool_use,
    tool_result with structured content) contribute their JSON
    representation so the count stays a conservative upper bound
    rather than silently dropping bytes.
    """
    parts: list[str] = []

    def _walk_blocks(blocks: list) -> None:
        for block in blocks:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            # Fallback: dump structured block as JSON so its bytes count.
            try:
                parts.append(json.dumps(block, ensure_ascii=False))
            except (TypeError, ValueError):
                parts.append(str(block))

    if isinstance(req.system, str):
        parts.append(req.system)
    elif isinstance(req.system, list):
        _walk_blocks(req.system)

    for message in req.messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            _walk_blocks(content)

    if req.tools:
        for tool in req.tools:
            try:
                parts.append(json.dumps(tool, ensure_ascii=False))
            except (TypeError, ValueError):
                parts.append(str(tool))

    return "\n".join(p for p in parts if p)


def _load_tiktoken_encoder():
    """Return a ``cl100k_base`` encoder, or ``None`` if tiktoken is not
    available. Indirection exists so tests can monkeypatch this single
    seam instead of mangling ``sys.modules`` and ``builtins.__import__``
    (which leaks across tests in subtle ways)."""
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 — any failure → heuristic fallback
        return None


def _count_tokens_local(text: str) -> int:
    """Cheap local token count.

    Uses ``tiktoken``'s ``cl100k_base`` encoder when importable (close
    enough to Claude's BPE for sizing purposes), else falls back to
    ``ceil(len(text) / 4)`` — the same conservative heuristic
    Anthropic's own SDK uses when no tokenizer is available.
    """
    if not text:
        return 0
    encoder = _load_tiktoken_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:  # noqa: BLE001 — encoder runtime failure → heuristic
            pass
    # Round up so a 1-char prompt still costs at least 1 token.
    return (len(text) + 3) // 4


@router.post("/messages/count_tokens")
def count_tokens(req: CountTokensRequest):
    """Anthropic ``POST /v1/messages/count_tokens`` — local-only count."""
    text = _flatten_text_for_tokens(req)
    return {"input_tokens": _count_tokens_local(text)}

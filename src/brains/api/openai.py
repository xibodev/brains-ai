import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from brains.api.auth import require_api_key
from brains.api.schemas import ChatCompletionRequest, ChatMessage, ResponsesRequest
from brains.config import settings
from brains.context.planner import plan
from brains.gateway.normalizer import redact_payload
from brains.observability.metrics import record_passthrough_mutation
from brains.providers.registry import ProviderConfigError, ProviderInvocationError
from brains.router.classifier import classify
from brains.router.model_router import ModelNotFoundError, select_model
from brains.router.resolver import normalize_model_name
from brains.router.savings import record_usage, record_usage_from_response
from brains.storage.repositories import write_route, write_trace

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

logger = logging.getLogger(__name__)


def session_attribution(request: Request) -> str | None:
    """The Brains Session a gateway caller claims this call belongs to.

    Attribution is opt-in and additive: a client that sends
    ``X-Brains-Session`` lets an Issue rollup sum this call's real cost, and a
    client that does not is recorded unattributed rather than guessed onto a
    Session. The value is only ever a lookup key - an unknown id attributes
    nothing (see :func:`brains.router.savings._attribute_usage`).
    """
    value = request.headers.get("x-brains-session")
    return value.strip() or None if value else None


def _provider_error_payload(provider_name: str, exc: Exception) -> dict:
    # Client-facing payload deliberately does NOT name the upstream
    # provider or echo its raw exception text; both can carry internal
    # URLs, model ids, or auth-shaped strings. The operator gets the
    # full picture via the logger.warning below.
    logger.warning("provider failure on /v1/chat: provider=%s error=%s", provider_name, exc)
    return {
        "error": {
            "message": "Upstream provider request failed.",
            "type": "provider_error",
            "code": "provider_invocation_failed",
        }
    }


def _accumulate_stream_usage(chunk_payload: str, acc: dict) -> None:
    """Best-effort extraction of token usage from a streaming chunk.

    OpenAI streams emit a final ``usage`` block when the client opts in
    via ``stream_options: {include_usage: true}``. We accept either
    shape (top-level ``usage`` or under ``choices[*].usage``) so the
    savings ledger captures whatever the provider gave us. Echo
    provider and providers that don't emit usage just leave the
    accumulator at zero, which the ledger records faithfully.
    """
    try:
        obj = json.loads(chunk_payload)
    except (TypeError, ValueError):
        return
    if not isinstance(obj, dict):
        return
    usage = obj.get("usage")
    if isinstance(usage, dict):
        acc["prompt_tokens"] = int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or acc.get("prompt_tokens", 0)
        )
        acc["completion_tokens"] = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or acc.get("completion_tokens", 0)
        )


def _model_matches_passthrough(requested_model: str | None, forwarded_model: str | None) -> bool:
    requested = normalize_model_name(requested_model)
    forwarded = normalize_model_name(forwarded_model)
    if requested == forwarded:
        return True
    if "/" in requested:
        _, _, tail = requested.partition("/")
        return tail == forwarded
    return False


def _check_passthrough_integrity(
    *,
    route: dict,
    requested_model: str | None,
    forwarded_model: str | None,
    client_messages: list[dict],
    forwarded_messages: list[dict],
) -> None:
    """Best-effort alarm if a direct faithful-proxy request is mutated."""

    if route.get("resolution") != "direct":
        return
    reasons: list[str] = []
    if not _model_matches_passthrough(requested_model, forwarded_model):
        reasons.append("model")
    if forwarded_messages != client_messages:
        reasons.append("body")
    if not reasons:
        return
    record_passthrough_mutation("model_and_body" if len(reasons) > 1 else reasons[0])


@router.post("/chat/completions")
def chat(req: ChatCompletionRequest, request: Request):
    payload = req.model_dump()
    safe_payload = redact_payload(payload)
    messages = [m.model_dump() for m in req.messages]
    classification = classify(messages, req.model)
    # Opt-in strict enforcement: prepend the configured preamble as a system
    # message on the way to the provider. Classification runs on the user's
    # original messages; only the model call carries the preamble.
    provider_messages = messages
    if settings.gateway_preamble:
        provider_messages = [{"role": "system", "content": settings.gateway_preamble}] + messages
    try:
        route = select_model(classification, requested_model=req.model)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderConfigError as exc:
        logger.warning("provider config error during route on /v1/chat: %s", exc)
        raise HTTPException(
            status_code=500, detail="Gateway is not configured for the requested model."
        ) from exc
    planned = plan(
        classification,
        messages=messages,
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
        "strategy": planned["strategy"],
        "simulated": bool(route.get("is_stub")),
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
        # Some tests monkeypatch write_route(route, tier) with the legacy signature.
        write_route(classification.task_type, route["tier"])
    write_trace(classification.task_type, json.dumps(safe_payload))

    provider_kwargs = {
        key: value
        for key, value in {
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "tools": req.tools,
            "tool_choice": req.tool_choice,
            "metadata": req.metadata,
            "top_p": req.top_p,
            "stop": req.stop,
            "reasoning_effort": req.reasoning_effort,
        }.items()
        if value is not None
    }
    _check_passthrough_integrity(
        route=route,
        requested_model=req.model,
        forwarded_model=model,
        client_messages=messages,
        forwarded_messages=provider_messages,
    )

    if req.stream:
        attributed_session = session_attribution(request)

        def stream_gen():
            usage_acc: dict = {"prompt_tokens": 0, "completion_tokens": 0}
            try:
                iterator = provider.stream(model, provider_messages, **provider_kwargs)
            except (ProviderInvocationError, ProviderConfigError) as exc:
                yield f"data: {json.dumps(_provider_error_payload(provider_name, exc))}\n\n"
                yield "data: [DONE]\n\n"
                return
            try:
                for chunk in iterator:
                    _accumulate_stream_usage(chunk, usage_acc)
                    yield f"data: {chunk}\n\n"
            except (ProviderInvocationError, ProviderConfigError) as exc:
                yield f"data: {json.dumps(_provider_error_payload(provider_name, exc))}\n\n"
            yield "data: [DONE]\n\n"
            record_usage(
                endpoint="openai.chat",
                requested_model=req.model,
                routed_model=model,
                provider=provider_name,
                input_tokens=usage_acc.get("prompt_tokens", 0),
                output_tokens=usage_acc.get("completion_tokens", 0),
                task_type=classification.task_type,
                session_id=attributed_session,
            )

        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    try:
        response = provider.complete(model, provider_messages, **provider_kwargs)
    except ProviderConfigError as exc:
        logger.warning(
            "provider config error on /v1/chat: provider=%s error=%s", provider_name, exc
        )
        raise HTTPException(status_code=500, detail="Upstream provider is misconfigured.") from exc
    except ProviderInvocationError as exc:
        logger.warning(
            "provider invocation error on /v1/chat: provider=%s error=%s", provider_name, exc
        )
        raise HTTPException(status_code=502, detail="Upstream provider request failed.") from exc

    # Faithful response: rewrite the ``model`` field to the actual
    # upstream model that served the call, regardless of what the
    # provider chose to echo. Telemetry and clients that branch on
    # response.model see the real id, never the alias.
    response["model"] = model
    # The brains classifier/plan payload is debug data, not contract;
    # only expose it when the operator opts in via the overlay. Off
    # by default so the OpenAI wire stays drop-in standard and matches
    # what the Anthropic facade returns.
    if settings.router.expose_classifier_in_response:
        response["brains"] = {
            "classification": classification.model_dump(),
            "plan": planned,
        }
    record_usage_from_response(
        endpoint="openai.chat",
        requested_model=req.model,
        routed_model=model,
        provider=provider_name,
        provider_response=response,
        task_type=classification.task_type,
        session_id=session_attribution(request),
    )
    return response


@router.post("/responses")
def responses(req: ResponsesRequest, request: Request):
    input_value = req.input if isinstance(req.input, str) else json.dumps(req.input)
    faux = ChatCompletionRequest(
        model=req.model,
        messages=[ChatMessage(role="user", content=input_value)],
        stream=req.stream,
    )
    return chat(faux, request)

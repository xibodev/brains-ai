from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from brains.config import settings
from brains.providers.base import Provider


class OllamaProviderError(RuntimeError):
    pass


def _chat_url() -> str:
    return f"{settings.ollama_base_url.rstrip('/')}/api/chat"


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map incoming chat messages onto Ollama's native ``/api/chat`` shape.

    Roles (system / user / assistant / tool) are preserved rather than
    flattened to a text prompt. A message is dropped only when it carries
    nothing — no content AND no tool_calls / tool_call_id — so assistant
    tool-call turns and tool results survive multi-turn tool loops.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user")).strip() or "user"
        raw = message.get("content")
        content = "" if raw is None else str(raw)
        tool_calls = message.get("tool_calls")
        tool_call_id = message.get("tool_call_id")
        if not content.strip() and not tool_calls and not tool_call_id:
            continue
        msg: dict[str, Any] = {"role": role, "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        if message.get("name"):
            msg["name"] = message["name"]
        out.append(msg)
    return out


def _build_options(kwargs: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if kwargs.get("temperature") is not None:
        options["temperature"] = kwargs["temperature"]
    if kwargs.get("max_tokens") is not None:
        options["num_predict"] = kwargs["max_tokens"]
    if kwargs.get("top_p") is not None:
        options["top_p"] = kwargs["top_p"]
    return options


def _build_payload(
    model: str, messages: list[dict], stream: bool, kwargs: dict[str, Any]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(messages),
        "stream": stream,
    }
    options = _build_options(kwargs)
    if options:
        payload["options"] = options
    tools = kwargs.get("tools")
    if tools:
        payload["tools"] = tools
    return payload


def _map_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]] | None:
    """Translate Ollama tool_calls to the OpenAI shape.

    Ollama returns ``[{"function": {"name", "arguments": {..}}}]`` with
    ``arguments`` as an object; OpenAI clients expect a string id, a
    ``type: function`` wrapper, and ``arguments`` as a JSON-encoded string.
    """
    if not raw_tool_calls:
        return None
    mapped: list[dict[str, Any]] = []
    for i, tc in enumerate(raw_tool_calls):
        fn = (tc or {}).get("function", {}) or {}
        args = fn.get("arguments", {})
        if not isinstance(args, str):
            args = json.dumps(args)
        mapped.append(
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": args},
            }
        )
    return mapped or None


def _usage(data: dict[str, Any]) -> dict[str, int]:
    prompt = int(data.get("prompt_eval_count") or 0)
    completion = int(data.get("eval_count") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _chunk(model: str, delta: dict[str, Any], finish_reason: str | None) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-ollama",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
    )


def _error_message(response: Any) -> str:
    detail = ""
    try:
        detail = str((response.json() or {}).get("error", "")).strip()
    except Exception:
        detail = (getattr(response, "text", "") or "").strip()
    suffix = f": {detail}" if detail else ""
    return f"Ollama request failed ({response.status_code}){suffix}"


class OllamaProvider(Provider):
    """Faithful Ollama provider over ``/api/chat``.

    Preserves chat roles, streams tokens, and passes tool/function-calling
    through in both directions — enough fidelity to back a real coding agent
    routed through the gateway, using a free local model and no API key.
    """

    def list_models(self) -> list[dict[str, Any]]:
        """Best-effort enumeration via ``GET /api/tags``.

        Returns the pulled-locally model catalog (Ollama doesn't expose a
        remote registry on this endpoint). Empty list when the daemon is
        unreachable so the UI degrades to free-text entry.
        """
        try:
            response = httpx.get(
                f"{settings.ollama_base_url.rstrip('/')}/api/tags",
                timeout=min(settings.ollama_timeout_seconds, 10.0),
            )
        except httpx.HTTPError:
            return []
        if response.status_code >= 400:
            return []
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            return []
        items = body.get("models") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")
            if not name:
                continue
            details = entry.get("details") or {}
            family = details.get("family") if isinstance(details, dict) else None
            out.append(
                {
                    "id": str(name),
                    "vendor": str(family) if family else "ollama",
                    "label": None,
                }
            )
        return out

    def complete(self, model: str, messages: list[dict], **kwargs: Any) -> dict:
        payload = _build_payload(model, messages, False, kwargs)
        try:
            response = httpx.post(
                _chat_url(), json=payload, timeout=settings.ollama_timeout_seconds
            )
            if response.status_code >= 400:
                raise OllamaProviderError(_error_message(response))
            data = response.json()
        except httpx.TimeoutException as exc:
            raise OllamaProviderError("Ollama request timed out") from exc
        except OllamaProviderError:
            raise
        except httpx.HTTPError as exc:
            raise OllamaProviderError(f"Ollama request failed: {exc}") from exc
        except (ValueError, TypeError) as exc:
            raise OllamaProviderError("Invalid Ollama response payload") from exc

        if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
            raise OllamaProviderError("Invalid Ollama response payload")
        message = data["message"]
        content = str(message.get("content", "") or "")
        tool_calls = _map_tool_calls(message.get("tool_calls"))
        if not content and not tool_calls:
            raise OllamaProviderError("Ollama returned an empty response")

        out_message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            out_message["tool_calls"] = tool_calls
        return {
            "id": "chatcmpl-ollama",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": out_message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": _usage(data),
        }

    def stream(self, model: str, messages: list[dict], **kwargs: Any) -> Iterator[str]:
        payload = _build_payload(model, messages, True, kwargs)
        try:
            with httpx.stream(
                "POST",
                _chat_url(),
                json=payload,
                timeout=settings.ollama_timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise OllamaProviderError(_error_message(response))
                emitted = False
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    message = event.get("message") or {}
                    delta_content = str(message.get("content", "") or "")
                    tool_calls = _map_tool_calls(message.get("tool_calls"))
                    if delta_content or tool_calls:
                        delta: dict[str, Any] = {}
                        if delta_content:
                            delta["content"] = delta_content
                        if tool_calls:
                            delta["tool_calls"] = tool_calls
                        emitted = True
                        yield _chunk(model, delta, None)
                    if event.get("done"):
                        yield _chunk(model, {}, "tool_calls" if tool_calls else "stop")
                        return
                if not emitted:
                    raise OllamaProviderError("Ollama returned an empty response")
                yield _chunk(model, {}, "stop")
        except httpx.TimeoutException as exc:
            raise OllamaProviderError("Ollama request timed out") from exc
        except OllamaProviderError:
            raise
        except httpx.HTTPError as exc:
            raise OllamaProviderError(f"Ollama request failed: {exc}") from exc

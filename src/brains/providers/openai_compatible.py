"""HTTP provider for any OpenAI-compatible chat completions endpoint.

Works with the OpenAI API itself plus drop-in replacements (Together, Groq,
OpenRouter, Anyscale, local vLLM/llama.cpp servers, etc.). The transport is
intentionally minimal: it forwards the OpenAI chat-completions schema, surfaces
upstream errors as structured ``ProviderInvocationError`` instances, and yields
raw SSE chunks for streaming.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import httpx

from brains.config import settings
from brains.providers.base import Provider


class ProviderInvocationError(RuntimeError):
    """Raised when an upstream provider call fails for any reason."""


class OpenAICompatibleProvider(Provider):
    """Forwards chat-completion calls to any OpenAI-compatible HTTP endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._base_url = (base_url or settings.openai_compatible_base_url).rstrip("/")
        self._api_key = api_key or settings.openai_compatible_api_key
        self._timeout = timeout or settings.openai_compatible_timeout_seconds

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_payload(
        self, model: str, messages: list[dict], stream: bool, **kwargs: Any
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        for key in ("temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice"):
            value = kwargs.get(key)
            if value is not None:
                payload[key] = value
        return payload

    def complete(self, model: str, messages: list[dict], **kwargs: Any) -> dict:
        payload = self._build_payload(model, messages, stream=False, **kwargs)
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderInvocationError("openai_compatible: upstream request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderInvocationError(
                f"openai_compatible: upstream transport error: {exc}"
            ) from exc

        if response.status_code >= 400:
            detail = self._extract_error(response)
            raise ProviderInvocationError(
                f"openai_compatible: upstream returned {response.status_code}: {detail}"
            )

        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderInvocationError(
                "openai_compatible: invalid JSON in upstream response"
            ) from exc

    def stream(self, model: str, messages: list[dict], **kwargs: Any) -> Iterator[str]:
        payload = self._build_payload(model, messages, stream=True, **kwargs)
        try:
            with httpx.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=self._timeout,
            ) as response:
                if response.status_code >= 400:
                    # The streaming body has not been loaded yet; read it before
                    # trying to parse an error out of it, otherwise httpx raises
                    # ResponseNotRead and the real upstream error (e.g. a 413
                    # "request too large") is lost behind a 500.
                    with contextlib.suppress(Exception):
                        response.read()
                    detail = self._extract_error(response)
                    raise ProviderInvocationError(
                        f"openai_compatible: upstream returned {response.status_code}: {detail}"
                    )
                for line in response.iter_lines():
                    if not line:
                        continue
                    text = line.decode() if isinstance(line, bytes) else line
                    if text.startswith("data:"):
                        text = text[len("data:") :].strip()
                    if not text or text == "[DONE]":
                        continue
                    yield text
        except httpx.TimeoutException as exc:
            raise ProviderInvocationError("openai_compatible: streaming request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderInvocationError(
                f"openai_compatible: streaming transport error: {exc}"
            ) from exc

    @staticmethod
    def _extract_error(response: httpx.Response) -> str:
        try:
            body = response.json()
        except Exception:
            try:
                text = response.text
            except Exception:  # noqa: BLE001 - unread streaming body, etc.
                return "no body"
            return text.strip() or "no body"
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str):
                return err
        return json.dumps(body)[:512]

    def list_models(self) -> list[dict[str, Any]]:
        """Best-effort enumeration via ``GET {base_url}/models``.

        OpenAI's catalog endpoint returns ``{"data": [{"id": ..., ...}]}``.
        Drop-in providers (Together, vLLM, OpenRouter, LM Studio) follow
        the same shape. Returns ``[]`` on any failure so a dropdown can
        gracefully fall back to free-text entry.
        """
        try:
            response = httpx.get(
                f"{self._base_url}/models",
                headers=self._headers(),
                timeout=min(self._timeout, 10.0),
            )
        except httpx.HTTPError:
            return []
        if response.status_code >= 400:
            return []
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            return []
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id") or entry.get("name")
            if not model_id:
                continue
            vendor = entry.get("owned_by") or entry.get("vendor")
            out.append(
                {
                    "id": str(model_id),
                    "vendor": str(vendor) if vendor else None,
                    "label": None,
                }
            )
        return out

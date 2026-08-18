"""Optional LiteLLM provider.

LiteLLM is a popular unification layer over many model APIs (OpenAI, Anthropic,
Bedrock, Vertex AI, Mistral, etc.). It is an optional dependency: install with
``pip install brains-ai[litellm]``. If the package is missing, the provider raises
a clear configuration error on first use instead of at import time, so the rest
of Brains keeps working in environments without LiteLLM.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from brains.config import settings
from brains.providers.base import Provider
from brains.providers.openai_compatible import ProviderInvocationError


class LiteLLMProvider(Provider):
    """Routes calls through the litellm SDK when installed."""

    def __init__(self, timeout: float | None = None) -> None:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover - guarded path
            raise ProviderInvocationError(
                "litellm provider requested but litellm is not installed. "
                "Install with: pip install 'brains-ai[litellm]'"
            ) from exc
        self._litellm = litellm
        self._timeout = timeout or settings.litellm_timeout_seconds

    def complete(self, model: str, messages: list[dict], **kwargs: Any) -> dict:
        try:
            response = self._litellm.completion(
                model=model,
                messages=messages,
                stream=False,
                timeout=self._timeout,
                **self._forward_kwargs(kwargs),
            )
        except Exception as exc:  # litellm exceptions are diverse
            raise ProviderInvocationError(f"litellm: {exc}") from exc

        # litellm responses are pydantic-like; normalize to plain dict for our gateway.
        if hasattr(response, "model_dump"):
            return response.model_dump()
        if hasattr(response, "dict"):
            return response.dict()
        return dict(response)

    def stream(self, model: str, messages: list[dict], **kwargs: Any) -> Iterator[str]:
        try:
            iterator = self._litellm.completion(
                model=model,
                messages=messages,
                stream=True,
                timeout=self._timeout,
                **self._forward_kwargs(kwargs),
            )
        except Exception as exc:
            raise ProviderInvocationError(f"litellm: {exc}") from exc

        try:
            for chunk in iterator:
                if hasattr(chunk, "model_dump"):
                    payload: Any = chunk.model_dump()
                elif hasattr(chunk, "dict"):
                    payload = chunk.dict()
                else:
                    payload = chunk
                yield json.dumps(payload)
        except Exception as exc:
            raise ProviderInvocationError(f"litellm stream: {exc}") from exc

    @staticmethod
    def _forward_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        allowed = ("temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice")
        return {k: kwargs[k] for k in allowed if kwargs.get(k) is not None}

    def list_models(self) -> list[dict[str, Any]]:
        """Curated catalog of common LiteLLM model strings.

        LiteLLM has no single catalog endpoint — its strength is breadth
        across vendors that each have their own. We return a small curated
        list of the most-used identifiers so the UI dropdown ships with
        sensible options; the field stays editable for everything else.
        """
        return [
            {"id": "openai/gpt-4o", "vendor": "openai", "label": "OpenAI GPT-4o"},
            {"id": "openai/gpt-4o-mini", "vendor": "openai", "label": "OpenAI GPT-4o mini"},
            {"id": "openai/o3-mini", "vendor": "openai", "label": "OpenAI o3-mini"},
            {
                "id": "anthropic/claude-3-5-sonnet-latest",
                "vendor": "anthropic",
                "label": "Claude 3.5 Sonnet",
            },
            {
                "id": "anthropic/claude-3-5-haiku-latest",
                "vendor": "anthropic",
                "label": "Claude 3.5 Haiku",
            },
            {"id": "gemini/gemini-2.0-flash", "vendor": "google", "label": "Gemini 2.0 Flash"},
            {"id": "mistral/mistral-large-latest", "vendor": "mistral", "label": "Mistral Large"},
            {
                "id": "groq/llama-3.3-70b-versatile",
                "vendor": "groq",
                "label": "Llama 3.3 70B (Groq)",
            },
            {
                "id": "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
                "vendor": "aws-bedrock",
                "label": "Claude 3.5 Sonnet (Bedrock)",
            },
        ]

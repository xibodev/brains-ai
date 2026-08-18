"""GitHub Copilot provider — proxies the Copilot chat-completions endpoint.

Maps OpenAI-shaped requests onto ``https://api.githubcopilot.com/chat/completions``
using a session token resolved through :mod:`brains.auth.copilot`. The
Copilot endpoint already speaks the OpenAI chat-completions schema, so
the request/response plumbing is intentionally thin — the value this
provider adds is OAuth resolution, session-token caching, and the editor
identity headers that unlock the chat endpoint.

Models exposed by the upstream depend on the user's Copilot subscription
(``gpt-5``, ``claude-sonnet-4.5``, ``gemini-2.5-pro``, ``o3-mini`` etc.);
the provider passes the ``model`` field through unchanged.

NOTE: GitHub's Copilot terms scope it to "code suggestions in editors."
Using it as a general gateway provider is the same path OpenCode/aider
take, but operators should treat it as a personal-use grey area, not a
sanctioned public API. The endpoint and headers can change without
notice.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import httpx

from brains.auth.copilot import (
    COPILOT_TOS_WARNING,
    CopilotAuthError,
    CopilotSession,
    assert_copilot_proxy_allowed,
    get_session,
)
from brains.config import settings
from brains.providers.base import Provider
from brains.providers.openai_compatible import ProviderInvocationError

logger = logging.getLogger(__name__)

_tos_warned = False
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TOKEN_RE = re.compile(
    r"\b(?:gh[oupsr]_[A-Za-z0-9_]{10,}|sk-[A-Za-z0-9_-]{10,}|[A-Fa-f0-9]{20,}|[A-Za-z0-9]{20,})\b"
)


def _redact(text: str) -> str:
    text = _EMAIL_RE.sub("[redacted]", text)
    return _TOKEN_RE.sub("[redacted]", text)


def _warn_tos_once() -> None:
    """Emit the Copilot grey-area notice once per process, on first use."""
    global _tos_warned
    if not _tos_warned:
        _tos_warned = True
        logger.warning("github_copilot provider in use: %s", COPILOT_TOS_WARNING)


def _request_headers(session: CopilotSession) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {session.token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Copilot-Integration-Id": settings.github_copilot_integration_id,
        "Editor-Version": settings.github_copilot_editor_version,
        "Editor-Plugin-Version": "brains-gateway/0.1",
        "OpenAI-Intent": "conversation-panel",
        "User-Agent": "GithubCopilotChat/brains-gateway",
    }


def _build_payload(
    model: str, messages: list[dict], stream: bool, kwargs: dict[str, Any]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    for key in (
        "temperature",
        "top_p",
        "max_tokens",
        "stop",
        "tools",
        "tool_choice",
        "reasoning_effort",
    ):
        value = kwargs.get(key)
        if value is not None:
            payload[key] = value
    return payload


def _extract_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return _redact((getattr(response, "text", "") or "").strip() or "no body")
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return _redact(str(err["message"]))
        if isinstance(err, str):
            return _redact(err)
    return _redact(json.dumps(body)[:512])


def _extract_capabilities(caps: Any) -> dict[str, Any]:
    """Distill the Copilot ``capabilities`` block into the bits an operator
    actually wants when choosing a model: the **reasoning_effort** levels it
    accepts (``none``/``low``/``medium``/``high``/``xhigh`` — i.e. its
    thinking levels), the headline feature flags, and the context window.

    Returns an empty dict for models that report nothing useful, so the
    field is simply omitted for non-reasoning models like ``gpt-4o``.
    """
    if not isinstance(caps, dict):
        return {}
    out: dict[str, Any] = {}
    supports = caps.get("supports")
    if isinstance(supports, dict):
        reasoning = supports.get("reasoning_effort")
        if isinstance(reasoning, list) and reasoning:
            out["reasoning_effort"] = [str(x) for x in reasoning]
        for flag in (
            "streaming",
            "tool_calls",
            "vision",
            "structured_outputs",
            "parallel_tool_calls",
        ):
            val = supports.get(flag)
            if isinstance(val, bool):
                out[flag] = val
    limits = caps.get("limits")
    if isinstance(limits, dict):
        ctx = limits.get("max_context_window_tokens") or limits.get("max_prompt_tokens")
        if isinstance(ctx, int):
            out["context_window"] = ctx
        mx = limits.get("max_output_tokens")
        if isinstance(mx, int):
            out["max_output_tokens"] = mx
    family = caps.get("family")
    if isinstance(family, str) and family:
        out["family"] = family
    return out


class GitHubCopilotProvider(Provider):
    """Forwards chat-completion calls to GitHub Copilot's chat endpoint."""

    def _session(self, *, force_refresh: bool = False) -> CopilotSession:
        _warn_tos_once()
        try:
            assert_copilot_proxy_allowed()
            return get_session(force_refresh=force_refresh)
        except CopilotAuthError as exc:
            raise ProviderInvocationError(f"github_copilot: {exc}") from exc

    def complete(self, model: str, messages: list[dict], **kwargs: Any) -> dict:
        session = self._session()
        url = f"{session.chat_base_url}/chat/completions"
        payload = _build_payload(model, messages, stream=False, kwargs=kwargs)
        timeout = settings.github_copilot_timeout_seconds

        try:
            response = httpx.post(
                url, json=payload, headers=_request_headers(session), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise ProviderInvocationError("github_copilot: upstream request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderInvocationError(
                f"github_copilot: upstream transport error: {exc}"
            ) from exc

        # 401 means our session token is stale or revoked. Force a refresh
        # exactly once before declaring failure so a 30-min expiry boundary
        # doesn't surface as a user-visible error.
        if response.status_code == 401:
            session = self._session(force_refresh=True)
            try:
                response = httpx.post(
                    f"{session.chat_base_url}/chat/completions",
                    json=payload,
                    headers=_request_headers(session),
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                raise ProviderInvocationError(
                    f"github_copilot: upstream transport error on retry: {exc}"
                ) from exc

        if response.status_code >= 400:
            detail = _extract_error(response)
            raise ProviderInvocationError(
                f"github_copilot: upstream returned {response.status_code}: {detail}"
            )

        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderInvocationError(
                "github_copilot: invalid JSON in upstream response"
            ) from exc

    def stream(self, model: str, messages: list[dict], **kwargs: Any) -> Iterator[str]:
        session = self._session()
        url = f"{session.chat_base_url}/chat/completions"
        payload = _build_payload(model, messages, stream=True, kwargs=kwargs)
        timeout = settings.github_copilot_timeout_seconds

        try:
            with httpx.stream(
                "POST",
                url,
                json=payload,
                headers=_request_headers(session),
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    detail = _extract_error(response)
                    raise ProviderInvocationError(
                        f"github_copilot: upstream returned {response.status_code}: {detail}"
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
            raise ProviderInvocationError("github_copilot: streaming request timed out") from exc
        except ProviderInvocationError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderInvocationError(
                f"github_copilot: streaming transport error: {exc}"
            ) from exc

    def list_models(self) -> list[dict[str, Any]]:
        """Best-effort catalog via ``GET {session.api}/models``.

        Returns the plan-specific Copilot catalog (varies by subscription
        — Individual, Business, Enterprise). Empty list when auth has
        not been set up yet, when the session can't refresh, or when
        the upstream rejects us — keeping the UI fall-back path clean
        regardless of failure mode.
        """
        try:
            session = self._session()
        except ProviderInvocationError:
            return []
        try:
            response = httpx.get(
                f"{session.chat_base_url}/models",
                headers=_request_headers(session),
                timeout=min(settings.github_copilot_timeout_seconds, 10.0),
            )
        except httpx.HTTPError:
            return []
        if response.status_code == 401:
            try:
                session = self._session(force_refresh=True)
                response = httpx.get(
                    f"{session.chat_base_url}/models",
                    headers=_request_headers(session),
                    timeout=min(settings.github_copilot_timeout_seconds, 10.0),
                )
            except (httpx.HTTPError, ProviderInvocationError):
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
            vendor = entry.get("vendor") or entry.get("owned_by")
            label = entry.get("name") if entry.get("name") != model_id else None
            row: dict[str, Any] = {
                "id": str(model_id),
                "vendor": str(vendor) if vendor else None,
                "label": str(label) if label else None,
            }
            caps = _extract_capabilities(entry.get("capabilities"))
            if caps:
                row["capabilities"] = caps
            out.append(row)
        return out


__all__ = ["GitHubCopilotProvider"]

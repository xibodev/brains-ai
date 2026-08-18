import json
from typing import Any

from .base import Provider


class EchoProvider(Provider):
    """Built-in stub provider used by the dev harness and the test suite.

    Echo is **not** a real upstream — it returns a deterministic
    ``echo:<prompt>`` reply so the gateway boots out of the box without
    requiring any API keys. Marked ``is_stub=True`` so the savings
    ledger excludes its traffic from the dashboard headline (counting
    synthetic calls against a real baseline like ``claude-opus-4``
    would inflate the savings number with noise) and the admin UI can
    label it visibly. Replace with a real provider in production.
    """

    is_stub = True

    def complete(self, model: str, messages: list, **kwargs):
        content = f"echo:{messages[-1].get('content', '')}" if messages else "echo:"
        return {
            "id": "chatcmpl-echo",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def stream(self, model: str, messages: list, **kwargs):
        text = f"echo:{messages[-1].get('content', '')}" if messages else "echo:"
        for ch in [text[: max(1, len(text) // 2)], text[max(1, len(text) // 2) :]]:
            yield json.dumps(
                {
                    "id": "chatcmpl-echo",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": ch}, "finish_reason": None}],
                }
            )
        yield json.dumps(
            {
                "id": "chatcmpl-echo",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {"id": "echo-small", "vendor": "echo", "label": "Echo (small)"},
            {"id": "echo-default", "vendor": "echo", "label": "Echo (default)"},
            {"id": "echo-strong", "vendor": "echo", "label": "Echo (strong)"},
            {"id": "echo-deep", "vendor": "echo", "label": "Echo (deep)"},
        ]

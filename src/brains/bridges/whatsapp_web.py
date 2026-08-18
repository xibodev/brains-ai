"""WhatsApp Web bridge (companion-device, via the ``wa-web`` sidecar).

OpenClaw-style channel: instead of the Meta Cloud API, brains pushes outbound
notifications to a small Node/Baileys sidecar that is linked to *your own*
WhatsApp as a companion device and relays them into one dedicated chat. Inbound
replies flow the other way (sidecar → brains ``/relay/reply``), so this module
is **outbound-only** — it just POSTs text to the sidecar's ``/send`` endpoint.

See ``services/wa-web/`` for the sidecar.

Configuration (env vars, NEVER the overlay):

- ``BRAINS_WHATSAPP_WEB_URL`` — sidecar base URL, e.g. ``http://localhost:8788``.
- ``BRAINS_WHATSAPP_WEB_TOKEN`` — bearer matching the sidecar's ``WA_SEND_TOKEN``
  (optional but strongly recommended; the sidecar rejects /send without it when set).

Gated by ``subsystems.bridges.whatsapp_web.enabled``. The ``whatsapp_web`` extra
is a marker extra (only needs ``httpx``, already in the lean core).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from brains.bridges.base import (
    BridgeKind,
    BridgeMessage,
    BridgeStatus,
    Sender,
)
from brains.bridges.base import (
    send_message as _send_message,
)

_ENV_URL = "BRAINS_WHATSAPP_WEB_URL"
_ENV_TOKEN = "BRAINS_WHATSAPP_WEB_TOKEN"
NAME = "whatsapp_web"


def status(settings_obj: Any) -> BridgeStatus:
    enabled = bool(
        getattr(
            getattr(getattr(settings_obj, "subsystems", None), "bridges", None),
            "whatsapp_web",
            None,
        )
        and settings_obj.subsystems.bridges.whatsapp_web.enabled
    )
    has_url = bool(os.environ.get(_ENV_URL))
    if not enabled:
        detail = "disabled in subsystems.bridges.whatsapp_web"
    elif not has_url:
        detail = f"missing env: ${_ENV_URL}"
    else:
        detail = "ready"
    return BridgeStatus(
        name=NAME,
        enabled=enabled,
        configured=enabled and has_url,
        detail=detail,
    )


def build_sender(
    url: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Sender:
    """Return a callable that POSTs text to the wa-web sidecar's ``/send``.

    ``transport`` is injected by tests via ``httpx.MockTransport`` so we can
    assert outbound JSON without a real network call.
    """
    resolved_url = url if url is not None else os.environ.get(_ENV_URL)
    resolved_token = token if token is not None else os.environ.get(_ENV_TOKEN)
    if not resolved_url:
        raise RuntimeError(f"WhatsApp Web bridge requires env var: ${_ENV_URL}")

    endpoint = resolved_url.rstrip("/") + "/send"
    headers = {"Content-Type": "application/json"}
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"

    def _send(body: str) -> bool:
        client_kwargs: dict[str, Any] = {"timeout": 15.0}
        if transport is not None:
            client_kwargs["transport"] = transport
        with httpx.Client(**client_kwargs) as client:
            response = client.post(endpoint, json={"text": body}, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(
                f"WhatsApp Web send failed: HTTP {response.status_code} {response.text[:200]}"
            )
        return True

    return _send


def send(
    text: str,
    kind: BridgeKind = BridgeKind.INFO,
    correlation_id: str | None = None,
    sender: Sender | None = None,
) -> bool:
    msg = BridgeMessage(kind=kind, text=text, correlation_id=correlation_id)
    actual = sender if sender is not None else build_sender()
    return _send_message(actual, msg)


__all__ = ["NAME", "build_sender", "send", "status"]

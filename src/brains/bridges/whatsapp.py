"""WhatsApp Cloud API bridge (Meta Business Platform).

Out-bound only in this first cut: post text messages via the Cloud API
``/messages`` endpoint. Inbound (webhook) receiver is scaffolded in
:mod:`brains.bridges.whatsapp_webhook` (future — needs a Meta Business
account, app review, and a public HTTPS endpoint to exercise).

Configuration (env vars, NEVER the overlay):

- ``BRAINS_WHATSAPP_TOKEN`` — Bearer access token from the Meta app.
- ``BRAINS_WHATSAPP_PHONE_ID`` — Phone Number ID (NOT the phone number).
- ``BRAINS_WHATSAPP_RECIPIENT`` — E.164 phone number (e.g. ``+447700900123``)
  of the operator to ping. WhatsApp Cloud requires the recipient to have
  messaged the bot first within 24h OR to use an approved template.

Gated by ``subsystems.bridges.whatsapp.enabled``. The ``whatsapp`` extra
is a marker extra (no probe modules) because we only need ``httpx``,
which is already in the lean core.
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

_ENV_TOKEN = "BRAINS_WHATSAPP_TOKEN"
_ENV_PHONE_ID = "BRAINS_WHATSAPP_PHONE_ID"
_ENV_RECIPIENT = "BRAINS_WHATSAPP_RECIPIENT"
_API_VERSION = "v18.0"
_API_BASE = "https://graph.facebook.com"
NAME = "whatsapp"


def status(settings_obj: Any) -> BridgeStatus:
    enabled = bool(
        getattr(
            getattr(getattr(settings_obj, "subsystems", None), "bridges", None),
            "whatsapp",
            None,
        )
        and settings_obj.subsystems.bridges.whatsapp.enabled
    )
    missing = [
        var for var in (_ENV_TOKEN, _ENV_PHONE_ID, _ENV_RECIPIENT) if not os.environ.get(var)
    ]
    if not enabled:
        detail = "disabled in subsystems.bridges.whatsapp"
    elif missing:
        detail = "missing env: " + ", ".join(f"${name}" for name in missing)
    else:
        detail = "ready"
    return BridgeStatus(
        name=NAME,
        enabled=enabled,
        configured=enabled and not missing,
        detail=detail,
    )


def build_sender(
    token: str | None = None,
    phone_id: str | None = None,
    recipient: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Sender:
    """Return a callable that posts text via the WhatsApp Cloud API.

    ``transport`` is injected by tests via ``httpx.MockTransport`` so we
    can assert outbound JSON without a real network call.
    """
    resolved_token = token if token is not None else os.environ.get(_ENV_TOKEN)
    resolved_phone = phone_id if phone_id is not None else os.environ.get(_ENV_PHONE_ID)
    resolved_recipient = recipient if recipient is not None else os.environ.get(_ENV_RECIPIENT)
    missing = [
        name
        for name, value in (
            (_ENV_TOKEN, resolved_token),
            (_ENV_PHONE_ID, resolved_phone),
            (_ENV_RECIPIENT, resolved_recipient),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "WhatsApp bridge requires env vars: " + ", ".join(f"${name}" for name in missing)
        )

    url = f"{_API_BASE}/{_API_VERSION}/{resolved_phone}/messages"
    headers = {
        "Authorization": f"Bearer {resolved_token}",
        "Content-Type": "application/json",
    }

    def _send(body: str) -> bool:
        payload = {
            "messaging_product": "whatsapp",
            "to": resolved_recipient,
            "type": "text",
            "text": {"body": body},
        }
        client_kwargs: dict[str, Any] = {"timeout": 15.0}
        if transport is not None:
            client_kwargs["transport"] = transport
        with httpx.Client(**client_kwargs) as client:
            response = client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(
                f"WhatsApp send failed: HTTP {response.status_code} {response.text[:200]}"
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

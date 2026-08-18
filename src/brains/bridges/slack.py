"""Slack bridge.

Out-bound only in this first cut: post to a Slack channel via the
``chat.postMessage`` Web API (which only needs a bot token). Socket-mode
inbound replies are scaffolded in :mod:`brains.bridges.slack_receiver`
(future — needs an app-level token to exercise meaningfully).

Configuration (env vars, NEVER the overlay):

- ``BRAINS_SLACK_BOT_TOKEN`` — ``xoxb-...`` bot token. Required.
- ``BRAINS_SLACK_CHANNEL`` — channel ID (e.g. ``C0123ABC``) or ``#name``.
  Required.

Gated by ``subsystems.bridges.slack.enabled`` AND ``pip install 'brains-ai[slack]'``.
"""

from __future__ import annotations

import os
from typing import Any

from brains.bridges.base import (
    BridgeKind,
    BridgeMessage,
    BridgeStatus,
    Sender,
)
from brains.bridges.base import (
    send_message as _send_message,
)

_ENV_TOKEN = "BRAINS_SLACK_BOT_TOKEN"
_ENV_CHANNEL = "BRAINS_SLACK_CHANNEL"
NAME = "slack"


def status(settings_obj: Any) -> BridgeStatus:
    enabled = bool(
        getattr(
            getattr(getattr(settings_obj, "subsystems", None), "bridges", None),
            "slack",
            None,
        )
        and settings_obj.subsystems.bridges.slack.enabled
    )
    has_token = bool(os.environ.get(_ENV_TOKEN))
    has_channel = bool(os.environ.get(_ENV_CHANNEL))
    if not enabled:
        detail = "disabled in subsystems.bridges.slack"
    elif not has_token and not has_channel:
        detail = f"set ${_ENV_TOKEN} and ${_ENV_CHANNEL} to enable"
    elif not has_token:
        detail = f"missing env ${_ENV_TOKEN}"
    elif not has_channel:
        detail = f"missing env ${_ENV_CHANNEL}"
    else:
        detail = "ready"
    return BridgeStatus(
        name=NAME,
        enabled=enabled,
        configured=enabled and has_token and has_channel,
        detail=detail,
    )


def build_sender(token: str | None = None, channel: str | None = None) -> Sender:
    """Return a callable that posts text to a Slack channel."""
    resolved_token = token if token is not None else os.environ.get(_ENV_TOKEN)
    resolved_channel = channel if channel is not None else os.environ.get(_ENV_CHANNEL)
    if not resolved_token:
        raise RuntimeError(
            f"Slack bridge requires ${_ENV_TOKEN}. Set it in the environment and restart."
        )
    if not resolved_channel:
        raise RuntimeError(
            f"Slack bridge requires ${_ENV_CHANNEL}. Set it in the environment and restart."
        )

    # Deferred import — extra may not be installed at module-load time.
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=resolved_token)

    def _send(body: str) -> bool:
        try:
            response = client.chat_postMessage(channel=resolved_channel, text=body)
        except SlackApiError as exc:
            raise RuntimeError(f"Slack send failed: {exc.response['error']}") from exc
        return bool(response.get("ok"))

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

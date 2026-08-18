"""Telegram bridge.

Out-bound only in this first cut: send approvals / alerts / info pings to a
Telegram chat. Long-polling / inbound replies are scaffolded in
:mod:`brains.bridges.telegram_receiver` (next session — needs a live bot
token to exercise meaningfully).

Configuration (env vars, NEVER the overlay):

- ``BRAINS_TELEGRAM_BOT_TOKEN`` — bot token issued by @BotFather. Required.
- ``BRAINS_TELEGRAM_CHAT_ID`` — destination chat ID (operator's private
  chat or a group). Required.

The bridge is gated by ``subsystems.bridges.telegram.enabled`` in the
runtime overlay AND by ``pip install 'brains-ai[telegram]'``. Both gates are
enforced by :func:`brains.config._enforce_subsystem_extras` at startup.
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

_ENV_TOKEN = "BRAINS_TELEGRAM_BOT_TOKEN"
_ENV_CHAT_ID = "BRAINS_TELEGRAM_CHAT_ID"
NAME = "telegram"


def status(settings_obj: Any) -> BridgeStatus:
    """Cheap, no-network status snapshot.

    Reads the overlay flag and env-var presence. Does NOT import the
    ``telegram`` package so it's safe to call when the extra is missing.
    """
    enabled = bool(
        getattr(
            getattr(getattr(settings_obj, "subsystems", None), "bridges", None),
            "telegram",
            None,
        )
        and settings_obj.subsystems.bridges.telegram.enabled
    )
    has_token = bool(os.environ.get(_ENV_TOKEN))
    has_chat = bool(os.environ.get(_ENV_CHAT_ID))
    if not enabled:
        detail = "disabled in subsystems.bridges.telegram"
    elif not has_token and not has_chat:
        detail = f"set ${_ENV_TOKEN} and ${_ENV_CHAT_ID} to enable"
    elif not has_token:
        detail = f"missing env ${_ENV_TOKEN}"
    elif not has_chat:
        detail = f"missing env ${_ENV_CHAT_ID}"
    else:
        detail = "ready"
    return BridgeStatus(
        name=NAME,
        enabled=enabled,
        configured=enabled and has_token and has_chat,
        detail=detail,
    )


def build_sender(token: str | None = None, chat_id: str | None = None) -> Sender:
    """Return a callable that posts text to Telegram.

    The actual SDK import is deferred until the sender is built so importing
    this module never requires the extra. ``token`` and ``chat_id`` default
    to the env vars, which is the production path; tests can pass values
    directly to avoid touching the environment.
    """
    resolved_token = token if token is not None else os.environ.get(_ENV_TOKEN)
    resolved_chat = chat_id if chat_id is not None else os.environ.get(_ENV_CHAT_ID)
    if not resolved_token:
        raise RuntimeError(
            f"Telegram bridge requires ${_ENV_TOKEN}. Set it in the environment and restart."
        )
    if not resolved_chat:
        raise RuntimeError(
            f"Telegram bridge requires ${_ENV_CHAT_ID}. Set it in the environment and restart."
        )

    # Deferred import — extras may not be installed at module-load time.
    from telegram import Bot

    bot = Bot(token=resolved_token)

    def _send(body: str) -> bool:
        # python-telegram-bot's send_message is async since v20. We bridge
        # it via asyncio.run because the rest of Brains is sync and this
        # is a one-shot per-message call (not a hot loop).
        import asyncio

        async def _go() -> None:
            await bot.send_message(chat_id=resolved_chat, text=body)

        try:
            asyncio.run(_go())
        except RuntimeError:
            # ``asyncio.run`` raises if we're already inside a loop. Fall
            # back to the running loop in that case.
            loop = asyncio.get_event_loop()
            loop.run_until_complete(_go())
        return True

    return _send


def send(
    text: str,
    kind: BridgeKind = BridgeKind.INFO,
    correlation_id: str | None = None,
    sender: Sender | None = None,
) -> bool:
    """High-level entry point: send one message.

    ``sender`` is injected by tests; production callers leave it None and
    we build a real Telegram sender on demand.
    """
    msg = BridgeMessage(kind=kind, text=text, correlation_id=correlation_id)
    actual = sender if sender is not None else build_sender()
    return _send_message(actual, msg)


__all__ = [
    "NAME",
    "build_sender",
    "send",
    "status",
]

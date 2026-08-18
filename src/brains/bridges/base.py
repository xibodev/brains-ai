"""Shared types and helpers for operator-control bridges.

A bridge sends one direction of operator-facing notifications (approvals,
done/stuck pings, recurring-job status) and optionally accepts replies that
turn into ``decisions``/``mailbox`` ledger events.

Design rules — these apply to every concrete bridge (Telegram, Slack,
WhatsApp, future Discord, etc.):

1. Tokens live in **env vars**, never in the overlay. The overlay only
   carries the ``enabled`` flag. This matches the existing secrets policy
   (``ENV_REF_ALLOWED_FIELDS`` is the only place ``${ENV:NAME}`` is
   honoured).
2. The bridge module exposes two pure-data helpers — ``status()`` returns
   "are we configured" and ``BridgeMessage`` describes one outbound ping.
3. Side-effecting calls (``send_message``) accept an injected ``send_fn``
   so tests can stub the SDK without monkeypatching deep imports.
4. Receiver loops (long-polling, sockets, webhooks) live in their own
   ``run_*`` module and are NOT imported by default — they're heavy and
   require live credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class BridgeKind(StrEnum):
    """Why the bridge is being asked to send a message.

    Operators wire alerting differently per kind (e.g. silence INFO at
    night, page on APPROVAL).
    """

    INFO = "info"
    APPROVAL = "approval"
    ALERT = "alert"


@dataclass(frozen=True)
class BridgeMessage:
    """A single outbound notification.

    ``correlation_id`` lets the receiver tie a reply back to a decision or
    mailbox entry. Optional — INFO pings often have nothing to wait on.
    """

    kind: BridgeKind
    text: str
    correlation_id: str | None = None


@dataclass(frozen=True)
class BridgeStatus:
    """Runtime introspection for ``brains health`` and the dashboard."""

    name: str
    enabled: bool
    configured: bool
    detail: str = ""


# A ``Sender`` is the lowest-level outbound primitive: text in, success bool
# out. Bridges return one from ``build_sender`` after validating env-var
# credentials, and ``send_message`` composes it with the BridgeMessage.
Sender = Callable[[str], bool]


def send_message(sender: Sender, message: BridgeMessage) -> bool:
    """Format ``message`` and hand it to ``sender``.

    Currently the format is just a kind-prefixed text body, e.g.::

        [APPROVAL] Recurring task 'nightly-summary' needs your sign-off.
        ref: decision-7a3f

    Concrete bridges may post-process this string (Slack adds blocks,
    WhatsApp adds an interactive button, etc.).
    """
    prefix = f"[{message.kind.value.upper()}]"
    body = f"{prefix} {message.text}"
    if message.correlation_id:
        body = f"{body}\nref: {message.correlation_id}"
    return sender(body)


__all__ = [
    "BridgeKind",
    "BridgeMessage",
    "BridgeStatus",
    "Sender",
    "send_message",
]

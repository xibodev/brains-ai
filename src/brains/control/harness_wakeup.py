"""Harness-owned, body-free wakeup boundary for durable mailbox delivery.

The harness supplies only its native Session identity and current directory.
Brains resolves an already-managed attachment, claims one fixed nudge, and emits
the harness' documented stop-hook continuation shape. Durable mail remains the
source of truth and is never copied into hook output.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import Any

from brains.control.durable_mail import (
    MAILBOX_NUDGE,
    settle_mailbox_notification,
    take_mailbox_notification,
)
from brains.control.durable_mailbox import (
    canonical_mailbox_tool,
    resolve_managed_notification_proof,
)

_TURN_BOUNDARY_TOOLS = frozenset({"claude-code"})


def _event_identity(tool: str, payload: dict[str, Any]) -> tuple[str, str, bool]:
    if tool == "claude-code":
        native_id = payload.get("session_id")
        workspace = payload.get("cwd")
        already_continued = payload.get("stop_hook_active", False)
    else:
        raise ValueError("unsupported harness wakeup adapter")
    if not isinstance(native_id, str) or not isinstance(workspace, str):
        raise ValueError("harness wakeup identity is unavailable")
    return native_id, workspace, already_continued is True


def handle_harness_wakeup(
    adapter: str,
    payload: dict[str, Any],
    *,
    emit: Callable[[dict[str, str]], None],
) -> dict[str, Any]:
    """Return one safe hook response plus a bounded internal outcome.

    ``output`` is the only portion printed to the harness. It is either empty or
    the fixed body-free continuation directive. The remaining fields are useful
    to local diagnostics/tests and intentionally contain no paths, credentials,
    mailbox addresses, native IDs, notification IDs, or message content.
    """
    emitted = False

    def emit_once(output: dict[str, str]) -> None:
        nonlocal emitted
        emitted = True
        emit(output)

    try:
        tool = canonical_mailbox_tool(adapter)
        if tool not in _TURN_BOUNDARY_TOOLS:
            output: dict[str, str] = {}
            emit_once(output)
            return {"state": "pull", "reason": "adapter-unavailable", "output": output}
        native_id, workspace, already_continued = _event_identity(tool, payload)
        if already_continued:
            output = {}
            emit_once(output)
            return {"state": "pull", "reason": "continuation-bounded", "output": output}
        session_id, binding_secret, mode = resolve_managed_notification_proof(
            workspace,
            tool,
            native_id,
        )
        if mode != "turn_boundary":
            output = {}
            emit_once(output)
            return {"state": "pull", "reason": "adapter-unavailable", "output": output}
        claimed = take_mailbox_notification(session_id, binding_secret)
        notification = claimed.get("notification")
        if not isinstance(notification, dict) or notification.get("status") != "claimed":
            reason = "delivery-uncertain" if claimed.get("uncertain") is True else "no-pending-mail"
            output = {}
            emit_once(output)
            return {"state": "pull", "reason": reason, "output": output}
        notification_id = notification.get("notification_id")
        if not isinstance(notification_id, str):
            output = {}
            emit_once(output)
            return {"state": "pull", "reason": "delivery-uncertain", "output": output}
        output = {"decision": "block", "reason": MAILBOX_NUDGE}
        emit_once(output)
        try:
            settle_mailbox_notification(
                session_id,
                binding_secret,
                notification_id,
                status="delivered",
            )
        except Exception:
            # The hook has already flushed its only safe output. Any storage or
            # event-journal failure after that point is an uncertain delivery,
            # so the claimed row is deliberately left for lease-based reclaim.
            return {"state": "uncertain", "reason": "settlement-unconfirmed", "output": output}
        return {"state": "delivered", "reason": "hook-continuation", "output": output}
    except Exception:
        # Stop hooks must fail closed. Never echo an exception because it may
        # include a path, identity, or driver detail.
        output = {}
        if emitted:
            return {"state": "uncertain", "reason": "output-unconfirmed", "output": output}
        if not emitted:
            with suppress(OSError, ValueError, TypeError):
                emit_once(output)
        return {"state": "pull", "reason": "adapter-unavailable", "output": output}


__all__ = ["handle_harness_wakeup"]

"""Messaging relay for the gated executor — approvals + webhook-triggered triage.

Three jobs:

1. ``notify_pending_approval`` — when the action gate files an outward-action ASK,
   fan it out to whatever messaging bridge is configured (WhatsApp / Telegram /
   Slack) so the operator can approve from their phone, away from the box.

2. ``handle_inbound_reply`` — parse an inbound operator message ("approve
   ASK-0005", "deny ASK-0005 risky") and resolve the decision. Used by the
   WhatsApp/Telegram inbound webhooks.

3. ``trigger_triage`` — a webhook (Chatwoot, bugsink, …) hands brains a raw event
   (a new bug, a conversation); brains spawns a **gated** copilot+brains session
   to analyse/act on it in the target repo, with every outward action still
   blocked for operator approval. The whole point: copilot+brains is authed to
   git and knows the codebase, so it can actually triage — but it can't *ship*
   anything without a human yes.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# inbound command grammar: "<verb> <CODE> [reason...]"
_REPLY_RE = re.compile(
    r"^\s*(?P<verb>approve|approved|yes|ok|deny|denied|no|reject|rejected)\s+"
    r"(?P<code>ASK-\d+)\s*(?P<reason>.*)$",
    re.IGNORECASE,
)
_APPROVE_VERBS = {"approve", "approved", "yes", "ok"}


def _active_bridge_senders() -> list[Callable[..., object]]:
    """Return outbound ``send(text, ...)`` callables for configured bridges.

    A bridge that is not configured is not an attempted delivery.
    """
    senders: list[Callable[..., object]] = []
    from brains.config import settings

    for name in ("whatsapp", "whatsapp_web", "telegram", "slack"):
        try:
            module = __import__(f"brains.bridges.{name}", fromlist=["status", "send"])
            st = module.status(settings)
            if getattr(st, "configured", False):
                senders.append(module.send)
        except Exception:
            continue
    return senders


def notify_pending_approval(code: str, action_type: str, summary: str, cwd: str) -> int:
    """Push a pending-approval message to all configured bridges. Returns count sent."""
    text = (
        f"🔐 brains needs approval ({code})\n"
        f"action: {action_type}\n"
        f"  {summary}\n"
        f"dir: {cwd}\n\n"
        f"reply: approve {code}   |   deny {code} <reason>"
    )
    from brains.control import integration_deliveries as deliveries_ctl

    sent = 0
    for send in _active_bridge_senders():
        bridge = str(
            getattr(
                send,
                "_brains_bridge_name",
                getattr(send, "__module__", "bridge").rsplit(".", 1)[-1],
            )
        )
        delivery, created = deliveries_ctl.claim(
            bridge,
            "outbound",
            f"approval:{code}",
            subject=action_type,
            retry_failed=True,
        )
        if not created:
            if delivery["status"] == "completed":
                sent += 1
            continue
        try:
            if send(text) is not True:
                raise RuntimeError("bridge reported delivery failure")
            sent += 1
        except Exception as exc:
            deliveries_ctl.settle(
                delivery["id"],
                "failed",
                attempt=delivery["attempts"],
                detail=f"{type(exc).__name__}: bridge send failed",
            )
            continue
        deliveries_ctl.settle(
            delivery["id"],
            "completed",
            attempt=delivery["attempts"],
            result={"sent": True, "approval_code": code},
        )
    return sent


def handle_inbound_reply(message: str) -> dict:
    """Parse an operator reply and resolve the referenced decision.

    Returns ``{handled, code?, status?, error?}``. A message that isn't an
    approve/deny command is ``{handled: False}`` (so a bridge can ignore chatter).
    """
    m = _REPLY_RE.match(message or "")
    if not m:
        return {"handled": False}
    verb = m.group("verb").lower()
    code = m.group("code").upper()
    reason = (m.group("reason") or "").strip()
    approve = verb in _APPROVE_VERBS
    from brains.control.decisions import get_decision, resolve_decision

    state = get_decision(code)
    if state is None:
        return {"handled": True, "code": code, "error": "unknown decision"}
    if state["status"] != "open":
        return {"handled": True, "code": code, "error": f"already {state['status']}"}
    resolve_decision(
        code,
        chosen="approve" if approve else "deny",
        reasoning=reason or ("approved via bridge" if approve else "denied via bridge"),
        status="resolved" if approve else "rejected",
    )
    return {"handled": True, "code": code, "status": "resolved" if approve else "rejected"}


_DENY_VERBS = {"deny", "denied", "no", "reject", "rejected", "cancel", "stop"}
_ID_RE = re.compile(r"\b(a\d+|ASK-\d+)\b", re.IGNORECASE)


def code_to_short(code: str) -> str:
    """``ASK-0003`` -> ``a3`` (phone-friendly, collision-free per ask)."""
    m = re.match(r"^ASK-0*(\d+)$", code, re.IGNORECASE)
    return f"a{m.group(1)}" if m else code


def short_to_code(token: str) -> str | None:
    """Map a reply's leading token (``a3`` or ``ASK-0003``) back to a full code."""
    m = re.match(r"^a(\d+)$", token, re.IGNORECASE)
    if m:
        return f"ASK-{int(m.group(1)):04d}"
    if re.match(r"^ASK-\d+$", token, re.IGNORECASE):
        return token.upper()
    return None


def _send_hint(text: str) -> None:
    for send in _active_bridge_senders():
        try:
            send(text)
        except Exception:
            continue


def handle_inbound_answer(message: str) -> dict:
    """Resolve an open ask with a FREEFORM answer (numbered choice, deny, text).

    The inbound half of ``brains_ask_human``. Addressing rules (collision-safe):
    - ``a3 <answer>`` / ``ASK-0003 <answer>`` targets a specific ask.
    - a bare answer resolves the ask ONLY when exactly one is open; when 2+ are
      open it refuses and texts back the open list so you can address one.
    Returns ``{handled, code?, answer?, status?, error?}``.
    """
    text = (message or "").strip()
    if not text:
        return {"handled": False}
    from brains.control.decisions import (
        get_decision,
        list_open_requests,
        resolve_decision,
    )

    # Find an ask id ANYWHERE in the message (people write "let's do a3, blue
    # green", not "a3 blue green"). Strip the id token out to get the answer.
    m = _ID_RE.search(text)
    if m:
        code = short_to_code(m.group(1))
        answer = re.sub(r"\s+", " ", (text[: m.start()] + text[m.end() :])).strip(" ,.;:-")
    else:
        opens = list_open_requests()
        if len(opens) == 1:
            code, answer = opens[0]["code"], text
        elif len(opens) > 1:
            example = code_to_short(opens[0]["code"])
            listing = ", ".join(f"{code_to_short(o['code'])} ({o['title'][:24]})" for o in opens)
            _send_hint(
                f"⚠️ {len(opens)} open asks: {listing}. "
                f"Mention an id, e.g. '{example} deny' or '{example} 2'."
            )
            return {"handled": True, "error": "ambiguous", "open": [o["code"] for o in opens]}
        else:
            return {"handled": False}

    if code is None:  # pragma: no cover - _ID_RE only matches mappable tokens
        return {"handled": True, "code": code, "error": "unknown ask"}
    state = get_decision(code)
    if state is None:
        return {"handled": True, "code": code, "error": "unknown ask"}
    if state["status"] != "open":
        return {"handled": True, "code": code, "error": f"already {state['status']}"}
    low = f" {answer.lower()} "
    deny = any(f" {v} " in low for v in _DENY_VERBS)
    resolve_decision(
        code,
        chosen=answer or "(no answer)",
        reasoning="answered via bridge",
        status="rejected" if deny else "resolved",
    )
    return {
        "handled": True,
        "code": code,
        "answer": answer,
        "status": "rejected" if deny else "resolved",
    }


# --- triage spawner -------------------------------------------------------

_TRIAGE_PROMPT = """A new {source} item needs triage. Investigate it in THIS repository and
propose a concrete fix plan; make local edits and a local commit if you are confident, but do
NOT push, deploy, or take any outward action — those are gated for human approval.

--- {source} payload ---
{payload}
--- end payload ---

Steps: (1) identify the affected files/areas, (2) explain the likely root cause with file
references, (3) if a fix is clear, apply it as local edits + a local commit named
"triage: <summary>", (4) summarise what you changed and what (if anything) needs a human to
approve (e.g. a push or deploy)."""


def trigger_triage(
    workspace: str,
    source: str,
    payload: str,
    *,
    tool: str = "copilot",
    model: str | None = None,
    orient_query: str | None = None,
) -> dict:
    """Spawn a gated triage session for an inbound webhook event.

    Returns ``{exec_id}``. The session runs through the action gate, so anything
    it tries to ship is blocked for operator approval (and relayed to the bridges).
    """
    from brains.exec.runner import start_streamed_session

    prompt = _TRIAGE_PROMPT.format(source=source, payload=payload[:6000])
    exec_id = start_streamed_session(
        tool=tool,
        prompt=prompt,
        workspace_path=workspace,
        model=model,
        orient_query=orient_query or f"{source} bug fix",
    )
    return {"exec_id": exec_id, "source": source, "workspace": workspace}


__all__ = [
    "notify_pending_approval",
    "handle_inbound_reply",
    "handle_inbound_answer",
    "trigger_triage",
]

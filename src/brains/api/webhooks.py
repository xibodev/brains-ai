"""Inbound webhook delivery endpoint.

``POST /hooks/<slug>`` is the public front door for webhook triggers. It is
intentionally NOT behind the gateway API-key dependency: each trigger carries
its own bearer token, so callers authenticate per-endpoint. A valid delivery
fires the bound recurring-task definition (see ``control.webhooks``).
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from fastapi import APIRouter, Header, HTTPException, Request

from brains.api.realtime_publish import publish_issue
from brains.control import integration_deliveries as deliveries_ctl
from brains.control.webhooks import (
    WebhookAuthError,
    WebhookNotFound,
    deliver_webhook,
)

router = APIRouter()


def _bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _relay_token() -> str:
    """Shared bearer for the relay endpoints. Set ``BRAINS_RELAY_TOKEN``; if unset,
    the relay endpoints are disabled (return 503) rather than open."""
    return os.environ.get("BRAINS_RELAY_TOKEN", "").strip()


def _relay_dedupe_key(raw_body: bytes, supplied: str | None) -> str:
    if supplied:
        return supplied
    window = int(time.time() // 300)
    return f"body:{hashlib.sha256(raw_body).hexdigest()}:{window}"


def _check_relay_auth(authorization: str | None) -> None:
    expected = _relay_token()
    if not expected:
        raise HTTPException(status_code=503, detail="relay disabled (set BRAINS_RELAY_TOKEN)")
    import hmac

    if not hmac.compare_digest(_bearer(authorization), expected):
        raise HTTPException(status_code=401, detail="invalid relay token")


async def process_github_webhook_request(
    request: Request,
    signature: str | None,
    delivery_id: str | None,
    event_type: str | None,
) -> dict:
    from brains.control import github as github_ctl

    try:
        result = github_ctl.process_webhook(
            await request.body(),
            signature=signature,
            delivery_id=delivery_id,
            event_type=event_type,
        )
    except github_ctl.GitHubWebhookDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except github_ctl.GitHubWebhookAuthError as exc:
        raise HTTPException(status_code=401, detail="invalid GitHub webhook signature") from exc
    except github_ctl.GitHubWebhookPayloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except github_ctl.GitHubWebhookInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result.get("merged") and result.get("issue") and not result.get("duplicate"):
        publish_issue(
            "issue.updated",
            {"issue": result["issue"], "status": "done"},
            dedupe_key=f"github:{result['delivery_id']}:issue-updated",
        )
    return result


@router.post("/hooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict:
    """Public GitHub ingress authenticated by HMAC and repository-to-Org scope."""
    return await process_github_webhook_request(
        request,
        x_hub_signature_256,
        x_github_delivery,
        x_github_event,
    )


@router.post("/hooks/{slug}")
async def receive_webhook(
    slug: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_dedupe_key: str | None = Header(default=None),
) -> dict:
    token = _bearer(authorization)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"_raw": payload}
    try:
        return deliver_webhook(slug, token, payload=payload, dedupe_key=x_dedupe_key)
    except WebhookNotFound as exc:
        raise HTTPException(status_code=404, detail="unknown webhook") from exc
    except WebhookAuthError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc


@router.post("/relay/reply")
async def relay_inbound_reply(
    request: Request,
    authorization: str | None = Header(default=None),
    x_dedupe_key: str | None = Header(default=None),
) -> dict:
    """Inbound operator reply from a messaging bridge (WhatsApp/Telegram).

    Accepts ``{"message": "approve ASK-0005"}`` (or a raw WhatsApp/Telegram webhook
    payload — the text is extracted best-effort) and resolves the decision.
    """
    _check_relay_auth(authorization)
    raw_body = await request.body()
    dedupe_key = _relay_dedupe_key(raw_body, x_dedupe_key)
    delivery, created = deliveries_ctl.claim(
        "relay_reply",
        "inbound",
        dedupe_key,
        retry_failed=True,
    )
    if not created:
        if delivery["status"] == "processing":
            raise HTTPException(status_code=409, detail="relay delivery is already processing")
        return {
            **(delivery.get("result") or {}),
            "duplicate": True,
            "delivery_status": delivery["status"],
        }
    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError):
        payload = {}
    message = _extract_inbound_text(payload)
    from brains.exec.relay import handle_inbound_answer, handle_inbound_reply

    try:
        result = handle_inbound_reply(message)
        if not result.get("handled"):
            # Not an approve/deny command -> treat as a freeform answer to the open
            # ask (the inbound half of brains_ask_human).
            result = handle_inbound_answer(message)
    except Exception:
        deliveries_ctl.settle(
            delivery["id"],
            "failed",
            attempt=delivery["attempts"],
            detail="relay_reply_failed",
        )
        raise
    deliveries_ctl.settle(
        delivery["id"],
        "completed",
        attempt=delivery["attempts"],
        result=result,
    )
    return result


@router.post("/relay/triage")
async def relay_triage(
    request: Request,
    authorization: str | None = Header(default=None),
    x_dedupe_key: str | None = Header(default=None),
) -> dict:
    """Webhook entry for Chatwoot / bugsink / any bug source → spawn a GATED
    triage session (copilot+brains) on the target repo.

    Body: ``{"workspace": "/path/to/repo", "source": "bugsink", "payload": {...},
    "tool": "copilot", "model": "...", "orient": "..."}``. The session runs through
    the action gate, so it can investigate + edit + local-commit, but anything it
    tries to push/deploy is blocked for operator approval and relayed to bridges.
    """
    _check_relay_auth(authorization)
    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    workspace = body.get("workspace")
    if not workspace:
        raise HTTPException(status_code=400, detail="workspace is required")
    dedupe_key = _relay_dedupe_key(raw_body, x_dedupe_key)
    delivery, created = deliveries_ctl.claim(
        "relay_triage",
        "inbound",
        dedupe_key,
        retry_failed=True,
    )
    if not created:
        if delivery["status"] == "processing":
            raise HTTPException(status_code=409, detail="relay delivery is already processing")
        return {
            **(delivery.get("result") or {}),
            "duplicate": True,
            "delivery_status": delivery["status"],
        }
    import json as _json

    raw_payload = body.get("payload", body)
    payload_text = (
        raw_payload if isinstance(raw_payload, str) else _json.dumps(raw_payload, indent=2)
    )
    from brains.exec.relay import trigger_triage

    try:
        result = trigger_triage(
            workspace=workspace,
            source=str(body.get("source", "webhook")),
            payload=payload_text,
            tool=str(body.get("tool", "copilot")),
            model=body.get("model"),
            orient_query=body.get("orient"),
        )
    except Exception:
        deliveries_ctl.settle(
            delivery["id"],
            "failed",
            attempt=delivery["attempts"],
            detail="relay_triage_failed",
        )
        raise
    deliveries_ctl.settle(
        delivery["id"],
        "completed",
        attempt=delivery["attempts"],
        result=result,
    )
    return result


def _extract_inbound_text(payload: object) -> str:
    """Pull the operator's text out of a relay/bridge payload (best-effort)."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    if isinstance(payload.get("message"), str):
        return payload["message"]
    # Telegram: {"message": {"text": ...}}
    msg = payload.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("text"), str):
        return msg["text"]
    # WhatsApp Cloud: entry[].changes[].value.messages[].text.body
    try:
        entry = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        body = entry.get("text", {}).get("body")
        if isinstance(body, str):
            return body
    except Exception:
        pass
    return ""

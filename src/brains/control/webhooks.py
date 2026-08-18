"""Inbound webhook triggers for recurring-task definitions.

A webhook trigger is a thin, authenticated front door: an external system POSTs
to ``/hooks/<slug>`` and, if the bearer token matches and an optional event
filter passes, the bound recurring-task definition fires (recording a
``recurring_runs`` row with ``source=webhook``). Deliveries are idempotent on a
caller-supplied dedupe key.

Tokens are stored only as salted SHA-256 hashes; the plaintext is shown once at
creation time and never persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from brains.control.recurring import fire_recurring_task
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    RecurringTaskDefinition,
    WebhookDelivery,
    WebhookTrigger,
)

_TOKEN_PREFIX = "whk_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_slug(slug: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in slug.strip().lower())
    cleaned = cleaned.strip("-_")
    if not cleaned:
        raise ValueError("slug must contain at least one alphanumeric character")
    return cleaned


def create_webhook_trigger(
    slug: str,
    definition_name: str,
    event_filter: str | None = None,
) -> dict:
    """Create a webhook trigger bound to a recurring-task definition.

    Returns the trigger plus a freshly minted ``token`` (plaintext, shown
    once). ``event_filter`` is an optional ``key=value`` matched against the
    delivered JSON body.
    """
    slug = _normalize_slug(slug)
    init_db()
    if event_filter is not None and "=" not in event_filter:
        raise ValueError("event_filter must be of the form 'key=value'")
    with SessionLocal() as session:
        definition = (
            session.query(RecurringTaskDefinition)
            .filter(RecurringTaskDefinition.name == definition_name)
            .one_or_none()
        )
        if definition is None:
            raise ValueError(f"unknown recurring task: {definition_name}")
        existing = session.query(WebhookTrigger).filter(WebhookTrigger.slug == slug).one_or_none()
        if existing is not None:
            raise ValueError(f"webhook trigger already exists: {slug}")
        token = _TOKEN_PREFIX + secrets.token_urlsafe(24)
        trigger = WebhookTrigger(
            slug=slug,
            definition_name=definition_name,
            token_hash=_hash_token(token),
            event_filter=event_filter,
            enabled=True,
        )
        session.add(trigger)
        session.commit()
        return {
            "slug": slug,
            "definition_name": definition_name,
            "event_filter": event_filter,
            "enabled": True,
            "token": token,
            "url": f"/hooks/{slug}",
        }


def list_webhook_triggers() -> list[dict]:
    """Return all webhook triggers (tokens are never included)."""
    init_db()
    with SessionLocal() as session:
        rows = session.query(WebhookTrigger).order_by(WebhookTrigger.slug).all()
        return [
            {
                "slug": r.slug,
                "definition_name": r.definition_name,
                "event_filter": r.event_filter,
                "enabled": bool(r.enabled),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def set_webhook_enabled(slug: str, enabled: bool = True) -> dict:
    init_db()
    with SessionLocal() as session:
        trigger = session.query(WebhookTrigger).filter(WebhookTrigger.slug == slug).one_or_none()
        if trigger is None:
            raise ValueError(f"unknown webhook trigger: {slug}")
        trigger.enabled = enabled
        session.commit()
        return {"slug": slug, "enabled": enabled}


def _event_filter_passes(event_filter: str | None, payload: dict) -> bool:
    if not event_filter:
        return True
    key, _, expected = event_filter.partition("=")
    return str(payload.get(key.strip())) == expected.strip()


class WebhookAuthError(Exception):
    """Raised when a delivery's bearer token does not match the trigger."""


class WebhookNotFound(Exception):
    """Raised when no enabled trigger matches the slug."""


def deliver_webhook(
    slug: str,
    token: str,
    payload: dict | None = None,
    dedupe_key: str | None = None,
) -> dict:
    """Authenticate and process one inbound delivery.

    Returns a result dict with ``status`` in ``fired`` | ``duplicate`` |
    ``filtered``. Raises ``WebhookNotFound`` / ``WebhookAuthError`` for the
    not-found / bad-token cases so the route can map them to 404 / 401.
    """
    payload = payload or {}
    init_db()
    with SessionLocal() as session:
        trigger = (
            session.query(WebhookTrigger)
            .filter(WebhookTrigger.slug == slug, WebhookTrigger.enabled.is_(True))
            .one_or_none()
        )
        if trigger is None:
            raise WebhookNotFound(slug)
        if not hmac.compare_digest(trigger.token_hash, _hash_token(token or "")):
            raise WebhookAuthError(slug)
        definition_name = trigger.definition_name
        trigger_id = trigger.id
        event_filter = trigger.event_filter

        if not _event_filter_passes(event_filter, payload):
            return {"status": "filtered", "slug": slug}

        key = (
            dedupe_key
            or hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
        )
        already = (
            session.query(WebhookDelivery)
            .filter(
                WebhookDelivery.trigger_id == trigger_id,
                WebhookDelivery.dedupe_key == key,
            )
            .one_or_none()
        )
        if already is not None:
            return {"status": "duplicate", "slug": slug, "task_code": already.task_code}

    fired = fire_recurring_task(definition_name, source="webhook", trigger_payload=payload)
    task_code = fired["task"]["code"]
    with SessionLocal() as session:
        session.add(
            WebhookDelivery(
                trigger_id=trigger_id,
                dedupe_key=key,
                status="fired",
                task_code=task_code,
            )
        )
        session.commit()
    return {"status": "fired", "slug": slug, "task_code": task_code}

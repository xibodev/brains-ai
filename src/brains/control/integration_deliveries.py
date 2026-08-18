"""Durable delivery outcomes shared by inbound and outbound integrations."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from brains.control.common import utc_now
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import IntegrationDelivery


class IntegrationDeliveryOwnershipError(RuntimeError):
    pass


def _view(row: IntegrationDelivery) -> dict[str, Any]:
    result = None
    if row.result_json:
        try:
            result = json.loads(row.result_json)
        except (TypeError, ValueError):
            result = None
    return {
        "id": row.id,
        "channel": row.channel,
        "direction": row.direction,
        "delivery_key": row.delivery_key,
        "status": row.status,
        "subject": row.subject,
        "detail": row.detail,
        "result": result,
        "attempts": row.attempts,
        "lease_expires_at": (
            row.lease_expires_at.isoformat() if row.lease_expires_at is not None else None
        ),
    }


def claim(
    channel: str,
    direction: str,
    delivery_key: str,
    *,
    subject: str | None = None,
    retry_failed: bool = False,
    lease_seconds: int = 300,
) -> tuple[dict[str, Any], bool]:
    """Reserve one delivery before its effect; return the existing row on replay."""
    init_db()
    with SessionLocal() as session:
        existing = (
            session.query(IntegrationDelivery)
            .filter(
                IntegrationDelivery.channel == channel,
                IntegrationDelivery.direction == direction,
                IntegrationDelivery.delivery_key == delivery_key,
            )
            .one_or_none()
        )
        if existing is not None:
            if retry_failed:
                now = utc_now()
                updated = (
                    session.query(IntegrationDelivery)
                    .filter(
                        IntegrationDelivery.id == existing.id,
                        IntegrationDelivery.status == "failed",
                    )
                    .update(
                        {
                            IntegrationDelivery.status: "processing",
                            IntegrationDelivery.detail: None,
                            IntegrationDelivery.result_json: None,
                            IntegrationDelivery.attempts: IntegrationDelivery.attempts + 1,
                            IntegrationDelivery.lease_expires_at: now
                            + timedelta(seconds=lease_seconds),
                            IntegrationDelivery.updated_at: now,
                        },
                        synchronize_session=False,
                    )
                )
                session.commit()
                if updated == 1:
                    session.expire_all()
                    reclaimed = session.get(IntegrationDelivery, existing.id)
                    if reclaimed is None:
                        raise RuntimeError("reclaimed integration delivery disappeared")
                    return _view(reclaimed), True
                session.refresh(existing)
            return _view(existing), False
        row = IntegrationDelivery(
            channel=channel,
            direction=direction,
            delivery_key=delivery_key,
            status="processing",
            subject=subject,
            attempts=1,
            lease_expires_at=utc_now() + timedelta(seconds=lease_seconds),
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(IntegrationDelivery)
                .filter(
                    IntegrationDelivery.channel == channel,
                    IntegrationDelivery.direction == direction,
                    IntegrationDelivery.delivery_key == delivery_key,
                )
                .one()
            )
            return _view(existing), False
        session.refresh(row)
        return _view(row), True


def settle(
    delivery_id: int,
    status: str,
    *,
    attempt: int,
    detail: str | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init_db()
    with SessionLocal() as session:
        updated = (
            session.query(IntegrationDelivery)
            .filter(
                IntegrationDelivery.id == delivery_id,
                IntegrationDelivery.status == "processing",
                IntegrationDelivery.attempts == attempt,
            )
            .update(
                {
                    IntegrationDelivery.status: status,
                    IntegrationDelivery.detail: detail,
                    IntegrationDelivery.result_json: (
                        json.dumps(result, sort_keys=True) if result is not None else None
                    ),
                    IntegrationDelivery.lease_expires_at: None,
                    IntegrationDelivery.updated_at: utc_now(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        if updated != 1:
            raise IntegrationDeliveryOwnershipError(
                f"integration delivery {delivery_id} attempt {attempt} no longer owns settlement"
            )
        row = session.get(IntegrationDelivery, delivery_id)
        if row is None:
            raise RuntimeError("settled integration delivery disappeared")
        return _view(row)


def list_deliveries(*, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with SessionLocal() as session:
        query = session.query(IntegrationDelivery)
        if status is not None:
            query = query.filter(IntegrationDelivery.status == status)
        rows = query.order_by(IntegrationDelivery.updated_at.desc()).limit(limit).all()
        return [_view(row) for row in rows]

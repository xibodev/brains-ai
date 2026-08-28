"""Renewable liveness leases for PID-less coordination Sessions."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from brains.control.common import utc_now
from brains.storage.models import AgentSession, SessionLease, SessionSuccessor

DEFAULT_SESSION_LEASE_SECONDS = 60 * 60


def session_lease_seconds() -> int:
    try:
        raw = int(
            os.environ.get("BRAINS_SESSION_LEASE_SECONDS", str(DEFAULT_SESSION_LEASE_SECONDS))
        )
    except ValueError:
        return DEFAULT_SESSION_LEASE_SECONDS
    return max(60, raw)


def renew_session_lease(
    session: Any,
    agent: AgentSession,
    *,
    now: datetime | None = None,
    reactivate: bool = True,
    create: bool = False,
) -> SessionLease | None:
    """Renew ``agent`` when it is a non-terminal PID-less Session."""
    if (
        agent.pid is not None
        or agent.ended_at is not None
        or agent.runtime_id is not None
        or agent.issue_id is not None
        or agent.persona_id is not None
    ):
        return None
    current = now or utc_now()
    lease = session.get(SessionLease, agent.id)
    if lease is None:
        if not create:
            return None
        lease = SessionLease(session_id=agent.id)
        session.add(lease)
    if agent.state == "dormant" and not reactivate:
        return lease
    if reactivate and agent.state == "dormant":
        successor = session.get(SessionSuccessor, agent.id)
        if successor is not None:
            raise ValueError(
                f"session {agent.id} was superseded by {successor.successor_session_id}; "
                "resume or heartbeat the successor"
            )
    lease.renewed_at = current
    lease.lease_expires_at = current + timedelta(seconds=session_lease_seconds())
    agent.last_activity_at = current
    if reactivate and agent.state == "dormant":
        agent.state = "running"
    return lease


def lease_is_current(lease: SessionLease | None, *, now: datetime | None = None) -> bool:
    if lease is None:
        return False
    current = now or utc_now()
    expires = lease.lease_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=current.tzinfo)
    return expires >= current


__all__ = [
    "DEFAULT_SESSION_LEASE_SECONDS",
    "lease_is_current",
    "renew_session_lease",
    "session_lease_seconds",
]

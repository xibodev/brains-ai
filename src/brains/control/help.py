"""Cross-session peer help — long-poll RPC over SQLite.

This module backs the ``ask_peer`` / ``wait_for_request`` /
``answer_request`` MCP tools. The protocol lets one agent session ask a
question that targets another workspace (or session), and lets a peer
that's polling for work pick it up and answer — all blocking with a
configurable timeout so the asker can simply ``await`` the answer.

Why polling and not SQLite ``LISTEN/NOTIFY``? SQLite doesn't have it.
We approximate with short-sleep DB polls; the poll interval is small
enough (~200 ms by default) that round trips feel real-time at human
scale, and the long-poll timeouts cap server-side wait so a stuck peer
never wedges anyone forever.

Safety rules baked into this module:

* ``ask_peer`` requires both ``subject`` and ``question``.
* ``answer_request`` refuses an empty ``evidence`` string — answers must
  cite *something* (file paths, log refs, URLs) so the asker can verify.
* ``ask_depth`` defaults to 1 and is capped at 2. If a peer that is
  *itself* currently answering an open request files a new ``ask_peer``,
  the new request inherits ``ask_depth = parent + 1``. Anything beyond
  the cap is refused — this is the deadlock guard for A↔B chains.

All operations use the dev DB. There is no API authentication in this
module; the MCP tool layer above is the trust boundary.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, HelpRequest, Workspace


# How often to re-poll the DB while blocked on ask_peer / wait_for_request.
# Tunable via ``BRAINS_HELP_POLL_INTERVAL_MS`` for tests that want to
# tighten the loop. Range-clamped at the boundary so a bad env value can't
# spin the loop.
def _poll_interval_seconds() -> float:
    try:
        raw = int(os.environ.get("BRAINS_HELP_POLL_INTERVAL_MS", "200"))
    except ValueError:
        raw = 200
    return max(0.01, min(raw / 1000.0, 2.0))


# Hard cap on ``ask_depth``. A->B is depth 1; B's nested ask while
# answering A is depth 2; anything beyond is refused. Tightening this in
# the future is a non-breaking change.
MAX_ASK_DEPTH = 2

# Default timeout when callers don't specify one. 30 s is short enough to
# feel responsive and long enough for a peer to pick up the work.
DEFAULT_TIMEOUT_MS = 30_000


class HelpDeadlockError(ValueError):
    """Raised when an ask would exceed ``MAX_ASK_DEPTH``."""


class HelpExpiredError(ValueError):
    """Raised when answer_request targets a request that already expired."""


def _row_to_dict(row: HelpRequest) -> dict[str, Any]:
    return {
        "code": row.code,
        "from_session_id": row.from_session_id,
        "from_workspace_id": row.from_workspace_id,
        "to_workspace": row.to_workspace,
        "to_session_id": row.to_session_id,
        "subject": row.subject,
        "question": row.question,
        "context": row.context,
        "status": row.status,
        "claimed_by_session_id": row.claimed_by_session_id,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "answer": row.answer,
        "evidence": row.evidence,
        "answered_at": row.answered_at.isoformat() if row.answered_at else None,
        "ask_depth": row.ask_depth,
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
    }


def _next_code() -> str:
    return f"HR-{uuid.uuid4().hex[:8]}"


def _resolve_session_workspace_id(session, session_id: str | None) -> int | None:
    if not session_id:
        return None
    row = session.query(AgentSession).filter(AgentSession.id == session_id).one_or_none()
    return row.workspace_id if row else None


def _current_ask_depth_for_session(session, session_id: str | None) -> int:
    """If this session is currently claimed-as-answerer on an open request,
    its outgoing asks inherit ``parent.ask_depth + 1``. Otherwise depth
    starts at 1.
    """
    if not session_id:
        return 1
    row = (
        session.query(HelpRequest)
        .filter(
            HelpRequest.claimed_by_session_id == session_id,
            HelpRequest.status == "claimed",
        )
        .order_by(HelpRequest.claimed_at.desc())
        .first()
    )
    if row is None:
        return 1
    return row.ask_depth + 1


def _expire_due(session) -> int:
    """Flip stale ``open`` / ``claimed`` requests to ``expired``.

    Called opportunistically by every public entry point so callers don't
    have to babysit the table. Returns the count of rows flipped, mainly
    for tests.
    """
    now = utc_now()
    q = session.query(HelpRequest).filter(
        HelpRequest.expires_at < now,
        HelpRequest.status.in_(("open", "claimed")),
    )
    count = q.count()
    if count:
        q.update({"status": "expired"}, synchronize_session=False)
    return count


def ask_peer(
    subject: str,
    question: str,
    *,
    from_session_id: str | None = None,
    to_workspace: str | None = None,
    to_session_id: str | None = None,
    context: str = "",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """File a help request and block until a peer answers or it expires.

    Returns the request dict; ``status`` will be one of:

    * ``"answered"`` — peer answered. ``answer`` + ``evidence`` populated.
    * ``"expired"`` — server-side timeout reached without an answer.
    * ``"cancelled"`` — asker withdrew (not currently exposed via MCP).

    Raises ``ValueError`` for missing required fields and
    ``HelpDeadlockError`` for over-deep ask chains.
    """
    if not subject or not subject.strip():
        raise ValueError("subject is required")
    if not question or not question.strip():
        raise ValueError("question is required")
    if to_workspace is None and to_session_id is None:
        raise ValueError("ask_peer requires to_workspace or to_session_id")
    timeout_ms = max(100, int(timeout_ms))

    init_db()
    now = utc_now()
    expires_at = now + timedelta(milliseconds=timeout_ms)
    code = _next_code()
    with SessionLocal() as session:
        _expire_due(session)
        depth = _current_ask_depth_for_session(session, from_session_id)
        if depth > MAX_ASK_DEPTH:
            raise HelpDeadlockError(
                f"ask_peer refused: ask_depth would exceed cap ({depth} > {MAX_ASK_DEPTH})"
            )
        workspace_id = _resolve_session_workspace_id(session, from_session_id)
        row = HelpRequest(
            code=code,
            from_session_id=from_session_id,
            from_workspace_id=workspace_id,
            to_workspace=to_workspace,
            to_session_id=to_session_id,
            subject=subject.strip(),
            question=question.strip(),
            context=context or None,
            status="open",
            ask_depth=depth,
            created_at=now,
            expires_at=expires_at,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        request_id = row.id

    append_event(
        "help_asked",
        f"{code}: {subject.strip()}",
        workspace_id=workspace_id,
        session_id=from_session_id,
        metadata={
            "code": code,
            "to_workspace": to_workspace,
            "to_session_id": to_session_id,
            "depth": depth,
        },
    )

    # Long-poll for an answer. We sleep in small slices so the asker
    # observes the answer within ~poll_interval after it lands.
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    poll = _poll_interval_seconds()
    final_status = "expired"
    while True:
        with SessionLocal() as session:
            polled = session.query(HelpRequest).filter(HelpRequest.id == request_id).one_or_none()
            if polled is None:
                # Defensive: another caller deleted the row (shouldn't
                # happen). Treat as cancelled.
                final_status = "cancelled"
                result = {"code": code, "status": "cancelled"}
                break
            if polled.status in ("answered", "expired", "cancelled"):
                final_status = polled.status
                result = _row_to_dict(polled)
                break
        if time.monotonic() >= deadline:
            # Flip ourselves to expired and return that snapshot.
            with SessionLocal() as session:
                expiring = (
                    session.query(HelpRequest).filter(HelpRequest.id == request_id).one_or_none()
                )
                if expiring is not None and expiring.status in ("open", "claimed"):
                    expiring.status = "expired"
                    session.commit()
                    session.refresh(expiring)
                    result = _row_to_dict(expiring)
                else:
                    result = (
                        _row_to_dict(expiring)
                        if expiring
                        else {
                            "code": code,
                            "status": "expired",
                        }
                    )
            final_status = result.get("status", "expired")
            break
        time.sleep(poll)

    append_event(
        f"help_{final_status}",
        f"{code}: {final_status}",
        workspace_id=workspace_id,
        session_id=from_session_id,
        metadata={"code": code, "status": final_status},
    )
    return result


def wait_for_request(
    *,
    session_id: str,
    workspace_slug: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any] | None:
    """Block until an open peer-help request targets this peer, then claim it.

    A request matches this peer if any of these are true:

    * ``to_session_id == session_id``, OR
    * ``to_workspace == workspace_slug`` (when ``workspace_slug`` is set), OR
    * the peer's session belongs to the workspace named in ``to_workspace``.

    On match we atomically flip ``status`` to ``"claimed"`` and stamp
    ``claimed_by_session_id`` / ``claimed_at``, then return the row dict.

    Returns ``None`` on timeout.
    """
    if not session_id:
        raise ValueError("wait_for_request requires session_id")
    timeout_ms = max(100, int(timeout_ms))

    init_db()
    # Resolve peer's workspace slug once; we won't refetch it on every loop.
    resolved_slug: str | None = workspace_slug
    if resolved_slug is None:
        with SessionLocal() as session:
            row = (
                session.query(AgentSession, Workspace)
                .join(Workspace, Workspace.id == AgentSession.workspace_id)
                .filter(AgentSession.id == session_id)
                .one_or_none()
            )
            if row is not None:
                resolved_slug = row[1].slug

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    poll = _poll_interval_seconds()
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    # An operator should never be able to claim a request that
    # originated in a private workspace they aren't a member of, even
    # if the request was addressed by session_id directly.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    while True:
        with SessionLocal() as session:
            _expire_due(session)
            filters = [HelpRequest.to_session_id == session_id]
            if resolved_slug:
                filters.append(HelpRequest.to_workspace == resolved_slug)
            query = (
                session.query(HelpRequest)
                .filter(
                    HelpRequest.status == "open",
                    or_(*filters),
                )
                .order_by(HelpRequest.created_at.asc())
            )
            if visible is not None:
                query = query.filter(
                    (HelpRequest.from_workspace_id.is_(None))
                    | (HelpRequest.from_workspace_id.in_(visible))
                )
            candidate = query.first()
            if candidate is not None:
                # Atomic claim: re-check the row is still open via a
                # conditional update so two waiters can't both grab it.
                now = utc_now()
                updated = (
                    session.query(HelpRequest)
                    .filter(
                        and_(
                            HelpRequest.id == candidate.id,
                            HelpRequest.status == "open",
                        )
                    )
                    .update(
                        {
                            "status": "claimed",
                            "claimed_by_session_id": session_id,
                            "claimed_at": now,
                        },
                        synchronize_session=False,
                    )
                )
                session.commit()
                if updated:
                    session.refresh(candidate)
                    result = _row_to_dict(candidate)
                    append_event(
                        "help_claimed",
                        f"{candidate.code}: claimed by {session_id}",
                        session_id=session_id,
                        metadata={
                            "code": candidate.code,
                            "from_session_id": candidate.from_session_id,
                        },
                    )
                    return result
                # Lost the race — fall through to keep polling.
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def answer_request(
    code: str,
    answer: str,
    evidence: str,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Provide the answer + evidence for a peer help request.

    ``evidence`` must be non-empty. ``code`` must reference a request in
    ``"claimed"`` state that was claimed by ``session_id`` (a peer cannot
    answer a request another peer claimed). Returns the updated row dict.

    Raises ``ValueError`` for missing/invalid inputs and
    ``HelpExpiredError`` if the request expired before the answer landed.
    """
    if not code:
        raise ValueError("code is required")
    if not answer or not answer.strip():
        raise ValueError("answer is required")
    if not evidence or not evidence.strip():
        raise ValueError("evidence is required — cite paths, log refs or URLs")
    if not session_id:
        raise ValueError("answer_request requires session_id")

    init_db()
    with SessionLocal() as session:
        _expire_due(session)
        row = session.query(HelpRequest).filter(HelpRequest.code == code).one_or_none()
        if row is None:
            raise ValueError(f"unknown help request: {code}")
        if row.status == "expired":
            raise HelpExpiredError(f"help request expired: {code}")
        if row.status == "answered":
            raise ValueError(f"help request already answered: {code}")
        if row.status != "claimed":
            raise ValueError(f"help request not in claimed state: {code} (status={row.status})")
        if row.claimed_by_session_id and row.claimed_by_session_id != session_id:
            raise ValueError(f"help request claimed by another session: {code}")
        row.answer = answer.strip()
        row.evidence = evidence.strip()
        row.answered_at = utc_now()
        row.status = "answered"
        if row.claimed_by_session_id is None:
            row.claimed_by_session_id = session_id
        session.commit()
        session.refresh(row)
        result = _row_to_dict(row)

    append_event(
        "help_answered",
        f"{code}: answered by {session_id}",
        session_id=session_id,
        metadata={"code": code, "to": row.from_session_id},
    )
    return result


def list_open_help_requests(
    *,
    to_workspace: str | None = None,
    to_session_id: str | None = None,
    include_answered: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Inspect outstanding requests — for dashboards and tests."""
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    # HelpRequest.from_workspace_id is the workspace the requester sits
    # in; we filter on that so private workspaces' asks never leak into
    # another operator's queue.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        _expire_due(session)
        query = session.query(HelpRequest)
        if not include_answered:
            query = query.filter(HelpRequest.status.in_(("open", "claimed")))
        if to_workspace:
            query = query.filter(HelpRequest.to_workspace == to_workspace)
        if to_session_id:
            query = query.filter(HelpRequest.to_session_id == to_session_id)
        if visible is not None:
            query = query.filter(
                (HelpRequest.from_workspace_id.is_(None))
                | (HelpRequest.from_workspace_id.in_(visible))
            )
        rows = query.order_by(HelpRequest.created_at.asc()).limit(limit).all()
        return [_row_to_dict(row) for row in rows]


__all__ = [
    "ask_peer",
    "wait_for_request",
    "answer_request",
    "list_open_help_requests",
    "HelpDeadlockError",
    "HelpExpiredError",
    "MAX_ASK_DEPTH",
    "DEFAULT_TIMEOUT_MS",
]

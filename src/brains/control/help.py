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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, HelpRequest, HelpRequestConstraint, Workspace


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


def _required_tool_for(session, code: str) -> str | None:
    row = (
        session.query(HelpRequestConstraint)
        .filter(HelpRequestConstraint.request_code == code)
        .one_or_none()
    )
    return row.required_tool if row else None


def _row_to_dict(session, row: HelpRequest) -> dict[str, Any]:
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
        "required_tool": _required_tool_for(session, row.code),
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


#: Harness-constraint grammar (agent comms slice 1): an exact tool name or
#: ``not:<tool>``, case-insensitive. ``None``/empty means any harness.
def normalize_required_tool(raw: str | None) -> str | None:
    if raw is None:
        return None
    req = raw.strip().lower()
    if not req:
        return None
    if req.startswith("not:"):
        target = req[4:].strip()
        if not target:
            raise ValueError("required_tool 'not:' needs a tool name")
        return f"not:{target}"
    return req


def tool_matches_requirement(required_tool: str | None, tool: str | None) -> bool:
    """True when ``tool`` may claim a request carrying ``required_tool``.

    An empty claimer tool matches only unconstrained requests: a session
    that cannot name its harness cannot prove it satisfies either form.
    """
    if not required_tool:
        return True
    claimer = (tool or "").strip().lower()
    if not claimer:
        return False
    req = required_tool.strip().lower()
    if req.startswith("not:"):
        return claimer != req[4:]
    return claimer == req


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


#: How long a *claimed* request may stay unanswered before it expires.
#: Expiry otherwise counts only time spent ``open`` — once a peer claims a
#: request and goes to gather real evidence (grep the repo, run a probe),
#: punishing that work by expiring the request discards exactly the answer
#: the evidence requirement demanded. Field report 2026-08-24, issue #3.
DEFAULT_CLAIM_GRACE_SECONDS = 600


def _claim_grace_seconds() -> int:
    try:
        raw = int(
            os.environ.get("BRAINS_HELP_CLAIM_GRACE_SECONDS", str(DEFAULT_CLAIM_GRACE_SECONDS))
        )
    except ValueError:
        return DEFAULT_CLAIM_GRACE_SECONDS
    return max(1, raw)


def _expire_due(session) -> int:
    """Flip stale requests to ``expired``.

    Expiry counts only time spent ``open``: an unclaimed request past its
    deadline dies, but a **claimed** request survives its original
    ``expires_at`` — the claim is the promise that someone is actively
    producing the evidence-backed answer. A claimed request expires only
    when the claim itself goes stale (no answer within
    ``BRAINS_HELP_CLAIM_GRACE_SECONDS``, default 600s), so an abandoned
    claim cannot park a request forever.

    Called opportunistically by every public entry point so callers don't
    have to babysit the table. Returns the count of rows flipped, mainly
    for tests.
    """
    now = utc_now()
    grace_cutoff = now - timedelta(seconds=_claim_grace_seconds())
    open_q = session.query(HelpRequest).filter(
        HelpRequest.expires_at < now,
        HelpRequest.status == "open",
    )
    open_count = open_q.count()
    if open_count:
        open_q.update({"status": "expired"}, synchronize_session=False)
    claimed_q = session.query(HelpRequest).filter(
        HelpRequest.status == "claimed",
        HelpRequest.claimed_at < grace_cutoff,
    )
    claimed_count = claimed_q.count()
    if claimed_count:
        claimed_q.update({"status": "expired"}, synchronize_session=False)
    return open_count + claimed_count


def ask_peer(
    subject: str,
    question: str,
    *,
    from_session_id: str | None = None,
    to_workspace: str | None = None,
    to_session_id: str | None = None,
    context: str = "",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    required_tool: str | None = None,
) -> dict[str, Any]:
    """File a help request and block until a peer answers or it expires.

    ``required_tool`` constrains which harness may claim the request —
    an exact tool name (``"claude"``) or ``not:<tool>`` (``"not:copilot"``,
    "any harness except this one"). This is how a session asks a *different*
    CLI to validate its work without either side sharing context.

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
    required_tool_norm = normalize_required_tool(required_tool)
    timeout_ms = max(100, int(timeout_ms))

    init_db()
    now = utc_now()
    expires_at = now + timedelta(milliseconds=timeout_ms)
    code = _next_code()
    with SessionLocal() as session:
        _expire_due(session)
        if from_session_id:
            # Attribution is validated against liveness: a reaped session
            # cannot file asks (field report #2, issue a).
            from brains.control.sessions import require_live_session

            require_live_session(session, from_session_id, action="ask_peer")
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
        if required_tool_norm:
            # Harness constraint lives in its own delta-created table: the
            # help_requests table is frozen baseline and must not drift.
            session.add(HelpRequestConstraint(request_code=code, required_tool=required_tool_norm))
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
            "required_tool": required_tool_norm,
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
                result = _row_to_dict(session, polled)
                break
            if polled.status == "claimed" and polled.claimed_at is not None:
                # A peer claimed it and is gathering evidence: stop counting
                # the asker's original timeout and wait out the claim grace
                # instead, so a good-but-slow answer isn't discarded.
                claimed_at = polled.claimed_at
                if claimed_at.tzinfo is None:
                    claimed_at = claimed_at.replace(tzinfo=UTC)
                grace_deadline = claimed_at + timedelta(seconds=_claim_grace_seconds())
                remaining = (grace_deadline - datetime.now(UTC)).total_seconds()
                if remaining > 0:
                    deadline = max(deadline, time.monotonic() + remaining)
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
                    result = _row_to_dict(session, expiring)
                else:
                    result = (
                        _row_to_dict(session, expiring)
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
    # Resolve peer's workspace slug + harness once; we won't refetch on
    # every loop. The harness gates which constrained requests this peer
    # may claim (``required_tool`` matching).
    resolved_slug: str | None = workspace_slug
    my_tool: str | None = None
    with SessionLocal() as session:
        row = (
            session.query(AgentSession, Workspace)
            .outerjoin(Workspace, Workspace.id == AgentSession.workspace_id)
            .filter(AgentSession.id == session_id)
            .one_or_none()
        )
        if row is not None:
            my_tool = row[0].tool
            if resolved_slug is None:
                resolved_slug = row[1].slug if row[1] is not None else None

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
                session.query(HelpRequest, HelpRequestConstraint.required_tool)
                .outerjoin(
                    HelpRequestConstraint,
                    HelpRequestConstraint.request_code == HelpRequest.code,
                )
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
            # Harness constraint: skip requests this session's tool may not
            # claim. They stay open for a matching peer; we keep polling.
            candidate: HelpRequest | None = None
            candidate_required_tool: str | None = None
            for row, required_tool in query.limit(25).all():
                if tool_matches_requirement(required_tool, my_tool):
                    candidate = row
                    candidate_required_tool = required_tool
                    break
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
                    result = _row_to_dict(session, candidate)
                    append_event(
                        "help_claimed",
                        f"{candidate.code}: claimed by {session_id}",
                        session_id=session_id,
                        metadata={
                            "code": candidate.code,
                            "from_session_id": candidate.from_session_id,
                            "required_tool": candidate_required_tool,
                            "claimer_tool": my_tool,
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
        result = _row_to_dict(session, row)

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
        return [_row_to_dict(session, row) for row in rows]


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

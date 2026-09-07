"""Cross-session peer help — durable lifecycle plus bounded waits over SQLite.

This module backs the ``ask_peer`` / ``wait_for_request`` /
``answer_request`` MCP tools. The protocol lets one agent Session file a
question that targets another Workspace (or Session), continue working, and
return later by code. A compatibility wrapper still supports one blocking
call, while the durable lifecycle exposes file/get/wait/cancel/release.

Why polling and not SQLite ``LISTEN/NOTIFY``? SQLite doesn't have it.
We approximate with short-sleep DB polls; the poll interval is small
enough (~200 ms by default) that round trips feel real-time at human
scale, and the long-poll timeouts cap server-side wait so a stuck peer
never wedges anyone forever.

Safety rules baked into this module:

* Filing requires both ``subject`` and ``question``.
* ``answer_request`` refuses an empty ``evidence`` string — answers must
  cite *something* (file paths, log refs, URLs) so the asker can verify.
* ``ask_depth`` defaults to 1 and is capped at 2. If a peer that is
  *itself* currently answering an open request files a new ``ask_peer``,
  the new request inherits ``ask_depth = parent + 1``. Anything beyond
  the cap is refused — this is the deadlock guard for A↔B chains.

Session actors are checked against the transport/local principal before
liveness renewal. Request visibility remains scoped to its source Workspace.
The transport authenticates credentials; local calls inherit the resolver's
operating-system trust boundary. A caller-supplied Session id is not a credential.
Claim/answer/release/cancel recheck the actor and current Workspace visibility
under the SQLite lifecycle writer lock, held through the request transition.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select

from brains.authz.principal import Principal
from brains.control.common import normalize_path, utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    HelpRequest,
    HelpRequestConstraint,
    HelpRequestExecution,
    Org,
    Workspace,
    WorkspaceAlias,
    WorkspaceMembership,
)


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
EXECUTION_MODES = frozenset({"auto", "existing", "ephemeral"})
EPHEMERAL_REVIEW_TOOLS = frozenset({"claude", "codex", "copilot"})


def _peer_grace_milliseconds() -> int:
    try:
        raw = int(os.environ.get("BRAINS_HELP_PEER_GRACE_MS", "1000"))
    except ValueError:
        raw = 1000
    return max(0, min(raw, 30_000))


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
    execution = session.get(HelpRequestExecution, row.code)
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
        "execution_mode": execution.mode if execution is not None else "existing",
        "execution": (
            {
                "status": execution.status,
                "runtime_id": execution.runtime_id,
                "review_session_id": execution.review_session_id,
                "attempt": execution.attempt,
                "launch_after": execution.launch_after.isoformat(),
                "lease_expires_at": (
                    execution.lease_expires_at.isoformat() if execution.lease_expires_at else None
                ),
                "error_code": execution.error_code,
            }
            if execution is not None
            else None
        ),
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


def _claimable_request_query(
    session,
    *,
    session_id: str,
    workspace_slug: str | None,
    tool: str | None,
    visible_workspace_ids: set[int] | None,
    owned_claim: bool = False,
):
    """Return peer-help rows this Session could claim, before any limit."""
    targets = [HelpRequest.to_session_id == session_id]
    if workspace_slug:
        targets.append(HelpRequest.to_workspace == workspace_slug)
    query = (
        session.query(HelpRequest, HelpRequestConstraint.required_tool)
        .outerjoin(
            HelpRequestConstraint,
            HelpRequestConstraint.request_code == HelpRequest.code,
        )
        .filter(
            or_(
                HelpRequest.status == "open",
                and_(
                    HelpRequest.status == "claimed",
                    HelpRequest.claimed_by_session_id == session_id,
                ),
            )
            if owned_claim
            else HelpRequest.status == "open",
            or_(*targets),
        )
    )
    if visible_workspace_ids is not None:
        query = query.filter(
            (HelpRequest.from_workspace_id.is_(None))
            | (HelpRequest.from_workspace_id.in_(visible_workspace_ids))
        )
    normalized_tool = (tool or "").strip().lower()
    required = func.lower(HelpRequestConstraint.required_tool)
    if normalized_tool:
        query = query.filter(
            (HelpRequestConstraint.request_code.is_(None))
            | (required == normalized_tool)
            | (required.like("not:%") & (required != f"not:{normalized_tool}"))
        )
    else:
        query = query.filter(HelpRequestConstraint.request_code.is_(None))
    return query.order_by(HelpRequest.created_at.asc(), HelpRequest.id.asc())


def _execution_tool(required_tool: str | None) -> str | None:
    required = normalize_required_tool(required_tool)
    if required is None or required.startswith("not:"):
        return None
    return required if required in EPHEMERAL_REVIEW_TOOLS else None


def _resolve_target_workspace(session, reference: str) -> Workspace | None:
    raw = (reference or "").strip()
    if not raw:
        return None
    workspace = session.query(Workspace).filter(Workspace.slug == raw).one_or_none()
    if workspace is not None:
        return workspace
    try:
        normalized = normalize_path(raw)
    except (OSError, ValueError):
        return None
    alias = session.query(WorkspaceAlias).filter(WorkspaceAlias.path == normalized).one_or_none()
    if alias is not None:
        return session.get(Workspace, alias.workspace_id)
    return session.query(Workspace).filter(Workspace.path == normalized).one_or_none()


def _resolved_execution_mode(
    requested: str,
    *,
    required_tool: str | None,
    to_workspace: str | None,
    to_session_id: str | None,
) -> str:
    del required_tool, to_workspace, to_session_id
    mode = (requested or "existing").strip().lower()
    if mode != "existing":
        raise ValueError(
            "automatic and ephemeral peer execution is withdrawn; execution_mode must be 'existing'"
        )
    return mode


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


def _help_workspace_visible(session, principal: Principal, workspace_id: int) -> bool:
    """The shared/private Org policy, read on the transition's own connection."""
    if principal.is_bootstrap_admin:
        return True
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        return False
    org_id = workspace.org_id
    if org_id is None:
        org_id = session.query(Org.id).filter(Org.slug == "default").scalar()
    if org_id not in (principal.visible_org_ids() or set()):
        return False
    return workspace.visibility != "private" or (
        session.query(WorkspaceMembership.id)
        .filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.operator_id == principal.operator_id,
        )
        .first()
        is not None
    )


def _require_help_actor(
    session,
    session_id: str,
    *,
    action: str,
    lock: bool = False,
    request_code: str | None = None,
) -> AgentSession:
    from brains.authz.resolver import resolve_local_principal
    from brains.control.sessions import _lock_session_lifecycle, require_live_session

    # Resolve before the writer lock: local resolution may initialize the DB.
    principal = resolve_local_principal()
    if not principal.is_operator or principal.operator_id is None:
        raise ValueError("help session unavailable")
    if lock:
        _lock_session_lifecycle(session, session_id)
        # expire_on_commit=False otherwise retains the pre-lock actor snapshot.
        session.expire_all()
    agent = session.get(AgentSession, session_id)
    if agent is None:
        raise ValueError("help session unavailable")
    # Pre-operator persisted Sessions and synthetic fixtures have NULL owners.
    # Only the install admin may keep using those; known owners always match.
    legacy_admin = agent.created_by_operator_id is None and principal.is_bootstrap_admin
    if (
        not legacy_admin and agent.created_by_operator_id != principal.operator_id
    ) or not _help_workspace_visible(session, principal, agent.workspace_id):
        raise ValueError("help session unavailable")
    if request_code is not None:
        request = session.query(HelpRequest).filter(HelpRequest.code == request_code).one_or_none()
        if request is None or (
            request.from_workspace_id is not None
            and not _help_workspace_visible(session, principal, request.from_workspace_id)
        ):
            raise ValueError(f"unknown or unavailable help request: {request_code}")
    return require_live_session(session, session_id, action=action)


def _current_claim(now: datetime):
    # Retained running reviews may outlive peer grace; this does not launch work.
    live_review = select(HelpRequestExecution.request_code).where(
        HelpRequestExecution.status == "running",
        HelpRequestExecution.lease_expires_at >= now,
    )
    return or_(
        HelpRequest.claimed_at >= now - timedelta(seconds=_claim_grace_seconds()),
        HelpRequest.code.in_(live_review),
    )


def _request_snapshot(session, row: HelpRequest):
    """Fence transitions against a concurrent terminal change or release/reclaim."""
    return session.query(HelpRequest).filter(
        HelpRequest.id == row.id,
        HelpRequest.status == row.status,
        HelpRequest.claimed_by_session_id == row.claimed_by_session_id,
        HelpRequest.claimed_at == row.claimed_at,
        HelpRequest.expires_at == row.expires_at,
    )


def _help_transition(session, row: HelpRequest, *, session_id: str, action: str):
    # Capture the request CAS before refreshing under the lifecycle writer lock.
    # No commit may separate this actor/visibility recheck from the transition.
    code = row.code
    query = _request_snapshot(session, row)
    _require_help_actor(session, session_id, action=action, lock=True, request_code=code)
    return query


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
    open_q = session.query(HelpRequest).filter(
        HelpRequest.expires_at < now,
        HelpRequest.status == "open",
    )
    open_count = open_q.count()
    if open_count:
        open_count = open_q.update({"status": "expired"}, synchronize_session=False)
    claimed_q = session.query(HelpRequest).filter(
        HelpRequest.status == "claimed",
        ~_current_claim(now),
    )
    claimed_count = claimed_q.count()
    if claimed_count:
        claimed_count = claimed_q.update({"status": "expired"}, synchronize_session=False)
    if open_count or claimed_count:
        expired_codes = session.query(HelpRequest.code).filter(HelpRequest.status == "expired")
        session.query(HelpRequestExecution).filter(
            HelpRequestExecution.request_code.in_(expired_codes),
            HelpRequestExecution.status.in_(("queued", "running")),
        ).update(
            {
                "status": "cancelled",
                "lease_expires_at": None,
                "completed_at": now,
                "updated_at": now,
                "error_code": "request_expired",
            },
            synchronize_session=False,
        )
    return open_count + claimed_count


def file_help_request(
    subject: str,
    question: str,
    *,
    from_session_id: str | None = None,
    to_workspace: str | None = None,
    to_session_id: str | None = None,
    context: str = "",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    required_tool: str | None = None,
    execution_mode: str = "existing",
) -> dict[str, Any]:
    """File a durable peer-help request and return without waiting.

    ``required_tool`` constrains which harness may claim the request —
    an exact tool name (``"claude"``) or ``not:<tool>`` (``"not:copilot"``,
    "any harness except this one"). This is how a session asks a *different*
    CLI to validate its work without either side sharing context.

    The returned request is ``open``. Call :func:`wait_help_request` or
    :func:`get_help_request` later; the request remains independently claimable
    while the filing caller continues other work or exits.
    """
    if not subject or not subject.strip():
        raise ValueError("subject is required")
    if not question or not question.strip():
        raise ValueError("question is required")
    if to_workspace is None and to_session_id is None:
        raise ValueError("ask_peer requires to_workspace or to_session_id")
    required_tool_norm = normalize_required_tool(required_tool)
    resolved_mode = _resolved_execution_mode(
        execution_mode,
        required_tool=required_tool_norm,
        to_workspace=to_workspace,
        to_session_id=to_session_id,
    )
    timeout_ms = max(100, int(timeout_ms))

    if resolved_mode != "existing" and to_workspace and os.path.isdir(to_workspace):
        from brains.control.sessions import register_workspace

        register_workspace(to_workspace)

    init_db()
    now = utc_now()
    expires_at = now + timedelta(milliseconds=timeout_ms)
    code = _next_code()
    with SessionLocal() as session:
        if from_session_id:
            _require_help_actor(session, from_session_id, action="ask_peer")
        _expire_due(session)
        depth = _current_ask_depth_for_session(session, from_session_id)
        if depth > MAX_ASK_DEPTH:
            raise HelpDeadlockError(
                f"ask_peer refused: ask_depth would exceed cap ({depth} > {MAX_ASK_DEPTH})"
            )
        workspace_id = _resolve_session_workspace_id(session, from_session_id)
        target_workspace = (
            _resolve_target_workspace(session, to_workspace or "")
            if resolved_mode != "existing"
            else None
        )
        if resolved_mode != "existing" and target_workspace is None:
            raise ValueError(f"unknown Workspace for ephemeral review: {to_workspace!r}")
        if target_workspace is not None:
            from brains.control.memberships import visible_workspace_ids_for_current

            visible = visible_workspace_ids_for_current()
            if visible is not None and target_workspace.id not in visible:
                raise ValueError("Workspace unavailable for ephemeral review")
        canonical_target = target_workspace.slug if target_workspace is not None else to_workspace
        row = HelpRequest(
            code=code,
            from_session_id=from_session_id,
            from_workspace_id=workspace_id,
            to_workspace=canonical_target,
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
        if target_workspace is not None:
            launch_delay = _peer_grace_milliseconds() if resolved_mode == "auto" else 0
            session.add(
                HelpRequestExecution(
                    request_code=code,
                    mode=resolved_mode,
                    source_workspace_id=target_workspace.id,
                    required_tool=required_tool_norm or "",
                    status="queued",
                    attempt=0,
                    launch_after=now + timedelta(milliseconds=launch_delay),
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
        session.refresh(row)
        result = _row_to_dict(session, row)

    append_event(
        "help_asked",
        f"{code}: {subject.strip()}",
        workspace_id=workspace_id,
        session_id=from_session_id,
        metadata={
            "code": code,
            "to_workspace": canonical_target,
            "to_session_id": to_session_id,
            "required_tool": required_tool_norm,
            "depth": depth,
            "execution_mode": resolved_mode,
        },
    )

    if resolved_mode != "existing":
        from brains.control.help_execution import schedule_help_review

        schedule_help_review(code)

    return result


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
    execution_mode: str = "existing",
) -> dict[str, Any]:
    """File a request; block only for the existing-peer compatibility lane."""
    timeout_ms = max(100, int(timeout_ms))
    filed = file_help_request(
        subject,
        question,
        from_session_id=from_session_id,
        to_workspace=to_workspace,
        to_session_id=to_session_id,
        context=context,
        timeout_ms=timeout_ms,
        required_tool=required_tool,
        execution_mode=execution_mode,
    )
    if filed["execution_mode"] != "existing":
        return filed
    code = filed["code"]
    workspace_id = filed["from_workspace_id"]
    with SessionLocal() as session:
        request_id = session.query(HelpRequest.id).filter(HelpRequest.code == code).scalar()
    if request_id is None:  # pragma: no cover - the filing transaction just committed
        return {"code": code, "status": "cancelled"}

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
            # The call-local deadline cannot overwrite a concurrently acquired
            # claim or answer. Only the durable lifetime can expire the row.
            with SessionLocal() as session:
                _expire_due(session)
                session.commit()
                expiring = (
                    session.query(HelpRequest).filter(HelpRequest.id == request_id).one_or_none()
                )
                result = (
                    _row_to_dict(session, expiring)
                    if expiring
                    else {"code": code, "status": "expired"}
                )
            if result.get("status") in {"open", "claimed"}:
                time.sleep(poll)
                continue
            final_status = result.get("status", "expired")
            break
        time.sleep(poll)

    if final_status == "expired":
        append_event(
            "help_expired",
            f"{code}: expired",
            workspace_id=workspace_id,
            session_id=from_session_id,
            metadata={"code": code, "status": final_status},
        )
    return result


def _request_visible(row: HelpRequest) -> bool:
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    return visible is None or row.from_workspace_id is None or row.from_workspace_id in visible


def get_help_request(code: str, *, session_id: str | None = None) -> dict[str, Any] | None:
    """Return one visible request, applying expiry without blocking."""
    if not code:
        raise ValueError("code is required")
    init_db()
    with SessionLocal() as session:
        if session_id:
            _require_help_actor(session, session_id, action="get_help_request")
        _expire_due(session)
        row = session.query(HelpRequest).filter(HelpRequest.code == code).one_or_none()
        session.commit()
        if row is None or not _request_visible(row):
            return None
        return _row_to_dict(session, row)


def wait_help_request(
    code: str,
    *,
    session_id: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """Wait a bounded interval for one request to become terminal.

    A wait timeout never expires the durable request. It returns the current
    row with ``wait_timed_out=True`` so a caller can continue other work and
    wait again later.
    """
    timeout_ms = max(100, int(timeout_ms))
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while True:
        result = get_help_request(code, session_id=session_id)
        if result is None:
            raise ValueError(f"unknown or unavailable help request: {code}")
        if result["status"] in {"answered", "expired", "cancelled"}:
            return result
        if time.monotonic() >= deadline:
            return {**result, "wait_timed_out": True}
        time.sleep(_poll_interval_seconds())


def cancel_help_request(code: str, *, session_id: str) -> dict[str, Any]:
    """Cancel an open/claimed request as the Session that filed it."""
    if not code:
        raise ValueError("code is required")
    if not session_id:
        raise ValueError("cancel_help_request requires session_id")
    init_db()
    with SessionLocal() as session:
        _require_help_actor(session, session_id, action="cancel_help_request")
        _expire_due(session)
        row = session.query(HelpRequest).filter(HelpRequest.code == code).one_or_none()
        session.commit()
        if row is None or not _request_visible(row):
            raise ValueError(f"unknown or unavailable help request: {code}")
        if row.from_session_id != session_id:
            raise ValueError(f"help request {code} was filed by another session")
        if row.status == "cancelled":
            return {**_row_to_dict(session, row), "duplicate": True}
        if row.status not in {"open", "claimed"}:
            raise ValueError(f"help request {code} is {row.status}, not cancellable")
        execution = session.get(HelpRequestExecution, code)
        review_session_id = execution.review_session_id if execution is not None else None
        updated = (
            _help_transition(session, row, session_id=session_id, action="cancel_help_request")
            .filter(
                HelpRequest.from_session_id == session_id,
                or_(
                    and_(HelpRequest.status == "open", HelpRequest.expires_at >= utc_now()),
                    and_(HelpRequest.status == "claimed", _current_claim(utc_now())),
                ),
            )
            .update({"status": "cancelled"}, synchronize_session=False)
        )
        session.commit()
        if not updated:
            session.refresh(row)
            raise ValueError(f"help request {code} changed state before cancellation")
        if execution is not None and execution.status in {"queued", "running", "failed"}:
            execution.status = "cancelled"
            execution.lease_expires_at = None
            execution.completed_at = utc_now()
            execution.updated_at = execution.completed_at
        if review_session_id:
            review_session = session.get(AgentSession, review_session_id)
            if review_session is not None and review_session.ended_at is None:
                review_session.ended_at = utc_now()
                review_session.state = "failed"
                review_session.summary = "ephemeral help review cancelled by requester"
        session.commit()
        session.refresh(row)
        result = {**_row_to_dict(session, row), "duplicate": False}
        workspace_id = row.from_workspace_id
    if review_session_id:
        from brains.exec.session_channel import stop_session

        stop_session(review_session_id)
    append_event(
        "help_cancelled",
        f"{code}: cancelled by requester",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"code": code},
    )
    return result


def release_help_request(
    code: str,
    *,
    session_id: str,
    retry_timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """Release a claimed request back to the open queue as its claimant."""
    if not code:
        raise ValueError("code is required")
    if not session_id:
        raise ValueError("release_help_request requires session_id")
    retry_timeout_ms = max(100, int(retry_timeout_ms))
    init_db()
    with SessionLocal() as session:
        _require_help_actor(session, session_id, action="release_help_request")
        _expire_due(session)
        row = session.query(HelpRequest).filter(HelpRequest.code == code).one_or_none()
        session.commit()
        if row is None or not _request_visible(row):
            raise ValueError(f"unknown or unavailable help request: {code}")
        if row.status != "claimed":
            raise ValueError(f"help request {code} is {row.status}, not claimed")
        if row.claimed_by_session_id != session_id:
            raise ValueError(f"help request claimed by another session: {code}")
        now = utc_now()
        updated = (
            _help_transition(session, row, session_id=session_id, action="release_help_request")
            .filter(
                HelpRequest.status == "claimed",
                HelpRequest.claimed_by_session_id == session_id,
                _current_claim(utc_now()),
            )
            .update(
                {
                    "status": "open",
                    "claimed_by_session_id": None,
                    "claimed_at": None,
                    "expires_at": now + timedelta(milliseconds=retry_timeout_ms),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        if not updated:
            session.refresh(row)
            raise ValueError(f"help request {code} changed state before release")
        session.refresh(row)
        result = _row_to_dict(session, row)
        workspace_id = row.from_workspace_id
    append_event(
        "help_released",
        f"{code}: released by {session_id}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"code": code, "retry_timeout_ms": retry_timeout_ms},
    )
    return result


def _claim_request(
    *, session_id: str, workspace_slug: str | None = None, code: str | None = None
) -> dict[str, Any] | None:
    from brains.control.memberships import visible_workspace_ids_for_current

    with SessionLocal() as session:
        agent = _require_help_actor(session, session_id, action="claim_help_request")
        workspace = session.get(Workspace, agent.workspace_id)
        slug = workspace_slug
        if slug is None and workspace is not None:
            slug = workspace.slug
        tool = agent.tool
        visible = visible_workspace_ids_for_current()
        _expire_due(session)
        session.commit()
        query = _claimable_request_query(
            session,
            session_id=session_id,
            workspace_slug=slug,
            tool=tool,
            visible_workspace_ids=visible,
            owned_claim=code is not None,
        )
        if code is not None:
            query = query.filter(HelpRequest.code == code)
        match = query.first()
        if match is None:
            return None
        candidate, required_tool = match
        transition = _help_transition(
            session, candidate, session_id=session_id, action="claim_help_request"
        )
        # The actor's Workspace and harness can change before the lock too.
        current_agent = session.get(AgentSession, session_id)
        if current_agent is None:
            raise ValueError("help session unavailable")
        workspace = session.get(Workspace, current_agent.workspace_id)
        slug = workspace_slug
        if slug is None and workspace is not None:
            slug = workspace.slug
        tool = current_agent.tool
        eligible = _claimable_request_query(
            session,
            session_id=session_id,
            workspace_slug=slug,
            tool=tool,
            visible_workspace_ids=None,  # Source visibility was checked under the lock.
            owned_claim=code is not None,
        ).filter(HelpRequest.code == candidate.code)
        if eligible.first() is None:
            return None
        now = utc_now()
        if candidate.status == "claimed":
            # A retry is a read, not a renewal or a second acceptance event.
            current = eligible.filter(_current_claim(utc_now())).first()
            return _row_to_dict(session, current[0]) if current is not None else None
        updated = transition.filter(
            HelpRequest.status == "open", HelpRequest.expires_at >= utc_now()
        ).update(
            {
                "status": "claimed",
                "claimed_by_session_id": session_id,
                "claimed_at": now,
            },
            synchronize_session=False,
        )
        if not updated:
            # A simultaneous retry by the same owner may have won the CAS.
            # Never fall back to another code, or extend the winning claim.
            if code is not None:
                current = eligible.filter(
                    HelpRequest.status == "claimed",
                    HelpRequest.claimed_by_session_id == session_id,
                    _current_claim(utc_now()),
                ).first()
                if current is not None:
                    return _row_to_dict(session, current[0])
            return None
        session.query(HelpRequestExecution).filter(
            HelpRequestExecution.request_code == candidate.code,
            HelpRequestExecution.mode == "auto",
        ).update(
            {
                "status": "cancelled",
                "completed_at": now,
                "updated_at": now,
                "lease_expires_at": None,
            },
            synchronize_session=False,
        )
        session.commit()
        session.refresh(candidate)
        result = _row_to_dict(session, candidate)
    append_event(
        "help_claimed",
        f"{result['code']}: claimed by {session_id}",
        session_id=session_id,
        metadata={
            "code": result["code"],
            "from_session_id": result["from_session_id"],
            "required_tool": required_tool,
            "claimer_tool": tool,
        },
    )
    return result


def claim_help_request(code: str, *, session_id: str) -> dict[str, Any]:
    """Accept exactly one eligible request; an owned retry never renews it."""
    if not code:
        raise ValueError("code is required")
    if not session_id:
        raise ValueError("claim_help_request requires session_id")
    init_db()
    result = _claim_request(code=code, session_id=session_id)
    if result is None:
        raise ValueError(f"unknown or unavailable help request: {code}")
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
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    poll = _poll_interval_seconds()
    while True:
        result = _claim_request(session_id=session_id, workspace_slug=workspace_slug)
        if result is not None:
            return result
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
        _require_help_actor(session, session_id, action="answer_request")
        expired = _expire_due(session)
        row = session.query(HelpRequest).filter(HelpRequest.code == code).one_or_none()
        session.commit()
        if expired and row is not None:
            session.refresh(row)
        if row is None or not _request_visible(row):
            raise ValueError(f"unknown or unavailable help request: {code}")
        if row.status == "expired":
            raise HelpExpiredError(f"help request expired: {code}")
        if row.status == "answered":
            raise ValueError(f"help request already answered: {code}")
        if row.status != "claimed":
            raise ValueError(f"help request not in claimed state: {code} (status={row.status})")
        if row.claimed_by_session_id != session_id:
            raise ValueError(f"help request claimed by another session: {code}")
        now = utc_now()
        updated = (
            _help_transition(session, row, session_id=session_id, action="answer_request")
            .filter(_current_claim(utc_now()))
            .update(
                {
                    "answer": answer.strip(),
                    "evidence": evidence.strip(),
                    "answered_at": now,
                    "status": "answered",
                },
                synchronize_session=False,
            )
        )
        session.commit()
        if not updated:
            _expire_due(session)
            session.commit()
            session.refresh(row)
            if row.status == "expired":
                raise HelpExpiredError(f"help request expired: {code}")
            raise ValueError(f"help request {code} changed state before answer")
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
        result = [_row_to_dict(session, row) for row in rows]
        session.commit()
        return result


__all__ = [
    "ask_peer",
    "file_help_request",
    "get_help_request",
    "wait_help_request",
    "cancel_help_request",
    "release_help_request",
    "claim_help_request",
    "wait_for_request",
    "answer_request",
    "list_open_help_requests",
    "HelpDeadlockError",
    "HelpExpiredError",
    "MAX_ASK_DEPTH",
    "DEFAULT_TIMEOUT_MS",
]

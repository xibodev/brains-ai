"""Coordination queue health + continuity repair — BL-P1-12.

Approvals, handoffs, mailbox messages, help requests, workspace claims, and
Session commands each persist with their own status column and their own
opportunistic staleness sweep (see ``brains.control.handoffs.mark_stale_handoffs``,
``brains.control.claims._expire_claims``, ``brains.control.help._expire_due``,
``brains.control.session_commands.expire_leases``) - but there has never been
one place that named each family's owner, scope, lifecycle, and expiry policy
(or explicit indefinite policy), or that could say "how healthy is the
coordination plane right now" without an operator reading six modules.

This module is that place. It never invents a new expiry rule and never
deletes unresolved work - it summarizes what each family's own rules already
say (:func:`summarize`), detects orphaned Session/Workspace references and
stale leases without mutating anything (:func:`diagnose`), and offers a
dry-run repair plan (:func:`plan_repair`) whose ``apply`` (:func:`apply_repair`)
does nothing but call each family's own existing fenced helper - the same
code path the family's own read functions already call opportunistically.

Checkpoints (``snapshots``) are included in the family inventory for
completeness with an explicit **indefinite** policy - a checkpoint is
point-in-time evidence with no transition and no expiry, kept until its
owning Workspace is pruned. The static tool registry (``registered_tools``)
is a capability record, not a per-Session work item with a lifecycle, and is
deliberately out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from brains.control.common import utc_now
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    ApprovalRequest,
    Handoff,
    HelpRequest,
    MailboxMessage,
    SessionCommand,
    Snapshot,
    Workspace,
    WorkspaceClaim,
)

#: Bounded sample size for issue rows returned by :func:`diagnose`. Detection
#: must never return an unbounded payload - an operator needs to see *that*
#: there are 4,000 orphaned rows, not read all 4,000 in one response.
_SAMPLE_LIMIT = 20


@dataclass(frozen=True)
class QueueFamily:
    """Owner/scope/lifecycle/expiry metadata for one durable queue family."""

    name: str
    owner: str
    scope: str
    lifecycle: str
    expiry_policy: str


#: The queue-health contract BL-P1-12 asks for: every family, its owner, its
#: scope, its lifecycle transitions, and its expiry policy - or an explicit
#: statement that the policy is indefinite by design.
FAMILIES: tuple[QueueFamily, ...] = (
    QueueFamily(
        name="approvals",
        owner="the human resolver separated from the requester (brains.control.decisions)",
        scope="Workspace-scoped ASK, resolved through the console, CLI, or MCP",
        lifecycle="open -> resolved|rejected|deferred -> consumed",
        expiry_policy=(
            "indefinite while open by design - an open ASK blocks the Session that "
            "filed it until a human resolves it; there is no automatic expiry"
        ),
    ),
    QueueFamily(
        name="handoffs",
        owner="the Session that set it; picked up by the next Session in that Workspace",
        scope="Workspace-scoped, one active handoff per Workspace",
        lifecycle="active -> picked_up|cleared|stale",
        expiry_policy=(
            "active > BRAINS_HANDOFF_STALE_HOURS (default 24h) is opportunistically "
            "flipped to stale by brains.control.handoffs.mark_stale_handoffs"
        ),
    ),
    QueueFamily(
        name="mailbox",
        owner="the addressed Session (to_session_id), or a Workspace broadcast",
        scope="Session-scoped or Workspace-scoped",
        lifecycle="unread -> read",
        expiry_policy=(
            "indefinite - read_messages() marks a message read, it never expires or "
            "deletes one; unread mail waits for its recipient"
        ),
    ),
    QueueFamily(
        name="help_requests",
        owner="the peer Session/Workspace it targets",
        scope="Session-scoped or Workspace-scoped, per-request timeout",
        lifecycle="open -> claimed -> answered|expired|cancelled",
        expiry_policy=(
            "each request's own expires_at (from its timeout_ms at ask_peer time); "
            "past-deadline open/claimed rows flip to expired via "
            "brains.control.help._expire_due"
        ),
    ),
    QueueFamily(
        name="workspace_claims",
        owner="the Session holding the claim",
        scope="Workspace-scoped, exactly one claim per Workspace",
        lifecycle="claimed -> released|expired",
        expiry_policy=(
            "expires_at (duration_minutes at claim time); expired rows are deleted "
            "(not merely marked) by brains.control.claims._expire_claims - releasing "
            "the Workspace, not hiding unresolved work, since a claim carries no "
            "content of its own"
        ),
    ),
    QueueFamily(
        name="session_commands",
        owner="the Runtime or local consumer bound to the command's Session",
        scope="Session-scoped",
        lifecycle="requested -> delivered -> acknowledged|failed|cancelled",
        expiry_policy=(
            "BRAINS_SESSION_COMMAND_LEASE_SECONDS per claimed attempt; an expired "
            "lease is requeued by brains.control.session_commands.expire_leases, "
            "or settled failed once BRAINS_SESSION_COMMAND_MAX_ATTEMPTS is spent"
        ),
    ),
    QueueFamily(
        name="checkpoints",
        owner="the Workspace it snapshots",
        scope="Workspace-scoped",
        lifecycle="captured (point-in-time; no further transition)",
        expiry_policy="indefinite by design - kept until the owning Workspace is pruned",
    ),
)

_FAMILY_BY_NAME: dict[str, QueueFamily] = {family.name: family for family in FAMILIES}


def _family_metadata(name: str) -> dict[str, str]:
    family = _FAMILY_BY_NAME[name]
    return {
        "owner": family.owner,
        "scope": family.scope,
        "lifecycle": family.lifecycle,
        "expiry_policy": family.expiry_policy,
    }


# --------------------------------------------------------------------------- #
# stale/expired prediction — read-only mirrors of each family's own fenced
# expiry rule, used by both summarize() (counts) and plan_repair() (dry-run).
# --------------------------------------------------------------------------- #


def _predict_stale_handoffs() -> int:
    from brains.control.handoffs import _stale_after_hours

    cutoff = utc_now() - timedelta(hours=_stale_after_hours())
    with SessionLocal() as session:
        return (
            session.query(Handoff)
            .filter(Handoff.status == "active", Handoff.set_at < cutoff)
            .count()
        )


def _predict_expired_claims() -> int:
    now = utc_now()
    with SessionLocal() as session:
        return session.query(WorkspaceClaim).filter(WorkspaceClaim.expires_at < now).count()


def _predict_expired_help_requests() -> int:
    now = utc_now()
    with SessionLocal() as session:
        return (
            session.query(HelpRequest)
            .filter(HelpRequest.expires_at < now, HelpRequest.status.in_(("open", "claimed")))
            .count()
        )


def _predict_expirable_session_command_leases() -> int:
    """Mirrors ``session_commands.expire_leases``'s own candidate selection
    (``delivered`` + a lease that has passed) without mutating anything."""
    from brains.control.session_commands import STATUS_DELIVERED

    now = utc_now()
    count = 0
    with SessionLocal() as session:
        rows = (
            session.query(SessionCommand)
            .filter(SessionCommand.status == STATUS_DELIVERED)
            .filter(SessionCommand.lease_expires_at.isnot(None))
            .all()
        )
        for row in rows:
            expires = row.lease_expires_at
            if expires is not None and expires.tzinfo is None:
                from datetime import UTC

                expires = expires.replace(tzinfo=UTC)
            if expires is not None and expires <= now:
                count += 1
    return count


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #


def summarize() -> dict[str, Any]:
    """One bounded snapshot of every durable queue family's health.

    Per family: total rows, the count still "open"/active, and the count
    that is stale/expired-but-not-yet-swept (the same rows :func:`plan_repair`
    would act on) — plus the owner/scope/lifecycle/expiry metadata every
    family declares in :data:`FAMILIES`.
    """
    init_db()
    with SessionLocal() as session:
        approvals_total = session.query(ApprovalRequest).count()
        approvals_open = (
            session.query(ApprovalRequest).filter(ApprovalRequest.status == "open").count()
        )
        handoffs_total = session.query(Handoff).count()
        handoffs_active = session.query(Handoff).filter(Handoff.status == "active").count()
        mailbox_total = session.query(MailboxMessage).count()
        mailbox_unread = (
            session.query(MailboxMessage).filter(MailboxMessage.read_at.is_(None)).count()
        )
        help_total = session.query(HelpRequest).count()
        help_open = (
            session.query(HelpRequest).filter(HelpRequest.status.in_(("open", "claimed"))).count()
        )
        claims_total = session.query(WorkspaceClaim).count()
        session_commands_total = session.query(SessionCommand).count()
        from brains.control.session_commands import OPEN_STATUSES

        session_commands_open = (
            session.query(SessionCommand).filter(SessionCommand.status.in_(OPEN_STATUSES)).count()
        )
        checkpoints_total = session.query(Snapshot).count()

    families = {
        "approvals": {
            "total": approvals_total,
            "open": approvals_open,
            "stale_or_expired": 0,
            **_family_metadata("approvals"),
        },
        "handoffs": {
            "total": handoffs_total,
            "open": handoffs_active,
            "stale_or_expired": _predict_stale_handoffs(),
            **_family_metadata("handoffs"),
        },
        "mailbox": {
            "total": mailbox_total,
            "open": mailbox_unread,
            "stale_or_expired": 0,
            **_family_metadata("mailbox"),
        },
        "help_requests": {
            "total": help_total,
            "open": help_open,
            "stale_or_expired": _predict_expired_help_requests(),
            **_family_metadata("help_requests"),
        },
        "workspace_claims": {
            "total": claims_total,
            "open": claims_total,
            "stale_or_expired": _predict_expired_claims(),
            **_family_metadata("workspace_claims"),
        },
        "session_commands": {
            "total": session_commands_total,
            "open": session_commands_open,
            "stale_or_expired": _predict_expirable_session_command_leases(),
            **_family_metadata("session_commands"),
        },
        "checkpoints": {
            "total": checkpoints_total,
            "open": checkpoints_total,
            "stale_or_expired": 0,
            **_family_metadata("checkpoints"),
        },
    }
    return {"generated_at": utc_now().isoformat(), "families": families}


# --------------------------------------------------------------------------- #
# orphan detection — non-destructive, bounded
# --------------------------------------------------------------------------- #


def _orphan_check(session, family: str, model: Any, column: Any, valid_ids: Any) -> dict | None:
    query = session.query(model).filter(column.isnot(None)).filter(~column.in_(valid_ids))
    count = query.count()
    if not count:
        return None
    field = column.key
    sample = [
        {"id": getattr(row, "id", None), field: getattr(row, field, None)}
        for row in query.limit(_SAMPLE_LIMIT).all()
    ]
    referenced = "session" if "session" in field else "workspace"
    return {
        "code": "orphaned_reference",
        "family": family,
        "field": field,
        "count": count,
        "detail": (
            f"{family}.{field}: {count} row(s) reference a {referenced} that no longer exists"
        ),
        "sample": sample,
    }


def _orphan_text_check(
    session,
    family: str,
    model: Any,
    column: Any,
    valid_values: Any,
    *,
    referenced: str,
) -> dict | None:
    query = session.query(model).filter(column.isnot(None)).filter(~column.in_(valid_values))
    count = query.count()
    if not count:
        return None
    field = column.key
    return {
        "code": "orphaned_reference",
        "family": family,
        "field": field,
        "count": count,
        "detail": f"{family}.{field}: {count} row(s) reference a {referenced} that no longer exists",
        "sample": [
            {"id": getattr(row, "id", None), field: getattr(row, field, None)}
            for row in query.limit(_SAMPLE_LIMIT).all()
        ],
    }


def diagnose() -> dict[str, Any]:
    """Detect orphaned Session/Workspace references and stale leases.

    Read-only: nothing is deleted or mutated. Every issue names the family,
    the field, a count, and a bounded sample of affected rows so an operator
    can decide what (if anything) needs manual resolution before a repair.
    """
    init_db()
    issues: list[dict[str, Any]] = []
    with SessionLocal() as session:
        live_sessions = session.query(AgentSession.id)
        live_workspaces = session.query(Workspace.id)
        session_ref_checks: tuple[tuple[str, Any, Any], ...] = (
            ("approvals", ApprovalRequest, ApprovalRequest.session_id),
            ("handoffs", Handoff, Handoff.set_by_session_id),
            ("handoffs", Handoff, Handoff.picked_up_by_session_id),
            ("help_requests", HelpRequest, HelpRequest.from_session_id),
            ("help_requests", HelpRequest, HelpRequest.claimed_by_session_id),
            ("workspace_claims", WorkspaceClaim, WorkspaceClaim.session_id),
            ("session_commands", SessionCommand, SessionCommand.session_id),
            ("mailbox", MailboxMessage, MailboxMessage.to_session_id),
            ("mailbox", MailboxMessage, MailboxMessage.from_session_id),
        )
        for family, model, column in session_ref_checks:
            issue = _orphan_check(session, family, model, column, live_sessions)
            if issue is not None:
                issues.append(issue)

        workspace_ref_checks: tuple[tuple[str, Any, Any], ...] = (
            ("approvals", ApprovalRequest, ApprovalRequest.workspace_id),
            ("handoffs", Handoff, Handoff.workspace_id),
            ("workspace_claims", WorkspaceClaim, WorkspaceClaim.workspace_id),
            ("mailbox", MailboxMessage, MailboxMessage.workspace_id),
            ("help_requests", HelpRequest, HelpRequest.from_workspace_id),
            ("session_commands", SessionCommand, SessionCommand.workspace_id),
            ("checkpoints", Snapshot, Snapshot.workspace_id),
        )
        for family, model, column in workspace_ref_checks:
            issue = _orphan_check(session, family, model, column, live_workspaces)
            if issue is not None:
                issues.append(issue)
        for issue in (
            _orphan_text_check(
                session,
                "help_requests",
                HelpRequest,
                HelpRequest.to_session_id,
                session.query(AgentSession.id),
                referenced="session",
            ),
            _orphan_text_check(
                session,
                "help_requests",
                HelpRequest,
                HelpRequest.to_workspace,
                session.query(Workspace.slug),
                referenced="workspace",
            ),
        ):
            if issue is not None:
                issues.append(issue)

    return {
        "generated_at": utc_now().isoformat(),
        "issue_count": sum(issue["count"] for issue in issues),
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# repair — dry-run plan, and an explicit apply that only calls each family's
# own existing fenced helper.
# --------------------------------------------------------------------------- #

#: The repairs this module considers objectively safe: each is a status
#: transition (or, for an expired claim, a release) that the family's own
#: read path already performs opportunistically. None of them deletes an
#: open approval, an unread message, or any other unresolved work.
SAFE_REPAIRS: tuple[str, ...] = (
    "stale_handoffs",
    "expired_workspace_claims",
    "expired_help_requests",
    "expired_session_command_leases",
)


def plan_repair() -> dict[str, Any]:
    """Dry-run: exactly what :func:`apply_repair` would do, without doing it."""
    init_db()
    return {
        "generated_at": utc_now().isoformat(),
        "actions": [
            {
                "code": "stale_handoffs",
                "family": "handoffs",
                "description": "mark active handoffs older than the stale window as stale",
                "would_affect_rows": _predict_stale_handoffs(),
            },
            {
                "code": "expired_workspace_claims",
                "family": "workspace_claims",
                "description": "release workspace claims past their expiry",
                "would_affect_rows": _predict_expired_claims(),
            },
            {
                "code": "expired_help_requests",
                "family": "help_requests",
                "description": "flip open/claimed help requests past their expiry to expired",
                "would_affect_rows": _predict_expired_help_requests(),
            },
            {
                "code": "expired_session_command_leases",
                "family": "session_commands",
                "description": (
                    "return session commands whose consumer lease expired to the "
                    "queue, or settle them failed once attempts are exhausted"
                ),
                "would_affect_rows": _predict_expirable_session_command_leases(),
            },
        ],
        "unresolved_work_preserved": True,
    }


def apply_repair() -> dict[str, Any]:
    """Apply only the safe continuity repairs, via each family's own helper.

    Every action here is exactly the fenced helper the family's own read path
    already calls opportunistically - this is not a new mutation rule, only a
    single place to trigger every family's existing one and report what
    happened. Never deletes unresolved work: an open approval, unread mail,
    and an un-expired help request or claim are left untouched.
    """
    init_db()
    from brains.control.claims import _expire_claims
    from brains.control.handoffs import mark_stale_handoffs
    from brains.control.help import _expire_due
    from brains.control.session_commands import expire_leases

    stale_handoffs = mark_stale_handoffs()

    with SessionLocal() as session:
        expired_claims = _expire_claims(session)
        session.commit()

    with SessionLocal() as session:
        expired_help = _expire_due(session)
        session.commit()

    requeued_or_failed = expire_leases()

    return {
        "applied_at": utc_now().isoformat(),
        "actions": [
            {"code": "stale_handoffs", "family": "handoffs", "applied_rows": stale_handoffs},
            {
                "code": "expired_workspace_claims",
                "family": "workspace_claims",
                "applied_rows": expired_claims,
            },
            {
                "code": "expired_help_requests",
                "family": "help_requests",
                "applied_rows": expired_help,
            },
            {
                "code": "expired_session_command_leases",
                "family": "session_commands",
                "applied_rows": len(requeued_or_failed),
            },
        ],
        "unresolved_work_preserved": True,
    }


__all__ = [
    "FAMILIES",
    "SAFE_REPAIRS",
    "QueueFamily",
    "apply_repair",
    "diagnose",
    "plan_repair",
    "summarize",
]

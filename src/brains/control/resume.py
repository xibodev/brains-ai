"""Brain-session resume: tool-session links + checkpoints.

When a CLI tool (Claude Code, Copilot CLI, Codex, …) starts a brain
session, the durable thread is the brain ``AgentSession``. The tool
keeps its own session state under its own id, and that id changes every
time the tool restarts. This module gives operators and tools a clean
way to:

* Link the current tool-side session id to a brain session
  (``link_tool_session``).
* Resume a brain session after a tool crash and learn everything that
  matters in one call (``resume_brain_session``) — last checkpoint,
  active claims / handoffs / tasks, unread mail, registered tool-session
  links so it's obvious which past tool sessions served this brain
  session.
* Reverse-lookup which brain sessions a tool-side id is bound to
  (``find_brain_sessions``).
* Write resume cairns at natural breakpoints (``checkpoint``).
* Read them back (``list_checkpoints``, ``latest_checkpoint``).

Brain does not try to mirror the tool's working memory. Checkpoints are
intentionally minimal — narrative + next-action + blockers + an optional
pointer to the tool's local scratchpad path. That keeps the durable
record small and forces the agent to think about what actually matters
for the next session to pick up.
"""

from __future__ import annotations

import json
from typing import Any

from brains.control.events import append_event
from brains.control.handoffs import list_handoffs, mark_stale_handoffs
from brains.control.mailbox import read_messages
from brains.control.sessions import (
    AgentSessionNotFoundError,
    heartbeat_session,
    reap_zombie_sessions,
)
from brains.control.tasks import list_tasks
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    SessionCheckpoint,
    SessionLease,
    ToolSessionLink,
    Workspace,
    WorkspaceClaim,
)


def _link_to_dict(row: ToolSessionLink) -> dict[str, Any]:
    return {
        "id": row.id,
        "brain_session_id": row.brain_session_id,
        "tool": row.tool,
        "tool_session_id": row.tool_session_id,
        "linked_at": row.linked_at.isoformat(),
        "linked_by": row.linked_by,
    }


def _checkpoint_to_dict(row: SessionCheckpoint) -> dict[str, Any]:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "workspace_id": row.workspace_id,
        "summary": row.summary,
        "next_action": row.next_action,
        "blockers": row.blockers,
        "scratchpad_path": row.scratchpad_path,
        "metadata": json.loads(row.metadata_json) if row.metadata_json else {},
        "created_at": row.created_at.isoformat(),
    }


def link_tool_session(
    brain_session_id: str,
    tool: str,
    tool_session_id: str,
    *,
    linked_by: str = "auto",
) -> dict[str, Any]:
    """Record that ``tool_session_id`` (under ``tool``) is serving
    ``brain_session_id``.

    Idempotent — the same triple recorded twice returns the existing
    row instead of erroring. Raises ``AgentSessionNotFoundError`` if the
    brain session id is unknown so callers learn early rather than
    silently writing orphan links.
    """
    if not brain_session_id or not tool or not tool_session_id:
        raise ValueError("brain_session_id, tool and tool_session_id are all required")
    if linked_by not in ("auto", "operator"):
        raise ValueError("linked_by must be 'auto' or 'operator'")
    init_db()
    heartbeat_session(brain_session_id, allow_ended=True)
    with SessionLocal() as session:
        agent = (
            session.query(AgentSession).filter(AgentSession.id == brain_session_id).one_or_none()
        )
        if agent is None:
            raise AgentSessionNotFoundError(f"unknown brain session: {brain_session_id}")
        existing = (
            session.query(ToolSessionLink)
            .filter(
                ToolSessionLink.brain_session_id == brain_session_id,
                ToolSessionLink.tool == tool,
                ToolSessionLink.tool_session_id == tool_session_id,
            )
            .one_or_none()
        )
        if existing is not None:
            return _link_to_dict(existing)
        row = ToolSessionLink(
            brain_session_id=brain_session_id,
            tool=tool,
            tool_session_id=tool_session_id,
            linked_by=linked_by,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        workspace_id = agent.workspace_id
        result = _link_to_dict(row)
    append_event(
        "tool_session_linked",
        f"{tool}:{tool_session_id} linked to {brain_session_id}",
        workspace_id=workspace_id,
        session_id=brain_session_id,
        metadata={"tool": tool, "tool_session_id": tool_session_id, "by": linked_by},
    )
    return result


def list_tool_session_links(brain_session_id: str) -> list[dict[str, Any]]:
    """Return every tool-side session id that's been linked to this brain
    session, oldest first."""
    if not brain_session_id:
        raise ValueError("brain_session_id is required")
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(ToolSessionLink)
            .filter(ToolSessionLink.brain_session_id == brain_session_id)
            .order_by(ToolSessionLink.linked_at.asc(), ToolSessionLink.id.asc())
            .all()
        )
        return [_link_to_dict(r) for r in rows]


def find_brain_sessions(
    tool: str,
    tool_session_id: str,
) -> list[dict[str, Any]]:
    """Reverse lookup: which brain sessions has this tool-side id been
    bound to? Useful when the operator only knows the tool-side id."""
    if not tool or not tool_session_id:
        raise ValueError("tool and tool_session_id are required")
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(ToolSessionLink, AgentSession, Workspace)
            .join(AgentSession, AgentSession.id == ToolSessionLink.brain_session_id)
            .join(Workspace, Workspace.id == AgentSession.workspace_id)
            .filter(
                ToolSessionLink.tool == tool,
                ToolSessionLink.tool_session_id == tool_session_id,
            )
            .order_by(ToolSessionLink.linked_at.desc())
            .all()
        )
        out: list[dict[str, Any]] = []
        for link, agent, workspace in rows:
            out.append(
                {
                    "brain_session_id": link.brain_session_id,
                    "tool": link.tool,
                    "tool_session_id": link.tool_session_id,
                    "linked_at": link.linked_at.isoformat(),
                    "linked_by": link.linked_by,
                    "workspace": workspace.slug,
                    "session_ended_at": (agent.ended_at.isoformat() if agent.ended_at else None),
                    "last_activity_at": (
                        agent.last_activity_at.isoformat() if agent.last_activity_at else None
                    ),
                }
            )
        return out


def checkpoint(
    session_id: str,
    summary: str,
    *,
    next_action: str = "",
    blockers: str = "",
    scratchpad_path: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drop a resume cairn against ``session_id``.

    Call this at natural breakpoints — end of a sub-task, before a long
    operation, before tool-side compaction. The resumed session reads
    the latest checkpoint via ``resume_brain_session`` to re-orient
    cheaply instead of replaying the tool's entire scratchpad.

    ``summary`` is required and must be non-empty. Everything else is
    optional — keep entries small.
    """
    if not session_id:
        raise ValueError("session_id is required")
    if not summary or not summary.strip():
        raise ValueError("summary is required")
    init_db()
    with SessionLocal() as session:
        agent = session.query(AgentSession).filter(AgentSession.id == session_id).one_or_none()
        if agent is None:
            raise AgentSessionNotFoundError(f"unknown brain session: {session_id}")
        row = SessionCheckpoint(
            session_id=session_id,
            workspace_id=agent.workspace_id,
            summary=summary.strip(),
            next_action=next_action.strip() or None,
            blockers=blockers.strip() or None,
            scratchpad_path=scratchpad_path.strip() or None,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        workspace_id = agent.workspace_id
        result = _checkpoint_to_dict(row)
    append_event(
        "checkpoint_written",
        summary.strip()[:200],
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={
            "checkpoint_id": result["id"],
            "has_next_action": bool(next_action.strip()),
            "has_blockers": bool(blockers.strip()),
        },
    )
    return result


def list_checkpoints(
    session_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return checkpoints for ``session_id``, newest first."""
    if not session_id:
        raise ValueError("session_id is required")
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(SessionCheckpoint)
            .filter(SessionCheckpoint.session_id == session_id)
            .order_by(SessionCheckpoint.created_at.desc(), SessionCheckpoint.id.desc())
            .limit(limit)
            .all()
        )
        return [_checkpoint_to_dict(r) for r in rows]


def latest_checkpoint(session_id: str) -> dict[str, Any] | None:
    """Convenience: the most recent checkpoint, or ``None``."""
    rows = list_checkpoints(session_id, limit=1)
    return rows[0] if rows else None


def resume_brain_session(
    brain_session_id: str,
    *,
    tool: str | None = None,
    tool_session_id: str | None = None,
    operator: bool = False,
    mail_limit: int = 10,
    event_limit: int = 20,
) -> dict[str, Any]:
    """Build a one-call resume packet for a brain session.

    If ``tool`` and ``tool_session_id`` are both provided, the new
    tool-side id is linked to the brain session in the same call (with
    ``linked_by="operator"`` when ``operator=True``, else ``"auto"``).
    That way the typical resume call from a restarted CLI is:

        brains.resume_brain_session(
            brain_session_id="ses_abc123",
            tool="copilot-cli",
            tool_session_id="<fresh tool-side uuid>",
        )

    The returned packet contains:

    * ``brain_session`` — id, workspace slug, started/ended/last_activity
      timestamps, tool string.
    * ``last_checkpoint`` — the most recent checkpoint (or ``None``).
    * ``checkpoints_total`` — how many checkpoints exist (so the caller
      can ask for more via ``list_checkpoints`` if needed).
    * ``active_handoffs`` — handoffs still active on the workspace.
    * ``active_claims`` — workspace claims this session still owns.
    * ``open_tasks`` — tasks claimed by this session that are not yet
      done.
    * ``unread_messages`` — count + a small preview, not marked read.
    * ``tool_session_links`` — every tool-side id ever linked to this
      brain session (including the one we just linked).
    * ``recent_events`` — last few events touching this session, newest
      first.

    Raises ``AgentSessionNotFoundError`` if the brain session id is
    unknown.
    """
    if not brain_session_id:
        raise ValueError("brain_session_id is required")
    # Reap zombies + stale handoffs first so the packet is honest about
    # what's actually still live.
    try:
        reap_zombie_sessions()
        mark_stale_handoffs()
    except Exception:
        pass
    init_db()
    # Resuming is the explicit operation that may reactivate an expired
    # coordination lease. It also refuses a handle that already has a successor.
    heartbeat_session(brain_session_id)
    # Auto-link the fresh tool-side id if provided. We do this BEFORE
    # building the packet so list_tool_session_links includes it.
    if tool and tool_session_id:
        link_tool_session(
            brain_session_id,
            tool,
            tool_session_id,
            linked_by="operator" if operator else "auto",
        )

    with SessionLocal() as session:
        agent = (
            session.query(AgentSession).filter(AgentSession.id == brain_session_id).one_or_none()
        )
        if agent is None:
            raise AgentSessionNotFoundError(f"unknown brain session: {brain_session_id}")
        workspace = session.query(Workspace).filter(Workspace.id == agent.workspace_id).one()
        active_claims = (
            session.query(WorkspaceClaim)
            .filter(WorkspaceClaim.session_id == brain_session_id)
            .all()
        )
        active_claim_payload = [
            {
                "workspace_id": c.workspace_id,
                "scope": c.scope,
                "claimed_at": c.claimed_at.isoformat(),
                "expires_at": c.expires_at.isoformat(),
            }
            for c in active_claims
        ]
        lease = session.get(SessionLease, agent.id) if agent.pid is None else None
        brain_session_dict = {
            "id": agent.id,
            "workspace": workspace.slug,
            "workspace_path": workspace.path,
            "tool": agent.tool,
            "state": agent.state,
            "pid": agent.pid,
            "started_at": agent.started_at.isoformat(),
            "ended_at": agent.ended_at.isoformat() if agent.ended_at else None,
            "last_activity_at": (
                agent.last_activity_at.isoformat() if agent.last_activity_at else None
            ),
            "summary": agent.summary,
            "lease_expires_at": lease.lease_expires_at.isoformat() if lease else None,
        }

    last = latest_checkpoint(brain_session_id)
    with SessionLocal() as session:
        checkpoints_total = (
            session.query(SessionCheckpoint)
            .filter(SessionCheckpoint.session_id == brain_session_id)
            .count()
        )

    handoffs = list_handoffs(workspace_path=workspace.path, active_only=True)
    tasks_for_workspace = list_tasks(
        workspace_path=workspace.path,
        limit=100,
    )
    open_tasks = [
        t
        for t in tasks_for_workspace
        if t.get("claimed_by_session_id") == brain_session_id and t.get("status") != "done"
    ]

    # Unread mail: count + preview. Never mark read on resume — let the
    # agent decide.
    unread = read_messages(brain_session_id, mark_read=False, limit=mail_limit)
    mail_preview = [
        {"subject": m["subject"], "from": m["from_session_id"]}
        for m in unread[: min(mail_limit, 5)]
    ]

    links = list_tool_session_links(brain_session_id)

    # Recent events touching this session (newest first).
    with SessionLocal() as session:
        from brains.storage.models import Event

        ev_rows = (
            session.query(Event)
            .filter(Event.session_id == brain_session_id)
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(event_limit)
            .all()
        )
        recent_events = [
            {
                "id": r.id,
                "kind": r.kind,
                "message": r.message,
                "created_at": r.created_at.isoformat(),
            }
            for r in ev_rows
        ]

    packet = {
        "brain_session": brain_session_dict,
        "last_checkpoint": last,
        "checkpoints_total": checkpoints_total,
        "active_handoffs": handoffs,
        "active_claims": active_claim_payload,
        "open_tasks": open_tasks,
        "unread_messages": {"count": len(unread), "preview": mail_preview},
        "tool_session_links": links,
        "recent_events": recent_events,
    }

    append_event(
        "session_resumed",
        f"brain session {brain_session_id} resumed",
        workspace_id=agent.workspace_id,
        session_id=brain_session_id,
        metadata={
            "tool": tool,
            "tool_session_id": tool_session_id,
            "by": "operator" if operator else "auto",
        },
    )
    return packet


__all__ = [
    "link_tool_session",
    "list_tool_session_links",
    "find_brain_sessions",
    "checkpoint",
    "list_checkpoints",
    "latest_checkpoint",
    "resume_brain_session",
]

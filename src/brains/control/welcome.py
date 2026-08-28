"""Session-start "welcome packet" assembly.

When a CLI agent calls ``start_session`` we want it to discover the
coordination plane's other surfaces — mailbox, knowledge patterns,
retrievable memories, registered tools, indexed sources — without having
to read the whole skill doc first. This module assembles a small,
deterministic payload describing what's relevant for the newly-started
session's workspace so the calling agent can act on it immediately.

The shape of the payload is intentionally narrow: counts and a few
representative names, never the full bodies. Agents that want to act
follow up with the targeted tool (``read_messages``, ``use_pattern``,
``retrieve_memory``, ``list_sources``, etc).

Mailbox messages are *not* marked read. The only mutation is a bounded local
PATH readiness refresh for registered tools; it resolves executables without
starting them and never overwrites remote Runtime evidence.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from brains import __version__
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    KnowledgePattern,
    MailboxMessage,
    Memory,
    RegisteredTool,
    Runtime,
    Source,
    Workspace,
)

# How many representative items to surface per category. Kept tiny so the
# welcome payload never balloons.
_PREVIEW_LIMIT = 5


def _matches_workspace(applies_to: str | None, workspace: Workspace) -> bool:
    """Return True if a pattern's ``applies_to`` glob matches the workspace.

    ``applies_to`` is treated as a comma- or newline-separated list of
    glob patterns. A pattern matches if it matches either the workspace
    ``slug`` or any segment of its ``path``. An empty / null
    ``applies_to`` means the pattern is generally applicable and matches
    everything — we treat it as a non-match for the welcome packet (so we
    don't drown the agent in generic patterns) and let the agent discover
    those via the explicit ``list_patterns`` call.
    """
    if not applies_to:
        return False
    candidates = [c.strip() for c in applies_to.replace("\n", ",").split(",")]
    targets = [workspace.slug, workspace.path]
    for pat in candidates:
        if not pat:
            continue
        for target in targets:
            if fnmatch.fnmatchcase(target, pat):
                return True
            # Also try case-insensitive on Windows-style paths.
            if fnmatch.fnmatchcase(target.lower(), pat.lower()):
                return True
    return False


def build_welcome(workspace: Workspace, session_id: str) -> dict[str, Any]:
    """Assemble a welcome packet for a freshly-started session.

    The packet has these top-level keys:

    * ``unread_messages``: ``{"count": int, "subjects": [str, ...]}`` —
      mail addressed either to this session id or to the workspace at
      large that is still unread. Not marked read.
    * ``applicable_patterns``: list of approved patterns whose
      ``applies_to`` glob matches this workspace.
    * ``relevant_memories``: list of stored memory ``key``s whose key
      contains the workspace slug. Values are not inlined — agents call
      ``retrieve_memory`` for the body.
    * ``tool_status``: aggregate registry counts plus readiness for this
      Session's local PATH or bound Runtime.
    * ``index_status``: ``{"sources": int, "indexed": int}`` for this
      workspace's RAG / repo-indexer state. Lets the agent notice the
      indexer is empty before it goes hunting blindly.
    * ``hints``: short string list of suggested next tool calls based on
      what's present. e.g. "you have 3 unread messages — call
      read_messages".
    * ``brain_version``: the installed ``brains`` package version. Lets
      the agent — and any operator reading the trace — confirm which
      build of the coordination plane is serving them. Run
      ``brains upgrade`` if a newer release has shipped.
    * ``skills``: deduplicated Skills attached to this session's Persona
      and/or Project (BL-P1-08), each carrying ``sources`` provenance
      (``["persona"]``, ``["project"]``, or both). Empty when the session
      has no persona/project or nothing is attached.

    Defensive: every section catches its own exception and degrades
    gracefully, so a single corrupt row never blocks session start.
    """
    init_db()
    payload: dict[str, Any] = {
        "unread_messages": {"count": 0, "subjects": []},
        "applicable_patterns": [],
        "knowledge": {"count": 0, "entries": []},
        "relevant_memories": [],
        "tool_status": {
            "registered": 0,
            "available": 0,
            "missing": 0,
            "unverified": 0,
        },
        "index_status": {"sources": 0, "indexed": 0},
        "hints": [],
        "brain_version": __version__,
    }

    try:
        with SessionLocal() as session:
            from brains.control.sessions import predecessor_session_ids

            recipient_ids = [session_id, *predecessor_session_ids(session, session_id)]
            # Unread mail addressed to this session OR to the workspace.
            # NULL-workspace rows are deliberately NOT catch-all: they are
            # direct-delivery only (to_session_id set), never a cross-project
            # firehose.
            mail_q = session.query(MailboxMessage).filter(
                MailboxMessage.read_at.is_(None),
                (MailboxMessage.to_session_id.in_(recipient_ids))
                | (
                    MailboxMessage.to_session_id.is_(None)
                    & (MailboxMessage.workspace_id == workspace.id)
                ),
            )
            total = mail_q.count()
            subjects = [
                row.subject
                for row in mail_q.order_by(MailboxMessage.created_at.asc())
                .limit(_PREVIEW_LIMIT)
                .all()
            ]
            payload["unread_messages"] = {"count": total, "subjects": subjects}
            if total:
                payload["hints"].append(f"{total} unread message(s) — call read_messages")
    except Exception:
        pass

    try:
        with SessionLocal() as session:
            approved = (
                session.query(KnowledgePattern).filter(KnowledgePattern.status == "approved").all()
            )
            matched = [p for p in approved if _matches_workspace(p.applies_to, workspace)]
            matched.sort(key=lambda p: (-p.usage_count, p.name))
            payload["applicable_patterns"] = [
                {
                    "name": p.name,
                    "category": p.category,
                    "description": p.description,
                    "usage_count": p.usage_count,
                }
                for p in matched[:_PREVIEW_LIMIT]
            ]
            if matched:
                payload["hints"].append(
                    f"{len(matched)} matching pattern(s) — call use_pattern before improvising"
                )
    except Exception:
        pass

    try:
        from brains.control.knowledge import search_knowledge

        rows = search_knowledge(
            workspace_path=workspace.path,
            status="active",
            limit=_PREVIEW_LIMIT,
        )
        entries = [
            {
                "code": row["code"],
                "type": row["type"],
                "title": row["title"],
                "scope": row["scope"],
            }
            for row in rows
        ]
        payload["knowledge"] = {"count": len(entries), "entries": entries}
        if entries:
            payload["hints"].append(
                f"{len(entries)} active knowledge item(s) — call knowledge_search"
            )
    except Exception:
        pass

    try:
        with SessionLocal() as session:
            # Memories whose key contains the workspace slug are treated
            # as workspace-scoped. We dedupe by key (memories table is
            # append-only).
            slug = workspace.slug
            memory_rows = (
                session.query(Memory.key)
                .filter(Memory.key.contains(slug))
                .distinct()
                .limit(_PREVIEW_LIMIT)
                .all()
            )
            keys = [row[0] for row in memory_rows]
            payload["relevant_memories"] = keys
            if keys:
                payload["hints"].append(
                    f"{len(keys)} workspace memory key(s) — call retrieve_memory"
                )
    except Exception:
        pass

    try:
        from brains.control.sessions import current_machine_id
        from brains.control.tool_registry import list_registered_tools

        with SessionLocal() as session:
            agent = session.get(AgentSession, session_id)
            bound_runtime = (
                session.get(Runtime, agent.runtime_id)
                if agent is not None and agent.runtime_id is not None
                else None
            )
        local_session = agent is None or (
            bound_runtime is None
            and (agent.machine_id is None or agent.machine_id == current_machine_id())
        )
        if local_session:
            list_registered_tools(verify_now=True)
        with SessionLocal() as session:
            tool_rows = session.query(RegisteredTool).all()
            registered = len(tool_rows)
            available = sum(1 for r in tool_rows if r.is_available)
            unverified = sum(1 for r in tool_rows if r.last_verified_at is None)
            missing = registered - available - unverified
            if missing < 0:
                missing = 0
            payload["tool_status"] = {
                "registered": registered,
                "available": available,
                "missing": missing,
                "unverified": unverified,
                "session_tool": agent.tool if agent else None,
                "verification_scope": "runtime" if bound_runtime else "control_plane",
                "session_ready": (
                    bound_runtime.status == "online" and bound_runtime.health == "healthy"
                    if bound_runtime
                    else any(row.name == agent.tool and bool(row.is_available) for row in tool_rows)
                    if agent
                    else None
                ),
            }
            if registered and missing:
                payload["hints"].append(
                    f"{missing} registered tool(s) currently missing on PATH — see list_registered_tools"
                )
            if registered and unverified and not local_session:
                payload["hints"].append(
                    f"{unverified} control-plane tool(s) have no local PATH verification"
                )
    except Exception:
        pass

    try:
        with SessionLocal() as session:
            sources = session.query(Source).filter(Source.workspace_id == workspace.id).count()
            indexed = (
                session.query(Source)
                .filter(
                    Source.workspace_id == workspace.id,
                    Source.status == "active",
                )
                .count()
            )
            payload["index_status"] = {"sources": sources, "indexed": indexed}
            if sources == 0:
                payload["hints"].append(
                    "no indexed sources for this workspace — consider fetch_and_index_source"
                )
    except Exception:
        pass

    # F10 (BL-P1-08): Skills attached to this session's Persona and/or Project,
    # deduplicated with provenance. ``None`` (nothing attached, or the session
    # is not yet visible in this transaction) reports as an empty list rather
    # than raising, so a welcome failure here never blocks session start.
    try:
        from brains.control.skills import resolve_context_for_session

        skill_context = resolve_context_for_session(session_id)
        skills = skill_context["skills"] if skill_context else []
        payload["skills"] = skills
        if skills:
            payload["hints"].append(
                f"{len(skills)} Skill(s) attached to this Persona/Project — see 'skills'"
            )
    except Exception:
        payload["skills"] = []

    return payload


__all__ = ["build_welcome"]

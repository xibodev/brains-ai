"""Agent topic boards — the pub/sub half of the agent comms slice.

A *topic* is a named, install-wide, flat board. Any live session can post;
replies reference their parent post. The design deliberately avoids
free-form chat: one row per post, no typing indicators, no sockets.

Delivery follows the "agents poll only their inbox" rule (comms design,
scenario 5): posting a topic blasts exactly one mailbox notification per
*other* workspace that has live sessions. A workspace with nothing running
gets nothing to read later by design — the board itself is the archive and
``read_topic`` / ``list_topics`` are how an agent catches up.

Live means: ``ended_at IS NULL`` and fresh within ``BRAINS_TOPIC_BLAST_TTL``
seconds (default 900), where freshness is the same opportunistic heartbeat
every brain tool call stamps (``agent_sessions.last_activity_at``, falling
back to ``started_at``).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.help import normalize_required_tool
from brains.control.mailbox import send_message
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, TopicPost, Workspace

_TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

DEFAULT_BLAST_TTL_SECONDS = 900

#: Per-harness wake capability: can an orchestrator deliver a message into a
#: *running* instance of this CLI? Today no shipped agent CLI is launched
#: with an open input channel (BL-P0-05), so every entry is False — recorded
#: explicitly so an orchestrator can check before building on "arm a loop and
#: I'll message you". A session that must receive something mid-run has to
#: hold its own turn open with wait_for_request / read_messages polling.
TOOL_INTERACTIVE_INPUT: dict[str, bool] = {
    "claude": False,
    "copilot": False,
    "codex": False,
    "opencode": False,
}


def _interactive_input(tool: str | None) -> bool:
    return TOOL_INTERACTIVE_INPUT.get((tool or "").strip().lower(), False)


def _blast_ttl_seconds() -> int:
    raw = os.environ.get("BRAINS_TOPIC_BLAST_TTL")
    if not raw:
        return DEFAULT_BLAST_TTL_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_BLAST_TTL_SECONDS


def _validate_topic(topic: str) -> str:
    name = (topic or "").strip().lower()
    if not _TOPIC_PATTERN.match(name):
        raise ValueError(
            "topic must be 1-64 chars of lowercase letters, digits, dot, dash or "
            "underscore, starting alphanumeric"
        )
    return name


def _freshness_cutoff(now: datetime, ttl_seconds: int) -> datetime:
    return now - timedelta(seconds=ttl_seconds)


def live_agent_sessions(ttl_seconds: int | None = None) -> list[dict[str, Any]]:
    """Every session alive within ``ttl_seconds``, across all workspaces.

    This is scenario 1 of the comms design: an agent on any workspace can
    discover every other live agent regardless of where it runs. Freshness
    uses the opportunistic heartbeat, so no dedicated presence ping exists.
    """
    ttl = ttl_seconds if ttl_seconds is not None else _blast_ttl_seconds()
    cutoff = _freshness_cutoff(utc_now(), max(1, int(ttl)))
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(AgentSession, Workspace)
            .outerjoin(Workspace, Workspace.id == AgentSession.workspace_id)
            .filter(
                AgentSession.ended_at.is_(None),
                func.coalesce(AgentSession.last_activity_at, AgentSession.started_at) >= cutoff,
            )
            .order_by(func.coalesce(AgentSession.last_activity_at, AgentSession.started_at).desc())
            .all()
        )
        out: list[dict[str, Any]] = []
        for agent, workspace in rows:
            last = agent.last_activity_at or agent.started_at
            out.append(
                {
                    "session_id": agent.id,
                    "workspace": workspace.slug if workspace else None,
                    "tool": agent.tool,
                    "state": agent.state,
                    "started_at": agent.started_at.isoformat(),
                    "last_activity_at": last.isoformat() if last else None,
                    "interactive_input": _interactive_input(agent.tool),
                }
            )
    return out


def _live_workspace_ids(exclude_workspace_id: int | None, ttl_seconds: int) -> list[int]:
    """Distinct workspaces with at least one live session, minus the poster's."""
    cutoff = _freshness_cutoff(utc_now(), max(1, int(ttl_seconds)))
    with SessionLocal() as session:
        rows = (
            session.query(AgentSession.workspace_id)
            .filter(
                AgentSession.ended_at.is_(None),
                AgentSession.workspace_id.isnot(None),
                func.coalesce(AgentSession.last_activity_at, AgentSession.started_at) >= cutoff,
            )
            .distinct()
            .all()
        )
        ids = [row[0] for row in rows if row[0] is not None]
    if exclude_workspace_id is not None:
        ids = [wid for wid in ids if wid != exclude_workspace_id]
    return ids


def _post_to_dict(row: TopicPost, workspace_slug: str | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "topic": row.topic,
        "from_session_id": row.from_session_id,
        "from_workspace": workspace_slug,
        "reply_to_id": row.reply_to_id,
        "subject": row.subject,
        "body": row.body,
        "required_tool": row.required_tool,
        "created_at": row.created_at.isoformat(),
    }


def post_topic(
    topic: str,
    subject: str,
    body: str = "",
    *,
    from_session_id: str | None = None,
    workspace_path: str | None = None,
    required_tool: str | None = None,
    reply_to: int | None = None,
    blast: bool = True,
) -> dict[str, Any]:
    """Post to a topic board and notify other live workspaces via their inbox."""
    name = _validate_topic(topic)
    if not subject or not subject.strip():
        raise ValueError("subject is required")
    required_tool_norm = normalize_required_tool(required_tool)

    from brains.control.sessions import register_workspace

    poster_ws: Workspace | None = None
    if workspace_path:
        poster_ws = register_workspace(workspace_path)

    init_db()
    with SessionLocal() as session:
        if poster_ws is None and from_session_id:
            agent = (
                session.query(AgentSession).filter(AgentSession.id == from_session_id).one_or_none()
            )
            if agent is not None and agent.workspace_id is not None:
                poster_ws = (
                    session.query(Workspace)
                    .filter(Workspace.id == agent.workspace_id)
                    .one_or_none()
                )
        from_session_ok = None
        if from_session_id:
            exists = (
                session.query(AgentSession.id)
                .filter(AgentSession.id == from_session_id)
                .one_or_none()
            )
            from_session_ok = from_session_id if exists else None
        row = TopicPost(
            topic=name,
            from_session_id=from_session_ok,
            from_workspace_id=poster_ws.id if poster_ws else None,
            reply_to_id=reply_to,
            subject=subject.strip(),
            body=body or None,
            required_tool=required_tool_norm,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        result = _post_to_dict(row, poster_ws.slug if poster_ws else None)
        post_id = row.id
        poster_ws_id = poster_ws.id if poster_ws else None

    append_event(
        "topic_posted",
        f"[{name}] {subject.strip()}",
        workspace_id=poster_ws_id,
        session_id=from_session_ok,
        metadata={
            "topic": name,
            "post_id": post_id,
            "reply_to": reply_to,
            "required_tool": required_tool_norm,
        },
    )

    notified: list[str] = []
    if blast:
        ttl = _blast_ttl_seconds()
        for ws_id in _live_workspace_ids(poster_ws_id, ttl):
            with SessionLocal() as session:
                ws = session.query(Workspace).filter(Workspace.id == ws_id).one_or_none()
                slug = ws.slug if ws else None
                path = ws.path if ws else None
            if not path:
                continue
            requirement = f" Harness wanted: {required_tool_norm}." if required_tool_norm else ""
            send_message(
                subject=f"[topic:{name}] {subject.strip()}",
                body=(
                    f"New post #{post_id} on topic '{name}'"
                    f" from {result['from_workspace'] or 'unknown'}:"
                    f" {subject.strip()}.{requirement}"
                    f"\nRead the board: MCP `brains_topic_read(topic='{name}')`"
                    f" or CLI `brains-ai topic-read {name}`."
                ),
                workspace_path=path,
                kind="topic",
                from_session_id=from_session_ok,
            )
            notified.append(slug or str(ws_id))

    result["notified_workspaces"] = notified
    return result


def read_topic(
    topic: str | None = None,
    limit: int = 50,
    reply_to: int | None = None,
) -> list[dict[str, Any]]:
    """Read the board: newest posts first, optionally scoped to one topic."""
    init_db()
    with SessionLocal() as session:
        query = session.query(TopicPost, Workspace.slug)
        query = query.outerjoin(Workspace, Workspace.id == TopicPost.from_workspace_id)
        if topic:
            query = query.filter(TopicPost.topic == _validate_topic(topic))
        if reply_to is not None:
            query = query.filter(TopicPost.reply_to_id == reply_to)
        rows = query.order_by(TopicPost.created_at.desc(), TopicPost.id.desc()).limit(limit).all()
        return [_post_to_dict(post, slug) for post, slug in rows]


def list_topics(limit: int = 100) -> list[dict[str, Any]]:
    """Every topic with post count and latest activity."""
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(
                TopicPost.topic,
                func.count(TopicPost.id).label("posts"),
                func.max(TopicPost.created_at).label("last_post_at"),
            )
            .group_by(TopicPost.topic)
            .order_by(func.max(TopicPost.created_at).desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "topic": row.topic,
                "posts": int(row.posts),
                "last_post_at": row.last_post_at.isoformat() if row.last_post_at else None,
            }
            for row in rows
        ]


__all__ = [
    "DEFAULT_BLAST_TTL_SECONDS",
    "list_topics",
    "live_agent_sessions",
    "post_topic",
    "read_topic",
]

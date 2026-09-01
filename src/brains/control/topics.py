"""Agent topic boards — the pub/sub half of the agent comms slice.

A *topic* is a named, install-wide, flat board. Any live session can post;
replies reference their parent post. The design deliberately avoids
free-form chat: one row per post, no typing indicators, no sockets.

Delivery is interest-scoped: a notifying post creates one announcement row,
and live Sessions that explicitly subscribed wake from ``inbox_wait``. The
board itself remains the archive and ``read_topic`` advances the subscriber's
durable cursor. Posting therefore does not multiply mailbox rows by the number
of live Workspaces.

Live means: ``ended_at IS NULL`` and fresh within ``BRAINS_TOPIC_BLAST_TTL``
seconds (default 900), where freshness is the same opportunistic heartbeat
every brain tool call stamps (``agent_sessions.last_activity_at``, falling
back to ``started_at``).
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.help import normalize_required_tool
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    SessionLease,
    TopicAnnouncement,
    TopicPost,
    TopicSubscription,
    Workspace,
)

_TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

DEFAULT_BLAST_TTL_SECONDS = 900
DEFAULT_AGENT_LIVE_TTL_SECONDS = 3600

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


def _require_topic_session(session, session_id: str, *, action: str) -> AgentSession:
    from brains.control.memberships import visible_workspace_ids_for_current
    from brains.control.sessions import require_live_session

    agent = require_live_session(session, session_id, action=action)
    visible = visible_workspace_ids_for_current()
    if visible is not None and agent.workspace_id not in visible:
        raise ValueError(f"session unavailable for {action}")
    return agent


def _freshness_cutoff(now: datetime, ttl_seconds: int) -> datetime:
    return now - timedelta(seconds=ttl_seconds)


def live_agent_sessions(ttl_seconds: int | None = None) -> list[dict[str, Any]]:
    """Every session alive within ``ttl_seconds``, across all workspaces.

    This is scenario 1 of the comms design: an agent on any workspace can
    discover every other live agent regardless of where it runs. Freshness
    uses the opportunistic heartbeat, so no dedicated presence ping exists.
    """
    ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_AGENT_LIVE_TTL_SECONDS
    cutoff = _freshness_cutoff(utc_now(), max(1, int(ttl)))
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(AgentSession, Workspace)
            .outerjoin(Workspace, Workspace.id == AgentSession.workspace_id)
            .filter(
                func.coalesce(AgentSession.last_activity_at, AgentSession.started_at) >= cutoff,
            )
            .order_by(func.coalesce(AgentSession.last_activity_at, AgentSession.started_at).desc())
            .all()
        )
        out: list[dict[str, Any]] = []
        from brains.control.session_liveness import lease_is_current

        for agent, workspace in rows:
            lease = None
            last = agent.last_activity_at or agent.started_at
            if agent.ended_at is not None:
                ended = (
                    agent.ended_at if agent.ended_at.tzinfo else agent.ended_at.replace(tzinfo=UTC)
                )
                activity = last if last.tzinfo else last.replace(tzinfo=UTC)
                if activity <= ended + timedelta(seconds=1):
                    continue
            if agent.state == "dormant":
                continue
            if agent.pid is None:
                lease = session.get(SessionLease, agent.id)
                if lease is not None and not lease_is_current(lease):
                    continue
            out.append(
                {
                    "session_id": agent.id,
                    "workspace": workspace.slug if workspace else None,
                    "tool": agent.tool,
                    "state": agent.state,
                    "started_at": agent.started_at.isoformat(),
                    "last_activity_at": last.isoformat() if last else None,
                    "lease_expires_at": (
                        lease.lease_expires_at.isoformat()
                        if agent.pid is None and lease is not None
                        else None
                    ),
                    "interactive_input": _interactive_input(agent.tool),
                }
            )
    return out


def _live_subscribers(
    topic: str,
    exclude_workspace_id: int | None,
    ttl_seconds: int,
) -> list[tuple[str, str]]:
    """Live subscribed Sessions for ``topic``, excluding the poster's Workspace."""
    cutoff = _freshness_cutoff(utc_now(), max(1, int(ttl_seconds)))
    with SessionLocal() as session:
        rows = (
            session.query(AgentSession, Workspace.slug)
            .join(TopicSubscription, TopicSubscription.session_id == AgentSession.id)
            .join(Workspace, Workspace.id == AgentSession.workspace_id)
            .filter(
                AgentSession.workspace_id.isnot(None),
                TopicSubscription.topic == topic,
                func.coalesce(AgentSession.last_activity_at, AgentSession.started_at) >= cutoff,
            )
            .order_by(Workspace.slug.asc(), AgentSession.id.asc())
            .all()
        )
        subscribers: list[tuple[str, str]] = []
        from brains.control.session_liveness import lease_is_current

        for row, workspace_slug in rows:
            if row.workspace_id == exclude_workspace_id:
                continue
            last = row.last_activity_at or row.started_at
            if row.ended_at is not None:
                ended = row.ended_at if row.ended_at.tzinfo else row.ended_at.replace(tzinfo=UTC)
                activity = last if last.tzinfo else last.replace(tzinfo=UTC)
                if activity <= ended + timedelta(seconds=1):
                    continue
            if row.state == "dormant":
                continue
            if row.pid is None:
                lease = session.get(SessionLease, row.id)
                if lease is not None and not lease_is_current(lease):
                    continue
            subscribers.append((row.id, workspace_slug))
    return subscribers


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
    """Post to a board and create one announcement for interested subscribers."""
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
        from_session_ok = None
        if from_session_id:
            agent = _require_topic_session(session, from_session_id, action="post_topic")
            if poster_ws is not None and agent.workspace_id != poster_ws.id:
                raise ValueError("originating Session and topic Workspace must match")
            if poster_ws is None and agent.workspace_id is not None:
                poster_ws = (
                    session.query(Workspace)
                    .filter(Workspace.id == agent.workspace_id)
                    .one_or_none()
                )
            from_session_ok = agent.id
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
        session.flush()
        if blast:
            session.add(
                TopicAnnouncement(
                    post_id=row.id,
                    excluded_workspace_id=poster_ws.id if poster_ws else None,
                    created_at=row.created_at,
                )
            )
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

    notified_sessions: list[str] = []
    notified_workspaces: list[str] = []
    if blast:
        ttl = _blast_ttl_seconds()
        subscribers = _live_subscribers(name, poster_ws_id, ttl)
        notified_sessions = [session_id for session_id, _slug in subscribers]
        notified_workspaces = list(dict.fromkeys(slug for _session_id, slug in subscribers))

    result["notified_sessions"] = notified_sessions
    result["notified_workspaces"] = notified_workspaces
    return result


def subscribe_topic(
    topic: str,
    session_id: str,
    *,
    include_existing: bool = False,
) -> dict[str, Any]:
    """Subscribe a live Session and initialize its durable post cursor."""
    name = _validate_topic(topic)
    init_db()
    now = utc_now()
    with SessionLocal() as session:
        agent = _require_topic_session(session, session_id, action="subscribe_topic")
        existing = session.get(TopicSubscription, (session_id, name))
        latest = session.query(func.max(TopicPost.id)).filter(TopicPost.topic == name).scalar() or 0
        if existing is None:
            existing = TopicSubscription(
                session_id=session_id,
                topic=name,
                last_seen_post_id=0 if include_existing else int(latest),
                subscribed_at=now,
                updated_at=now,
            )
            session.add(existing)
        else:
            existing.updated_at = now
        session.commit()
        return {
            "session_id": agent.id,
            "topic": name,
            "last_seen_post_id": existing.last_seen_post_id,
            "subscribed_at": existing.subscribed_at.isoformat(),
            "updated_at": existing.updated_at.isoformat(),
        }


def unsubscribe_topic(topic: str, session_id: str) -> dict[str, Any]:
    """Remove one Session's topic interest."""
    name = _validate_topic(topic)
    init_db()
    with SessionLocal() as session:
        _require_topic_session(session, session_id, action="unsubscribe_topic")
        deleted = (
            session.query(TopicSubscription)
            .filter(
                TopicSubscription.session_id == session_id,
                TopicSubscription.topic == name,
            )
            .delete(synchronize_session=False)
        )
        session.commit()
    return {"session_id": session_id, "topic": name, "unsubscribed": bool(deleted)}


def list_topic_subscriptions(session_id: str) -> list[dict[str, Any]]:
    """List one live Session's subscriptions and pending announcement counts."""
    init_db()
    with SessionLocal() as session:
        agent = _require_topic_session(
            session,
            session_id,
            action="list_topic_subscriptions",
        )
        rows = (
            session.query(TopicSubscription)
            .filter(TopicSubscription.session_id == session_id)
            .order_by(TopicSubscription.topic.asc())
            .all()
        )
        result = [
            {
                "session_id": row.session_id,
                "topic": row.topic,
                "last_seen_post_id": row.last_seen_post_id,
                "pending": (
                    session.query(TopicAnnouncement)
                    .join(TopicPost, TopicPost.id == TopicAnnouncement.post_id)
                    .filter(
                        TopicPost.topic == row.topic,
                        TopicPost.id > row.last_seen_post_id,
                        (TopicAnnouncement.excluded_workspace_id.is_(None))
                        | (TopicAnnouncement.excluded_workspace_id != agent.workspace_id),
                    )
                    .count()
                ),
                "subscribed_at": row.subscribed_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in rows
        ]
        session.commit()
        return result


def pending_topic_updates(session_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Pending subscribed-topic announcements, newest first, without mutation."""
    init_db()
    with SessionLocal() as session:
        agent = session.get(AgentSession, session_id)
        if agent is None:
            return []
        from brains.control.memberships import visible_workspace_ids_for_current

        visible = visible_workspace_ids_for_current()
        if visible is not None and agent.workspace_id not in visible:
            return []
        rows = (
            session.query(TopicPost, Workspace.slug)
            .join(TopicAnnouncement, TopicAnnouncement.post_id == TopicPost.id)
            .join(
                TopicSubscription,
                (TopicSubscription.session_id == session_id)
                & (TopicSubscription.topic == TopicPost.topic)
                & (TopicPost.id > TopicSubscription.last_seen_post_id),
            )
            .outerjoin(Workspace, Workspace.id == TopicPost.from_workspace_id)
            .filter(
                (TopicAnnouncement.excluded_workspace_id.is_(None))
                | (TopicAnnouncement.excluded_workspace_id != agent.workspace_id)
            )
            .order_by(TopicPost.id.desc())
            .limit(max(1, limit))
            .all()
        )
        return [_post_to_dict(post, slug) for post, slug in rows]


def read_topic(
    topic: str | None = None,
    limit: int = 50,
    reply_to: int | None = None,
    session_id: str | None = None,
    after_post_id: int | None = None,
) -> list[dict[str, Any]]:
    """Read the board and advance a subscriber cursor when ``session_id`` is set."""
    if session_id and not topic:
        raise ValueError("a Session-scoped topic read requires one topic")
    if session_id and reply_to is not None:
        raise ValueError("a Session-scoped topic read cannot filter a thread")
    if session_id and after_post_id is not None:
        raise ValueError("a Session-scoped topic read uses its stored cursor, not after_post_id")
    init_db()
    with SessionLocal() as session:
        name = _validate_topic(topic) if topic else None
        subscription = None
        if session_id:
            _require_topic_session(session, session_id, action="read_topic")
            subscription = session.get(TopicSubscription, (session_id, name))
            if subscription is None:
                raise ValueError(f"session {session_id} is not subscribed to topic {name}")
        query = session.query(TopicPost, Workspace.slug)
        query = query.outerjoin(Workspace, Workspace.id == TopicPost.from_workspace_id)
        if name:
            query = query.filter(TopicPost.topic == name)
        if reply_to is not None:
            query = query.filter(TopicPost.reply_to_id == reply_to)
        if subscription is not None:
            query = query.filter(TopicPost.id > subscription.last_seen_post_id)
            rows = query.order_by(TopicPost.id.asc()).limit(limit).all()
        else:
            if after_post_id is not None:
                query = query.filter(TopicPost.id > max(0, after_post_id))
                rows = query.order_by(TopicPost.id.asc()).limit(limit).all()
            else:
                rows = (
                    query.order_by(TopicPost.created_at.desc(), TopicPost.id.desc())
                    .limit(limit)
                    .all()
                )
        if session_id and name and rows:
            high_water = max(post.id for post, _slug in rows)
            (
                session.query(TopicSubscription)
                .filter(
                    TopicSubscription.session_id == session_id,
                    TopicSubscription.topic == name,
                    TopicSubscription.last_seen_post_id < high_water,
                )
                .update(
                    {
                        "last_seen_post_id": high_water,
                        "updated_at": utc_now(),
                    },
                    synchronize_session=False,
                )
            )
        session.commit()
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
    "list_topic_subscriptions",
    "live_agent_sessions",
    "pending_topic_updates",
    "post_topic",
    "read_topic",
    "subscribe_topic",
    "unsubscribe_topic",
]

"""Layer 3 of the multi-operator model — cross-workspace presence.

The coordination plane gives sessions in the SAME workspace full
visibility into each other (that's the whole point). The privacy
boundary is the workspace, enforced by Layer 2's membership filter.

Layer 3 closes the corresponding awareness gap on the OTHER side of
that boundary: "I know other work is happening on this brain, even if
I can't see the details." Without it, a multi-operator install feels
like a single-operator install that has mysteriously gone quiet — a
sibling operator's private workspace is invisible by design (good),
but the *existence* of that operator is too (bad — there's nobody to
go ask about a half-mentioned reference in a shared workspace).

The single public entrypoint is :func:`list_other_operators_active`:

* Reads the current operator from the resolver.
* Aggregates ``agent_sessions`` where ``ended_at IS NULL`` by
  ``created_by_operator_id``.
* Returns the projection ``(operator_slug, display_name,
  workspace_count, active_session_count, last_activity_at)`` for every
  other operator with at least one active session.
* Deliberately omits workspace names, session ids, tools, summaries,
  and bodies — the goal per decision record 0002 is "go ask them",
  not "introspect every session running on the box".

Admin presence is included so non-admin operators can tell that the
admin operator is currently working — useful when admin is acting as
a real human collaborator on a multi-operator brain. The caller's own
operator row is always excluded.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

from sqlalchemy import func

from brains.control.operators import resolve_current_operator
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, Operator


class OperatorPresence(TypedDict):
    operator_slug: str
    display_name: str | None
    workspace_count: int
    active_session_count: int
    last_activity_at: str | None


def _coalesce_last_activity(
    last_activity_at: datetime | None, started_at: datetime | None
) -> datetime | None:
    """Prefer the heartbeat-updated ``last_activity_at`` when present.

    Older sessions and sessions whose tool never calls ``append_event``
    with a ``session_id`` keep ``last_activity_at`` NULL, so we fall
    back to ``started_at`` to ensure every active session contributes
    a freshness signal.
    """
    if last_activity_at is not None:
        return last_activity_at
    return started_at


def list_other_operators_active() -> list[OperatorPresence]:
    """Return per-operator presence for every operator OTHER than the caller.

    The aggregation runs in SQL (count + max) so it stays cheap on
    multi-operator installs with many sessions. Sessions whose
    ``created_by_operator_id`` is NULL (pre-Layer-1 rows or tests that
    bypassed the resolver) contribute to no operator and are silently
    dropped — they cannot be attributed to anyone.
    """
    init_db()
    me = resolve_current_operator()
    my_id = me.get("id")
    with _db_module.SessionLocal() as session:
        # Subquery-free aggregation: join sessions to operators and
        # group on the operator row so we get the slug + display_name
        # plus the count + max in one round-trip.
        rows = (
            session.query(
                Operator.id,
                Operator.slug,
                Operator.display_name,
                func.count(func.distinct(AgentSession.workspace_id)).label("workspace_count"),
                func.count(AgentSession.id).label("active_session_count"),
                func.max(AgentSession.last_activity_at).label("max_activity"),
                func.max(AgentSession.started_at).label("max_started"),
            )
            .join(AgentSession, AgentSession.created_by_operator_id == Operator.id)
            .filter(AgentSession.ended_at.is_(None))
            .group_by(Operator.id, Operator.slug, Operator.display_name)
            .all()
        )
    out: list[OperatorPresence] = []
    for row in rows:
        if my_id is not None and row.id == my_id:
            # Never include the caller in their own presence list.
            continue
        last = _coalesce_last_activity(row.max_activity, row.max_started)
        out.append(
            {
                "operator_slug": row.slug,
                "display_name": row.display_name,
                "workspace_count": int(row.workspace_count or 0),
                "active_session_count": int(row.active_session_count or 0),
                "last_activity_at": last.isoformat() if last is not None else None,
            }
        )
    # Sort by most-recently-active first so the dashboard panel and
    # the MCP tool both surface the freshest peer at the top.
    out.sort(
        key=lambda r: r["last_activity_at"] or "",
        reverse=True,
    )
    return out


__all__ = [
    "OperatorPresence",
    "list_other_operators_active",
]

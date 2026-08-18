"""Issue execution evidence: what an Issue actually caused (F4, BL-P1-02).

An Issue's detail view is a claim about work. This module makes that claim
reconcilable: everything it reports is read from persisted rows - Sessions,
durable events, Session commands, approval decisions, and attributed gateway
usage - and everything it cannot read is named as an explicit gap rather than
estimated.

Three properties the rollup holds
--------------------------------

**Stable identity.** Every collection is keyed by the row's own primary key
and de-duplicated by it, so an Issue whose events arrive from several writers,
or whose dispatch was retried, is counted once. Usage is attributed through
``usage_attributions``, whose ``usage_entry_id`` is unique, so one gateway call
can never be summed twice.

**No invented attribution.** A Session belongs to the Issue when
``agent_sessions.issue_id`` says so. An event belongs to the Issue when it is
bound to one of those Sessions, or when it names the Issue in its own metadata.
Gateway usage belongs to the Issue only when a caller identified its Session.
Calls that did not are reported as unattributed, with the count, instead of
being spread across Sessions by guesswork.

**Deterministic dispatch semantics.** :func:`dispatch_plan` resolves the one
Persona and Runtime a dispatch would use, from the Issue's single assignee, and
returns a stable ``blocked_reason`` when it cannot. :func:`dispatch` is
idempotent while an attempt is in flight: a retried click returns the Session
the first click created, marked ``duplicate``, instead of spawning a second.

Pure control logic - no FastAPI.
"""

from __future__ import annotations

import json
from typing import Any

from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    ApprovalRequest,
    Event,
    Issue,
    Operator,
    Persona,
    Project,
    Runtime,
    SessionCommand,
    Squad,
    UsageAttribution,
    UsageLedgerEntry,
)

#: Stable reasons an Issue cannot be dispatched. Part of the API contract.
DISPATCH_BLOCKED_REASONS = (
    "issue_closed",
    "unassigned",
    "assigned_to_operator",
    "persona_unknown",
    "persona_archived",
    "persona_no_runtime",
    "runtime_unknown",
    "runtime_other_org",
    "runtime_offline",
    "runtime_tool_mismatch",
    "pod_archived",
    "pod_empty",
    "pod_no_leader",
    "pod_no_capable_member",
)

#: A Session in one of these states is finished; it does not hold a dispatch open.
_TERMINAL_SESSION_STATES = frozenset({"completed", "failed"})

TERMINAL_ISSUE_STATUSES = frozenset({"done", "cancelled"})


class IssueEvidenceError(ValueError):
    """A refused Issue execution operation, with the operator-facing reason."""


def _issue_row(session, ref: str | int) -> Issue | None:
    if isinstance(ref, int):
        return session.get(Issue, ref)
    if isinstance(ref, str):
        if ref.isdigit():
            return session.get(Issue, int(ref))
        return session.query(Issue).filter(Issue.code == ref).one_or_none()
    return None


def _require_issue(session, ref: str | int) -> Issue:
    row = _issue_row(session, ref)
    if row is None:
        raise IssueEvidenceError(f"unknown issue: {ref!r}")
    return row


def _issue_org_id(session, issue: Issue) -> int | None:
    project = session.get(Project, issue.project_id)
    return project.org_id if project is not None else None


def _persona_runtime_plan(session, persona: Persona, org_id: int | None) -> dict[str, Any]:
    """The Runtime a Persona would run on, or the reason it cannot run now."""
    plan: dict[str, Any] = {
        "persona_id": persona.id,
        "persona_slug": persona.slug,
        "persona_name": persona.name,
        "model": persona.model,
        "tool": persona.tool,
        "runtime_id": None,
        "runtime_slug": None,
        "runtime_status": None,
        "runtime_working_root": None,
    }
    if persona.status != "active":
        return {**plan, "blocked_reason": "persona_archived"}
    if persona.default_runtime_id is None:
        return {**plan, "blocked_reason": "persona_no_runtime"}
    runtime = session.get(Runtime, persona.default_runtime_id)
    if runtime is None:
        return {**plan, "blocked_reason": "runtime_unknown"}
    plan.update(
        {
            "runtime_id": runtime.id,
            "runtime_slug": runtime.slug,
            "runtime_status": runtime.status,
            "runtime_working_root": runtime.working_root,
        }
    )
    if org_id is not None and runtime.org_id is not None and runtime.org_id != org_id:
        return {**plan, "blocked_reason": "runtime_other_org"}
    if runtime.status != "online":
        return {**plan, "blocked_reason": "runtime_offline"}
    if persona.tool and runtime.tool and persona.tool != runtime.tool:
        return {**plan, "blocked_reason": "runtime_tool_mismatch"}
    return {**plan, "tool": persona.tool or runtime.tool, "blocked_reason": None}


def _open_session_id(session, issue_id: int) -> str | None:
    """The Session already running this Issue, if one is still in flight."""
    rows = (
        session.query(AgentSession)
        .filter(AgentSession.issue_id == issue_id)
        .order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
        .all()
    )
    for row in rows:
        if row.ended_at is not None:
            continue
        if (getattr(row, "state", None) or "running") in _TERMINAL_SESSION_STATES:
            continue
        return row.id
    return None


def dispatch_plan(issue_ref: str | int) -> dict:
    """Resolve, deterministically, what dispatching this Issue would do.

    Returns the assignee, the Persona and Runtime that would run it, whether a
    dispatch is already in flight, and - when it cannot run - one stable
    ``blocked_reason`` from :data:`DISPATCH_BLOCKED_REASONS`.
    """
    from brains.control import pods as pods_ctl

    init_db()
    with SessionLocal() as session:
        issue = _require_issue(session, issue_ref)
        org_id = _issue_org_id(session, issue)
        plan: dict[str, Any] = {
            "issue_id": issue.id,
            "issue_code": issue.code,
            "issue_status": issue.status,
            "org_id": org_id,
            "assignee_kind": None,
            "assignee_id": None,
            "assignee_label": None,
            "persona_id": None,
            "runtime_id": None,
            "runtime_working_root": None,
            "tool": None,
            "pod_id": issue.assignee_pod_id,
            "in_flight_session_id": _open_session_id(session, issue.id),
            "dispatchable": False,
            "blocked_reason": None,
            "candidates": [],
        }
        if issue.status in TERMINAL_ISSUE_STATUSES:
            return {**plan, "blocked_reason": "issue_closed"}

        if issue.assignee_persona_id is not None:
            persona = session.get(Persona, issue.assignee_persona_id)
            plan.update(
                {
                    "assignee_kind": "persona",
                    "assignee_id": issue.assignee_persona_id,
                }
            )
            if persona is None:
                return {**plan, "blocked_reason": "persona_unknown"}
            resolved = _persona_runtime_plan(session, persona, org_id)
            plan.update(
                {
                    "assignee_label": persona.name,
                    "persona_id": persona.id,
                    "runtime_id": resolved["runtime_id"],
                    "runtime_working_root": resolved.get("runtime_working_root"),
                    "tool": resolved.get("tool"),
                    "candidates": [resolved],
                }
            )
            if resolved["blocked_reason"] is not None:
                return {**plan, "blocked_reason": resolved["blocked_reason"]}
            return {**plan, "dispatchable": True}

        if issue.assignee_pod_id is not None:
            squad = session.get(Squad, issue.assignee_pod_id)
            plan.update(
                {
                    "assignee_kind": "pod",
                    "assignee_id": issue.assignee_pod_id,
                    "assignee_label": squad.name if squad is not None else None,
                }
            )
            if squad is None:
                return {**plan, "blocked_reason": "pod_empty"}
            resolution = pods_ctl.resolve_dispatch(issue.assignee_pod_id)
            plan.update(
                {
                    "pod_leader_persona_id": resolution.get("leader_persona_id"),
                    "candidates": resolution.get("candidates", []),
                    "persona_id": resolution.get("persona_id"),
                    "runtime_id": resolution.get("runtime_id"),
                    "runtime_working_root": resolution.get("runtime_working_root"),
                    "tool": resolution.get("tool"),
                }
            )
            if resolution.get("blocked_reason") is not None:
                return {**plan, "blocked_reason": resolution["blocked_reason"]}
            return {**plan, "dispatchable": True}

        if issue.assignee_operator_id is not None:
            operator = session.get(Operator, issue.assignee_operator_id)
            return {
                **plan,
                "assignee_kind": "operator",
                "assignee_id": issue.assignee_operator_id,
                "assignee_label": operator.slug if operator is not None else None,
                "blocked_reason": "assigned_to_operator",
            }
        return {**plan, "blocked_reason": "unassigned"}


def dispatch(issue_ref: str | int, *, session_id: str | None = None) -> dict:
    """Dispatch an Issue to the Persona its assignment resolves to.

    Idempotent while an attempt is in flight: when a Session for this Issue is
    still open, the existing Session is returned with ``duplicate: True`` and
    no second Session, spawn order or event is produced. A refusal names one
    stable reason from :data:`DISPATCH_BLOCKED_REASONS`.
    """
    from brains.control import assignments as assignments_ctl
    from brains.control import sessions as sessions_ctl

    plan = dispatch_plan(issue_ref)
    if plan["in_flight_session_id"]:
        return {
            "status": "already_dispatched",
            "duplicate": True,
            "issue_id": plan["issue_id"],
            "issue_code": plan["issue_code"],
            "session_id": plan["in_flight_session_id"],
            "persona_id": plan["persona_id"],
            "runtime_id": plan["runtime_id"],
            "blocked_reason": None,
        }
    if not plan["dispatchable"]:
        raise IssueEvidenceError(
            f"{plan['issue_code']} cannot be dispatched: {plan['blocked_reason']}"
        )
    session_row = sessions_ctl.open_spawn_session(
        persona_id=plan["persona_id"],
        tool=plan["tool"] or "copilot",
        issue_id=plan["issue_id"],
        runtime_id=plan["runtime_id"],
        workspace_path=plan.get("runtime_working_root"),
        org_id=plan["org_id"],
    )
    result = assignments_ctl.enqueue_spawn(
        persona_id=plan["persona_id"],
        issue_id=plan["issue_id"],
        runtime_id=plan["runtime_id"],
        session_id=session_row["id"],
    )
    append_event(
        "issue_dispatched",
        f"{plan['issue_code']} dispatched to persona {plan['persona_id']}",
        session_id=session_row["id"],
        metadata={
            "issue_id": plan["issue_id"],
            "issue_code": plan["issue_code"],
            "persona_id": plan["persona_id"],
            "runtime_id": plan["runtime_id"],
            "pod_id": plan.get("pod_id"),
        },
    )
    return {
        **result,
        "status": "spawning",
        "duplicate": False,
        "issue_code": plan["issue_code"],
        "session_id": session_row["id"],
        "pod_id": plan.get("pod_id"),
        "blocked_reason": None,
    }


# --------------------------------------------------------------------------- #
# Rollup
# --------------------------------------------------------------------------- #


def _event_names_issue(row: Event, issue: Issue) -> bool:
    raw = row.metadata_json
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("issue_id") == issue.id:
        return True
    return payload.get("code") == issue.code or payload.get("issue_code") == issue.code


def rollup(issue_ref: str | int) -> dict:
    """Everything persisted that this Issue caused, reconciled and de-duplicated.

    The returned shape is stable:

    ``sessions``
        Every Session bound to the Issue, newest first, each with its own event
        and command counts and its attributed usage.
    ``events``
        Total and per-kind counts over the union of Session-bound events and
        events whose metadata names the Issue, keyed by event id so a row that
        satisfies both is counted once.
    ``commands``
        Durable Session commands for those Sessions, by status.
    ``decisions``
        Approval requests raised by those Sessions, with how many are still open.
    ``usage``
        Summed gateway usage attributed to those Sessions, plus how many
        Sessions carry no attributed call at all, so a zero is readable as
        "nothing was attributed" rather than "nothing was spent".
    """
    from brains.control import sessions as sessions_ctl

    init_db()
    with SessionLocal() as session:
        issue = _require_issue(session, issue_ref)
        issue_id = issue.id
        org_id = _issue_org_id(session, issue)
        session_rows = (
            session.query(AgentSession)
            .filter(AgentSession.issue_id == issue_id)
            .order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
            .all()
        )
        session_ids = [row.id for row in session_rows]

        events: dict[int, Event] = {}
        if session_ids:
            for event_row in session.query(Event).filter(Event.session_id.in_(session_ids)).all():
                events[event_row.id] = event_row
        for event_row in session.query(Event).filter(Event.kind.like("issue%")).all():
            if _event_names_issue(event_row, issue):
                events[event_row.id] = event_row
        for event_row in (
            session.query(Event)
            .filter(Event.kind.in_(("spawn_enqueued", "assignment_acked")))
            .all()
        ):
            if _event_names_issue(event_row, issue):
                events[event_row.id] = event_row

        by_kind: dict[str, int] = {}
        events_per_session: dict[str, int] = {}
        for event_row in events.values():
            by_kind[event_row.kind] = by_kind.get(event_row.kind, 0) + 1
            if event_row.session_id:
                events_per_session[event_row.session_id] = (
                    events_per_session.get(event_row.session_id, 0) + 1
                )

        commands: dict[int, SessionCommand] = {}
        if session_ids:
            for command_row in (
                session.query(SessionCommand)
                .filter(SessionCommand.session_id.in_(session_ids))
                .all()
            ):
                commands[command_row.id] = command_row
        commands_by_status: dict[str, int] = {}
        commands_per_session: dict[str, int] = {}
        for command_row in commands.values():
            commands_by_status[command_row.status] = (
                commands_by_status.get(command_row.status, 0) + 1
            )
            if command_row.session_id:
                commands_per_session[command_row.session_id] = (
                    commands_per_session.get(command_row.session_id, 0) + 1
                )

        decisions: list[dict] = []
        if session_ids:
            for decision_row in (
                session.query(ApprovalRequest)
                .filter(ApprovalRequest.session_id.in_(session_ids))
                .order_by(ApprovalRequest.id)
                .all()
            ):
                decisions.append(
                    {
                        "code": decision_row.code,
                        "status": decision_row.status,
                        "session_id": decision_row.session_id,
                        "title": decision_row.title,
                        "created_at": (
                            decision_row.created_at.isoformat() if decision_row.created_at else None
                        ),
                    }
                )

        usage_totals: dict[str, Any] = {
            "attributed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_actual_usd": 0.0,
            "priced_calls": 0,
            "unpriced_calls": 0,
        }
        usage_per_session: dict[str, dict[str, Any]] = {}
        if session_ids:
            pairs = (
                session.query(UsageAttribution, UsageLedgerEntry)
                .join(UsageLedgerEntry, UsageLedgerEntry.id == UsageAttribution.usage_entry_id)
                .filter(UsageAttribution.session_id.in_(session_ids))
                .all()
            )
            seen_entries: set[int] = set()
            for attribution, entry in pairs:
                if entry.id in seen_entries:
                    continue
                seen_entries.add(entry.id)
                bucket = usage_per_session.setdefault(
                    attribution.session_id or "",
                    {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_actual_usd": 0.0},
                )
                usage_totals["attributed_calls"] += 1
                usage_totals["input_tokens"] += int(entry.input_tokens or 0)
                usage_totals["output_tokens"] += int(entry.output_tokens or 0)
                bucket["calls"] += 1
                bucket["input_tokens"] += int(entry.input_tokens or 0)
                bucket["output_tokens"] += int(entry.output_tokens or 0)
                if entry.cost_actual_usd is None:
                    usage_totals["unpriced_calls"] += 1
                else:
                    usage_totals["priced_calls"] += 1
                    usage_totals["cost_actual_usd"] += float(entry.cost_actual_usd)
                    bucket["cost_actual_usd"] += float(entry.cost_actual_usd)
        usage_totals["cost_actual_usd"] = round(usage_totals["cost_actual_usd"], 6)
        usage_totals["sessions_with_usage"] = len(usage_per_session)
        usage_totals["sessions_without_usage"] = len(session_ids) - len(usage_per_session)
        usage_totals["attribution"] = (
            "gateway calls that identified their Session; calls that did not are absent"
        )

        sessions_view: list[dict] = []
        for session_row in session_rows:
            view = sessions_ctl._agent_session_to_dict(session_row)
            view["events"] = events_per_session.get(session_row.id, 0)
            view["commands"] = commands_per_session.get(session_row.id, 0)
            view["usage"] = usage_per_session.get(
                session_row.id,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_actual_usd": 0.0},
            )
            sessions_view.append(view)

        running = sum(1 for row in session_rows if row.ended_at is None)
        return {
            "issue_id": issue_id,
            "issue_code": issue.code,
            "org_id": org_id,
            "assignment": dispatch_plan(issue_id),
            "sessions": sessions_view,
            "totals": {
                "sessions": len(session_rows),
                "running_sessions": running,
                "ended_sessions": len(session_rows) - running,
                "events": len(events),
                "commands": len(commands),
                "decisions": len(decisions),
                "open_decisions": sum(1 for row in decisions if row["status"] == "open"),
            },
            "events": {"total": len(events), "by_kind": dict(sorted(by_kind.items()))},
            "commands": {
                "total": len(commands),
                "by_status": dict(sorted(commands_by_status.items())),
            },
            "decisions": decisions,
            "usage": usage_totals,
            "links": {
                "sessions": session_ids,
                "comments": f"/v1/issues/{issue.code}/comments",
                "session_list": f"/v1/issues/{issue.code}/sessions",
            },
        }

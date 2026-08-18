"""Assignments: the runtime spawn queue derived from Issues.

An *assignment* is a request to spawn a session for one ``issues`` row, bound to
a specific runtime. It is **not** a new table; as documented in
``docs/ARCHITECTURE.md``, it is *derived* from existing data —

> an open ``issues`` row whose resolved assignee maps to this runtime: a persona
> whose ``default_runtime_id`` is this runtime, or a pod whose leader operator is
> bound to such a persona.

The assignee binding *is* the queued daemon-pull request. Claiming is an
**atomic compare-and-set** on
``issues.status`` (``open`` → ``in_progress``) — the multi-machine analogue of
``control.claims.claim_workspace`` — so two daemons racing the same assignment
resolve to exactly one winner (SQLite serialises the conditional UPDATE).

Stable assignment handle: ``as_<issue_id>`` (one in-flight attempt per Issue).

Pure control logic — no FastAPI.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    Issue,
    Persona,
    Runtime,
    Squad,
    Workspace,
)

ASSIGNMENT_PREFIX = "as_"
DEFAULT_MAX_RUNTIME_S = 5400


def assignment_id_for_issue(issue_id: int) -> str:
    return f"{ASSIGNMENT_PREFIX}{issue_id}"


def issue_id_from_assignment(assignment_id: str) -> int:
    raw = assignment_id
    if raw.startswith(ASSIGNMENT_PREFIX):
        raw = raw[len(ASSIGNMENT_PREFIX) :]
    if not raw.isdigit():
        raise ValueError(f"malformed assignment id: {assignment_id!r}")
    return int(raw)


def _persona_for_runtime(session, runtime_id: int, persona_id: int | None) -> Persona | None:
    """Return the persona that binds ``persona_id`` (or a pod leader) to this runtime."""
    if persona_id is not None:
        persona = session.get(Persona, persona_id)
        if persona is not None and persona.default_runtime_id == runtime_id:
            return persona
    return None


def _pod_persona_for_runtime(session, runtime_id: int, pod_id: int) -> Persona | None:
    """pod -> a member Persona bound to this Runtime (F5, BL-P1-03).

    The Pod's roster is Personas, and the order is the documented dispatch
    order: the leader first, then members by Persona id. The first member
    bound to *this* Runtime is the one this Runtime may claim, so two Runtimes
    polling the same Pod never resolve to the same member by accident.
    """
    from brains.control import pods as pods_ctl

    squad = session.get(Squad, pod_id)
    if squad is None or squad.status != "active":
        return None
    for persona_id in pods_ctl.member_persona_ids(pod_id):
        persona = session.get(Persona, persona_id)
        if (
            persona is not None
            and persona.status == "active"
            and persona.default_runtime_id == runtime_id
        ):
            return persona
    return None


def _resolve_persona(session, runtime_id: int, issue: Issue) -> Persona | None:
    persona = _persona_for_runtime(session, runtime_id, issue.assignee_persona_id)
    if persona is not None:
        return persona
    if issue.assignee_pod_id is not None:
        return _pod_persona_for_runtime(session, runtime_id, issue.assignee_pod_id)
    return None


def _workspace_path(session, issue: Issue, runtime: Runtime) -> str | None:
    if issue.workspace_id is not None:
        ws = session.get(Workspace, issue.workspace_id)
        if ws is not None:
            return ws.path
    return runtime.working_root


def _render_prompt(issue: Issue) -> str:
    body = (issue.body or "").strip()
    if body:
        return f"{issue.title}\n\n{body}"
    return issue.title


def _assignment_dict(session, runtime: Runtime, issue: Issue, persona: Persona) -> dict:
    return {
        "assignment_id": assignment_id_for_issue(issue.id),
        "issue_id": issue.id,
        "issue_code": issue.code,
        "persona_id": persona.id,
        "tool": runtime.tool,
        "model": persona.model,
        "workspace_path": _workspace_path(session, issue, runtime),
        "prompt": _render_prompt(issue),
        "orient_query": issue.title,
        "max_runtime_s": DEFAULT_MAX_RUNTIME_S,
    }


def list_assignments_for_runtime(runtime_ref: str | int) -> list[dict]:
    """Return pending spawn requests (open, resolved-to-this-runtime issues)."""
    init_db()
    with SessionLocal() as session:
        runtime = _get_runtime(session, runtime_ref)
        if runtime is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        # A draining/offline runtime accepts no new work.
        if runtime.status != "online":
            return []
        candidates = (
            session.query(Issue)
            .filter(Issue.status == "open")
            .filter((Issue.assignee_persona_id.isnot(None)) | (Issue.assignee_pod_id.isnot(None)))
            .order_by(Issue.priority, Issue.id)
            .all()
        )
        out: list[dict] = []
        for issue in candidates:
            persona = _resolve_persona(session, runtime.id, issue)
            if persona is None:
                continue
            out.append(_assignment_dict(session, runtime, issue, persona))
        return out


def claim_assignment(runtime_ref: str | int, assignment_id: str) -> dict:
    """Atomically claim an assignment for this runtime.

    Returns ``{claimed: True, issue_id, persona_id, session_token}`` to the winner
    and ``{claimed: False}`` to anyone who races and loses (the issue already left
    ``open``). The compare-and-set is a single conditional UPDATE — only one caller
    can flip ``open`` → ``in_progress``.
    """
    init_db()
    issue_id = issue_id_from_assignment(assignment_id)
    now = utc_now()
    with SessionLocal() as session:
        runtime = _get_runtime(session, runtime_ref)
        if runtime is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        issue = session.get(Issue, issue_id)
        if issue is None:
            raise ValueError(f"unknown issue for assignment: {assignment_id!r}")
        persona = _resolve_persona(session, runtime.id, issue)
        if persona is None:
            # Not (or no longer) addressed to this runtime.
            return {"claimed": False, "reason": "not_assigned_to_runtime"}
        # Atomic CAS: only succeed if the issue is still open.
        updated = (
            session.query(Issue)
            .filter(Issue.id == issue_id, Issue.status == "open")
            .update(
                {"status": "in_progress", "updated_at": now},
                synchronize_session=False,
            )
        )
        session.commit()
        if not updated:
            return {"claimed": False, "reason": "already_claimed"}
        result = {
            "claimed": True,
            "assignment_id": assignment_id,
            "issue_id": issue_id,
            "persona_id": persona.id,
            "tool": runtime.tool,
            "session_token": uuid.uuid4().hex,
        }
    append_event(
        "assignment_claimed",
        f"{assignment_id} claimed by runtime {runtime_ref}",
        metadata={"assignment_id": assignment_id, "issue_id": issue_id},
    )
    return result


_ACK_STATES = {"started", "finished", "aborted"}
# state → issue status to apply on ack (None = leave as-is).
_ACK_TO_STATUS = {
    "started": "in_progress",
    "finished": "in_review",
    "aborted": "open",
}


def ack_assignment(
    runtime_ref: str | int,
    assignment_id: str,
    state: str,
    *,
    session_id: str | None = None,
    returncode: int | None = None,
) -> dict:
    """Report ``started`` / ``finished`` / ``aborted`` for a claimed assignment.

    ``aborted`` returns the issue to ``open`` so another runtime may re-claim it.
    """
    if state not in _ACK_STATES:
        raise ValueError(f"state must be one of {sorted(_ACK_STATES)}")
    init_db()
    issue_id = issue_id_from_assignment(assignment_id)
    now = utc_now()
    with SessionLocal() as session:
        runtime = _get_runtime(session, runtime_ref)
        if runtime is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        issue = session.get(Issue, issue_id)
        if issue is None:
            raise ValueError(f"unknown issue for assignment: {assignment_id!r}")
        new_status = _ACK_TO_STATUS[state]
        issue.status = new_status
        issue.updated_at = now
        if new_status in {"done", "cancelled"}:
            issue.closed_at = now
        else:
            issue.closed_at = None
        session.commit()
        result = {
            "assignment_id": assignment_id,
            "issue_id": issue_id,
            "state": state,
            "issue_status": new_status,
            "returncode": returncode,
        }
    append_event(
        "assignment_acked",
        f"{assignment_id} -> {state} (issue {new_status})",
        session_id=session_id,
        metadata={
            "assignment_id": assignment_id,
            "issue_id": issue_id,
            "state": state,
            "returncode": returncode,
        },
    )
    return result


def open_session(
    runtime_ref: str | int,
    *,
    persona_id: int | None = None,
    issue_id: int | None = None,
    workspace_path: str | None = None,
    tool: str | None = None,
    session_id: str | None = None,
    pid: int | None = None,
) -> dict:
    """Open (or patch) an ``agent_sessions`` row stamped with the WS2 link
    columns ``{runtime_id, persona_id, issue_id}`` (WS1 §5.4).

    If ``session_id`` names an existing row (e.g. one already created by
    ``exec.runner``), that row is patched in place; otherwise a fresh row is
    created. The hub owns these writes so a *remote* daemon never needs direct
    DB access — it just posts the three FKs.
    """
    from brains.control.sessions import register_workspace

    init_db()
    with SessionLocal() as session:
        runtime = _get_runtime(session, runtime_ref)
        if runtime is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        row: AgentSession | None = None
        if session_id is not None:
            row = session.get(AgentSession, session_id)
        if row is None:
            # Resolve a workspace for the FK (path → workspace_id).
            path = workspace_path or runtime.working_root
            if path is None:
                raise ValueError("workspace_path is required to open a session")
            workspace = register_workspace(path)
            new_id = session_id or f"ses_{uuid.uuid4().hex[:12]}"
            row = AgentSession(
                id=new_id,
                workspace_id=workspace.id,
                tool=tool or runtime.tool,
                pid=pid,
                machine_id=runtime.machine_id,
            )
            session.add(row)
        # Stamp / refresh the three WS2 link columns.
        row.runtime_id = runtime.id
        if runtime.machine_id:
            # A row opened by the hub (a console spawn) carries the hub's
            # machine until the Runtime that will actually run the agent opens
            # it. Re-stamping here is what keeps the machine a fact about the
            # process rather than about whoever created the row.
            row.machine_id = runtime.machine_id
        if persona_id is not None:
            row.persona_id = persona_id
        if issue_id is not None:
            row.issue_id = issue_id
        # The daemon has claimed + opened the session → it is now running
        # (F3.2), unless it has already reached a terminal/blocked state.
        if row.ended_at is None and getattr(row, "state", None) in (None, "spawning"):
            row.state = "running"
        session.commit()
        session.refresh(row)
        result: dict[str, Any] = {
            "session_id": row.id,
            "runtime_id": row.runtime_id,
            "persona_id": row.persona_id,
            "issue_id": row.issue_id,
            "workspace_id": row.workspace_id,
        }
    append_event(
        "runtime_session_opened",
        f"session {result['session_id']} on runtime {result['runtime_id']}",
        session_id=result["session_id"],
        metadata={
            "runtime_id": result["runtime_id"],
            "persona_id": result["persona_id"],
            "issue_id": result["issue_id"],
        },
    )
    return result


def record_session_event(
    runtime_ref: str | int,
    session_id: str,
    *,
    seq: int,
    stream: str,
    chunk: str,
    ts: str | None = None,
    exec_id: str | None = None,
) -> dict:
    """Durably ingest one streamed stdout/lifecycle event as an ``events`` row so
    the UI can backfill a remote session's output (WS1 §5.3)."""
    init_db()
    with SessionLocal() as session:
        runtime = _get_runtime(session, runtime_ref)
        if runtime is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        # Only attach the FK if the session row actually exists (events.session_id
        # is a FK); otherwise keep the linkage in metadata so ingest never fails.
        linked = session.get(AgentSession, session_id) is not None
    append_event(
        "session_event",
        chunk,
        session_id=session_id if linked else None,
        metadata={
            "runtime_id": runtime.id,
            "session_id": session_id,
            "seq": seq,
            "stream": stream,
            "ts": ts,
            "exec_id": exec_id,
        },
    )
    # Fan the chunk out on the per-session topic so the console transcript streams
    # live (F3.1) — the SessionDetail view subscribes to ``session/{id}/stdout``.
    # Transcript chunks are notification-only: the durable copy is the
    # ``events`` row written above, which the console backfills over REST, so
    # they are deliberately not duplicated into the realtime replay log. The
    # envelope still carries the resolved Org/Workspace so delivery is filtered
    # on the event's own scope rather than on its topic string alone.
    try:
        from brains.authz import policy
        from brains.events import topics as topic_grammar
        from brains.events.bus import publish

        workspace_id = policy.session_workspace_id(session_id)
        publish(
            topic_grammar.session_topic(session_id, "stdout"),
            "session.event",
            entity="agent_session",
            id=session_id,
            org_id=policy.workspace_org_id(workspace_id),
            workspace_id=workspace_id,
            payload={
                "session_id": session_id,
                "seq": seq,
                "stream": stream,
                "chunk": chunk,
                "ts": ts,
            },
        )
    except Exception:
        pass
    return {"recorded": True, "seq": seq, "session_id": session_id}


def _get_runtime(session, ref: str | int) -> Runtime | None:
    if isinstance(ref, int):
        return session.get(Runtime, ref)
    if isinstance(ref, str) and ref.isdigit():
        return session.get(Runtime, int(ref))
    return session.query(Runtime).filter(Runtime.slug == ref).one_or_none()


def enqueue_spawn(
    *,
    issue_id: int | None = None,
    persona_id: int | None = None,
    runtime_id: str | int | None = None,
    prompt: str | None = None,
    operator: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Queue a spawn order (queued + daemon-pull, DESIGN-SYNTHESIS fork WS3-4).

    An *assignment* is derived from an open ``issues`` row whose assignee binds a
    persona to a runtime. This binds those together so the daemon's
    ``list_assignments_for_runtime`` poll surfaces the work — it does **not**
    launch a process here and does **not** bypass the gate (outward actions inside
    the spawned session are still intercepted by ``exec.gate``).

    Returns ``{status, assignment_id, issue_id, persona_id, runtime_id}`` with
    ``status='spawning'``. Binds ``personas.operator_id`` on first spawn
    (WS2 fork #1).
    """
    from brains.control import issues as issues_ctl
    from brains.control import personas as personas_ctl
    from brains.control.operators import resolve_current_operator

    init_db()
    with SessionLocal() as session:
        issue: Issue | None = None
        if issue_id is not None:
            issue = session.get(Issue, issue_id)
            if issue is None:
                raise ValueError(f"unknown issue id: {issue_id!r}")
        # Resolve the persona: explicit, else the issue's current assignee.
        resolved_persona_id = persona_id
        if resolved_persona_id is None and issue is not None:
            resolved_persona_id = issue.assignee_persona_id
        if resolved_persona_id is None:
            raise ValueError("a persona_id (or an issue with a persona assignee) is required")
        persona = session.get(Persona, resolved_persona_id)
        if persona is None:
            raise ValueError(f"unknown persona id: {resolved_persona_id!r}")
        # Resolve the runtime: explicit, else the persona's default.
        runtime = None
        if runtime_id is not None:
            runtime = _get_runtime(session, runtime_id)
            if runtime is None:
                raise ValueError(f"unknown runtime: {runtime_id!r}")
        elif persona.default_runtime_id is not None:
            runtime = session.get(Runtime, persona.default_runtime_id)
        if runtime is None:
            raise ValueError("a runtime_id (or a persona with default_runtime_id) is required")
        resolved_runtime_id = runtime.id
        resolved_issue_id = issue.id if issue is not None else None
        issue_assignee_persona_id = issue.assignee_persona_id if issue is not None else None
        issue_status = issue.status if issue is not None else None
        persona_default_runtime_id = persona.default_runtime_id
        persona_operator_id = persona.operator_id

    # Ensure the persona points at the chosen runtime so the assignment resolves.
    if persona_default_runtime_id != resolved_runtime_id:
        personas_ctl.update(
            resolved_persona_id, default_runtime_id=resolved_runtime_id, session_id=session_id
        )
    # Bind the persona's operator principal on first spawn (WS2 fork #1).
    if persona_operator_id is None:
        op = resolve_current_operator(operator=operator)
        with contextlib.suppress(ValueError):
            personas_ctl.bind_operator(resolved_persona_id, op["slug"], session_id=session_id)

    if resolved_issue_id is not None:
        # Point the issue at the persona and (re)open it so the daemon poll
        # surfaces it as a pending assignment. Both writes are conditional: an
        # issue already assigned to this persona, or already open, must not
        # emit a second ``issue_assigned``/``issue_transitioned`` event, or an
        # Issue rollup would count one dispatch as two assignments.
        if issue_assignee_persona_id != resolved_persona_id:
            issues_ctl.assign(
                resolved_issue_id, persona_id=resolved_persona_id, session_id=session_id
            )
        if issue_status != "open":
            issues_ctl.transition(resolved_issue_id, "open", session_id=session_id)
        assignment_id = assignment_id_for_issue(resolved_issue_id)
    else:
        assignment_id = None

    result = {
        "status": "spawning",
        "assignment_id": assignment_id,
        "issue_id": resolved_issue_id,
        "persona_id": resolved_persona_id,
        "runtime_id": resolved_runtime_id,
        "prompt": prompt,
    }
    append_event(
        "spawn_enqueued",
        f"spawn queued: persona {resolved_persona_id} on runtime {resolved_runtime_id}"
        + (f" for issue {resolved_issue_id}" if resolved_issue_id else ""),
        session_id=session_id,
        metadata={
            "persona_id": resolved_persona_id,
            "runtime_id": resolved_runtime_id,
            "issue_id": resolved_issue_id,
            "assignment_id": assignment_id,
        },
    )
    return result

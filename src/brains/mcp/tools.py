from typing import Any

from brains.config import settings
from brains.context.code_graph import (
    build_code_graph,
    graph_neighbors,
    graph_path,
    graph_query,
    list_subsystems,
)
from brains.context.docs_indexer import index_docs
from brains.context.freshness import check_source
from brains.context.graph_viz import graph_export as _graph_export
from brains.context.pack_builder import build_context_pack
from brains.context.planner import plan
from brains.context.repo_indexer import search_repo
from brains.context.semantic import ORIENT_DOC_EXCLUDES, semantic_search_with_status
from brains.control.adoption import adoption_report
from brains.control.claims import (
    claim_workspace,
    list_workspace_claims,
    release_workspace,
)
from brains.control.decisions import (
    escalate_decision,
    file_decision_request,
    list_open_decisions,
    resolve_decision,
    route_decision,
)
from brains.control.events import append_event
from brains.control.handoffs import (
    clear_handoff,
    list_handoffs,
    pick_handoff,
    set_handoff,
)
from brains.control.help import (
    DEFAULT_TIMEOUT_MS as HELP_DEFAULT_TIMEOUT_MS,
)
from brains.control.help import (
    answer_request,
    ask_peer,
    cancel_help_request,
    file_help_request,
    get_help_request,
    list_open_help_requests,
    release_help_request,
    wait_for_request,
    wait_help_request,
)
from brains.control.knowledge import (
    add_knowledge_entry,
    resolve_knowledge_entry,
    search_knowledge,
)
from brains.control.learn import propose_from_history
from brains.control.mailbox import inbox_wait, read_messages, send_message  # noqa: I001
from brains.control.patterns import (
    approve_pattern,
    list_patterns,
    propose_pattern,
    use_pattern,
)
from brains.control.presence import list_other_operators_active
from brains.control.recurring import (
    create_recurring_task,
    fire_recurring_task,
    list_recurring_runs,
    list_recurring_tasks,
    set_recurring_enabled,
)
from brains.control.resume import (
    checkpoint as control_checkpoint,
)
from brains.control.resume import (
    find_brain_sessions,
    latest_checkpoint,
    link_tool_session,
    list_checkpoints,
    list_tool_session_links,
    resume_brain_session,
)
from brains.control.retrieve import retrieve_original
from brains.control.sessions import end_session, heartbeat_session, start_session
from brains.control.signals import list_signals
from brains.control.snapshots import capture_snapshot, latest_snapshot
from brains.control.state import get_state
from brains.control.tasks import (
    claim_task,
    complete_task,
    create_task,
    handoff_task,
    list_tasks,
    release_task,
)
from brains.control.tool_registry import (
    list_registered_tools,
    register_tool,
    verify_tool,
)
from brains.control.topics import (
    list_topic_subscriptions,
    list_topics,
    live_agent_sessions,
    post_topic,
    read_topic,
    subscribe_topic,
    unsubscribe_topic,
)
from brains.control.views import refresh_views
from brains.control.webhooks import (
    create_webhook_trigger,
    list_webhook_triggers,
    set_webhook_enabled,
)
from brains.router.classifier import classify
from brains.router.model_router import select_model
from brains.storage.repositories import retrieve_memory, store_memory


def plan_request(prompt: str, workspace_path: str | None = None):
    c = classify([{"role": "user", "content": prompt}])
    return {"classification": c.model_dump(), "plan": plan(c, workspace_path=workspace_path)}


def ask_human(
    prompt: str,
    options: list[str] | None = None,
    urgency: str = "normal",
    workspace_path: str | None = None,
    timeout_seconds: int = 20,
    wait_ticket: str | None = None,
):
    """Ask the operator a question and BLOCK until they answer (or timeout).

    The human-in-the-loop primitive for an agent that needs a decision,
    clarification, or approval. Files the request, fans it out to the operator's
    messaging bridges (WhatsApp/Telegram/Slack) + the dashboard, and polls until
    the operator answers from ANY channel — then returns their answer.

    Prefer this over guessing or asking in the terminal: it reaches the human
    wherever they are (e.g. on their phone) and is the only human-loop that works
    for a headless/remote session.

    Args:
        prompt: the question to ask.
        options: optional list of choices; the operator can reply with the number.
        urgency: ``normal`` | ``high`` (informational; may affect routing).
        workspace_path: repo the question relates to (defaults to CWD).
        timeout_seconds: block up to this long; if unanswered, returns
            ``status="pending"`` with a ``ticket`` — call again with
            ``wait_ticket=<ticket>`` to keep waiting.
        wait_ticket: continue waiting on an already-filed request.

    Returns ``{status, code, short_id, answer?}`` where status is
    ``resolved`` | ``rejected`` | ``pending``.
    """
    import os
    import time

    from brains.control.decisions import (
        file_decision_request,
        get_decision,
        list_open_requests,
    )
    from brains.exec.relay import _active_bridge_senders, code_to_short

    if wait_ticket:
        code = wait_ticket
    else:
        title = prompt[:120]
        # Idempotency: if an identical question is already open, reuse it rather
        # than filing a duplicate. Agents often re-call with the prompt (instead
        # of the ticket) while waiting — without this they'd spam duplicate asks.
        existing = next((o["code"] for o in list_open_requests() if o["title"] == title), None)
        if existing:
            code = existing
        else:
            body = prompt
            if options:
                body += "\n\nOptions:\n" + "\n".join(
                    f"  {i + 1}. {o}" for i, o in enumerate(options)
                )
            filed = file_decision_request(
                workspace_path=workspace_path or os.getcwd(),
                title=title,
                body=body,
                metadata={"urgency": urgency, "kind": "ask_human"},
            )
            code = filed["code"]
            short = code_to_short(code)
            lines = [f"\U0001f916 brains ask ({short})", prompt]
            if options:
                lines.append("")
                lines += [f"  {i + 1}. {o}" for i, o in enumerate(options)]
                lines.append("")
                lines.append(
                    f"reply '{short} 1' (the number), or just '1' if it's your only open ask"
                )
            else:
                lines.append(f"reply '{short} <your answer>'")
            msg = "\n".join(lines)
            for send in _active_bridge_senders():
                try:
                    send(msg)
                except Exception:
                    continue

    short = code_to_short(code)
    deadline = time.time() + max(1, int(timeout_seconds))
    while time.time() < deadline:
        st = get_decision(code)
        if st is None:
            return {"status": "error", "code": code, "error": "unknown ask"}
        if st["status"] != "open":
            return {
                "status": st["status"],
                "code": code,
                "short_id": short,
                "answer": st.get("chosen"),
            }
        time.sleep(2)
    return {
        "status": "pending",
        "code": code,
        "short_id": short,
        "ticket": code,
        "hint": (
            f"The human has NOT answered yet. Do not guess. Call ask_human AGAIN "
            f"with wait_ticket='{code}' (same ticket) to keep waiting, and repeat "
            f"until status is 'resolved'."
        ),
    }


def get_context_pack(prompt: str, repo_path: str = "."):
    return build_context_pack(prompt, repo_path=repo_path, limit=10)


def search_repo_tool(
    query: str | None = None,
    repo_path: str = ".",
    q: str | None = None,
    path: str | None = None,
    limit: int = 10,
):
    needle = query or q
    if not needle:
        raise ValueError("query is required")
    target_path = path or repo_path
    return {
        "query": needle,
        "repo_path": target_path,
        "results": search_repo(target_path, needle)[:limit],
    }


def search_semantic_tool(
    query: str | None = None,
    repo_path: str = ".",
    q: str | None = None,
    path: str | None = None,
    limit: int = 10,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
):
    """Embedding (cosine) repo search.

    Requires a prior ``embed-repo`` and a configured embedding model; returns
    an empty result set if nothing has been embedded yet (never raises for
    that case — callers can fall back to ``search_repo``).

    ``include`` / ``exclude`` filter by path substring/glob — e.g.
    ``exclude=["/docs/", "/tests/", "test_"]`` to surface implementation rather
    than prose docs or test files.
    """
    needle = query or q
    if not needle:
        raise ValueError("query is required")
    target_path = path or repo_path
    status = semantic_search_with_status(
        target_path, needle, limit=limit, include=include, exclude=exclude
    )
    out = {
        "query": needle,
        "repo_path": target_path,
        "results": status["results"],
        "status": status["status"],
        "indexed_workspaces": status.get("indexed_workspaces", []),
    }
    if status.get("hint"):
        out["hint"] = status["hint"]
    return out


def orient_tool(
    query: str,
    workspace_path: str = ".",
    limit: int = 5,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    include_docs: bool = False,
):
    """One-shot orientation: a *thin batching* call that returns code semantic-search
    hits (with snippets, inline) AND relevant knowledge in a SINGLE round-trip — so an
    agent can orient without the multi-turn dance of separate search / knowledge / read
    calls. This is the v1.0 "MCP as a thin batching caller over the CLI" primitive.

    By default narrative docs (``/docs/``, ``*.txt``, ``*.rst`` …) are excluded so
    real source isn't buried under prose for conceptual queries; pass
    ``include_docs=True`` to keep them, or supply your own ``include``/``exclude``.
    """
    if not query:
        raise ValueError("query is required")
    eff_exclude = list(exclude) if exclude else []
    if not include_docs:
        eff_exclude = list({*eff_exclude, *ORIENT_DOC_EXCLUDES})
    status = semantic_search_with_status(
        workspace_path, query, limit=limit, include=include, exclude=eff_exclude or None
    )
    try:
        knowledge = search_knowledge(query=query, workspace_path=workspace_path, limit=5)
    except Exception:
        knowledge = []
    out = {"query": query, "semantic": status["results"], "knowledge": knowledge}
    # Surface a retrieval miss so the agent doesn't silently fall through to grep
    # while believing brains had nothing — the wasted-turn footgun.
    if not status["results"] and status.get("hint"):
        out["semantic_status"] = status["status"]
        out["hint"] = status["hint"]
    return out


def graph_build(workspace_path: str, max_files: int = 2000):
    return build_code_graph(workspace_path, max_files=max_files)


def graph_query_tool(
    workspace_path: str,
    question: str,
    depth: int = 2,
    token_budget: int = 2000,
):
    return graph_query(workspace_path, question, depth=depth, token_budget=token_budget)


def graph_neighbors_tool(
    workspace_path: str,
    node_query: str,
    relation: str | None = None,
    limit: int = 50,
):
    return graph_neighbors(workspace_path, node_query, relation=relation, limit=limit)


def graph_path_tool(
    workspace_path: str,
    src_query: str,
    dst_query: str,
    max_depth: int = 6,
):
    return graph_path(workspace_path, src_query, dst_query, max_depth=max_depth)


def graph_subsystems(workspace_path: str):
    return list_subsystems(workspace_path)


def graph_export(workspace_path: str, out_path: str):
    return _graph_export(workspace_path, out_path)


def check_freshness(source: str):
    return check_source(source)


def fetch_and_index_source(path: str):
    return index_docs(path)


def retrieve_memory_tool(key: str, session_id: str | None = None):
    return retrieve_memory(key, session_id=session_id)


def store_memory_tool(key: str, value: str):
    store_memory(key, value)
    return {"ok": True}


def knowledge_add_tool(
    workspace_path: str,
    type: str,
    title: str,
    body: str = "",
    scope: str = "workspace",
    tags: str = "",
    confidence: str = "medium",
    severity: str = "info",
    evidence: str = "",
    supersedes_code: str | None = None,
    provenance: str = "inferred",
    importance: float = 0.5,
    valid_until: str | None = None,
    promoted_from: str | None = None,
    session_id: str | None = None,
):
    return add_knowledge_entry(
        workspace_path,
        type,
        title,
        body=body,
        scope=scope,
        tags=tags,
        confidence=confidence,
        severity=severity,
        evidence=evidence,
        supersedes_code=supersedes_code,
        provenance=provenance,
        importance=importance,
        valid_until=valid_until,
        promoted_from=promoted_from,
        session_id=session_id,
    )


def knowledge_resolve_tool(code: str, status: str = "resolved"):
    return resolve_knowledge_entry(code, status=status)


def knowledge_search_tool(
    query: str | None = None,
    type: str | None = None,
    status: str | None = None,
    workspace_path: str | None = None,
    tags: str | None = None,
    limit: int = 50,
):
    return search_knowledge(
        query=query,
        type=type,
        status=status,
        workspace_path=workspace_path,
        tags=tags,
        limit=limit,
    )


def retrieve_original_tool(ref: str):
    return retrieve_original(ref)


def learn_propose_tool(workspace_path: str | None = None, limit: int = 20):
    return propose_from_history(workspace_path=workspace_path, apply=False, limit=limit)


def list_signals_tool(workspace_path: str | None = None, limit: int = 50):
    return list_signals(workspace_path=workspace_path, limit=limit)


def explain_route(prompt: str, workspace_path: str | None = None):
    c = classify([{"role": "user", "content": prompt}])
    # ``explain_route`` exists to answer "what would brains pick for
    # this prompt?" — that question is only meaningful via the
    # classifier-driven ``brains/auto`` path, so we ask for it
    # explicitly. (With the new resolver, calling select_model with no
    # requested_model would raise ModelNotFoundError because we don't
    # silently classify on the client's behalf.)
    route = select_model(c, requested_model="brains/auto")
    p = plan(c, messages=[{"role": "user", "content": prompt}], workspace_path=workspace_path)
    policy = {
        "require_approval_for_deep": settings.control.require_approval_for_deep,
        "require_approval_for_external_docs": settings.control.require_approval_for_external_docs,
        "require_approval_for_large_scans": settings.control.require_approval_for_large_scans,
    }
    return {
        "classification": c.model_dump(),
        "task_type": c.task_type,
        "provider": route.get("provider_name")
        or route["provider"].__class__.__name__.replace("Provider", "").lower(),
        "model": route["model"],
        "model_tier": route["tier"],
        "strategy": p["strategy"],
        "policy": policy,
        "required_decisions": p["required_decisions"],
    }


def get_state_tool(
    workspace_path: str | None = None,
    session_id: str | None = None,
    limit: int = 50,
):
    return get_state(workspace_path=workspace_path, session_id=session_id, limit=limit)


def start_session_tool(
    workspace_path: str = ".",
    tool: str = "codex",
    predecessor_session_id: str | None = None,
):
    return start_session(
        workspace_path,
        tool=tool,
        predecessor_session_id=predecessor_session_id,
        reuse_existing=True,
        auto_link_predecessor=True,
    )


def heartbeat_session_tool(session_id: str):
    """Renew a PID-less coordination Session lease without journal noise."""
    return heartbeat_session(session_id)


def link_session_successor_tool(from_session_id: str, to_session_id: str):
    """Link one ended/replaced handle to its explicit same-workspace successor."""
    from brains.control.sessions import link_session_successor

    return link_session_successor(from_session_id, to_session_id)


def end_session_tool(session_id: str, summary: str = ""):
    return end_session(session_id, summary=summary)


def session_message_tool(session_id: str, text: str, operation_id: str | None = None):
    """Queue a durable message for a running Session (BL-P0-05).

    Recorded before delivery, idempotent per ``operation_id``, and settled
    with the outcome a consumer observed - never with an optimistic "sent".
    """
    from brains.control import session_commands as commands_ctl

    command, created = commands_ctl.enqueue(
        session_id,
        commands_ctl.KIND_MESSAGE,
        text=text,
        operation_id=operation_id,
        requested_by="mcp",
    )
    return {**command, "duplicate": not created}


def session_stop_tool(session_id: str, reason: str = "", operation_id: str | None = None):
    """Request that a Session's agent process be stopped (idempotent)."""
    from brains.control import session_commands as commands_ctl
    from brains.exec import session_dispatch

    command, created = commands_ctl.enqueue(
        session_id,
        commands_ctl.KIND_STOP,
        reason=reason or None,
        operation_id=operation_id,
        requested_by="mcp",
    )
    if created and command["status"] == commands_ctl.STATUS_REQUESTED:
        session_dispatch.dispatch_owned(session_id=session_id)
        command = commands_ctl.get(command["command_id"]) or command
    return {**command, "duplicate": not created}


def session_commands_tool(session_id: str, limit: int = 100):
    """The durable message/stop history for a Session."""
    from brains.control import session_commands as commands_ctl

    return commands_ctl.list_for_session(session_id, limit=limit)


def append_event_tool(kind: str, message: str, session_id: str | None = None):
    row = append_event(kind, message, session_id=session_id)
    return {"id": row.id, "kind": row.kind}


def file_decision_request_tool(
    workspace_path: str,
    title: str,
    body: str = "",
    proposed_answer: str | None = None,
    session_id: str | None = None,
):
    return file_decision_request(
        workspace_path,
        title=title,
        body=body,
        proposed_answer=proposed_answer,
        session_id=session_id,
    )


def resolve_decision_tool(
    code: str,
    chosen: str,
    reasoning: str = "",
    status: str = "resolved",
    session_id: str | None = None,
):
    """Resolve an approval request as the authenticated MCP principal.

    ``session_id`` names the Session performing the resolution. A Session may
    not resolve the approval it requested, and the Persona identity behind a
    request may not resolve it either - see
    :func:`brains.control.decisions.assert_resolver_allowed`.
    """
    from brains.authz.resolver import resolve_local_principal

    return resolve_decision(
        code,
        chosen=chosen,
        reasoning=reasoning,
        status=status,
        principal=resolve_local_principal(),
        resolving_session_id=session_id,
    )


def list_open_decisions_tool(workspace_path: str | None = None):
    return list_open_decisions(workspace_path)


def route_decision_tool(
    code: str,
    assigned_operator: str | None = None,
    clear_assignment: bool = False,
    priority: str | None = None,
    due_at: str | None = None,
    clear_due: bool = False,
    escalation_level: int | None = None,
    escalation_reason: str = "",
    operator: str | None = None,
):
    """Assign or escalate an open approval from a human-bound local principal."""
    from brains.authz.resolver import resolve_local_principal

    return route_decision(
        code,
        assigned_operator=assigned_operator,
        clear_assignment=clear_assignment,
        priority=priority,
        due_at=due_at,
        clear_due=clear_due,
        escalation_level=escalation_level,
        escalation_reason=escalation_reason,
        principal=resolve_local_principal(operator=operator),
    )


def escalate_decision_tool(
    code: str,
    reason: str,
    assigned_operator: str | None = None,
    due_at: str | None = None,
    operator: str | None = None,
):
    """Increment an open approval's escalation level as a local human."""
    from brains.authz.resolver import resolve_local_principal

    return escalate_decision(
        code,
        reason=reason,
        assigned_operator=assigned_operator,
        due_at=due_at,
        principal=resolve_local_principal(operator=operator),
    )


def set_handoff_tool(
    workspace_path: str,
    title: str,
    body: str = "",
    session_id: str | None = None,
):
    return set_handoff(workspace_path, title=title, body=body, session_id=session_id)


def pick_handoff_tool(workspace_path: str, session_id: str | None = None):
    return pick_handoff(workspace_path, session_id=session_id)


def clear_handoff_tool(
    workspace_path: str,
    reason: str = "",
    session_id: str | None = None,
):
    return clear_handoff(workspace_path, reason=reason, session_id=session_id)


def list_handoffs_tool(workspace_path: str | None = None, active_only: bool = True):
    return list_handoffs(workspace_path, active_only=active_only)


def generate_views_tool(workspace_path: str = "."):
    return refresh_views(workspace_path)


def create_task_tool(
    workspace_path: str,
    title: str,
    body: str = "",
    priority: str = "p2",
    depends_on: str = "",
    tags: str = "",
    session_id: str | None = None,
):
    return create_task(
        workspace_path,
        title=title,
        body=body,
        priority=priority,
        depends_on=depends_on,
        tags=tags,
        session_id=session_id,
    )


def claim_task_tool(task_code: str, session_id: str):
    return claim_task(task_code, session_id=session_id)


def complete_task_tool(task_code: str, session_id: str, summary: str = ""):
    return complete_task(task_code, session_id=session_id, summary=summary)


def release_task_tool(task_code: str, session_id: str, reason: str = ""):
    return release_task(task_code, session_id=session_id, reason=reason)


def handoff_task_tool(
    from_task_code: str,
    title: str,
    session_id: str,
    body: str = "",
    priority: str = "p2",
    extra_depends_on: str = "",
    tags: str = "",
    completion_summary: str = "",
):
    return handoff_task(
        from_task_code,
        title=title,
        session_id=session_id,
        body=body,
        priority=priority,
        extra_depends_on=extra_depends_on,
        tags=tags,
        completion_summary=completion_summary,
    )


def squad_create_tool(
    workspace_path: str,
    slug: str,
    name: str,
    leader: str,
    description: str = "",
    session_id: str | None = None,
):
    """Create a squad (a group of operators with a leader) in a workspace."""
    from brains.control import squads

    return squads.create_squad(
        workspace_path, slug, name, leader=leader, description=description, session_id=session_id
    )


def squad_add_member_tool(
    workspace_path: str,
    squad_slug: str,
    operator: str,
    role: str = "member",
    session_id: str | None = None,
):
    """Add an operator to a squad with an informational role."""
    from brains.control import squads

    return squads.add_member(workspace_path, squad_slug, operator, role=role, session_id=session_id)


def squad_remove_member_tool(
    workspace_path: str,
    squad_slug: str,
    operator: str,
    session_id: str | None = None,
):
    """Remove an operator from a squad (the leader cannot be removed)."""
    from brains.control import squads

    return squads.remove_member(workspace_path, squad_slug, operator, session_id=session_id)


def squad_list_tool(workspace_path: str, include_archived: bool = False):
    """List the squads in a workspace."""
    from brains.control import squads

    return squads.list_squads(workspace_path, include_archived=include_archived)


def squad_roster_tool(workspace_path: str, squad_slug: str):
    """Return a squad's roster (leader + members with roles and skill signals)."""
    from brains.control import squads

    return squads.roster(workspace_path, squad_slug)


def squad_assign_tool(
    workspace_path: str,
    squad_slug: str,
    title: str,
    body: str = "",
    priority: str = "p2",
    session_id: str | None = None,
):
    """Route new work to a squad. Creates a squad-tagged task and returns a
    leader brief; the squad leader then delegates to a member with squad_delegate."""
    from brains.control import squads

    return squads.assign_task_to_squad(
        workspace_path, squad_slug, title, body=body, priority=priority, session_id=session_id
    )


def squad_delegate_tool(
    task_code: str,
    to_operator: str,
    note: str = "",
    session_id: str | None = None,
):
    """As a squad leader, delegate a squad task to a chosen member operator.
    Delegate exactly once per task (re-delegation is rejected)."""
    from brains.control import squads

    return squads.delegate_task(task_code, to_operator, note=note, session_id=session_id)


def list_tasks_tool(
    workspace_path: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
    limit: int = 100,
):
    return list_tasks(
        workspace_path=workspace_path,
        status=status,
        priority=priority,
        tags=tags,
        limit=limit,
    )


def claim_workspace_tool(
    workspace_path: str,
    session_id: str,
    scope: str = "code",
    duration_minutes: int = 30,
):
    return claim_workspace(
        workspace_path,
        session_id=session_id,
        scope=scope,
        duration_minutes=duration_minutes,
    )


def release_workspace_tool(workspace_path: str, session_id: str):
    return release_workspace(workspace_path, session_id=session_id)


def list_workspace_claims_tool(
    workspace_path: str | None = None,
    include_expired: bool = False,
):
    return list_workspace_claims(
        workspace_path=workspace_path,
        include_expired=include_expired,
    )


def send_message_tool(
    subject: str,
    body: str = "",
    from_session_id: str | None = None,
    to_session_id: str | None = None,
    workspace_path: str | None = None,
    kind: str = "info",
    route_to_current: bool = False,
):
    return send_message(
        subject=subject,
        body=body,
        from_session_id=from_session_id,
        to_session_id=to_session_id,
        workspace_path=workspace_path,
        kind=kind,
        route_to_current=route_to_current,
    )


def read_messages_tool(
    session_id: str,
    mark_read: bool = True,
    include_read: bool = False,
    limit: int = 50,
    after_id: int | None = None,
):
    return read_messages(
        session_id,
        mark_read=mark_read,
        include_read=include_read,
        limit=limit,
        after_id=after_id,
    )


def inbox_wait_tool(
    session_id: str,
    timeout_ms: int = 25000,
    after_message_id: int | None = None,
):
    """Block until mail, subscribed-topic work, or a peer request arrives.

    The single long-poll an agent loops on instead of sleep-polling two
    surfaces. ``after_message_id`` is a client-held mailbox high-water mark.
    """
    return inbox_wait(
        session_id,
        timeout_ms=timeout_ms,
        after_message_id=after_message_id,
    )


def mail_send_tool(to: str, subject: str, body: str = "", session_id: str | None = None):
    """Send one outbound email via configured SMTP (SES works through its
    SMTP endpoint = config only). Refuses when the mailer is unconfigured."""
    from brains.control.mailer import send_email

    return send_email(to, subject, body, session_id=session_id)


def mail_status_tool():
    """Redacted mailer configuration snapshot (booleans + host; no secrets)."""
    from brains.control.mailer import mailer_status

    return mailer_status()


def list_live_agents_tool(ttl_seconds: int | None = None):
    """Discover every live agent session on this brain, across all workspaces.

    Returns session id, workspace, harness/tool, state and freshness. This
    is how a session finds peers to ask directly (comms scenario 1).
    """
    return live_agent_sessions(ttl_seconds=ttl_seconds)


def topic_post_tool(
    topic: str,
    subject: str,
    body: str = "",
    from_session_id: str | None = None,
    workspace_path: str | None = None,
    required_tool: str | None = None,
    reply_to: int | None = None,
    blast: bool = True,
):
    """Post to a named topic board (message board / pub-sub).

    Creates one durable announcement read only by live subscribed Sessions;
    no per-Workspace mailbox rows are synthesized. ``required_tool`` is an
    advisory harness hint ("claude" or "not:copilot").
    """
    return post_topic(
        topic,
        subject,
        body,
        from_session_id=from_session_id,
        workspace_path=workspace_path,
        required_tool=required_tool,
        reply_to=reply_to,
        blast=blast,
    )


def topic_read_tool(
    topic: str | None = None,
    limit: int = 50,
    reply_to: int | None = None,
    session_id: str | None = None,
    after_post_id: int | None = None,
):
    """Read a board and advance its subscription cursor when Session-scoped."""
    return read_topic(
        topic,
        limit=limit,
        reply_to=reply_to,
        session_id=session_id,
        after_post_id=after_post_id,
    )


def topic_subscribe_tool(
    topic: str,
    session_id: str,
    include_existing: bool = False,
):
    """Subscribe a live Session; new posts wake its unified inbox wait."""
    return subscribe_topic(topic, session_id, include_existing=include_existing)


def topic_unsubscribe_tool(topic: str, session_id: str):
    """Stop one live Session receiving wakeups for a topic."""
    return unsubscribe_topic(topic, session_id)


def topic_subscriptions_tool(session_id: str):
    """List a Session's topics, cursors, and pending announcement counts."""
    return list_topic_subscriptions(session_id)


def topic_list_tool(limit: int = 100):
    """List topics with post counts and latest activity."""
    return list_topics(limit=limit)


def capture_snapshot_tool(
    workspace_path: str,
    kind: str,
    data: dict | str,
    session_id: str | None = None,
):
    return capture_snapshot(workspace_path, kind=kind, data=data, session_id=session_id)


def latest_snapshot_tool(workspace_path: str, kind: str):
    return latest_snapshot(workspace_path, kind=kind)


def propose_pattern_tool(
    name: str,
    category: str,
    description: str,
    example: str = "",
    applies_to: str = "",
    session_id: str | None = None,
):
    return propose_pattern(
        name=name,
        category=category,
        description=description,
        example=example,
        applies_to=applies_to,
        session_id=session_id,
    )


def approve_pattern_tool(name: str, approved: bool = True):
    return approve_pattern(name, approved=approved)


def list_patterns_tool(
    category: str | None = None,
    status: str = "approved",
    limit: int = 100,
):
    return list_patterns(category=category, status=status, limit=limit)


def use_pattern_tool(name: str, session_id: str | None = None):
    return use_pattern(name, session_id=session_id)


def register_tool_tool(
    name: str,
    display_name: str,
    cli_command: str,
    spawn_args: str = "",
    capabilities: str = "",
    notes: str = "",
    verify: bool = True,
):
    return register_tool(
        name=name,
        display_name=display_name,
        cli_command=cli_command,
        spawn_args=spawn_args,
        capabilities=capabilities,
        notes=notes,
        verify=verify,
    )


def list_registered_tools_tool(verify_now: bool = False):
    return list_registered_tools(verify_now=verify_now)


def verify_tool_tool(name: str, session_id: str | None = None):
    return verify_tool(name, session_id=session_id)


def adoption_report_tool(
    window_minutes: int = 2,
    since_days: int = 14,
    workspace: str | None = None,
):
    """Per-surface adoption hit-rates for what the welcome packet offered.

    Joins ``session_start`` events (which carry a snapshot of the welcome
    counts in metadata) against follow-up events (``message_read``,
    ``pattern_used``, ``memory_retrieved``, ``tool_verified``) tied to the
    same session, within ``window_minutes`` of the start. The result tells
    you which welcome surfaces actually move the agent.
    """
    return adoption_report(
        window_minutes=window_minutes,
        since_days=since_days,
        workspace=workspace,
    )


def _auto_fire_notice(cron_expr: str) -> dict[str, Any] | None:
    """Loud, honest notice when a schedule cannot actually auto-fire.

    Scheduled auto-fire is experimental and disabled in the normal install;
    a definition created with a non-manual schedule would otherwise sit
    forever unfired with no explanation. Manual definitions are unaffected.
    """
    from brains.experimental import EXPERIMENTAL_ENV, experimental_enabled

    if experimental_enabled() or (cron_expr or "").strip().lower() == "manual":
        return None
    return {
        "experimental_auto_fire_disabled": True,
        "notice": (
            f"Scheduled auto-fire is experimental and disabled: this {cron_expr!r} "
            f"definition will not fire on its own. Set {EXPERIMENTAL_ENV}=1 to "
            f"enable auto-fire, or fire it manually."
        ),
    }


def create_recurring_task_tool(
    workspace_path: str,
    name: str,
    title_template: str,
    body_template: str = "",
    priority: str = "p2",
    tags: str = "",
    cron_expr: str = "manual",
    squad: str | None = None,
    session_id: str | None = None,
):
    result = create_recurring_task(
        workspace_path,
        name=name,
        title_template=title_template,
        body_template=body_template,
        priority=priority,
        tags=tags,
        cron_expr=cron_expr,
        squad=squad,
        session_id=session_id,
    )
    notice = _auto_fire_notice(cron_expr)
    if notice:
        result.update(notice)
    return result


def list_recurring_tasks_tool(
    workspace_path: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
):
    return list_recurring_tasks(
        workspace_path=workspace_path,
        enabled=enabled,
        limit=limit,
    )


def set_recurring_enabled_tool(name: str, enabled: bool = True):
    result = set_recurring_enabled(name, enabled=enabled)
    if isinstance(result, dict):
        notice = _auto_fire_notice(result.get("cron_expr", ""))
        if notice:
            result.update(notice)
    return result


def fire_recurring_task_tool(name: str, session_id: str | None = None):
    return fire_recurring_task(name, session_id=session_id)


def list_recurring_runs_tool(name: str | None = None, limit: int = 50):
    return list_recurring_runs(name=name, limit=limit)


def create_webhook_trigger_tool(
    slug: str,
    definition_name: str,
    event_filter: str | None = None,
):
    return create_webhook_trigger(slug, definition_name=definition_name, event_filter=event_filter)


def list_webhook_triggers_tool():
    return list_webhook_triggers()


def set_webhook_enabled_tool(slug: str, enabled: bool = True):
    return set_webhook_enabled(slug, enabled=enabled)


def ask_peer_tool(
    subject: str,
    question: str,
    from_session_id: str | None = None,
    to_workspace: str | None = None,
    to_session_id: str | None = None,
    context: str = "",
    timeout_ms: int = HELP_DEFAULT_TIMEOUT_MS,
    required_tool: str | None = None,
):
    """Ask another peer (session or workspace) a question and block until
    they answer or the request expires. Returns the resolved request.

    Either ``to_workspace`` or ``to_session_id`` must be set. ``context``
    is an optional free-form string the peer can use to ground the answer.

    ``required_tool`` constrains the claiming harness — exact tool name
    (``"claude"``) or ``not:<tool>`` (``"not:copilot"``) — so a session can
    route validation to a different CLI without sharing context.
    """
    return ask_peer(
        subject,
        question,
        from_session_id=from_session_id,
        to_workspace=to_workspace,
        to_session_id=to_session_id,
        context=context,
        timeout_ms=timeout_ms,
        required_tool=required_tool,
    )


def file_help_request_tool(
    subject: str,
    question: str,
    from_session_id: str | None = None,
    to_workspace: str | None = None,
    to_session_id: str | None = None,
    context: str = "",
    timeout_ms: int = HELP_DEFAULT_TIMEOUT_MS,
    required_tool: str | None = None,
):
    """File peer help and return immediately with its durable code."""
    return file_help_request(
        subject,
        question,
        from_session_id=from_session_id,
        to_workspace=to_workspace,
        to_session_id=to_session_id,
        context=context,
        timeout_ms=timeout_ms,
        required_tool=required_tool,
    )


def get_help_request_tool(code: str, session_id: str | None = None):
    """Read one visible peer-help request without blocking."""
    return get_help_request(code, session_id=session_id)


def wait_help_request_tool(
    code: str,
    session_id: str | None = None,
    timeout_ms: int = HELP_DEFAULT_TIMEOUT_MS,
):
    """Wait briefly for one request; timeout leaves the durable request open."""
    return wait_help_request(code, session_id=session_id, timeout_ms=timeout_ms)


def cancel_help_request_tool(code: str, session_id: str):
    """Cancel a request as the live Session that filed it."""
    return cancel_help_request(code, session_id=session_id)


def release_help_request_tool(
    code: str,
    session_id: str,
    retry_timeout_ms: int = HELP_DEFAULT_TIMEOUT_MS,
):
    """Release claimed help back to the open queue as its claimant."""
    return release_help_request(
        code,
        session_id=session_id,
        retry_timeout_ms=retry_timeout_ms,
    )


def wait_for_request_tool(
    session_id: str,
    workspace_slug: str | None = None,
    timeout_ms: int = HELP_DEFAULT_TIMEOUT_MS,
):
    """Block until a peer-help request is routed to this session /
    workspace, then claim it. Returns the request dict, or ``None`` on
    timeout."""
    return wait_for_request(
        session_id=session_id,
        workspace_slug=workspace_slug,
        timeout_ms=timeout_ms,
    )


def list_other_operators_active_tool():
    """Return cross-workspace presence: every OTHER operator with an
    active session.

    Per decision record 0002 (Layer 3), the projection is intentionally
    minimal — operator slug, display name, workspace count, active
    session count, and last activity timestamp. Workspace names,
    session ids, and bodies are deliberately omitted so this tool stays
    safe to expose to every operator on the brain.
    """
    return list_other_operators_active()


def answer_request_tool(
    code: str,
    answer: str,
    evidence: str,
    session_id: str,
):
    """Answer a previously-claimed peer help request. ``evidence`` is
    required and must be non-empty — answers must cite something."""
    return answer_request(code, answer, evidence, session_id=session_id)


def list_open_help_requests_tool(
    to_workspace: str | None = None,
    to_session_id: str | None = None,
    include_answered: bool = False,
    limit: int = 50,
):
    return list_open_help_requests(
        to_workspace=to_workspace,
        to_session_id=to_session_id,
        include_answered=include_answered,
        limit=limit,
    )


def link_tool_session_tool(
    brain_session_id: str,
    tool: str,
    tool_session_id: str,
    linked_by: str = "auto",
):
    """Bind a tool-side session id (Claude Code / Copilot CLI / Codex /
    custom) to a brain ``AgentSession``. Idempotent — the same triple
    can be recorded multiple times safely.

    Call this once per tool restart so the brain session keeps an audit
    trail of every tool-side incarnation that served it. ``linked_by``
    is ``"auto"`` when the tool registers itself or ``"operator"`` when
    a human pinned the link via the resume flow.
    """
    return link_tool_session(
        brain_session_id,
        tool,
        tool_session_id,
        linked_by=linked_by,
    )


def resume_brain_session_tool(
    brain_session_id: str,
    tool: str | None = None,
    tool_session_id: str | None = None,
    operator: bool = False,
    mail_limit: int = 10,
    event_limit: int = 20,
):
    """Re-attach to an existing brain session and get a one-call resume
    packet — last checkpoint, active claims / handoffs / tasks, unread
    mail count + preview, every tool-side session ever linked, and the
    most recent events. Provide ``tool`` + ``tool_session_id`` to record
    the new tool-side incarnation in the same call."""
    return resume_brain_session(
        brain_session_id,
        tool=tool,
        tool_session_id=tool_session_id,
        operator=operator,
        mail_limit=mail_limit,
        event_limit=event_limit,
    )


def find_brain_sessions_tool(tool: str, tool_session_id: str):
    """Reverse lookup — which brain sessions has this tool-side id ever
    been bound to? Useful when the operator only knows the tool-side
    id (e.g. ``copilot-cli`` shows a UUID and the operator wants to find
    the matching brain session)."""
    return find_brain_sessions(tool, tool_session_id)


def list_tool_session_links_tool(brain_session_id: str):
    """List every tool-side session id ever linked to this brain
    session, oldest first."""
    return list_tool_session_links(brain_session_id)


def checkpoint_tool(
    session_id: str,
    summary: str,
    next_action: str = "",
    blockers: str = "",
    scratchpad_path: str = "",
):
    """Drop a resume cairn against the current brain session.

    Call at natural breakpoints — end of a sub-task, before a long
    operation, before tool-side compaction. ``summary`` is required and
    short (one or two sentences). ``next_action`` and ``blockers`` are
    optional. ``scratchpad_path`` is a pointer to the tool's local
    working notes so the resumed session can pick them up if needed.

    Brain does not mirror the tool's working memory — only stores the
    cairns. Keep entries small.
    """
    return control_checkpoint(
        session_id,
        summary,
        next_action=next_action,
        blockers=blockers,
        scratchpad_path=scratchpad_path,
    )


def list_checkpoints_tool(session_id: str, limit: int = 20):
    """Return checkpoints for ``session_id``, newest first."""
    return list_checkpoints(session_id, limit=limit)


def latest_checkpoint_tool(session_id: str):
    """Convenience: the most recent checkpoint for ``session_id``, or
    ``None`` if there are no checkpoints yet."""
    return latest_checkpoint(session_id)


def audit_list_tool(
    limit: int = 100,
    since_id: int | None = None,
    action_prefix: str | None = None,
    actor: str | None = None,
):
    """List signed audit-log entries, newest first.

    Filter by ``action_prefix`` (matches via SQL LIKE) to narrow to a
    subsystem (``provider.``, ``task.``, ``admin.``). ``since_id``
    enables tail pagination (only ids strictly greater are returned).
    """
    from brains.audit import list_entries

    return {
        "entries": list_entries(
            limit=limit,
            since_id=since_id,
            action_prefix=action_prefix,
            actor=actor,
        )
    }


def audit_verify_tool():
    """Recompute the audit chain and report the first divergence.

    Returns ``{"ok": True, ...}`` when the chain is intact, including how
    many entries are stored and how many the head says were appended. On
    tamper detection returns ``{"ok": False, ...}`` with the diverging
    entry id, the reason, and the expected vs. actual hash so operators
    can pinpoint the row. Truncation and out-of-band deletion are
    reported through the head comparison, not just the per-row hashes.
    """
    from brains.audit import chain_status

    return chain_status()


def governed_action_list_tool(
    limit: int = 50,
    status: str | None = None,
    actor: str | None = None,
    action_prefix: str | None = None,
):
    """List governed actions: the decision record behind every outward effect.

    Each row carries actor, target, tool, the normalised-argument digest
    (never the arguments themselves), tier, decision, approval code,
    attempt, result and timestamps, and links to the audit entries that
    recorded the request, the decision and the outcome.
    """
    from brains.govern import list_governed_actions

    return {
        "actions": list_governed_actions(
            limit=limit,
            status=status,
            actor=actor,
            action_prefix=action_prefix,
        )
    }


def backup_create_tool(out_path: str):
    """Create a backup archive of the current brains DB.

    Dispatches on the configured ``subsystems.storage.backend``: the
    SQLite path uses the stdlib online backup API; the Postgres path
    shells out to ``pg_dump``. The archive is a ``.tar.gz`` containing
    a ``manifest.json`` and the raw data blob. Records
    ``admin.backup_created.attempted`` before it runs - a backup whose
    attempt cannot be recorded does not run - and ``admin.backup_created``
    once the archive exists.
    """
    from dataclasses import asdict

    from brains.audit import required_effect
    from brains.backup import create_backup

    with required_effect(
        actor="admin",
        action="admin.backup_created",
        payload={"out_path": str(out_path)},
    ) as effect:
        result = create_backup(out_path)
        payload = asdict(result)
        effect.record_outcome(payload)
    return payload


def backup_restore_tool(archive_path: str, target_url: str | None = None):
    """Restore a brains DB from a backup archive.

    Destructive: overwrites the on-disk SQLite file (or replays into
    the Postgres DB referenced by ``target_url`` / current settings).
    Records ``admin.restore_run.attempted`` before it touches anything -
    a restore whose attempt cannot be recorded does not run - and
    ``admin.restore_run`` once the restore returned. The attempt entry is
    written into the store the restore then replaces, so it stays with the
    pre-restore database.
    """
    from dataclasses import asdict

    from brains.audit import required_effect
    from brains.backup import restore_backup

    with required_effect(
        actor="admin",
        action="admin.restore_run",
        payload={"archive_path": str(archive_path), "target_url": bool(target_url)},
    ) as effect:
        result = restore_backup(archive_path, target_url=target_url)
        payload = asdict(result)
        effect.record_outcome(payload)
    return payload


def backup_inspect_tool(archive_path: str):
    """Return the manifest of a backup archive without restoring."""
    from brains.backup import inspect_archive

    return inspect_archive(archive_path)

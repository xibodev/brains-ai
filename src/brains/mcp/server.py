import argparse
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from brains.config import settings
from brains.control.recurring import (
    RecurringFireAlreadyClaimed,
    fire_recurring_task,
    is_valid_schedule,
    list_recurring_tasks,
)
from brains.experimental import (
    EXPERIMENTAL_ENV,
    EXPERIMENTAL_MCP_TOOLS,
    experimental_enabled,
)
from brains.mcp import tools
from brains.mcp.sse_auth import (
    ALLOW_PUBLIC_ENV,
    MCPAuthMiddleware,
    host_allowlist_for,
    resolve_bind_host,
)


def _build_mcp_transport_security() -> TransportSecuritySettings | None:
    """Build the MCP SDK's TransportSecuritySettings for our deployment.

    The upstream MCP SDK auto-enables DNS-rebinding protection when FastMCP is
    constructed without a host (defaulting to 127.0.0.1) and ships a hard-coded
    allowed_hosts list of ``localhost:*`` / ``127.0.0.1:*`` / ``[::1]:*``. That
    breaks any reverse-proxy deployment where the public Host header is something
    else (``mcp-brains.example.com``).

    When the operator has opted into a public bind via ``BRAINS_MCP_ALLOW_PUBLIC``,
    we DISABLE the SDK's host validation entirely — the reverse proxy in front
    of brains is responsible for hostname ACLs (TLS SNI + vhost match), and
    brains' own ``MCPAuthMiddleware`` still enforces bearer-token auth and rate
    limits. Returning ``None`` from this function preserves the SDK's default
    behaviour for loopback-only deployments.
    """
    raw = os.environ.get(ALLOW_PUBLIC_ENV, "").strip().lower()
    if raw not in {"1", "true", "yes", "on"}:
        return None
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


log = logging.getLogger("brains-mcp-server")


TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "plan_request": tools.plan_request,
    "ask_human": tools.ask_human,
    "get_context_pack": tools.get_context_pack,
    "search_repo": tools.search_repo_tool,
    "search_semantic": tools.search_semantic_tool,
    "orient": tools.orient_tool,
    "graph_build": tools.graph_build,
    "graph_query": tools.graph_query_tool,
    "graph_neighbors": tools.graph_neighbors_tool,
    "graph_path": tools.graph_path_tool,
    "graph_subsystems": tools.graph_subsystems,
    "graph_export": tools.graph_export,
    "check_freshness": tools.check_freshness,
    "fetch_and_index_source": tools.fetch_and_index_source,
    "retrieve_memory": tools.retrieve_memory_tool,
    "store_memory": tools.store_memory_tool,
    "explain_route": tools.explain_route,
    "get_state": tools.get_state_tool,
    "mailbox_register": tools.mailbox_register_tool,
    "mailbox_phonebook": tools.mailbox_phonebook_tool,
    "mailbox_lookup": tools.mailbox_lookup_tool,
    "mailbox_send": tools.mailbox_send_tool,
    "mailbox_broadcast": tools.mailbox_broadcast_tool,
    "mailbox_reply": tools.mailbox_reply_tool,
    "mailbox_forward": tools.mailbox_forward_tool,
    "mailbox_inbox": tools.mailbox_inbox_tool,
    "mailbox_sent": tools.mailbox_sent_tool,
    "mailbox_thread": tools.mailbox_thread_tool,
    "mailbox_notification_take": tools.mailbox_notification_take_tool,
    "mailbox_notification_settle": tools.mailbox_notification_settle_tool,
    "start_session": tools.start_session_tool,
    "heartbeat_session": tools.heartbeat_session_tool,
    "link_session_successor": tools.link_session_successor_tool,
    "end_session": tools.end_session_tool,
    "session_message": tools.session_message_tool,
    "session_stop": tools.session_stop_tool,
    "session_commands": tools.session_commands_tool,
    "append_event": tools.append_event_tool,
    "event_context": tools.event_context_tool,
    "event_scope_report": tools.event_scope_report_tool,
    "file_decision_request": tools.file_decision_request_tool,
    "resolve_decision": tools.resolve_decision_tool,
    "route_decision": tools.route_decision_tool,
    "escalate_decision": tools.escalate_decision_tool,
    "list_open_decisions": tools.list_open_decisions_tool,
    "set_handoff": tools.set_handoff_tool,
    "pick_handoff": tools.pick_handoff_tool,
    "clear_handoff": tools.clear_handoff_tool,
    "list_handoffs": tools.list_handoffs_tool,
    "generate_views": tools.generate_views_tool,
    "create_task": tools.create_task_tool,
    "claim_task": tools.claim_task_tool,
    "complete_task": tools.complete_task_tool,
    "release_task": tools.release_task_tool,
    "handoff_task": tools.handoff_task_tool,
    "list_tasks": tools.list_tasks_tool,
    "squad_create": tools.squad_create_tool,
    "squad_add_member": tools.squad_add_member_tool,
    "squad_remove_member": tools.squad_remove_member_tool,
    "squad_list": tools.squad_list_tool,
    "squad_roster": tools.squad_roster_tool,
    "squad_assign": tools.squad_assign_tool,
    "squad_delegate": tools.squad_delegate_tool,
    "claim_workspace": tools.claim_workspace_tool,
    "release_workspace": tools.release_workspace_tool,
    "list_workspace_claims": tools.list_workspace_claims_tool,
    "send_message": tools.send_message_tool,
    "read_messages": tools.read_messages_tool,
    "inbox_wait": tools.inbox_wait_tool,
    "mail_send": tools.mail_send_tool,
    "mail_status": tools.mail_status_tool,
    "list_live_agents": tools.list_live_agents_tool,
    "topic_post": tools.topic_post_tool,
    "topic_read": tools.topic_read_tool,
    "topic_list": tools.topic_list_tool,
    "topic_subscribe": tools.topic_subscribe_tool,
    "topic_unsubscribe": tools.topic_unsubscribe_tool,
    "topic_subscriptions": tools.topic_subscriptions_tool,
    "ask_peer": tools.ask_peer_tool,
    "file_help_request": tools.file_help_request_tool,
    "get_help_request": tools.get_help_request_tool,
    "wait_help_request": tools.wait_help_request_tool,
    "cancel_help_request": tools.cancel_help_request_tool,
    "release_help_request": tools.release_help_request_tool,
    "wait_for_request": tools.wait_for_request_tool,
    "answer_request": tools.answer_request_tool,
    "list_open_help_requests": tools.list_open_help_requests_tool,
    "feedback_report": tools.feedback_report_tool,
    "feedback_enrich": tools.feedback_enrich_tool,
    "feedback_get": tools.feedback_get_tool,
    "feedback_list": tools.feedback_list_tool,
    "capture_snapshot": tools.capture_snapshot_tool,
    "latest_snapshot": tools.latest_snapshot_tool,
    "propose_pattern": tools.propose_pattern_tool,
    "approve_pattern": tools.approve_pattern_tool,
    "list_patterns": tools.list_patterns_tool,
    "use_pattern": tools.use_pattern_tool,
    "register_tool": tools.register_tool_tool,
    "list_registered_tools": tools.list_registered_tools_tool,
    "verify_tool": tools.verify_tool_tool,
    "adoption_report": tools.adoption_report_tool,
    "create_recurring_task": tools.create_recurring_task_tool,
    "list_recurring_tasks": tools.list_recurring_tasks_tool,
    "set_recurring_enabled": tools.set_recurring_enabled_tool,
    "fire_recurring_task": tools.fire_recurring_task_tool,
    "list_recurring_runs": tools.list_recurring_runs_tool,
    "create_webhook_trigger": tools.create_webhook_trigger_tool,
    "list_webhook_triggers": tools.list_webhook_triggers_tool,
    "set_webhook_enabled": tools.set_webhook_enabled_tool,
    "link_tool_session": tools.link_tool_session_tool,
    "resume_brain_session": tools.resume_brain_session_tool,
    "find_brain_sessions": tools.find_brain_sessions_tool,
    "list_tool_session_links": tools.list_tool_session_links_tool,
    "checkpoint": tools.checkpoint_tool,
    "list_checkpoints": tools.list_checkpoints_tool,
    "latest_checkpoint": tools.latest_checkpoint_tool,
    "list_other_operators_active": tools.list_other_operators_active_tool,
    "list_signals": tools.list_signals_tool,
    "audit_list": tools.audit_list_tool,
    "audit_verify": tools.audit_verify_tool,
    "governed_action_list": tools.governed_action_list_tool,
    "backup_create": tools.backup_create_tool,
    "backup_restore": tools.backup_restore_tool,
    "backup_inspect": tools.backup_inspect_tool,
    "knowledge_add": tools.knowledge_add_tool,
    "knowledge_resolve": tools.knowledge_resolve_tool,
    "knowledge_search": tools.knowledge_search_tool,
    "retrieve_original": tools.retrieve_original_tool,
    "learn_propose": tools.learn_propose_tool,
}

mcp = FastMCP("Brains v2", transport_security=_build_mcp_transport_security())

# Namespace prefix for every MCP tool. MUST stay within Anthropic's tool-name
# rule ^[a-zA-Z0-9_-]+$ — an underscore, NOT a dot. A dotted name (the old
# "brains." prefix) is sanitised by Claude Code to "brains_<x>" when exposed,
# then forwarded verbatim on call; the server never registered that sanitised
# name, so every Claude `tools/call` failed with JSON-RPC -32602. Underscore
# round-trips cleanly across Claude Code, Copilot CLI, and Codex.
# See tests/test_mcp_server.py::test_registered_mcp_tool_names_are_anthropic_safe.
TOOL_PREFIX = "brains_"

# The MCP tool surface is configurable so a client only pays the context cost
# of the tools it needs. The lean core keeps the normal coordination contract.
#   BRAINS_MCP_TOOLS unset | "full" | "all"  -> all tools (back-compat default)
#   BRAINS_MCP_TOOLS = "lean"                 -> the curated core set below
#   BRAINS_MCP_TOOLS = "a,b,c"                -> an explicit allowlist
# Tools are always *callable* via call_tool(); this only scopes what is advertised.
LEAN_TOOLS = frozenset(
    {
        "start_session",
        "mailbox_register",
        "mailbox_phonebook",
        "mailbox_lookup",
        "mailbox_send",
        "mailbox_broadcast",
        "mailbox_reply",
        "mailbox_forward",
        "mailbox_inbox",
        "mailbox_sent",
        "mailbox_thread",
        "mailbox_notification_take",
        "mailbox_notification_settle",
        "heartbeat_session",
        "end_session",
        "append_event",
        "event_context",
        "event_scope_report",
        "set_handoff",
        "pick_handoff",
        "list_handoffs",
        "knowledge_add",
        "knowledge_search",
        "search_semantic",
        "orient",
        "file_decision_request",
        "file_help_request",
        "get_help_request",
        "wait_help_request",
        "cancel_help_request",
        "release_help_request",
        "feedback_report",
        "feedback_enrich",
        "feedback_get",
        "feedback_list",
        "resolve_decision",
        "route_decision",
        "escalate_decision",
        "claim_workspace",
        "read_messages",
        "inbox_wait",
        "send_message",
        "list_live_agents",
        "topic_post",
        "topic_read",
        "topic_list",
        "topic_subscribe",
        "topic_unsubscribe",
        "topic_subscriptions",
        "plan_request",
        "get_context_pack",
    }
)


def _resolve_active_tools() -> list[str]:
    raw = (os.environ.get("BRAINS_MCP_TOOLS") or "").strip().lower()
    if raw in ("", "full", "all"):
        selected = list(TOOL_REGISTRY)
    elif raw == "lean":
        selected = [n for n in TOOL_REGISTRY if n in LEAN_TOOLS]
    else:
        wanted = {x.strip() for x in raw.split(",") if x.strip()}
        selected = [n for n in TOOL_REGISTRY if n in wanted]
    # Experimental gate: the normal install neither advertises nor executes
    # the experimental tools, whatever selection mode named them. An explicit
    # allowlist is not an opt-in — only BRAINS_MCP_EXPERIMENTAL=1 is.
    if not experimental_enabled():
        selected = [n for n in selected if n not in EXPERIMENTAL_MCP_TOOLS]
    return selected


ACTIVE_TOOLS = _resolve_active_tools()

for name in ACTIVE_TOOLS:
    # Register each active tool with FastMCP dynamically under the brains_ namespace.
    # Note: FastMCP.tool takes a name argument.
    mcp.tool(name=f"{TOOL_PREFIX}{name}")(TOOL_REGISTRY[name])


def list_tools() -> list[str]:
    return [f"{TOOL_PREFIX}{k}" for k in ACTIVE_TOOLS]


def call_tool(tool_name: str, **kwargs):
    # Accept both the current ``brains_`` prefix and the legacy ``brains.``
    # form so older callers/tests keep working.
    for prefix in (TOOL_PREFIX, "brains."):
        if tool_name.startswith(prefix):
            tool_name = tool_name[len(prefix) :]
            break
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        raise ValueError(f"unknown Brains tool: {tool_name}")
    # Defence in depth for the experimental gate: registration already keeps
    # these out of the advertised surface, but a direct dispatch (tests,
    # internal callers, a future transport that bypasses registration) must
    # refuse exactly as loudly.
    if tool_name in EXPERIMENTAL_MCP_TOOLS and not experimental_enabled():
        from brains.experimental import EXPERIMENTAL_TOOL_REASONS

        reason = EXPERIMENTAL_TOOL_REASONS.get(tool_name, "not yet mature")
        raise ValueError(
            f"'{tool_name}' is experimental and disabled: {reason}. "
            f"Set {EXPERIMENTAL_ENV}=1 to enable it."
        )
    return fn(**kwargs)


def _parse_last_fired(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


_EVERY_PATTERN = re.compile(r"^every:(\d+)([smhd])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _is_due(cron_expr: str, last_fired: datetime | None, now: datetime) -> bool:
    """Determine whether a recurring task should fire at ``now``.

    Supported expressions (:func:`brains.control.recurring.is_valid_schedule`
    is the canonical grammar check this mirrors):
    - ``manual`` (never auto-fires)
    - ``hourly`` (fires when an hour has elapsed since the last firing)
    - ``daily`` (fires on a new UTC calendar date)
    - ``every:<N><s|m|h|d>`` (fires after N units have elapsed)

    Anything unrecognized - including a ``manual`` schedule or a real cron
    string, which this engine does not support - is treated as never-due.
    """
    expr = (cron_expr or "").strip().lower()
    if not is_valid_schedule(expr) or expr == "manual":
        return False
    every_match = _EVERY_PATTERN.match(expr)
    if last_fired is None:
        return True
    if expr == "hourly":
        return now - last_fired >= timedelta(hours=1)
    if expr == "daily":
        return now.date() > last_fired.date()
    assert every_match is not None  # narrowed above
    count = int(every_match.group(1))
    unit = every_match.group(2).lower()
    return now - last_fired >= timedelta(seconds=count * _UNIT_SECONDS[unit])


#: Runtime rows online-but-silent for longer than this are swept ``offline``
#: by the scheduler tick (BL-P1-13's periodic owner for BL-P0-XX's
#: ``sweep_stale``). Six heartbeats' worth of silence by default (heartbeat
#: defaults to 15s), overridable so an install with a slower/faster daemon
#: heartbeat can tune the window without a code change.
DEFAULT_RUNTIME_STALE_TTL_SECONDS = 90


def _runtime_stale_ttl_seconds() -> int:
    raw = os.environ.get("BRAINS_RUNTIME_STALE_TTL_SECONDS")
    if not raw:
        return DEFAULT_RUNTIME_STALE_TTL_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_RUNTIME_STALE_TTL_SECONDS


def _sweep_stale_runtimes() -> int:
    """Flip online Runtimes silent past the TTL to ``offline``.

    ``brains.control.runtimes.sweep_stale`` existed with no periodic caller —
    a Runtime that stopped heartbeating (crashed daemon, network partition,
    unplugged machine) stayed ``online`` forever unless an operator happened
    to read it. This tick is that owner. Never breaks the scheduler loop: a
    failed sweep is logged and retried on the next tick.
    """
    try:
        from brains.control.runtimes import sweep_stale

        flipped = sweep_stale(ttl_seconds=_runtime_stale_ttl_seconds())
    except Exception as exc:  # noqa: BLE001 - maintenance never gates a fire
        log.error("scheduler: runtime staleness sweep failed: %s", exc)
        return 0
    if flipped:
        log.info("scheduler: swept %d stale runtime(s) offline", len(flipped))
    return len(flipped)


def _sweep_governed_actions() -> int:
    """Settle expired governed actions; never break the scheduler loop.

    Failing to sweep is a maintenance miss, not an authorisation: nothing is
    released by skipping it, so it is logged and the tick continues.
    """
    try:
        from brains.govern import run_maintenance

        swept = int(run_maintenance().get("swept", 0))
    except Exception as exc:  # noqa: BLE001 - maintenance never gates a fire
        log.error("scheduler: governed-action sweep failed: %s", exc)
        return 0
    if swept:
        log.info("scheduler: settled %d stale governed action(s)", swept)
    return swept


def _sweep_stale_sessions() -> int:
    """Expire PID-less coordination leases without ending their history."""
    try:
        from brains.control.sessions import sweep_stale_session_leases

        dormant = sweep_stale_session_leases()
    except Exception as exc:  # noqa: BLE001 - maintenance never gates a fire
        log.error("scheduler: session lease sweep failed: %s", exc)
        return 0
    if dormant:
        log.info("scheduler: made %d stale coordination Session(s) dormant", len(dormant))
    return len(dormant)


def _dispatch_help_reviews() -> int:
    try:
        from brains.control.help_execution import dispatch_due_help_reviews

        scheduled = dispatch_due_help_reviews()
    except Exception as exc:  # noqa: BLE001 - maintenance must not break scheduler
        log.error("scheduler: ephemeral help review dispatch failed: %s", exc)
        return 0
    return len(scheduled)


def _scheduler_tick(now: datetime | None = None) -> list[dict]:
    """Evaluate every enabled recurring task and fire those that are due.

    Governed-action maintenance and the Runtime staleness sweep both run
    first, in the same tick: the expiry rules for stale
    ``pending``/``authorized``/``requested`` governed-action rows, and for
    Runtimes that stopped heartbeating, both need a periodic owner, and this
    loop is the one process that is always running where they matter. Neither
    sweep can settle a live/online row out from under itself — the governed
    sweep judges only an expired attempt lease, and the runtime sweep judges
    only silence past the heartbeat TTL.

    Scheduled auto-fire is experimental (limited grammar, cooperative gate,
    no end-to-end journey evidence), so the fire loop runs only when
    ``BRAINS_MCP_EXPERIMENTAL=1``. The sweeps are maintenance, not
    automation, and always run. Manual fire is unaffected.

    Returns a list of ``{name, task_code}`` entries describing each firing so
    callers (and tests) can verify scheduler behavior without sleeping.
    """
    now = now or datetime.now(UTC)
    _sweep_governed_actions()
    _sweep_stale_runtimes()
    _sweep_stale_sessions()
    _dispatch_help_reviews()
    if not experimental_enabled():
        return []
    fired: list[dict] = []
    for definition in list_recurring_tasks(enabled=True):
        name = definition.get("name")
        if not name:
            continue
        last_fired = _parse_last_fired(definition.get("last_fired_at"))
        if not _is_due(definition.get("cron_expr", "manual"), last_fired, now):
            continue
        try:
            result = fire_recurring_task(
                name,
                source="schedule",
                expected_last_fired_at=last_fired,
            )
        except RecurringFireAlreadyClaimed:
            continue
        except Exception as exc:  # noqa: BLE001 - never break the loop
            log.error("scheduler: failed to fire %s: %s", name, exc)
            continue
        fired.append({"name": name, "task_code": result["task"]["code"]})
    return fired


def _scheduler_loop(interval_seconds: int = 60):
    """Background thread that periodically fires due recurring tasks."""
    log.info("Scheduler started (interval=%ss)", interval_seconds)
    while True:
        try:
            fired = _scheduler_tick()
            if fired:
                log.info("Scheduler fired %d recurring task(s): %s", len(fired), fired)
        except Exception as exc:  # noqa: BLE001
            log.error("Scheduler error: %s", exc)
        time.sleep(interval_seconds)


def run_mcp_server(mode: str = "sse", port: int = 9877, scheduler_interval: int = 60):
    from brains.api.admin_key import ensure_admin_key

    # Both transports need the persisted key to decrypt local secure settings.
    ensure_admin_key(print_banner=mode == "sse")
    # Make sure the admin operator row exists before any tool can be
    # invoked over either transport. Idempotent and cheap.
    from brains.control.durable_mailbox import ensure_operator_mailboxes
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    ensure_operator_mailboxes()

    if mode == "sse":
        # Load the persisted admin key into settings so the SSE auth
        # middleware's _valid_keys() can resolve it — parity with the
        # gateway's lifespan (brains.main:lifespan). Without this, every
        # authenticated SSE request 500s with "API key not configured".
        sched_thread = threading.Thread(
            target=_scheduler_loop, args=(scheduler_interval,), daemon=True, name="brains-scheduler"
        )
        sched_thread.start()
        import uvicorn

        host = resolve_bind_host()
        sse_app = mcp.sse_app()
        guarded_app = MCPAuthMiddleware(sse_app, allowed_hosts=host_allowlist_for(host))
        print(f"Brains MCP server running on http://{host}:{port}/sse", file=sys.stderr)
        print(f"Scheduler active (every {scheduler_interval}s)", file=sys.stderr)
        if experimental_enabled():
            print("Autopilot auto-fire: enabled (experimental)", file=sys.stderr)
        else:
            print(
                f"Autopilot auto-fire: disabled (experimental; set {EXPERIMENTAL_ENV}=1 to enable)",
                file=sys.stderr,
            )
        if settings.allow_unauthenticated_api:
            print(
                "MCP SSE auth: DISABLED (settings.allow_unauthenticated_api=True)",
                file=sys.stderr,
            )
        else:
            print(
                "MCP SSE auth: required (Authorization: Bearer <api-key>)",
                file=sys.stderr,
            )
        os.environ["UVICORN_PORT"] = str(port)
        os.environ["UVICORN_HOST"] = host
        uvicorn.run(guarded_app, host=host, port=port, log_level="info")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="brains-mcp-server")
    parser.add_argument("--mode", choices=["sse", "stdio"], default="sse")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--scheduler-interval", type=int, default=60)
    args = parser.parse_args()
    run_mcp_server(mode=args.mode, port=args.port, scheduler_interval=args.scheduler_interval)

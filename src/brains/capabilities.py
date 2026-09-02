"""Machine-readable boundary for the capabilities shipped by Brains core.

Historical tables and implementation modules may remain importable so old SQLite
stores can be migrated. They are not activation surfaces.
"""

from __future__ import annotations

CORE_MCP_TOOLS = frozenset(
    {
        "get_state", "search_repo",
        "mailbox_register", "mailbox_phonebook", "mailbox_lookup", "mailbox_send",
        "mailbox_broadcast", "mailbox_reply", "mailbox_forward", "mailbox_inbox",
        "mailbox_sent", "mailbox_thread", "mailbox_notification_take",
        "mailbox_notification_settle", "start_session", "heartbeat_session",
        "link_session_successor", "end_session", "append_event", "event_context",
        "event_scope_report", "file_decision_request", "resolve_decision",
        "route_decision", "escalate_decision", "list_open_decisions", "set_handoff",
        "pick_handoff", "clear_handoff", "list_handoffs", "generate_views",
        "create_task", "claim_task", "complete_task", "release_task", "handoff_task",
        "list_tasks",
        "claim_workspace", "release_workspace", "list_workspace_claims", "inbox_wait",
        "file_help_request", "get_help_request",
        "wait_help_request", "cancel_help_request", "release_help_request",
        "wait_for_request", "answer_request", "list_open_help_requests",
        "link_tool_session", "resume_brain_session", "find_brain_sessions",
        "list_tool_session_links", "checkpoint", "list_checkpoints", "latest_checkpoint",
        "list_signals", "audit_list", "audit_verify", "governed_action_list",
        "backup_create", "backup_restore", "backup_inspect", "knowledge_add",
        "knowledge_resolve", "knowledge_search", "retrieve_original",
    }
)

WITHDRAWN_CLI_COMMANDS = frozenset(
    {
        "run", "prune-traces", "dashboard", "copilot-login", "copilot-status",
        "copilot-logout", "classify", "plan", "features", "index-repo", "repo-index",
        "repo-search", "graph-export", "graph-build", "graph-query", "graph-neighbors",
        "graph-path", "graph-subsystems", "docs-index", "embed-repo", "search-semantic",
        "orient", "live-agents", "topic-post", "topic-read", "topic-list",
        "topic-subscribe", "topic-unsubscribe", "topic-subscriptions", "feedback-report",
        "feedback-enrich", "feedback-get", "feedback-list", "feedback-triage",
        "feedback-promote", "session-message", "session-stop", "session-commands",
        "exec-session", "message-send", "message-read", "pattern-propose",
        "pattern-approve", "pattern-list", "pattern-use", "tool-register", "tool-list",
        "tool-verify", "recurring-create", "recurring-list", "recurring-enable",
        "recurring-fire", "mail-send", "mail-status", "check-source", "learn",
    }
)

WITHDRAWN_CLI_GROUPS = frozenset({"daemon", "jobs", "operator", "credentials"})

WITHDRAWN_HTTP_PATH_PREFIXES = (
    "/admin/api", "/admin/config", "/admin/secrets", "/admin/test",
    "/v1/config", "/v1/admin/configuration/email",
    "/v1/admin/configuration/secrets", "/v1/admin/configuration/general",
    "/v1/operator/mailboxes/smtp", "/v1/operator/feedback",
    "/v1/operator/patterns", "/v1/operator/tools", "/v1/operator/topics",
    "/v1/orgs/{org}/members", "/v1/orgs/{org}/pods",
    "/v1/orgs/{org}/autopilots", "/v1/orgs/{org}/skills",
    "/v1/orgs/{org}/personas", "/v1/orgs/{org}/projects", "/v1/pods",
    "/v1/autopilots", "/v1/personas", "/v1/projects", "/v1/issues",
    "/v1/runtimes", "/v1/integrations", "/v1/onboarding",
    "/v1/orgs/{org}/workspaces", "/hooks", "/relay",
)

WITHDRAWN_HTTP_EXACT_PATHS = frozenset(
    {
        "/admin", "/admin/healthz", "/admin/overview", "/v1/usage",
        "/v1/admin/coordination/overview",
        "/v1/orgs/{org}/usage", "/v1/sessions/spawn", "/v1/sessions/{session_id}/message",
        "/v1/sessions/{session_id}/stop", "/v1/workspaces/{slug}/messages",
        "/v1/sessions/{session_id}/commands", "/v1/sessions/{session_id}/state",
        "/v1/operator/workspaces/{slug}/messages", "/v1/orgs/onboard", "/v1/onboard",
        "/v1/operator/workspaces/{slug}/feedback",
    }
)


def withdrawn_http_path(path: str) -> bool:
    return path in WITHDRAWN_HTTP_EXACT_PATHS or path.startswith(WITHDRAWN_HTTP_PATH_PREFIXES)

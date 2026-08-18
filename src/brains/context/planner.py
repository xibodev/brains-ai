from brains.config import settings

_LARGE_SCAN_HINTS = {
    "entire repo",
    "whole repo",
    "all files",
    "scan the repo",
    "scan repo",
    "full repository",
    "full codebase",
    "entire codebase",
}


def _messages_text(messages) -> str:
    if not messages:
        return ""
    return " ".join(str(m.get("content", "")) for m in messages).lower()


def _requests_large_scan(messages) -> bool:
    text = _messages_text(messages)
    return any(hint in text for hint in _LARGE_SCAN_HINTS)


def plan(classification, messages=None, workspace_path=None, model_budget_policy=None):
    strategy = "direct"
    required_decisions = []
    context_sources = []
    active_handoffs = []
    active_claims = []
    available_tasks = []
    if workspace_path:
        try:
            from brains.control.handoffs import list_handoffs

            active_handoffs = list_handoffs(workspace_path, active_only=True)
        except Exception:
            active_handoffs = []
        try:
            from brains.control.claims import list_workspace_claims

            active_claims = list_workspace_claims(workspace_path)
        except Exception:
            active_claims = []
        try:
            from brains.control.tasks import list_tasks

            available_tasks = list_tasks(workspace_path, status="available", limit=5)
        except Exception:
            available_tasks = []
    if classification.task_type == "code_fix" and classification.needs_codebase:
        strategy = "repo_search"
    elif classification.task_type == "docs_lookup" and classification.freshness_required:
        strategy = "docs_freshness"
    elif (
        classification.task_type == "architecture"
        and classification.needs_codebase
        and classification.needs_external_docs
    ):
        strategy = "repo_and_docs"
    if workspace_path:
        context_sources.append({"type": "workspace", "path": workspace_path})
    if "http://" in _messages_text(messages) or "https://" in _messages_text(messages):
        context_sources.append({"type": "external_docs", "trust": "untrusted_external"})
    if active_handoffs:
        strategy = "handoff_resume"
    elif available_tasks and strategy == "direct":
        strategy = "direct"
    if (
        classification.recommended_model_tier == "deep"
        and settings.control.require_approval_for_deep
    ):
        strategy = "approval_required"
        required_decisions.append("deep_model_route")
    if classification.needs_external_docs and settings.control.require_approval_for_external_docs:
        strategy = "approval_required"
        required_decisions.append("external_docs")
    if (
        classification.needs_codebase
        and settings.control.require_approval_for_large_scans
        and _requests_large_scan(messages)
    ):
        strategy = "approval_required"
        required_decisions.append("large_repo_scan")

    return {
        "strategy": strategy,
        "steps": [
            "classify request",
            f"strategy:{strategy}",
            "route model tier",
        ],
        "do_not_do": [
            "read large repo slices before planning",
            "ignore active workspace claims before editing",
        ],
        "required_decisions": required_decisions,
        "context_sources": context_sources,
        "coordination": {
            "active_handoffs": active_handoffs,
            "active_claims": active_claims,
            "available_tasks": available_tasks,
        },
        "recommended_context_limit_tokens": 4000,
    }

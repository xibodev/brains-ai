from __future__ import annotations

from collections import Counter

from brains.context.docs_indexer import index_docs
from brains.control.decisions import file_decision_request, list_open_decisions
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import RouteDecision


def stale_docs_digest(workspace_path: str = ".") -> dict:
    result = index_docs(workspace_path)
    stale = [row for row in result["records"] if row.get("stale")]
    body = (
        "\n".join(
            f"- `{row['rel_path']}` modified {row['mtime'].date().isoformat()}: {row['title']}"
            for row in stale[:50]
        )
        or "No stale docs found."
    )
    return file_decision_request(
        workspace_path,
        "Stale docs digest",
        body=body,
        proposed_answer=f"Review {len(stale)} stale docs" if stale else "No action needed",
        metadata={"job": "stale-docs-digest", "stale_count": len(stale)},
    )


def open_decisions_digest(workspace_path: str = ".") -> dict:
    decisions = list_open_decisions(workspace_path)
    body = (
        "\n".join(f"- {row['code']}: {row['title']} ({row['created_at']})" for row in decisions)
        or "No open decisions."
    )
    return file_decision_request(
        workspace_path,
        "Open decisions digest",
        body=body,
        proposed_answer=f"Resolve {len(decisions)} open decisions"
        if decisions
        else "No action needed",
        metadata={"job": "open-decisions-digest", "open_count": len(decisions)},
    )


def route_audit(workspace_path: str = ".") -> dict:
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        rows = session.query(RouteDecision).order_by(RouteDecision.id.desc()).limit(200).all()
    counts = Counter(row.model_tier for row in rows)
    body = (
        "\n".join(f"- {tier}: {count}" for tier, count in sorted(counts.items()))
        or "No route decisions found."
    )
    return file_decision_request(
        workspace.path,
        "Route audit",
        body=body,
        proposed_answer="Review route distribution",
        metadata={"job": "route-audit", "counts": dict(counts)},
    )


JOBS = {
    "stale-docs-digest": stale_docs_digest,
    "open-decisions-digest": open_decisions_digest,
    "route-audit": route_audit,
}


def list_jobs() -> list[str]:
    return sorted(JOBS)


def run_job(name: str, workspace_path: str = ".") -> dict:
    job = JOBS.get(name)
    if job is None:
        raise ValueError(f"unknown job: {name}")
    try:
        result = job(workspace_path)
        append_event("job_run", f"job completed: {name}", metadata={"job": name})
        return result
    except Exception as exc:
        append_event("job_failed", f"job failed: {name}: {exc}", metadata={"job": name})
        raise

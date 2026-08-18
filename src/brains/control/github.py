"""GitHub integration (F8) — PR <-> issue linking + auto-Done on merge.

A GitHub ``pull_request`` webhook is parsed for an issue code (``ISS-NNNN``) in
the PR title, head branch, or body. On a merged PR the linked issue is moved to
``done`` and a comment recording the merge is posted. Pure control logic — the
route lives in :mod:`brains.api.webhooks`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re

from brains.config import settings
from brains.control import integration_deliveries as deliveries_ctl
from brains.control import issues as issues_ctl
from brains.control import orgs as orgs_ctl
from brains.control import projects as projects_ctl

_ISSUE_CODE = re.compile(r"\bISS-\d+\b", re.IGNORECASE)


class GitHubWebhookDisabled(RuntimeError):
    pass


class GitHubWebhookAuthError(ValueError):
    pass


class GitHubWebhookPayloadError(ValueError):
    pass


class GitHubWebhookInProgress(RuntimeError):
    pass


def _repository_bindings() -> dict[str, str]:
    bindings: dict[str, str] = {}
    for entry in settings.github_repository_org_bindings:
        repository, separator, org_slug = entry.partition("=")
        if separator and repository.strip() and org_slug.strip():
            bindings[repository.strip().lower()] = org_slug.strip()
    return bindings


def extract_issue_code(*texts: str | None) -> str | None:
    """Return the first ``ISS-NNNN`` code found across the given texts (title,
    head branch, body), upper-cased to match the canonical issue code."""
    for text in texts:
        if not text:
            continue
        m = _ISSUE_CODE.search(text)
        if m:
            return m.group(0).upper()
    return None


def referenced_issue_code(payload: dict) -> str | None:
    """The Issue code a ``pull_request`` payload names, or ``None``.

    Exposed so an HTTP route can authorize the caller against the Issue's Org
    *before* the event is allowed to transition anything.
    """
    pr = payload.get("pull_request") or {}
    return extract_issue_code(pr.get("title"), (pr.get("head") or {}).get("ref"), pr.get("body"))


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:].lower(), expected)


def process_webhook(
    raw_body: bytes,
    *,
    signature: str | None,
    delivery_id: str | None,
    event_type: str | None,
) -> dict:
    """Verify, scope, dedupe, execute, and persist one GitHub delivery."""
    secret = settings.github_webhook_secret
    bindings = _repository_bindings()
    if not secret or not bindings:
        raise GitHubWebhookDisabled(
            "GitHub webhook requires a secret and at least one repository-to-Org binding"
        )
    if not verify_signature(raw_body, signature, secret):
        raise GitHubWebhookAuthError("invalid GitHub webhook signature")
    if not delivery_id:
        raise GitHubWebhookPayloadError("X-GitHub-Delivery is required")
    if not event_type:
        raise GitHubWebhookPayloadError("X-GitHub-Event is required")
    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError) as exc:
        raise GitHubWebhookPayloadError("invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise GitHubWebhookPayloadError("payload must be an object")

    repository_payload = payload.get("repository")
    if not isinstance(repository_payload, dict):
        raise GitHubWebhookPayloadError("repository.full_name is required")
    repository = str(repository_payload.get("full_name") or "").strip()
    if not repository:
        raise GitHubWebhookPayloadError("repository.full_name is required")
    delivery, created = deliveries_ctl.claim(
        "github",
        "inbound",
        delivery_id,
        subject=repository or None,
        retry_failed=True,
    )
    if not created:
        if delivery["status"] == "processing":
            raise GitHubWebhookInProgress("GitHub delivery is already processing")
        prior = delivery.get("result") or {}
        return {**prior, "duplicate": True, "delivery_status": delivery["status"]}
    org_slug = bindings.get(repository.lower())
    if org_slug is None:
        result = {"accepted": False, "reason": "repository is outside the allowed scope"}
        deliveries_ctl.settle(
            delivery["id"],
            "rejected",
            attempt=delivery["attempts"],
            detail="repository_scope",
            result=result,
        )
        return result
    if event_type != "pull_request":
        result = {"accepted": True, "ignored": True, "event": event_type}
        deliveries_ctl.settle(
            delivery["id"],
            "ignored",
            attempt=delivery["attempts"],
            result=result,
        )
        return result
    try:
        result = handle_pull_request_event(
            payload,
            org_slug=org_slug,
            delivery_id=delivery_id,
        )
    except Exception:
        deliveries_ctl.settle(
            delivery["id"],
            "failed",
            attempt=delivery["attempts"],
            detail="pull_request_processing_failed",
        )
        raise
    result = {
        **result,
        "accepted": True,
        "repository": repository,
        "delivery_id": delivery_id,
    }
    deliveries_ctl.settle(
        delivery["id"],
        "completed",
        attempt=delivery["attempts"],
        result=result,
    )
    return result


def handle_pull_request_event(
    payload: dict,
    *,
    org_slug: str | None = None,
    delivery_id: str | None = None,
) -> dict:
    """Handle a GitHub ``pull_request`` webhook payload (F8).

    Links the PR to an issue by code; when the PR is merged, transitions the
    linked issue to ``done`` and posts a comment. Returns a summary describing
    what happened (no-op when no issue code is present). Never raises on a
    missing/closed-without-merge PR — only acts on real merges.
    """
    pr = payload.get("pull_request") or {}
    action = payload.get("action")
    number = pr.get("number")
    title = pr.get("title")
    body = pr.get("body")
    head = (pr.get("head") or {}).get("ref")
    merged = bool(pr.get("merged"))

    code = extract_issue_code(title, head, body)
    if code is None:
        return {"linked": False, "reason": "no issue code in PR title/branch/body"}

    issue = issues_ctl.get_issue(code)
    if issue is None:
        return {"linked": False, "reason": f"unknown issue {code}", "issue": code}
    if org_slug is not None:
        org = orgs_ctl.get_org(org_slug)
        project = projects_ctl.get_project(issue["project_id"])
        if org is None or project is None or project["org_id"] != org["id"]:
            return {
                "linked": False,
                "reason": "issue is outside the repository Org scope",
                "issue": code,
            }

    # Link is implicit (by code). Auto-Done only on a real merge.
    if action == "closed" and merged:
        moved = issue if issue["status"] == "done" else issues_ctl.transition(code, "done")
        comment = f"Closed by PR #{number} (merged)."
        if delivery_id is not None:
            comment = f"{comment} [GitHub delivery {delivery_id}]"
        if not issues_ctl.comment_exists(code, comment):
            issues_ctl.add_comment(code, comment, author_kind="system")
        return {"linked": True, "issue": code, "status": moved["status"], "merged": True}

    return {"linked": True, "issue": code, "merged": False, "action": action}

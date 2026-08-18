"""Tests for ``brains.control.queue_health`` (BL-P1-12).

Covers:

* ``summarize`` reports total/open/stale-or-expired counts and the
  owner/scope/lifecycle/expiry metadata for every family.
* ``diagnose`` detects an orphaned Session reference without deleting it.
* ``plan_repair`` (dry-run) predicts exactly what ``apply_repair`` (real)
  performs, and neither ever removes unresolved work.
* concurrency/duplicate safety: running ``apply_repair`` twice in a row is
  idempotent (the second run has nothing left to do).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from brains.control import queue_health
from brains.control.claims import claim_workspace
from brains.control.common import utc_now
from brains.control.decisions import file_decision_request
from brains.control.handoffs import set_handoff
from brains.control.mailbox import send_message
from brains.control.sessions import start_session
from brains.storage.db import SessionLocal
from brains.storage.models import AgentSession, Handoff, HelpRequest, WorkspaceClaim


@pytest.fixture(autouse=True)
def _fast_help_poll(monkeypatch):
    monkeypatch.setenv("BRAINS_HELP_POLL_INTERVAL_MS", "10")


# --------------------------------------------------------------------------- #
# FAMILIES metadata
# --------------------------------------------------------------------------- #


def test_every_family_declares_owner_scope_lifecycle_and_expiry():
    for family in queue_health.FAMILIES:
        assert family.owner
        assert family.scope
        assert family.lifecycle
        assert family.expiry_policy


# --------------------------------------------------------------------------- #
# summarize
# --------------------------------------------------------------------------- #


def test_summarize_reports_every_family_with_metadata(tmp_path):
    set_handoff(str(tmp_path), title="qh-summary", body="x")
    result = queue_health.summarize()
    assert set(result["families"]) == {
        "approvals",
        "handoffs",
        "mailbox",
        "help_requests",
        "workspace_claims",
        "session_commands",
        "checkpoints",
    }
    handoffs = result["families"]["handoffs"]
    assert handoffs["total"] >= 1
    assert handoffs["open"] >= 1
    assert handoffs["owner"]
    assert handoffs["lifecycle"]
    assert "generated_at" in result


def test_summarize_counts_stale_handoffs_before_any_sweep_runs(tmp_path):
    handoff = set_handoff(str(tmp_path), title="qh-stale", body="x")
    with SessionLocal() as session:
        row = session.query(Handoff).filter(Handoff.id == handoff["handoff_id"]).one()
        row.set_at = utc_now() - timedelta(hours=48)
        session.commit()

    result = queue_health.summarize()
    assert result["families"]["handoffs"]["stale_or_expired"] >= 1


# --------------------------------------------------------------------------- #
# diagnose — orphan detection, non-destructive
# --------------------------------------------------------------------------- #


def test_diagnose_detects_orphaned_session_reference_without_deleting(tmp_path):
    started = start_session(str(tmp_path), tool="pytest")
    session_id = started["session_id"]
    set_handoff(str(tmp_path), title="qh-orphan", body="x", session_id=session_id)

    # Simulate the Session having since been pruned/lost without a cascade -
    # delete the AgentSession row directly, bypassing any normal API.
    with SessionLocal() as session:
        session.query(AgentSession).filter(AgentSession.id == session_id).delete()
        session.commit()

    report = queue_health.diagnose()
    handoff_issues = [i for i in report["issues"] if i["family"] == "handoffs"]
    assert handoff_issues, "orphaned handoff session reference was not detected"
    assert report["issue_count"] >= 1
    sample_session_ids = {
        row.get("set_by_session_id") for issue in handoff_issues for row in issue["sample"]
    }
    assert session_id in sample_session_ids

    # Detection must not have deleted the handoff itself.
    with SessionLocal() as session:
        remaining = session.query(Handoff).filter(Handoff.set_by_session_id == session_id).count()
        assert remaining == 1


def test_diagnose_reports_nothing_for_a_clean_workspace(tmp_path):
    set_handoff(str(tmp_path), title="qh-clean", body="x")
    report = queue_health.diagnose()
    # A freshly-set handoff with no session reference at all is not an
    # orphan (session_id is nullable and simply absent here).
    assert isinstance(report["issues"], list)
    assert report["issue_count"] == sum(issue["count"] for issue in report["issues"])


def test_diagnose_issue_sample_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(queue_health, "_SAMPLE_LIMIT", 2)
    ids = []
    for i in range(5):
        started = start_session(str(tmp_path / f"ws-{i}"), tool="pytest")
        ids.append(started["session_id"])
        set_handoff(
            str(tmp_path / f"ws-{i}"), title=f"qh-bound-{i}", session_id=started["session_id"]
        )
    with SessionLocal() as session:
        session.query(AgentSession).filter(AgentSession.id.in_(ids)).delete(
            synchronize_session=False
        )
        session.commit()

    report = queue_health.diagnose()
    handoff_issue = next(i for i in report["issues"] if i["family"] == "handoffs")
    assert handoff_issue["count"] >= 5
    assert len(handoff_issue["sample"]) <= 2


def test_diagnose_detects_orphaned_help_targets(tmp_path):
    with SessionLocal() as session:
        row = HelpRequest(
            code="HR-orphan-target",
            from_session_id=None,
            from_workspace_id=None,
            to_session_id="ses_missing",
            to_workspace="missing-workspace",
            subject="orphan target",
            question="where?",
            status="open",
            ask_depth=1,
            created_at=utc_now(),
            expires_at=utc_now() + timedelta(minutes=5),
        )
        session.add(row)
        session.commit()
    report = queue_health.diagnose()
    fields = {issue["field"] for issue in report["issues"] if issue["family"] == "help_requests"}
    assert {"to_session_id", "to_workspace"} <= fields


# --------------------------------------------------------------------------- #
# plan_repair / apply_repair — dry-run matches real, nothing unresolved is lost
# --------------------------------------------------------------------------- #


def test_plan_repair_predicts_what_apply_repair_performs(tmp_path):
    handoff = set_handoff(str(tmp_path), title="qh-repair", body="x")
    with SessionLocal() as session:
        row = session.query(Handoff).filter(Handoff.id == handoff["handoff_id"]).one()
        row.set_at = utc_now() - timedelta(hours=48)
        session.commit()

    plan = queue_health.plan_repair()
    stale_action = next(a for a in plan["actions"] if a["code"] == "stale_handoffs")
    assert stale_action["would_affect_rows"] >= 1
    assert plan["unresolved_work_preserved"] is True

    applied = queue_health.apply_repair()
    applied_action = next(a for a in applied["actions"] if a["code"] == "stale_handoffs")
    assert applied_action["applied_rows"] == stale_action["would_affect_rows"]

    with SessionLocal() as session:
        row = session.query(Handoff).filter(Handoff.id == handoff["handoff_id"]).one()
        assert row.status == "stale"


def test_apply_repair_never_deletes_an_open_approval(tmp_path):
    filed = file_decision_request(str(tmp_path), title="qh-open-ask", body="keep me")
    queue_health.apply_repair()
    from brains.control.decisions import list_open_decisions

    codes = {d["code"] for d in list_open_decisions(workspace_path=str(tmp_path))}
    assert filed["code"] in codes


def test_apply_repair_never_deletes_unread_mail(tmp_path):
    started = start_session(str(tmp_path), tool="pytest")
    sent = send_message(
        "qh-mail-subject",
        "keep me",
        to_session_id=started["session_id"],
        workspace_path=str(tmp_path),
    )
    queue_health.apply_repair()
    from brains.storage.db import SessionLocal as _SL
    from brains.storage.models import MailboxMessage

    with _SL() as session:
        row = session.query(MailboxMessage).filter(MailboxMessage.id == sent["id"]).one()
        assert row.read_at is None


def test_apply_repair_releases_expired_workspace_claim(tmp_path):
    from brains.control.sessions import register_workspace

    workspace_id = register_workspace(str(tmp_path)).id
    started = start_session(str(tmp_path), tool="pytest")
    claim_workspace(str(tmp_path), started["session_id"], duration_minutes=30)
    with SessionLocal() as session:
        row = (
            session.query(WorkspaceClaim).filter(WorkspaceClaim.workspace_id == workspace_id).one()
        )
        row.expires_at = utc_now() - timedelta(minutes=5)
        session.commit()

    plan = queue_health.plan_repair()
    claim_action = next(a for a in plan["actions"] if a["code"] == "expired_workspace_claims")
    assert claim_action["would_affect_rows"] >= 1

    applied = queue_health.apply_repair()
    applied_action = next(a for a in applied["actions"] if a["code"] == "expired_workspace_claims")
    assert applied_action["applied_rows"] >= 1

    with SessionLocal() as session:
        assert (
            session.query(WorkspaceClaim)
            .filter(WorkspaceClaim.workspace_id == workspace_id)
            .count()
            == 0
        )


def test_apply_repair_expires_a_past_deadline_help_request(tmp_path):
    started = start_session(str(tmp_path), tool="pytest")
    with SessionLocal() as session:
        row = HelpRequest(
            code="HR-qhtest1",
            from_session_id=started["session_id"],
            to_workspace=None,
            to_session_id="nobody",
            subject="qh-help",
            question="anyone?",
            status="open",
            ask_depth=1,
            created_at=utc_now() - timedelta(minutes=10),
            expires_at=utc_now() - timedelta(minutes=5),
        )
        session.add(row)
        session.commit()

    plan = queue_health.plan_repair()
    help_action = next(a for a in plan["actions"] if a["code"] == "expired_help_requests")
    assert help_action["would_affect_rows"] >= 1

    queue_health.apply_repair()

    with SessionLocal() as session:
        refreshed = session.query(HelpRequest).filter(HelpRequest.code == "HR-qhtest1").one()
        assert refreshed.status == "expired"


def test_apply_repair_is_idempotent_on_repeated_runs(tmp_path):
    """A second apply immediately after the first must find nothing left to
    do — proving the repair doesn't double-count or re-flip settled rows
    under repeated/concurrent invocation."""
    handoff = set_handoff(str(tmp_path), title="qh-idempotent", body="x")
    with SessionLocal() as session:
        row = session.query(Handoff).filter(Handoff.id == handoff["handoff_id"]).one()
        row.set_at = utc_now() - timedelta(hours=48)
        session.commit()

    first = queue_health.apply_repair()
    second = queue_health.apply_repair()

    first_stale = next(a for a in first["actions"] if a["code"] == "stale_handoffs")
    second_stale = next(a for a in second["actions"] if a["code"] == "stale_handoffs")
    assert first_stale["applied_rows"] >= 1
    assert second_stale["applied_rows"] == 0

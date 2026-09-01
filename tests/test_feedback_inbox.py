"""Governed, privacy-safe agent-experience feedback inbox (BL-P1-15)."""

from __future__ import annotations

import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from brains.api.auth import mint_browser_token
from brains.authz.principal import CHANNEL_API
from brains.authz.resolver import bootstrap_principal
from brains.cli.app import app as cli_app
from brains.config import settings
from brains.control.feedback import (
    enrich_feedback,
    file_feedback,
    get_feedback,
    list_feedback,
    promote_feedback,
    triage_feedback,
)
from brains.control.memberships import set_workspace_visibility
from brains.control.queue_health import summarize
from brains.control.sessions import start_session
from brains.main import app
from brains.mcp import server as mcp_server
from brains.storage.db import SessionLocal
from brains.storage.integrity import workspace_cascade_tables
from brains.storage.models import (
    AgentTask,
    AuditLogEntry,
    FeedbackEnrichment,
    FeedbackPromotion,
    FeedbackReport,
    KnowledgeEntry,
)


def _report(tmp_path, **overrides):
    workspace = str(tmp_path / "repo")
    reporter = start_session(workspace, tool="opencode")
    values = {
        "workspace_path": workspace,
        "category": "defect",
        "severity": "high",
        "summary": f"Feedback failure {uuid.uuid4().hex[:8]}",
        "evidence": "tests/test_feedback_inbox.py",
        "reproduction": "run the focused test",
        "affected_version": "candidate",
        "surface": "mcp",
        "reporter_session_id": reporter["session_id"],
        "metadata": {"tool": "opencode"},
    }
    values.update(overrides)
    return reporter, file_feedback(**values)


def test_report_redacts_nested_metadata_before_persistence(tmp_path) -> None:
    reporter = start_session(str(tmp_path / "repo"), tool="opencode")
    secret = "AbCdEf0123456789AbCdEf0123456789"
    result = file_feedback(
        str(tmp_path / "repo"),
        "defect",
        "critical",
        f"request failed token={secret}",
        evidence=f"Authorization: Bearer {secret}",
        reproduction=f"API_KEY={secret} run command",
        reporter_session_id=reporter["session_id"],
        metadata={"api_key": secret, "nested": {"note": f"token={secret}"}},
    )

    encoded = json.dumps(result, sort_keys=True)
    assert secret not in encoded
    assert "<redacted>" in encoded
    with SessionLocal() as session:
        row = session.query(FeedbackReport).filter_by(code=result["code"]).one()
        assert secret not in " ".join(
            [row.summary, row.evidence, row.reproduction, row.metadata_json]
        )


def test_duplicate_report_links_to_canonical_and_dedupes_enrichment(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    first_session = start_session(workspace, tool="opencode")
    second_session = start_session(workspace, tool="claude")
    summary = f"same failure {uuid.uuid4().hex[:8]}"
    first = file_feedback(
        workspace,
        "defect",
        "medium",
        summary,
        reporter_session_id=first_session["session_id"],
    )
    duplicate = file_feedback(
        workspace,
        "defect",
        "high",
        summary.upper(),
        evidence="new evidence",
        reporter_session_id=second_session["session_id"],
    )
    retry = file_feedback(
        workspace,
        "defect",
        "high",
        summary.upper(),
        evidence="new evidence",
        reporter_session_id=second_session["session_id"],
    )

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["code"] == first["code"]
    assert duplicate["enrichment_id"] is not None
    assert retry["enrichment_duplicate"] is True
    with SessionLocal() as session:
        assert session.query(FeedbackReport).filter_by(code=first["code"]).count() == 1
        report = session.query(FeedbackReport).filter_by(code=first["code"]).one()
        assert (
            session.query(FeedbackEnrichment).filter_by(feedback_report_id=report.id).count() == 1
        )


def test_enrichment_requires_live_session_in_same_workspace(tmp_path) -> None:
    reporter, report = _report(tmp_path)
    other = start_session(str(tmp_path / "other"), tool="claude")
    with pytest.raises(ValueError, match="must match"):
        enrich_feedback(
            report["code"],
            reporter_session_id=other["session_id"],
            evidence="wrong workspace",
        )

    first = enrich_feedback(
        report["code"],
        reporter_session_id=reporter["session_id"],
        note="same note",
    )
    retry = enrich_feedback(
        report["code"],
        reporter_session_id=reporter["session_id"],
        note="same note",
    )
    assert first["duplicate"] is False
    assert retry["duplicate"] is True


def test_triage_is_human_only_and_idempotent(tmp_path) -> None:
    _reporter, report = _report(tmp_path)
    with pytest.raises(PermissionError, match="human-bound"):
        triage_feedback(
            report["code"],
            "triaged",
            principal=bootstrap_principal(channel=CHANNEL_API),
        )

    first = triage_feedback(
        report["code"],
        "triaged",
        note="accepted for review",
        principal=bootstrap_principal(),
    )
    retry = triage_feedback(
        report["code"],
        "triaged",
        note="accepted for review",
        principal=bootstrap_principal(),
    )
    assert first["duplicate"] is False
    assert retry["duplicate"] is True


@pytest.mark.parametrize("target", ["task", "knowledge"])
def test_promotion_is_exactly_once_and_audit_correlated(tmp_path, target) -> None:
    _reporter, report = _report(tmp_path, severity="critical")
    first = promote_feedback(report["code"], target, principal=bootstrap_principal())
    retry = promote_feedback(report["code"], target, principal=bootstrap_principal())

    assert first["duplicate"] is False
    assert retry["duplicate"] is True
    assert retry["target_ref"] == first["target_ref"]
    with SessionLocal() as session:
        stored = session.query(FeedbackReport).filter_by(code=report["code"]).one()
        promotion = session.get(FeedbackPromotion, stored.id)
        assert promotion.target_ref == first["target_ref"]
        assert session.get(AuditLogEntry, promotion.audit_entry_id) is not None
        if target == "task":
            rows = session.query(AgentTask).filter_by(code=first["target_ref"]).all()
        else:
            rows = session.query(KnowledgeEntry).filter_by(code=first["target_ref"]).all()
        assert len(rows) == 1


def test_concurrent_task_promotion_creates_one_target(tmp_path) -> None:
    _reporter, report = _report(tmp_path)
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[Exception] = []

    def promote() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                promote_feedback(report["code"], "task", principal=bootstrap_principal())
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=promote) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    assert len(results) == 2
    assert {row["target_ref"] for row in results} == {results[0]["target_ref"]}
    assert sorted(row["duplicate"] for row in results) == [False, True]
    with SessionLocal() as session:
        assert session.query(AgentTask).filter_by(code=results[0]["target_ref"]).count() == 1


def test_backlog_promotion_links_existing_reference_without_editing_docs(tmp_path) -> None:
    _reporter, report = _report(tmp_path)
    result = promote_feedback(
        report["code"],
        "backlog",
        backlog_ref="BL-P1-15",
        principal=bootstrap_principal(),
    )
    assert result["target_ref"] == "BL-P1-15"
    with pytest.raises(ValueError, match="BL-PN-NN"):
        _reporter2, report2 = _report(tmp_path / "second")
        promote_feedback(
            report2["code"],
            "backlog",
            backlog_ref="invented",
            principal=bootstrap_principal(),
        )


def test_feedback_visibility_queue_health_and_workspace_cascade(tmp_path) -> None:
    _reporter, report = _report(tmp_path)
    set_workspace_visibility(report["workspace"], "private")
    assert get_feedback(report["code"]) is not None  # bootstrap admin remains visible
    assert summarize()["families"]["feedback"]["open"] >= 1
    with SessionLocal() as session:
        raw = session.connection().connection
        connection = getattr(raw, "driver_connection", None) or getattr(raw, "connection", raw)
        steps = workspace_cascade_tables(connection)
    order = {step.table: index for index, step in enumerate(steps)}
    assert order["feedback_promotions"] < order["feedback_reports"]
    assert order["feedback_enrichments"] < order["feedback_reports"]


def test_http_cli_and_mcp_authority_boundaries(tmp_path) -> None:
    workspace = str(tmp_path / "repo")
    reporter = start_session(workspace, tool="opencode")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    slug = reporter["workspace"]
    response = client.post(
        f"/v1/operator/workspaces/{slug}/feedback",
        headers=headers,
        json={
            "category": "friction",
            "severity": "medium",
            "summary": "HTTP report",
            "reporter_session_id": reporter["session_id"],
        },
    )
    assert response.status_code == 200, response.text
    code = response.json()["code"]
    raw_triage = client.post(
        f"/v1/operator/feedback/{code}/triage",
        headers=headers,
        json={"status": "triaged"},
    )
    assert raw_triage.status_code == 403

    browser = TestClient(app)
    browser.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    triaged = browser.post(
        f"/v1/operator/feedback/{code}/triage",
        json={"status": "triaged", "note": "browser accepted"},
    )
    assert triaged.status_code == 200, triaged.text

    cli = CliRunner().invoke(cli_app, ["feedback-get", code])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.stdout)["code"] == code
    assert {"feedback_report", "feedback_enrich", "feedback_get", "feedback_list"} <= set(
        mcp_server.TOOL_REGISTRY
    )
    assert "feedback_triage" not in mcp_server.TOOL_REGISTRY
    assert "feedback_promote" not in mcp_server.TOOL_REGISTRY


def test_list_and_detail_include_linked_enrichments_and_promotion(tmp_path) -> None:
    reporter, report = _report(tmp_path)
    enrich_feedback(
        report["code"],
        reporter_session_id=reporter["session_id"],
        evidence="follow-up",
    )
    promote_feedback(report["code"], "task", principal=bootstrap_principal())

    listed = next(
        row for row in list_feedback(str(tmp_path / "repo")) if row["code"] == report["code"]
    )
    detail = get_feedback(report["code"])
    assert listed["enrichments"] == 1
    assert listed["promotion"]["target_kind"] == "task"
    assert detail is not None
    assert detail["enrichment_rows"][0]["evidence"] == "follow-up"

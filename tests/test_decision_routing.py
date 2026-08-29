"""Human-owned approval assignment, deadline, and escalation routing."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from brains.api.auth import mint_browser_token
from brains.authz.principal import CHANNEL_API
from brains.authz.resolver import (
    bootstrap_principal,
    principal_for_operator_slug,
)
from brains.cli.app import app as cli_app
from brains.config import settings
from brains.control import orgs as orgs_ctl
from brains.control.decisions import (
    ApprovalAuthorizationError,
    escalate_decision,
    file_decision_request,
    get_decision,
    list_open_decisions,
    resolve_decision,
    route_decision,
)
from brains.control.memberships import set_workspace_visibility
from brains.control.operators import add_operator, ensure_admin_operator
from brains.control.queue_health import summarize
from brains.main import app
from brains.mcp import server as mcp_server
from brains.storage.db import SessionLocal
from brains.storage.integrity import workspace_cascade_tables
from brains.storage.models import ApprovalRequest, ApprovalRouting, AuditLogEntry, Event


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_route_assigns_org_member_and_is_idempotent(tmp_path) -> None:
    ensure_admin_operator()
    target, _key = add_operator(_slug("approver"))
    org = orgs_ctl.ensure_default_org()
    orgs_ctl.add_member(org["id"], target["slug"], role="member")
    filed = file_decision_request(str(tmp_path), "route me")
    due = datetime.now(UTC) + timedelta(hours=1)

    first = route_decision(
        filed["code"],
        assigned_operator=target["slug"],
        priority="p1",
        due_at=due,
        principal=bootstrap_principal(),
    )
    retry = route_decision(
        filed["code"],
        assigned_operator=target["slug"],
        priority="p1",
        due_at=due,
        principal=bootstrap_principal(),
    )

    assert first["assigned_operator"] == target["slug"]
    assert first["priority"] == "p1"
    assert first["duplicate"] is False
    assert retry["duplicate"] is True
    listed = next(row for row in list_open_decisions(str(tmp_path)) if row["code"] == filed["code"])
    assert listed["assigned_operator"] == target["slug"]
    with SessionLocal() as session:
        request = session.query(ApprovalRequest).filter_by(code=filed["code"]).one()
        assert session.query(ApprovalRouting).filter_by(approval_request_id=request.id).count() == 1
        assert session.query(AuditLogEntry).filter_by(action="approval.routed").count() >= 1
        assert session.query(Event).filter_by(kind="decision_routed").count() >= 1


def test_escalation_increments_and_preserves_route_fields(tmp_path) -> None:
    filed = file_decision_request(str(tmp_path), "escalate me")
    due = datetime.now(UTC) - timedelta(minutes=1)
    route_decision(
        filed["code"],
        assigned_operator="admin",
        priority="p0",
        due_at=due,
        principal=bootstrap_principal(),
    )

    first = escalate_decision(
        filed["code"],
        reason="deadline missed",
        principal=bootstrap_principal(),
    )
    second = escalate_decision(
        filed["code"],
        reason="still blocked",
        principal=bootstrap_principal(),
    )

    assert first["escalation_level"] == 1
    assert second["escalation_level"] == 2
    assert second["assigned_operator"] == "admin"
    assert second["priority"] == "p0"
    assert second["due_at"] is not None
    assert second["overdue"] is True
    assert summarize()["families"]["approvals"]["stale_or_expired"] >= 1


def test_concurrent_escalations_each_increment_and_audit_committed_level(tmp_path) -> None:
    filed = file_decision_request(str(tmp_path), "concurrent escalation")
    route_decision(filed["code"], priority="p1", principal=bootstrap_principal())
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[Exception] = []

    def escalate(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                escalate_decision(
                    filed["code"],
                    reason=f"concurrent reason {index}",
                    principal=bootstrap_principal(),
                )
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=escalate, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sorted(row["escalation_level"] for row in results) == [1, 2]
    assert get_decision(filed["code"])["escalation_level"] == 2
    with SessionLocal() as session:
        levels = sorted(
            json.loads(row.payload_json)["payload"]["escalation_level"]
            for row in session.query(AuditLogEntry)
            .filter(AuditLogEntry.action == "approval.escalated")
            .all()
            if json.loads(row.payload_json)["payload"].get("code") == filed["code"]
        )
    assert levels == [1, 2]


def test_routing_requires_human_channel_and_org_member_assignee(tmp_path) -> None:
    filed = file_decision_request(str(tmp_path), "govern route")
    api_principal = bootstrap_principal(channel=CHANNEL_API)
    with pytest.raises(ApprovalAuthorizationError, match="human-bound"):
        route_decision(filed["code"], priority="p1", principal=api_principal)

    outsider, _key = add_operator(_slug("outsider"))
    with pytest.raises(ValueError, match="not a member"):
        route_decision(
            filed["code"],
            assigned_operator=outsider["slug"],
            principal=bootstrap_principal(),
        )


def test_private_workspace_routing_requires_visibility(tmp_path) -> None:
    ensure_admin_operator()
    operator, _key = add_operator(_slug("router"))
    org = orgs_ctl.ensure_default_org()
    orgs_ctl.add_member(org["id"], operator["slug"], role="member")
    filed = file_decision_request(str(tmp_path), "private route")
    workspace = next(
        row for row in list_open_decisions(str(tmp_path)) if row["code"] == filed["code"]
    )
    set_workspace_visibility(workspace["workspace"], "private")
    principal = principal_for_operator_slug(operator["slug"])
    assert principal is not None

    with pytest.raises(ApprovalAuthorizationError, match="cannot route"):
        route_decision(filed["code"], priority="p1", principal=principal)


def test_routing_rejects_decrease_and_closed_approval(tmp_path) -> None:
    filed = file_decision_request(str(tmp_path), "monotonic")
    route_decision(
        filed["code"],
        escalation_level=1,
        escalation_reason="first escalation",
        principal=bootstrap_principal(),
    )
    with pytest.raises(ValueError, match="cannot decrease"):
        route_decision(
            filed["code"],
            escalation_level=0,
            principal=bootstrap_principal(),
        )
    resolve_decision(filed["code"], "approve", principal=bootstrap_principal())
    with pytest.raises(ValueError, match="not open"):
        route_decision(filed["code"], priority="p2", principal=bootstrap_principal())


def test_browser_can_route_but_raw_api_key_cannot(tmp_path) -> None:
    ensure_admin_operator()
    filed = file_decision_request(str(tmp_path), "browser route")
    raw = TestClient(app).post(
        f"/v1/approvals/{filed['code']}/route",
        json={"priority": "p1"},
        headers={"Authorization": f"Bearer {settings.api_key}"},
    )
    assert raw.status_code == 403

    browser = TestClient(app)
    browser.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    response = browser.post(
        f"/v1/approvals/{filed['code']}/route",
        json={"assigned_operator": "admin", "priority": "p1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["assigned_operator"] == "admin"


def test_cli_and_mcp_routing_surfaces_are_wired(tmp_path) -> None:
    filed = file_decision_request(str(tmp_path), "cli route")
    result = CliRunner().invoke(
        cli_app,
        [
            "decision-route",
            "--code",
            filed["code"],
            "--assigned-operator",
            "admin",
            "--priority",
            "p1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["assigned_operator"] == "admin"
    assert {"route_decision", "escalate_decision"} <= set(mcp_server.TOOL_REGISTRY)
    assert get_decision(filed["code"])["priority"] == "p1"


def test_workspace_cascade_includes_approval_routing() -> None:
    with SessionLocal() as session:
        raw = session.connection().connection
        connection = getattr(raw, "driver_connection", None) or getattr(raw, "connection", raw)
        steps = workspace_cascade_tables(connection)
    order = {step.table: index for index, step in enumerate(steps)}
    assert order["approval_routing"] < order["approval_requests"]

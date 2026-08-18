"""Issue execution evidence and deterministic dispatch (F4, BL-P1-02).

These are the reconciliation checks AC-F4-04/05 name: the rollup is read from
persisted rows, counted once per row, and says plainly what it cannot attribute.
AC-F4-02/03/06 are covered by the deterministic dispatch cases, and AC-F4-07 by
the structured-only creation contract.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from fastapi.testclient import TestClient

from brains.control import issue_evidence as evidence_ctl
from brains.control import issues as issues_ctl
from brains.control import orgs as orgs_ctl
from brains.control import personas as personas_ctl
from brains.control import pods as pods_ctl
from brains.control import projects as projects_ctl
from brains.control import runtimes as runtimes_ctl
from brains.control.operators import add_operator, ensure_admin_operator
from brains.main import app

AUTH = {"Authorization": "Bearer local-dev-key"}


@pytest.fixture(autouse=True)
def _bootstrap():
    from brains.storage.migrations import init_db

    init_db()
    ensure_admin_operator()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def org():
    return orgs_ctl.create_org(_slug("org"), "Acme")


@pytest.fixture
def runtime(org, tmp_path):
    return runtimes_ctl.register_runtime(
        f"machine-{uuid.uuid4().hex[:8]}",
        "copilot",
        org_id=org["id"],
        working_root=str(tmp_path),
        status="online",
    )


@pytest.fixture
def persona(org, runtime):
    return personas_ctl.create_persona(
        org["id"],
        _slug("p"),
        "Forge",
        model="claude-opus-4.8",
        tool="copilot",
        default_runtime_id=runtime["id"],
    )


@pytest.fixture
def issue(org):
    project = projects_ctl.create_project(org["id"], _slug("proj"), "Proj")
    return issues_ctl.create_issue(project["id"], "Fix the thruster", body="broken")


# --------------------------------------------------------------------------- #
# Deterministic dispatch semantics
# --------------------------------------------------------------------------- #


def test_unassigned_issue_reports_a_stable_blocked_reason(issue):
    plan = evidence_ctl.dispatch_plan(issue["code"])
    assert plan["dispatchable"] is False
    assert plan["blocked_reason"] == "unassigned"
    assert plan["blocked_reason"] in evidence_ctl.DISPATCH_BLOCKED_REASONS


def test_operator_assignment_is_not_dispatchable(issue):
    with contextlib.suppress(Exception):
        add_operator("carol")
    issues_ctl.assign(issue["code"], operator="carol")
    plan = evidence_ctl.dispatch_plan(issue["code"])
    assert plan["blocked_reason"] == "assigned_to_operator"
    assert plan["assignee_kind"] == "operator"


def test_offline_runtime_blocks_dispatch_with_its_own_reason(issue, persona, runtime):
    runtimes_ctl.mark_offline(runtime["id"])
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    plan = evidence_ctl.dispatch_plan(issue["code"])
    assert plan["blocked_reason"] == "runtime_offline"
    with pytest.raises(evidence_ctl.IssueEvidenceError):
        evidence_ctl.dispatch(issue["code"])


def test_persona_without_runtime_blocks_dispatch(org, issue):
    persona = personas_ctl.create_persona(org["id"], _slug("p"), "Bare")
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    plan = evidence_ctl.dispatch_plan(issue["code"])
    assert plan["blocked_reason"] == "persona_no_runtime"


def test_tool_mismatch_blocks_dispatch(org, runtime, issue):
    persona = personas_ctl.create_persona(
        org["id"], _slug("p"), "Mismatch", tool="claude", default_runtime_id=runtime["id"]
    )
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    assert evidence_ctl.dispatch_plan(issue["code"])["blocked_reason"] == "runtime_tool_mismatch"


def test_closed_issue_is_not_dispatchable(issue, persona):
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    issues_ctl.transition(issue["code"], "done")
    assert evidence_ctl.dispatch_plan(issue["code"])["blocked_reason"] == "issue_closed"


def test_dispatch_is_idempotent_while_an_attempt_is_in_flight(issue, persona):
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    first = evidence_ctl.dispatch(issue["code"])
    second = evidence_ctl.dispatch(issue["code"])

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["session_id"] == first["session_id"]
    rollup = evidence_ctl.rollup(issue["code"])
    assert rollup["totals"]["sessions"] == 1, "a retried dispatch spawned a second session"


def test_archived_persona_cannot_be_assigned(issue, persona):
    personas_ctl.archive(persona["id"])
    with pytest.raises(ValueError, match="archived"):
        issues_ctl.assign(issue["code"], persona_id=persona["id"])


# --------------------------------------------------------------------------- #
# Pod dispatch (AC-F4-06)
# --------------------------------------------------------------------------- #


def test_pod_assignment_resolves_to_a_capable_member(org, runtime, persona, issue):
    idle = personas_ctl.create_persona(org["id"], _slug("p"), "Idle", tool="copilot")
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=idle["id"])
    pods_ctl.add_member(pod["id"], persona["id"])
    issues_ctl.assign(issue["code"], pod_id=pod["id"])

    plan = evidence_ctl.dispatch_plan(issue["code"])
    assert plan["assignee_kind"] == "pod"
    assert plan["dispatchable"] is True
    # The leader is considered first and skipped with its own reason.
    assert plan["candidates"][0]["persona_id"] == idle["id"]
    assert plan["candidates"][0]["blocked_reason"] == "persona_no_runtime"
    assert plan["persona_id"] == persona["id"]

    result = evidence_ctl.dispatch(issue["code"])
    rollup = evidence_ctl.rollup(issue["code"])
    assert rollup["sessions"][0]["id"] == result["session_id"]
    assert rollup["sessions"][0]["persona_id"] == persona["id"]


def test_pod_with_no_capable_member_reports_every_candidate(org, issue):
    a = personas_ctl.create_persona(org["id"], _slug("p"), "A", tool="copilot")
    b = personas_ctl.create_persona(org["id"], _slug("p"), "B", tool="copilot")
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=a["id"])
    pods_ctl.add_member(pod["id"], b["id"])
    issues_ctl.assign(issue["code"], pod_id=pod["id"])

    plan = evidence_ctl.dispatch_plan(issue["code"])
    assert plan["blocked_reason"] == "pod_no_capable_member"
    assert {c["persona_id"] for c in plan["candidates"]} == {a["id"], b["id"]}
    assert all(c["blocked_reason"] == "persona_no_runtime" for c in plan["candidates"])


def test_archived_pod_cannot_be_assigned(org, persona, issue):
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=persona["id"])
    pods_ctl.archive_pod(pod["id"])
    with pytest.raises(ValueError, match="archived"):
        issues_ctl.assign(issue["code"], pod_id=pod["id"])


# --------------------------------------------------------------------------- #
# Rollup reconciliation (AC-F4-04, AC-F4-05)
# --------------------------------------------------------------------------- #


def test_rollup_counts_each_event_once(issue, persona):
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    evidence_ctl.dispatch(issue["code"])
    rollup = evidence_ctl.rollup(issue["code"])

    total_by_kind = sum(rollup["events"]["by_kind"].values())
    assert total_by_kind == rollup["events"]["total"]
    # ``issue_created``/``issue_assigned`` name the Issue in metadata and carry
    # no Session; the dispatch events are Session-bound. Both are present once.
    assert rollup["events"]["by_kind"]["issue_created"] == 1
    assert rollup["events"]["by_kind"]["issue_assigned"] == 1
    assert rollup["events"]["by_kind"]["issue_dispatched"] == 1


def test_rollup_reports_unattributed_usage_rather_than_zero_cost(issue, persona):
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    evidence_ctl.dispatch(issue["code"])
    usage = evidence_ctl.rollup(issue["code"])["usage"]

    assert usage["attributed_calls"] == 0
    assert usage["sessions_without_usage"] == 1
    assert "identified their Session" in usage["attribution"]


def test_rollup_sums_attributed_usage_exactly_once(issue, persona):
    from brains.router import savings

    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    result = evidence_ctl.dispatch(issue["code"])
    session_id = result["session_id"]

    entry = savings.record_usage(
        endpoint="openai.chat",
        requested_model="m",
        routed_model="m",
        provider="echo",
        input_tokens=11,
        output_tokens=7,
        session_id=session_id,
    )
    assert entry is not None
    # A retried attribution of the same ledger row must not double-count.
    savings._attribute_usage(entry.id, session_id)

    usage = evidence_ctl.rollup(issue["code"])["usage"]
    assert usage["attributed_calls"] == 1
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 7
    assert usage["sessions_with_usage"] == 1


def test_usage_for_an_unknown_session_is_not_attributed(issue, persona):
    from brains.router import savings

    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    evidence_ctl.dispatch(issue["code"])
    savings.record_usage(
        endpoint="openai.chat",
        requested_model="m",
        routed_model="m",
        provider="echo",
        input_tokens=5,
        output_tokens=5,
        session_id="ses_not_a_real_session",
    )
    assert evidence_ctl.rollup(issue["code"])["usage"]["attributed_calls"] == 0


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


def test_evidence_route_returns_the_rollup(client, issue, persona):
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    client.post(f"/v1/issues/{issue['code']}/dispatch", headers=AUTH)
    resp = client.get(f"/v1/issues/{issue['code']}/evidence", headers=AUTH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["issue_code"] == issue["code"]
    assert body["totals"]["sessions"] == 1
    assert body["assignment"]["assignee_kind"] == "persona"
    assert body["links"]["sessions"] == [body["sessions"][0]["id"]]


def test_evidence_route_requires_auth(client, issue):
    assert client.get(f"/v1/issues/{issue['code']}/evidence").status_code in (401, 403)


def test_dispatch_refusal_carries_the_blocked_reason(client, issue):
    resp = client.post(f"/v1/issues/{issue['code']}/dispatch", headers=AUTH)
    assert resp.status_code == 400
    detail = resp.json()["error"]["message"]
    assert "unassigned" in str(detail)


def test_dispatch_route_is_idempotent(client, issue, persona):
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    first = client.post(f"/v1/issues/{issue['code']}/dispatch", headers=AUTH).json()
    second = client.post(f"/v1/issues/{issue['code']}/dispatch", headers=AUTH).json()
    assert second["session_id"] == first["session_id"]
    assert second["duplicate"] is True


def test_issue_creation_is_structured_only(client, org):
    """AC-F4-07: creation takes named fields; there is no prose-to-Issue route."""
    project = projects_ctl.create_project(org["id"], _slug("proj"), "Proj")
    missing_title = client.post(
        f"/v1/projects/{project['code']}/issues", json={"body": "just prose"}, headers=AUTH
    )
    assert missing_title.status_code == 422

    paths = {route.path for route in app.routes if getattr(route, "path", None)}
    for candidate in ("/v1/issues/parse", "/v1/issues/from-text", "/v1/issues/natural"):
        assert candidate not in paths, f"{candidate} would be an unreviewed prose parser"


def test_cross_org_issue_evidence_is_not_found(client):
    """An Issue in an Org the principal cannot read stays a 404, not a 403."""
    from brains.control.operators import add_operator

    outsider_slug = _slug("outsider")
    _record, key = add_operator(outsider_slug)
    outsider = {"Authorization": f"Bearer {key}"}

    hidden_org = orgs_ctl.create_org(_slug("hidden"), "Hidden")
    project = projects_ctl.create_project(hidden_org["id"], _slug("proj"), "Hidden Proj")
    hidden = issues_ctl.create_issue(project["id"], "Hidden work")

    for path in ("evidence", "dispatch-plan"):
        resp = client.get(f"/v1/issues/{hidden['code']}/{path}", headers=outsider)
        assert resp.status_code == 404, f"{path}: {resp.status_code} {resp.text}"
    assert client.post(f"/v1/issues/{hidden['code']}/dispatch", headers=outsider).status_code == 404

"""Fresh-state onboarding: durable attempts, resume, retry, truthful outcome (F6).

These cover AC-F6-01 through AC-F6-05: the fresh-state decision is derived from
the store, the attempt survives a reload, every step supports retry and the
machine step supports an explicit defer, and completion is stamped only when a
real Session exists for the attempt's Issue.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from brains.control import issue_evidence as evidence_ctl
from brains.control import issues as issues_ctl
from brains.control import onboarding as onboarding_ctl
from brains.control import orgs as orgs_ctl
from brains.control import personas as personas_ctl
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
def fresh_store(monkeypatch):
    """An install with no Org and no Persona, as a brand-new one is."""
    monkeypatch.setattr(onboarding_ctl, "_install_is_fresh", lambda _session: True)
    return True


def _operator() -> str:
    slug = _slug("op")
    add_operator(slug)
    return slug


# --------------------------------------------------------------------------- #
# Fresh-state decision (AC-F6-01)
# --------------------------------------------------------------------------- #


def test_fresh_store_requires_onboarding_and_opens_an_attempt(fresh_store):
    operator = _operator()
    state = onboarding_ctl.state(operator=operator)

    assert state["required"] is True
    assert state["fresh_install"] is True
    assert state["attempt"]["status"] == "in_progress"
    assert state["attempt"]["current_step"] == "org"
    assert [step["step"] for step in state["attempt"]["steps"]] == list(onboarding_ctl.STEPS)


def test_a_working_install_is_not_owed_onboarding(org_with_persona):
    operator = _operator()
    state = onboarding_ctl.state(operator=operator)

    assert state["fresh_install"] is False
    assert state["required"] is False
    # Reading the state on a working install must not manufacture an attempt.
    assert state["attempt"] is None


def test_a_deliberate_exit_is_not_undone_by_the_guard(fresh_store):
    operator = _operator()
    attempt = onboarding_ctl.state(operator=operator)["attempt"]
    onboarding_ctl.abandon(attempt["attempt_id"])

    state = onboarding_ctl.state(operator=operator)
    assert state["fresh_install"] is True
    assert state["required"] is False, "an abandoned attempt must not trap the operator"


def test_attempts_are_per_operator(fresh_store):
    first = onboarding_ctl.state(operator=_operator())["attempt"]["attempt_id"]
    second = onboarding_ctl.state(operator=_operator())["attempt"]["attempt_id"]
    assert first != second


# --------------------------------------------------------------------------- #
# Resume, retry, defer (AC-F6-02, AC-F6-03)
# --------------------------------------------------------------------------- #


@pytest.fixture
def org_with_persona(tmp_path):
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = runtimes_ctl.register_runtime(
        f"machine-{uuid.uuid4().hex[:8]}",
        "copilot",
        org_id=org["id"],
        working_root=str(tmp_path),
        status="online",
    )
    persona = personas_ctl.create_persona(
        org["id"], _slug("p"), "Forge", tool="copilot", default_runtime_id=runtime["id"]
    )
    return org, runtime, persona


def test_reading_the_state_again_resumes_the_same_attempt(fresh_store):
    operator = _operator()
    first = onboarding_ctl.state(operator=operator)["attempt"]
    second = onboarding_ctl.state(operator=operator)["attempt"]
    assert second["attempt_id"] == first["attempt_id"]


def test_retrying_a_step_updates_one_row(fresh_store):
    operator = _operator()
    attempt_id = onboarding_ctl.state(operator=operator)["attempt"]["attempt_id"]

    onboarding_ctl.record_step(attempt_id, "runtime", status="failed", error="daemon offline")
    view = onboarding_ctl.record_step(attempt_id, "runtime", status="failed", error="still offline")

    runtime_steps = [step for step in view["steps"] if step["step"] == "runtime"]
    assert len(runtime_steps) == 1
    assert runtime_steps[0]["attempts"] == 2
    assert runtime_steps[0]["error"] == "still offline"


def test_deferring_the_machine_is_a_recorded_outcome(fresh_store):
    operator = _operator()
    attempt_id = onboarding_ctl.state(operator=operator)["attempt"]["attempt_id"]
    onboarding_ctl.record_step(attempt_id, "org", entity_ref="acme")

    view = onboarding_ctl.record_step(attempt_id, "runtime", status="deferred")
    deferred = next(step for step in view["steps"] if step["step"] == "runtime")
    assert deferred["status"] == "deferred"
    # The flow moves on rather than stalling on the deferred step.
    assert view["current_step"] == "persona"


def test_only_the_machine_step_may_be_deferred(fresh_store):
    operator = _operator()
    attempt_id = onboarding_ctl.state(operator=operator)["attempt"]["attempt_id"]
    with pytest.raises(onboarding_ctl.OnboardingError, match="only the runtime step"):
        onboarding_ctl.record_step(attempt_id, "persona", status="deferred")


def test_unknown_step_or_status_is_refused(fresh_store):
    operator = _operator()
    attempt_id = onboarding_ctl.state(operator=operator)["attempt"]["attempt_id"]
    with pytest.raises(onboarding_ctl.OnboardingError):
        onboarding_ctl.record_step(attempt_id, "not-a-step")
    with pytest.raises(onboarding_ctl.OnboardingError):
        onboarding_ctl.record_step(attempt_id, "org", status="maybe")


# --------------------------------------------------------------------------- #
# Truthful outcome (AC-F6-04)
# --------------------------------------------------------------------------- #


def _run_to_work(attempt_id: str, org, persona, *, runtime=None):
    onboarding_ctl.record_step(attempt_id, "org", org_id=org["id"], entity_ref=org["slug"])
    if runtime is None:
        onboarding_ctl.record_step(attempt_id, "runtime", status="deferred")
    else:
        onboarding_ctl.record_step(attempt_id, "runtime", runtime_id=runtime["id"])
    onboarding_ctl.record_step(attempt_id, "persona", persona_id=persona["id"])
    project = projects_ctl.create_project(org["id"], _slug("proj"), "Proj")
    issue = issues_ctl.create_issue(project["id"], "First real task")
    issues_ctl.assign(issue["code"], persona_id=persona["id"])
    onboarding_ctl.record_step(
        attempt_id, "work", project_id=project["id"], issue_id=issue["id"], entity_ref=issue["code"]
    )
    return issue


def test_a_dispatched_session_completes_the_attempt(fresh_store, org_with_persona):
    org, runtime, persona = org_with_persona
    operator = _operator()
    attempt_id = onboarding_ctl.state(operator=operator)["attempt"]["attempt_id"]
    issue = _run_to_work(attempt_id, org, persona, runtime=runtime)

    result = evidence_ctl.dispatch(issue["code"])
    view = onboarding_ctl.record_step(attempt_id, "dispatch", session_id=result["session_id"])

    assert view["status"] == "completed"
    assert view["entities"]["session"]["id"] == result["session_id"]
    assert view["blocked_reason"] is None
    assert view["completed_at"] is not None


def test_a_deferred_machine_ends_blocked_with_a_recovery_action(fresh_store, org_with_persona):
    org, _runtime, _persona = org_with_persona
    bare = personas_ctl.create_persona(org["id"], _slug("p"), "Bare")
    operator = _operator()
    attempt_id = onboarding_ctl.state(operator=operator)["attempt"]["attempt_id"]
    issue = _run_to_work(attempt_id, org, bare)

    with pytest.raises(evidence_ctl.IssueEvidenceError):
        evidence_ctl.dispatch(issue["code"])
    view = onboarding_ctl.record_step(
        attempt_id, "dispatch", status="failed", error="persona_no_runtime"
    )

    assert view["status"] == "blocked"
    assert view["blocked_reason"] == "dispatch_refused"
    assert view["blocked_reason"] in onboarding_ctl.BLOCKED_REASONS
    assert view["recovery"]["route"] == "/issues"
    assert view["entities"]["session"] is None


def test_an_offline_runtime_blocks_rather_than_completes(fresh_store, org_with_persona):
    org, runtime, persona = org_with_persona
    operator = _operator()
    attempt_id = onboarding_ctl.state(operator=operator)["attempt"]["attempt_id"]
    _run_to_work(attempt_id, org, persona, runtime=runtime)
    runtimes_ctl.mark_offline(runtime["id"])

    view = onboarding_ctl.get_attempt(attempt_id)
    assert view["status"] == "blocked"
    assert view["blocked_reason"] == "runtime_unavailable"
    assert view["recovery"]["route"] == "/runtimes"


def test_a_dispatch_marked_done_without_a_session_is_not_a_completion(
    fresh_store, org_with_persona
):
    org, runtime, _persona = org_with_persona
    bare = personas_ctl.create_persona(org["id"], _slug("p"), "Bare")
    operator = _operator()
    attempt_id = onboarding_ctl.state(operator=operator)["attempt"]["attempt_id"]
    # The machine is connected and online, so the only thing missing is the
    # Session itself — which is exactly the state that must not read as done.
    _run_to_work(attempt_id, org, bare, runtime=runtime)

    view = onboarding_ctl.record_step(attempt_id, "dispatch", status="done")
    assert view["status"] == "blocked"
    assert view["blocked_reason"] == "session_missing"


def test_no_fixtures_are_seeded_by_reading_the_state(fresh_store):
    from brains.storage.db import SessionLocal
    from brains.storage.models import AgentSession, Issue, Org, Persona, Project

    tables = (Org, Persona, Project, Issue, AgentSession)
    with SessionLocal() as session:
        before = {table.__name__: session.query(table).count() for table in tables}

    operator = _operator()
    onboarding_ctl.state(operator=operator)
    onboarding_ctl.state(operator=operator)

    with SessionLocal() as session:
        after = {table.__name__: session.query(table).count() for table in tables}
    assert after == before, "reading onboarding state created product rows"


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


def test_onboarding_routes_require_auth(client):
    assert client.get("/v1/onboarding/state").status_code in (401, 403)
    assert client.post("/v1/onboarding/attempts").status_code in (401, 403)


def test_state_route_returns_the_guard_decision(client):
    resp = client.get("/v1/onboarding/state", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"required", "fresh_install", "attempt", "steps_order"}
    assert body["steps_order"] == list(onboarding_ctl.STEPS)


def test_start_route_is_idempotent(client):
    first = client.post("/v1/onboarding/attempts", headers=AUTH).json()["attempt"]
    second = client.post("/v1/onboarding/attempts", headers=AUTH).json()["attempt"]
    assert second["attempt_id"] == first["attempt_id"]


def test_step_route_records_and_resumes(client):
    attempt = client.post("/v1/onboarding/attempts", headers=AUTH).json()["attempt"]
    client.post(
        f"/v1/onboarding/attempts/{attempt['attempt_id']}/steps/org",
        json={"entity_ref": "acme"},
        headers=AUTH,
    )
    recorded = client.post(
        f"/v1/onboarding/attempts/{attempt['attempt_id']}/steps/runtime",
        json={"status": "deferred"},
        headers=AUTH,
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["current_step"] == "persona"

    resumed = client.get("/v1/onboarding/state", headers=AUTH).json()["attempt"]
    assert resumed["attempt_id"] == attempt["attempt_id"]
    assert resumed["current_step"] == "persona"


def test_another_operators_attempt_is_not_found(client):
    attempt = client.post("/v1/onboarding/attempts", headers=AUTH).json()["attempt"]
    _record, key = add_operator(_slug("other"))
    other = {"Authorization": f"Bearer {key}"}

    resp = client.post(
        f"/v1/onboarding/attempts/{attempt['attempt_id']}/steps/runtime",
        json={"status": "deferred"},
        headers=other,
    )
    assert resp.status_code == 404
    abandon = client.post(f"/v1/onboarding/attempts/{attempt['attempt_id']}/abandon", headers=other)
    assert abandon.status_code == 404


def test_step_route_rejects_cross_org_entities(client, fresh_store, tmp_path):
    first = orgs_ctl.create_org(_slug("org"), "First")
    second = orgs_ctl.create_org(_slug("org"), "Second")
    runtime = runtimes_ctl.register_runtime(
        f"machine-{uuid.uuid4().hex[:8]}",
        "copilot",
        org_id=second["id"],
        working_root=str(tmp_path),
        status="online",
    )
    attempt = client.post("/v1/onboarding/attempts", headers=AUTH).json()["attempt"]
    established = client.post(
        f"/v1/onboarding/attempts/{attempt['attempt_id']}/steps/org",
        json={"org_id": first["id"], "entity_ref": first["slug"]},
        headers=AUTH,
    )
    assert established.status_code == 200, established.text

    refused = client.post(
        f"/v1/onboarding/attempts/{attempt['attempt_id']}/steps/runtime",
        json={"runtime_id": runtime["id"]},
        headers=AUTH,
    )
    assert refused.status_code == 404


def test_unrelated_session_cannot_complete_an_attempt(fresh_store, org_with_persona):
    org, runtime, persona = org_with_persona
    attempt_id = onboarding_ctl.state(operator=_operator())["attempt"]["attempt_id"]
    issue = _run_to_work(attempt_id, org, persona, runtime=runtime)
    other_project = projects_ctl.create_project(org["id"], _slug("proj"), "Other")
    other_issue = issues_ctl.create_issue(other_project["id"], "Other task")
    issues_ctl.assign(other_issue["code"], persona_id=persona["id"])
    other_session = evidence_ctl.dispatch(other_issue["code"])

    view = onboarding_ctl.record_step(
        attempt_id,
        "dispatch",
        status="done",
        session_id=other_session["session_id"],
    )
    assert view["status"] == "blocked"
    assert view["blocked_reason"] == "session_missing"
    assert view["entities"]["issue"]["id"] == issue["id"]
    assert view["entities"]["session"] is None


def test_abandon_route_is_a_safe_exit(client):
    attempt = client.post("/v1/onboarding/attempts", headers=AUTH).json()["attempt"]
    resp = client.post(f"/v1/onboarding/attempts/{attempt['attempt_id']}/abandon", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "abandoned"
    assert client.get("/v1/onboarding/state", headers=AUTH).json()["required"] is False

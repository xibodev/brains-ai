"""Executable checks for selected Brains feature acceptance criteria.

The normative product contract is ``docs/product/FEATURE_CONTRACT.md`` and the
feature-to-test mapping is ``docs/product/TRACEABILITY.md``. Test presence does
not by itself establish local execution, UAT, or product acceptance.

This module provides backend/API evidence for F0-F10 acceptance criteria.
Journey-level browser contracts for J1-J11 live in ``tests/e2e/``. Passing a
named pytest command is E3 evidence only for the exact candidate and environment
that ran it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from brains.control import issues as issues_ctl
from brains.control import orgs as orgs_ctl
from brains.control import personas as personas_ctl
from brains.control import projects as projects_ctl
from brains.control import runtimes as runtimes_ctl
from brains.control.operators import ensure_admin_operator
from brains.main import app

pytestmark = pytest.mark.acceptance


def _signed_github_post(client, payload: dict, delivery_id: str):
    raw = json.dumps(payload).encode()
    signature = hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest()
    return client.post(
        "/hooks/github",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "pull_request",
        },
    )


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
def spawn_target(org, tmp_path):
    """``(runtime, persona, issue)`` wired and ready for a spawn."""
    machine = f"machine-{uuid.uuid4().hex[:8]}"
    rt = runtimes_ctl.register_runtime(
        machine,
        "copilot",
        org_id=org["id"],
        working_root=str(tmp_path),
        status="online",
    )
    persona = personas_ctl.create_persona(
        org["id"],
        _slug("p"),
        "Forge",
        model="claude-opus-4.8",
        tool="copilot",
        default_runtime_id=rt["id"],
    )
    proj = projects_ctl.create_project(org["id"], _slug("proj"), "Proj")
    issue = issues_ctl.create_issue(proj["id"], "Fix the thruster", body="broken")
    return rt, persona, issue


# --------------------------------------------------------------------------- #
# F0 / AC-F0-04 — Persona Spawn creates an attributable Session
# --------------------------------------------------------------------------- #


def test_f0_1_persona_spawn_alias_creates_session(client, spawn_target):
    """AC-F0-04: clicking Spawn on a Persona creates an agent_session and returns
    200 with a session id; the session then appears in Sessions."""
    rt, persona, issue = spawn_target
    resp = client.post(
        f"/v1/personas/{persona['id']}/spawn",
        json={"issue_id": issue["id"], "runtime_id": rt["id"]},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    session_id = body.get("session_id") or body.get("id")
    assert session_id, f"spawn response missing a session id: {body}"

    listed = client.get("/v1/sessions", headers=AUTH).json()
    rows = listed.get("items", listed.get("data", listed))
    ids = {r.get("session_id") or r.get("id") for r in rows}
    assert session_id in ids, "spawned session does not appear in /v1/sessions"


def test_f0_1_persona_spawn_filters_by_persona_id(client, spawn_target):
    """The spawn alias binds the session to the persona, so the persona-scoped
    Sessions read (``GET /v1/sessions?persona_id=..``) surfaces it. Also accepts
    the persona slug on the path."""
    rt, persona, issue = spawn_target
    resp = client.post(
        f"/v1/personas/{persona['slug']}/spawn",
        json={"issue_id": issue["id"], "runtime_id": rt["id"], "prompt": "go"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]

    listed = client.get("/v1/sessions", params={"persona_id": persona["id"]}, headers=AUTH).json()
    rows = listed.get("items", listed.get("data", listed))
    matched = {r["id"]: r for r in rows}
    assert session_id in matched, "persona-scoped sessions read missing the spawn"
    row = matched[session_id]
    assert row["persona_id"] == persona["id"]
    assert row["issue_id"] == issue["id"]
    assert row["runtime_id"] == rt["id"]


def test_f0_1_persona_spawn_unknown_persona_404(client):
    resp = client.post("/v1/personas/does-not-exist/spawn", json={}, headers=AUTH)
    assert resp.status_code == 404, resp.text


# --------------------------------------------------------------------------- #
# F1 / J2 — Connect a machine with enrolment tokens
# --------------------------------------------------------------------------- #


def test_f1_1_enrol_returns_valid_connect_command(client):
    """AC-F1-01: the minted connect command is valid and complete — correct CLI
    name (`brains-ai`, not `brains`), a real hub URL (no literal `<url>`), and a
    real enrolment token."""
    resp = client.post("/v1/runtimes/enrol", json={"label": "my-laptop"}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body.get("token"), "no enrolment token minted"
    assert body.get("expires_at"), "enrolment token has no expiry"
    command = body.get("command", "")
    assert "brains-ai" in command, f"command uses wrong CLI name: {command!r}"
    assert "<url>" not in command and "<hub" not in command, (
        f"command has an unsubstituted placeholder: {command!r}"
    )
    assert body["token"] in command, "minted token is not embedded in the command"
    # The historic broken command shipped `pip install brains` (wrong package).
    assert "pip install brains " not in command


def test_f1_4_enrol_token_single_use_and_expiry():
    """AC-F1-03: enrolment tokens are single-use and expire; a redeemed or expired
    token is rejected. Exercised at the control layer (no admin key needed)."""
    from brains.control import enrolment as enrol_ctl

    minted = enrol_ctl.mint_token(label="laptop", ttl_seconds=900)
    token = minted["token"]

    # First redemption succeeds and registers a machine.
    first = enrol_ctl.redeem_token(token, machine_id="machine-A")
    assert first is not None

    # Second redemption of the same token is rejected (single-use).
    with pytest.raises(ValueError, match="redeemed|invalid|used"):
        enrol_ctl.redeem_token(token, machine_id="machine-B")

    # An expired token is rejected.
    expired = enrol_ctl.mint_token(label="laptop2", ttl_seconds=-1)
    with pytest.raises(ValueError, match="expired|invalid"):
        enrol_ctl.redeem_token(expired["token"], machine_id="machine-C")


def test_f1_2_redeem_registers_one_runtime_per_cli_without_admin_key(client):
    """AC-F1-02: redeeming a token on a machine with copilot+claude registers one
    runtime per CLI, version-stamped, WITHOUT the operator supplying an admin
    key (the redeem route authenticates by the token itself)."""
    minted = client.post("/v1/runtimes/enrol", json={"label": "box"}, headers=AUTH).json()
    token = minted["token"]

    # NOTE: no Authorization header — the token is the credential.
    resp = client.post(
        "/v1/runtimes/enrol/redeem",
        json={
            "token": token,
            "machine_id": "box-1",
            "clis": [
                {"tool": "copilot", "version": "1.0.65"},
                {"tool": "claude", "version": "2.0.1"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    registered = resp.json().get("runtimes", [])
    tools = {r["tool"] for r in registered}
    assert {"copilot", "claude"} <= tools, f"expected one runtime per CLI, got {tools}"
    assert all(r.get("version") for r in registered), "runtimes are not version-stamped"


# --------------------------------------------------------------------------- #
# F2 / J3 — Persona-to-Runtime binding and capability-derived configuration
# --------------------------------------------------------------------------- #


def test_f2_1_runtime_exposes_capability_models_for_dropdown(client):
    """AC-F2-01 (backend half): a Runtime's detected models are exposed as a
    structured ``capabilities.models[]`` list so the persona form can derive the
    Model dropdown from the chosen runtime (not free text)."""
    import json as _json

    machine = f"cap-machine-{uuid.uuid4().hex[:8]}"
    runtimes_ctl.register_runtime(
        machine,
        "copilot",
        capabilities=_json.dumps({"tool": "copilot", "models": ["gpt-5.5", "claude-opus-4.8"]}),
        status="online",
    )
    listed = client.get("/v1/runtimes", headers=AUTH).json()["runtimes"]
    mine = [r for r in listed if r["machine_id"] == machine]
    assert mine, "registered runtime not returned"
    caps = mine[0]["capabilities"]
    assert isinstance(caps, dict), f"capabilities must be a structured object, got {type(caps)}"
    assert isinstance(caps.get("models"), list) and caps["models"], "capabilities.models[] missing"


def test_f2_2_persona_persists_runtime_binding_and_config(client, org, tmp_path):
    """AC-F2-02: a Persona persists default_runtime_id + model + tool +
    instructions; a subsequent read shows every field (edit-form == create-form)."""
    rt = runtimes_ctl.register_runtime(
        f"bind-machine-{uuid.uuid4().hex[:8]}",
        "copilot",
        org_id=org["id"],
        working_root=str(tmp_path),
        status="online",
    )
    created = client.post(
        f"/v1/orgs/{org['slug']}/personas",
        json={
            "slug": _slug("bind"),
            "name": "Bound",
            "model": "claude-opus-4.8",
            "tool": "copilot",
            "default_runtime_id": rt["id"],
            "system_prompt": "be precise",
        },
        headers=AUTH,
    )
    assert created.status_code in (200, 201), created.text
    pid = created.json()["id"]

    fetched = client.get(f"/v1/personas/{pid}", headers=AUTH).json()
    assert fetched["default_runtime_id"] == rt["id"]
    assert fetched["model"] == "claude-opus-4.8"
    assert fetched["tool"] == "copilot"
    assert fetched["system_prompt"] == "be precise"

    # Re-binding to a different runtime via PATCH persists.
    rt2 = runtimes_ctl.register_runtime(
        f"bind2-machine-{uuid.uuid4().hex[:8]}",
        "claude",
        org_id=org["id"],
        working_root=str(tmp_path),
        status="online",
    )
    client.patch(f"/v1/personas/{pid}", json={"default_runtime_id": rt2["id"]}, headers=AUTH)
    assert client.get(f"/v1/personas/{pid}", headers=AUTH).json()["default_runtime_id"] == rt2["id"]


def test_f2_4_persona_can_be_archived(client, org):
    """AC-F2-04: a Persona can be archived/deleted from the API (UI delete)."""
    created = client.post(
        f"/v1/orgs/{org['slug']}/personas",
        json={"slug": _slug("temp"), "name": "Temp"},
        headers=AUTH,
    ).json()
    pid = created["id"]
    resp = client.delete(f"/v1/personas/{pid}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    # Archived personas drop out of the default (active) list.
    active = client.get(f"/v1/orgs/{org['slug']}/personas", headers=AUTH).json()
    rows = active.get("data", active)
    assert all(p["id"] != pid for p in rows), "archived persona still in active list"


# --------------------------------------------------------------------------- #
# F3 / J7-J8 — Sessions, events, lifecycle, and human control
# --------------------------------------------------------------------------- #


def test_f3_2_session_state_lifecycle(client, spawn_target):
    """AC-F3-02: a Session carries an explicit lifecycle — spawning -> running ->
    completed — reflected live; the terminal state stamps a duration."""
    rt, persona, issue = spawn_target
    spawned = client.post(
        f"/v1/personas/{persona['id']}/spawn",
        json={"issue_id": issue["id"], "runtime_id": rt["id"]},
        headers=AUTH,
    ).json()
    sid = spawned["session_id"]

    # Freshly spawned -> spawning.
    got = client.get(f"/v1/sessions/{sid}", headers=AUTH).json()
    assert got["state"] == "spawning", got

    # Transition running, then completed.
    running = client.post(
        f"/v1/sessions/{sid}/state", json={"state": "running"}, headers=AUTH
    ).json()
    assert running["state"] == "running"
    assert running["ended_at"] is None

    done = client.post(
        f"/v1/sessions/{sid}/state",
        json={"state": "completed", "summary": "all green"},
        headers=AUTH,
    ).json()
    assert done["state"] == "completed"
    assert done["ended_at"] is not None
    assert done["duration_seconds"] is not None and done["duration_seconds"] >= 0
    assert done["summary"] == "all green"

    # An invalid state is rejected.
    bad = client.post(f"/v1/sessions/{sid}/state", json={"state": "nonsense"}, headers=AUTH)
    assert bad.status_code == 400


def test_f3_2_state_unknown_session_404(client):
    resp = client.post("/v1/sessions/ses_missing/state", json={"state": "running"}, headers=AUTH)
    assert resp.status_code == 404


def test_f3_1_session_event_ingest_and_read(client, spawn_target):
    """AC-F3-01: an ingested Session event is durably stored and read
    back via the session-scoped events endpoint (the transcript backfill); the
    ingest also fans out on the per-session topic for live streaming."""
    rt, persona, issue = spawn_target
    sid = client.post(
        f"/v1/personas/{persona['id']}/spawn",
        json={"issue_id": issue["id"], "runtime_id": rt["id"]},
        headers=AUTH,
    ).json()["session_id"]
    # The daemon opens the session on its runtime, then streams events.
    client.post(
        f"/v1/runtimes/{rt['id']}/sessions",
        json={"session_id": sid, "persona_id": persona["id"], "issue_id": issue["id"]},
        headers=AUTH,
    )
    posted = client.post(
        f"/v1/runtimes/{rt['id']}/sessions/{sid}/events",
        json={"seq": 1, "stream": "stdout", "chunk": "hello from the agent"},
        headers=AUTH,
    )
    assert posted.status_code == 200, posted.text

    events = client.get(f"/v1/sessions/{sid}/events", headers=AUTH).json()
    rows = events.get("data", events)
    chunks = " ".join(str(e.get("message") or e.get("chunk") or "") for e in rows)
    assert "hello from the agent" in chunks, f"streamed event not in transcript: {rows}"


def test_f3_3_agent_self_transitions_issue_and_comments(client, spawn_target):
    """AC-F3-03: an agent running in a Session can move its Issue status and
    post a comment that appears on the issue alongside human comments."""
    rt, persona, issue = spawn_target
    code = issue["code"]
    sid = client.post(
        f"/v1/personas/{persona['id']}/spawn",
        json={"issue_id": issue["id"], "runtime_id": rt["id"]},
        headers=AUTH,
    ).json()["session_id"]

    # The agent self-transitions the issue to blocked.
    moved = client.post(f"/v1/issues/{code}/transition", json={"status": "blocked"}, headers=AUTH)
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "blocked"

    # ...and posts a reasoned comment as the persona, linked to its session.
    posted = client.post(
        f"/v1/issues/{code}/comments",
        json={
            "body": "Blocked: upstream API returns 500; need a credential.",
            "author_kind": "persona",
            "persona_id": persona["id"],
            "session_id": sid,
        },
        headers=AUTH,
    )
    assert posted.status_code == 200, posted.text

    # The comment surfaces on the issue, attributed to the persona + session.
    listed = client.get(f"/v1/issues/{code}/comments", headers=AUTH).json()
    rows = listed.get("data", listed)
    assert len(rows) == 1
    c = rows[0]
    assert c["author_kind"] == "persona"
    assert c["author_persona_id"] == persona["id"]
    assert c["session_id"] == sid
    assert "Blocked" in c["body"]


def test_f3_3_comment_rejects_empty_and_unknown_issue(client, org):
    proj = projects_ctl.create_project(org["id"], _slug("p"), "P")
    iss = issues_ctl.create_issue(proj["id"], "T")
    empty = client.post(f"/v1/issues/{iss['code']}/comments", json={"body": "  "}, headers=AUTH)
    assert empty.status_code == 400
    missing = client.post("/v1/issues/ISS-999999/comments", json={"body": "x"}, headers=AUTH)
    assert missing.status_code == 404


def test_f3_4_gate_ask_surfaces_in_approvals_and_resolves(client, tmp_path):
    """AC-F3-04 (operator path): an in-Session gate Ask (filed by exec.gate when an
    outward action is intercepted) surfaces in the operator approvals queue and is
    resolvable from the console — the governance loop end-to-end. The actual
    intercept/block is proven by tests/test_gate_integration.py on Linux.)"""
    from brains.control.decisions import file_decision_request

    filed = file_decision_request(
        workspace_path=str(tmp_path),
        title="[gate] approve outward action: git push",
        body="Command:\n  git push origin main",
        proposed_answer="approve",
        metadata={"kind": "action_gate", "action_type": "git push"},
    )
    code = filed["code"]

    # Surfaces in the operator approvals queue (what the Inbox Approvals tab reads).
    pending = client.get("/v1/approvals", headers=AUTH).json()
    rows = pending.get("data", pending)
    match = next((a for a in rows if a["code"] == code), None)
    assert match is not None, "gate ASK did not surface in /v1/approvals"
    assert "outward action" in match["title"]

    # Operator approves from the console.
    resolved = client.post(
        f"/v1/approvals/{code}/resolve", json={"chosen": "approve"}, headers=AUTH
    )
    assert resolved.status_code == 200, resolved.text

    # No longer pending.
    after = client.get("/v1/approvals", headers=AUTH).json()
    after_rows = after.get("data", after)
    assert all(a["code"] != code for a in after_rows), "resolved approval still pending"


# --------------------------------------------------------------------------- #
# F4 / J6-J7 — Issue assignment, dispatch, and execution history
# --------------------------------------------------------------------------- #


def test_f4_assign_dispatch_and_execution_log(client, spawn_target):
    """AC-F4-02/03/04: assign an Issue to a Persona, dispatch a linked Session,
    and surface that Session in the Issue execution history."""
    rt, persona, issue = spawn_target
    code = issue["code"]

    # AC-F4-02: assign exactly one Persona target.
    assigned = client.post(
        f"/v1/issues/{code}/assign", json={"persona_id": persona["id"]}, headers=AUTH
    )
    assert assigned.status_code == 200, assigned.text
    assert (
        client.get(f"/v1/issues/{code}", headers=AUTH).json()["assignee_persona_id"]
        == persona["id"]
    )

    # AC-F4-03: dispatch creates a Session bound to the Issue and Persona.
    dispatched = client.post(f"/v1/issues/{code}/dispatch", headers=AUTH)
    assert dispatched.status_code == 200, dispatched.text
    sid = dispatched.json()["session_id"]
    assert sid

    # AC-F4-04: the Issue execution history surfaces the dispatch.
    sessions = client.get(f"/v1/issues/{code}/sessions", headers=AUTH).json()
    rows = sessions.get("data", sessions)
    assert any(s["id"] == sid for s in rows), "dispatched session not in issue execution log"


def test_f4_dispatch_requires_persona_assignee(client, org):
    proj = projects_ctl.create_project(org["id"], _slug("p"), "P")
    iss = issues_ctl.create_issue(proj["id"], "Unassigned")
    resp = client.post(f"/v1/issues/{iss['code']}/dispatch", headers=AUTH)
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# F9 / J10-J11 — Org membership and usage
# --------------------------------------------------------------------------- #


def test_f9_usage_summary_endpoint(client):
    """AC-F9-04: the Usage dashboard read returns token/cost totals + top models,
    degrading to zeros on an empty ledger (never errors)."""
    resp = client.get("/v1/usage", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "totals" in body and "top_models" in body
    assert isinstance(body["top_models"], list)


def test_f9_org_usage_summary_is_scoped_and_excludes_other_orgs(client, spawn_target, tmp_path):
    """AC-F9-04: ``GET /v1/orgs/{org}/usage`` declares its scope/org/days and
    only counts gateway calls this Org's own Sessions were attributed —
    another Org's attributed usage never leaks into these totals."""
    from brains.control import orgs as orgs_ctl_
    from brains.control import sessions as sessions_ctl
    from brains.router import savings

    rt, persona, issue = spawn_target
    org_id = persona["org_id"]
    other_org = orgs_ctl_.create_org(_slug("otherorg"), "Other")

    session_row = sessions_ctl.open_spawn_session(
        persona_id=persona["id"],
        tool="copilot",
        org_id=org_id,
        workspace_path=str(tmp_path / "org-a"),
    )
    other_session_row = sessions_ctl.open_spawn_session(
        persona_id=personas_ctl.create_persona(other_org["id"], _slug("op"), "Other Persona")["id"],
        tool="copilot",
        org_id=other_org["id"],
        workspace_path=str(tmp_path / "org-b"),
    )

    entry = savings.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="acme-test",
        input_tokens=1000,
        output_tokens=500,
        session_id=session_row["id"],
    )
    assert entry is not None
    other_entry = savings.record_usage(
        endpoint="openai.chat",
        requested_model="gpt-4o",
        routed_model="gpt-4o-mini",
        provider="acme-test",
        input_tokens=10_000,
        output_tokens=10_000,
        session_id=other_session_row["id"],
    )
    assert other_entry is not None

    org_slug = orgs_ctl_.get_org(org_id)["slug"]
    resp = client.get(f"/v1/orgs/{org_slug}/usage", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "org"
    assert body["org"] == org_slug
    assert body["org_id"] == org_id
    assert body["totals"]["calls"] == 1
    assert body["totals"]["input_tokens"] == 1000
    assert any(m["routed_model"] == "gpt-4o-mini" for m in body["top_models"])


def test_f9_create_org_and_manage_members(client, _operators):
    """AC-F9-01/02: create an Org and add/remove a member with a role."""
    slug = _slug("neworg")
    created = client.post("/v1/orgs", json={"slug": slug, "name": "New Org"}, headers=AUTH)
    assert created.status_code in (200, 201), created.text

    invited = client.post(
        f"/v1/orgs/{slug}/members", json={"operator_id": "member-op", "role": "admin"}, headers=AUTH
    )
    assert invited.status_code == 200, invited.text
    members = client.get(f"/v1/orgs/{slug}/members", headers=AUTH).json()
    rows = members.get("data", members)
    assert any(m["operator"] == "member-op" and m["role"] == "admin" for m in rows)

    removed = client.delete(f"/v1/orgs/{slug}/members/member-op", headers=AUTH)
    assert removed.status_code == 200, removed.text
    after = client.get(f"/v1/orgs/{slug}/members", headers=AUTH).json()
    assert all(m["operator"] != "member-op" for m in after.get("data", after))


# --------------------------------------------------------------------------- #
# F7 / J9 — Effective provider configuration and connectivity
# --------------------------------------------------------------------------- #


def test_f7_config_summary_is_real_not_stub(client):
    """AC-F7-02: the Config surface renders real config (providers + gateway), not
    a 'wire me' placeholder. Secrets are never returned."""
    resp = client.get("/v1/config/summary", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.get("providers"), list)
    assert "gateway" in body
    echo = next(provider for provider in body["providers"] if provider["name"] == "echo")
    assert echo["status"] == "simulated"
    assert echo["configured"] is False
    assert body["write_contract"]["mode"] == "read_only"
    assert "restart every Brains process" in body["write_contract"]["reload"]
    assert any(route["simulated"] for route in body["models"])
    assert isinstance(body["integrations"]["github"]["configured"], bool)
    assert isinstance(body["integrations"]["github"]["allowed_repository_count"], int)
    assert all(
        bridge["status"] in {"configured", "unconfigured", "degraded"}
        for bridge in body["integrations"]["bridges"]
    )
    # No secret material leaks.
    assert "api_key" not in str(body).lower() or "managed" in str(body).lower()


def test_f7_provider_test_returns_ok_or_fail(client):
    """AC-F7-01: Test connection returns a real ok/fail (never raises). An unknown/
    stub provider is a clean fail."""
    resp = client.post("/v1/config/providers/echo/test", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": False,
        "status": "simulated",
        "stage": "configuration",
        "latency_ms": 0,
        "detail": "This is a simulated provider; no upstream connection exists.",
    }

    unknown = client.post("/v1/config/providers/not-real/test", headers=AUTH)
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["status"] == "unknown"


def test_f7_admin_can_release_a_stuck_integration_attempt(client):
    from brains.control import integration_deliveries as deliveries_ctl

    key = f"stuck-{uuid.uuid4()}"
    delivery, created = deliveries_ctl.claim("relay_triage", "inbound", key)
    assert created is True
    listed = client.get(
        "/v1/config/integrations/deliveries?status=processing",
        headers=AUTH,
    )
    assert listed.status_code == 200
    assert any(row["id"] == delivery["id"] for row in listed.json()["data"])
    released = client.post(
        f"/v1/config/integrations/deliveries/{delivery['id']}/release",
        json={"attempt": delivery["attempts"]},
        headers=AUTH,
    )
    assert released.status_code == 200
    assert released.json()["status"] == "failed"
    retried, retry_created = deliveries_ctl.claim(
        "relay_triage",
        "inbound",
        key,
        retry_failed=True,
    )
    assert retry_created is True
    assert retried["attempts"] == 2


# --------------------------------------------------------------------------- #
# F8 / J6-J9 — GitHub pull-request linkage
# --------------------------------------------------------------------------- #


def test_f8_pr_merge_auto_dones_issue(client, org, monkeypatch):
    """AC-F8-01/02: a merged PR whose title carries the Issue code moves it to
    Done and records a comment; a non-merged PR only links (no transition)."""
    proj = projects_ctl.create_project(org["id"], _slug("p"), "P")
    iss = issues_ctl.create_issue(proj["id"], "Wire the thing")
    code = iss["code"]
    from brains.config import settings

    monkeypatch.setattr(settings, "github_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(
        settings,
        "github_repository_org_bindings",
        (f"owner/repo={org['slug']}",),
    )

    # An opened PR links but does NOT close the issue.
    opened = _signed_github_post(
        client,
        {
            "action": "opened",
            "pull_request": {"number": 7, "title": f"{code}: wip", "merged": False},
            "repository": {"full_name": "owner/repo"},
        },
        "delivery-opened",
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["linked"] is True and opened.json().get("merged") is False
    assert client.get(f"/v1/issues/{code}", headers=AUTH).json()["status"] != "done"

    # The merged PR auto-moves the issue to Done.
    merged = _signed_github_post(
        client,
        {
            "action": "closed",
            "pull_request": {"number": 7, "title": f"Implement {code}", "merged": True},
            "repository": {"full_name": "owner/repo"},
        },
        "delivery-merged",
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["status"] == "done"
    assert client.get(f"/v1/issues/{code}", headers=AUTH).json()["status"] == "done"
    # A system comment records the merge.
    comments = client.get(f"/v1/issues/{code}/comments", headers=AUTH).json()
    rows = comments.get("data", comments)
    assert any("merged" in (c["body"] or "").lower() for c in rows)


def test_f8_pr_without_issue_code_is_noop(client, org, monkeypatch):
    from brains.config import settings

    monkeypatch.setattr(settings, "github_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(
        settings,
        "github_repository_org_bindings",
        (f"owner/repo={org['slug']}",),
    )
    resp = _signed_github_post(
        client,
        {
            "action": "closed",
            "pull_request": {"number": 9, "title": "chore: deps", "merged": True},
            "repository": {"full_name": "owner/repo"},
        },
        "delivery-no-issue",
    )
    assert resp.status_code == 200
    assert resp.json()["linked"] is False


def test_f8_webhook_rejects_invalid_signature_and_repository_scope(client, org, monkeypatch):
    from brains.config import settings

    monkeypatch.setattr(settings, "github_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(
        settings,
        "github_repository_org_bindings",
        (f"owner/repo={org['slug']}",),
    )
    payload = {
        "action": "opened",
        "pull_request": {"number": 11, "title": "chore: test", "merged": False},
        "repository": {"full_name": "owner/repo"},
    }
    invalid = client.post(
        "/hooks/github",
        json=payload,
        headers={
            "X-Hub-Signature-256": "sha256=invalid",
            "X-GitHub-Delivery": "delivery-invalid",
            "X-GitHub-Event": "pull_request",
        },
    )
    assert invalid.status_code == 401

    payload["repository"]["full_name"] = "other/repo"
    scoped = _signed_github_post(client, payload, "delivery-out-of-scope")
    assert scoped.status_code == 200
    assert scoped.json() == {
        "accepted": False,
        "reason": "repository is outside the allowed scope",
    }


def test_f8_repository_binding_cannot_mutate_another_org_issue(client, org, monkeypatch):
    from brains.config import settings

    other_org = orgs_ctl.create_org(_slug("other-org"), "Other")
    project = projects_ctl.create_project(other_org["id"], _slug("other-project"), "Other")
    issue = issues_ctl.create_issue(project["id"], "Must stay scoped")
    monkeypatch.setattr(settings, "github_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(
        settings,
        "github_repository_org_bindings",
        (f"owner/repo={org['slug']}",),
    )
    response = _signed_github_post(
        client,
        {
            "action": "closed",
            "pull_request": {
                "number": 13,
                "title": f"Implement {issue['code']}",
                "merged": True,
            },
            "repository": {"full_name": "owner/repo"},
        },
        "delivery-cross-org",
    )
    assert response.status_code == 200
    assert response.json()["linked"] is False
    assert response.json()["reason"] == "issue is outside the repository Org scope"
    assert issues_ctl.get_issue(issue["code"])["status"] != "done"


def test_f8_webhook_delivery_replay_does_not_repeat_merge_comment(client, org, monkeypatch):
    from brains.config import settings

    monkeypatch.setattr(settings, "github_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(
        settings,
        "github_repository_org_bindings",
        (f"owner/repo={org['slug']}",),
    )
    proj = projects_ctl.create_project(org["id"], _slug("replay"), "Replay")
    issue = issues_ctl.create_issue(proj["id"], "Replay-safe merge")
    payload = {
        "action": "closed",
        "pull_request": {
            "number": 12,
            "title": f"Implement {issue['code']}",
            "merged": True,
        },
        "repository": {"full_name": "owner/repo"},
    }
    first = _signed_github_post(client, payload, "delivery-replay")
    replay = _signed_github_post(client, payload, "delivery-replay")
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    comments = client.get(
        f"/v1/issues/{issue['code']}/comments",
        headers=AUTH,
    ).json()
    rows = comments.get("data", comments)
    merge_comments = [row for row in rows if "merged" in (row["body"] or "").lower()]
    assert len(merge_comments) == 1


def test_f8_failed_delivery_retry_does_not_repeat_issue_transition(client, org, monkeypatch):
    from brains.config import settings

    monkeypatch.setattr(settings, "github_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(
        settings,
        "github_repository_org_bindings",
        (f"owner/repo={org['slug']}",),
    )
    project = projects_ctl.create_project(org["id"], _slug("retry-project"), "Retry")
    issue = issues_ctl.create_issue(project["id"], "Retry partial merge")
    payload = {
        "action": "closed",
        "pull_request": {
            "number": 14,
            "title": f"Implement {issue['code']}",
            "merged": True,
        },
        "repository": {"full_name": "owner/repo"},
    }
    original_add_comment = issues_ctl.add_comment

    def _fail_comment(*_args, **_kwargs):
        raise RuntimeError("comment write failed")

    monkeypatch.setattr(issues_ctl, "add_comment", _fail_comment)
    with pytest.raises(RuntimeError, match="comment write failed"):
        _signed_github_post(client, payload, "delivery-partial-retry")
    first_closed_at = issues_ctl.get_issue(issue["code"])["closed_at"]
    assert first_closed_at is not None

    monkeypatch.setattr(issues_ctl, "add_comment", original_add_comment)
    retried = _signed_github_post(client, payload, "delivery-partial-retry")
    assert retried.status_code == 200
    assert issues_ctl.get_issue(issue["code"])["closed_at"] == first_closed_at
    comments = client.get(
        f"/v1/issues/{issue['code']}/comments",
        headers=AUTH,
    ).json()
    rows = comments.get("data", comments)
    assert len([row for row in rows if "merged" in (row["body"] or "").lower()]) == 1


# --------------------------------------------------------------------------- #
# F10 / J10 — Autopilots and Skills
# --------------------------------------------------------------------------- #


def test_f10_autopilot_crud_and_fire(client, org):
    """AC-F10-01/03/06: create, list, toggle, and manually fire an Org Autopilot
    while retaining durable run behavior and disabled-fire rejection."""
    name = _slug("nightly")
    created = client.post(
        f"/v1/orgs/{org['slug']}/autopilots",
        json={"name": name, "title_template": "Nightly triage", "cron_expr": "manual"},
        headers=AUTH,
    )
    assert created.status_code in (200, 201), created.text

    listed = client.get(f"/v1/orgs/{org['slug']}/autopilots", headers=AUTH).json()
    rows = listed.get("data", listed)
    assert any(a["name"] == name for a in rows)

    # Fire it manually while enabled (the engine records a run; spawn stays gated).
    fired = client.post(f"/v1/autopilots/{name}/fire", headers=AUTH)
    assert fired.status_code == 200, fired.text

    # Disabling it persists; a disabled autopilot refuses to fire.
    disabled = client.post(f"/v1/autopilots/{name}/enabled", json={"enabled": False}, headers=AUTH)
    assert disabled.status_code == 200, disabled.text
    refused = client.post(f"/v1/autopilots/{name}/fire", headers=AUTH)
    assert refused.status_code == 404


def test_f10_skills_crud(client, org):
    """F10 Skill surface: create and list Org SKILL.md context packs.

    Attachment and provenance remain the separate AC-F10-05 gap.
    """
    created = client.post(
        f"/v1/orgs/{org['slug']}/skills",
        json={"slug": "code-review", "name": "Code Review", "content": "# How to review\n..."},
        headers=AUTH,
    )
    assert created.status_code in (200, 201), created.text
    listed = client.get(f"/v1/orgs/{org['slug']}/skills", headers=AUTH).json()
    rows = listed.get("data", listed)
    assert any(s["slug"] == "code-review" and s["name"] == "Code Review" for s in rows)
    # Duplicate slug in the same org is a conflict.
    dup = client.post(
        f"/v1/orgs/{org['slug']}/skills",
        json={"slug": "code-review", "name": "Dup"},
        headers=AUTH,
    )
    assert dup.status_code == 409


def test_f4_assignee_picker_sources_available(client, org, spawn_target):
    """The tri-modal picker is fed by three existing list endpoints (members,
    personas, pods) — all return cleanly for the org."""
    _, persona, _ = spawn_target
    members = client.get(f"/v1/orgs/{org['slug']}/members", headers=AUTH)
    personas = client.get(f"/v1/orgs/{org['slug']}/personas", headers=AUTH)
    pods = client.get(f"/v1/orgs/{org['slug']}/pods", headers=AUTH)
    assert members.status_code == 200
    assert personas.status_code == 200 and personas.json().get("data") is not None
    assert pods.status_code == 200


# --------------------------------------------------------------------------- #
# F5 / J4 — Pod CRUD and leader routing
# --------------------------------------------------------------------------- #


@pytest.fixture
def _operators():
    import contextlib

    from brains.control.operators import add_operator

    for slug in ("lead-op", "member-op"):
        with contextlib.suppress(Exception):
            add_operator(slug)
    yield


def test_f5_pod_crud_and_leader(client, org, tmp_path, _operators):
    """AC-F5-01/02/03: create a Pod led by a Persona, add a Persona member,
    replace the leader, remove a member, and archive the Pod.

    AC-F5-04 covers deterministic Issue-to-Pod dispatch separately.
    """
    rt = runtimes_ctl.register_runtime(
        f"pod-machine-{uuid.uuid4().hex[:8]}",
        "copilot",
        org_id=org["id"],
        working_root=str(tmp_path),
        status="online",
    )
    lead = personas_ctl.create_persona(
        org["id"], _slug("lead"), "Lead", tool="copilot", default_runtime_id=rt["id"]
    )
    hand = personas_ctl.create_persona(
        org["id"], _slug("hand"), "Hand", tool="copilot", default_runtime_id=rt["id"]
    )

    created = client.post(
        f"/v1/orgs/{org['slug']}/pods",
        json={"slug": _slug("ops"), "name": "Ops Pod", "leader_persona_id": str(lead["id"])},
        headers=AUTH,
    )
    assert created.status_code in (200, 201), created.text
    pod_id = created.json()["id"]
    assert created.json()["leader_persona"] == lead["slug"]

    added = client.post(
        f"/v1/pods/{pod_id}/members", json={"persona_id": str(hand["id"])}, headers=AUTH
    )
    assert added.status_code == 200, added.text
    assert {m["persona_id"] for m in added.json()["members"]} == {lead["id"], hand["id"]}

    relead = client.patch(
        f"/v1/pods/{pod_id}", json={"leader_persona_id": str(hand["id"])}, headers=AUTH
    )
    assert relead.status_code == 200, relead.text
    assert relead.json()["leader_persona"] == hand["slug"]
    assert sum(1 for m in relead.json()["members"] if m["is_leader"]) == 1

    removed = client.delete(f"/v1/pods/{pod_id}/members/{lead['id']}", headers=AUTH)
    assert removed.status_code == 200, removed.text
    assert {m["persona_id"] for m in removed.json()["members"]} == {hand["id"]}

    listed = client.get(f"/v1/orgs/{org['slug']}/pods", headers=AUTH).json()
    rows = listed.get("data", listed)
    assert any(p["id"] == pod_id for p in rows)

    archived = client.patch(f"/v1/pods/{pod_id}", json={"status": "archived"}, headers=AUTH)
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"


def test_f5_pod_dispatch_resolves_to_a_capable_member(client, org, tmp_path, _operators):
    """AC-F5-04 / AC-F4-06: a Pod-assigned Issue dispatches through the roster."""
    rt = runtimes_ctl.register_runtime(
        f"pod-machine-{uuid.uuid4().hex[:8]}",
        "copilot",
        org_id=org["id"],
        working_root=str(tmp_path),
        status="online",
    )
    lead = personas_ctl.create_persona(org["id"], _slug("lead"), "Lead", tool="copilot")
    hand = personas_ctl.create_persona(
        org["id"], _slug("hand"), "Hand", tool="copilot", default_runtime_id=rt["id"]
    )
    pod = client.post(
        f"/v1/orgs/{org['slug']}/pods",
        json={"slug": _slug("ops"), "name": "Ops", "leader_persona_id": str(lead["id"])},
        headers=AUTH,
    ).json()
    client.post(f"/v1/pods/{pod['id']}/members", json={"persona_id": str(hand["id"])}, headers=AUTH)

    proj = projects_ctl.create_project(org["id"], _slug("proj"), "Proj")
    issue = issues_ctl.create_issue(proj["id"], "Pod work")
    assigned = client.post(
        f"/v1/issues/{issue['code']}/assign", json={"pod_id": pod["id"]}, headers=AUTH
    )
    assert assigned.status_code == 200, assigned.text

    plan = client.get(f"/v1/issues/{issue['code']}/dispatch-plan", headers=AUTH).json()
    assert plan["assignee_kind"] == "pod"
    assert plan["dispatchable"] is True
    assert plan["persona_id"] == hand["id"], "the leader has no runtime and must be skipped"

    dispatched = client.post(f"/v1/issues/{issue['code']}/dispatch", headers=AUTH)
    assert dispatched.status_code == 200, dispatched.text
    evidence = client.get(f"/v1/issues/{issue['code']}/evidence", headers=AUTH).json()
    assert evidence["sessions"][0]["id"] == dispatched.json()["session_id"]
    assert evidence["sessions"][0]["persona_id"] == hand["id"]


def test_f5_pod_create_autoprovisions_workspace(client, org, _operators):
    """A fresh org with no workspace still gets a pod — the workspace backing the
    legacy squad row is auto-provisioned + stamped to the org."""
    fresh = orgs_ctl.create_org(_slug("noweksp"), "NoWs")
    leader = personas_ctl.create_persona(fresh["id"], _slug("lead"), "Lead", tool="copilot")
    resp = client.post(
        f"/v1/orgs/{fresh['slug']}/pods",
        json={"slug": _slug("ops"), "name": "Ops", "leader_persona_id": str(leader["id"])},
        headers=AUTH,
    )
    assert resp.status_code in (200, 201), resp.text
    pod_id = resp.json()["id"]
    listed = client.get(f"/v1/orgs/{fresh['slug']}/pods", headers=AUTH).json()
    rows = listed.get("data", listed)
    assert any(p["id"] == pod_id for p in rows), "auto-provisioned pod not listed org-scoped"


# --------------------------------------------------------------------------- #
# F6 / J1 — fresh-state onboarding
# --------------------------------------------------------------------------- #


def test_f6_onboarding_state_is_durable_and_resumable(client):
    """AC-F6-01/02/03: the attempt is server state, so a reload resumes it."""
    state = client.get("/v1/onboarding/state", headers=AUTH)
    assert state.status_code == 200, state.text
    assert state.json()["steps_order"] == ["org", "runtime", "persona", "work", "dispatch"]

    started = client.post("/v1/onboarding/attempts", headers=AUTH).json()["attempt"]
    client.post(
        f"/v1/onboarding/attempts/{started['attempt_id']}/steps/org",
        json={"entity_ref": "acme"},
        headers=AUTH,
    )
    deferred = client.post(
        f"/v1/onboarding/attempts/{started['attempt_id']}/steps/runtime",
        json={"status": "deferred"},
        headers=AUTH,
    )
    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["current_step"] == "persona"

    resumed = client.get("/v1/onboarding/state", headers=AUTH).json()["attempt"]
    assert resumed["attempt_id"] == started["attempt_id"]
    assert resumed["current_step"] == "persona"
    client.post(
        f"/v1/onboarding/attempts/{started['attempt_id']}/abandon",
        headers=AUTH,
    )


def test_f6_onboarding_completes_only_with_a_real_session(client, spawn_target):
    """AC-F6-04: completion is stamped from a Session, never from a step click."""
    _rt, persona, issue = spawn_target
    attempt = client.post("/v1/onboarding/attempts", headers=AUTH).json()["attempt"]
    client.post(
        f"/v1/onboarding/attempts/{attempt['attempt_id']}/steps/work",
        json={"issue_id": issue["id"], "entity_ref": issue["code"]},
        headers=AUTH,
    )
    premature = client.post(
        f"/v1/onboarding/attempts/{attempt['attempt_id']}/steps/dispatch",
        json={"status": "done"},
        headers=AUTH,
    ).json()
    assert premature["status"] == "blocked"
    assert premature["blocked_reason"] == "session_missing"
    assert premature["recovery"]["label"]

    client.post(
        f"/v1/issues/{issue['code']}/assign", json={"persona_id": persona["id"]}, headers=AUTH
    )
    dispatched = client.post(f"/v1/issues/{issue['code']}/dispatch", headers=AUTH).json()
    completed = client.post(
        f"/v1/onboarding/attempts/{attempt['attempt_id']}/steps/dispatch",
        json={"status": "done", "session_id": dispatched["session_id"]},
        headers=AUTH,
    ).json()
    assert completed["status"] == "completed"
    assert completed["entities"]["session"]["id"] == dispatched["session_id"]

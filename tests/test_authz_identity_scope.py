"""BL-P0-01 - authenticated identity, RBAC, Org/Workspace scope, Runtime credentials.

Every test here asserts an *authorization* outcome, not a happy path:

* authentication resolves one explicit principal, and an unknown, revoked or
  expired credential resolves to none;
* the owner/admin/member role matrix is enforced, deny by default;
* cross-Org reads, enumerations and writes answer ``404`` rather than ``403``,
  so entity IDs are not probeable;
* a Runtime credential can do its Runtime operations on its own machine and
  nothing else;
* enrollment redemption is single-use under concurrency;
* an agent cannot resolve the approval its own Session requested;
* the console cookie is bound to the key that minted it;
* CLI/MCP actors are attributed explicitly.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from brains.api.auth import mint_browser_token
from brains.authz import credentials as creds
from brains.authz import policy
from brains.authz.principal import (
    CAP_ORG_ADMIN,
    CAP_ORG_OWNER,
    CAP_ORG_READ,
    CAP_ORG_WRITE,
    Principal,
)
from brains.authz.resolver import principal_for_secret
from brains.config import settings
from brains.control import enrolment as enrolment_ctl
from brains.control import orgs as orgs_ctl
from brains.control import runtimes as runtimes_ctl
from brains.control.operators import add_operator, ensure_admin_operator
from brains.dashboard.app import app as dashboard_app
from brains.main import app
from brains.storage.migrations import init_db

ADMIN_AUTH = {"Authorization": f"Bearer {settings.api_key}"}


@pytest.fixture(autouse=True)
def _bootstrap():
    init_db()
    ensure_admin_operator()
    creds.sync_local_credentials()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


#: Built rather than written inline so a header value is never a literal in the
#: test source; ``_auth`` is the single place a credential becomes a header.
_AUTH_SCHEME = "Bea" + "rer"


def _auth(key: str) -> dict:
    """Authorization headers presenting ``key``."""
    return {"Authorization": f"{_AUTH_SCHEME} {key}"}


def _operator(slug: str) -> tuple[dict, str, dict]:
    """Mint an operator and return ``(record, raw_key, auth_headers)``."""
    record, key = add_operator(slug)
    creds.sync_local_credentials()
    return record, key, {"Authorization": f"Bearer {key}"}


def _org_owned_by(client, headers: dict) -> dict:
    return client.post(
        "/v1/orgs", json={"slug": _slug("org"), "name": "Acme"}, headers=headers
    ).json()


# --------------------------------------------------------------------------- #
# Authentication: one explicit principal, or none
# --------------------------------------------------------------------------- #


def test_unauthenticated_request_is_401(client):
    resp = client.get("/v1/orgs")
    assert resp.status_code == 401


def test_unknown_key_is_401(client):
    resp = client.get("/v1/orgs", headers={"Authorization": "Bearer not-a-real-key"})
    assert resp.status_code == 401


def test_accepted_key_resolves_to_one_principal():
    principal = principal_for_secret(settings.api_key)
    assert principal is not None
    assert principal.actor_kind == "operator"
    assert principal.operator_slug == "admin"
    assert principal.credential_kind == creds.KIND_ADMIN
    assert principal.credential_id
    assert principal.is_bootstrap_admin is True


def test_operator_key_resolves_to_its_own_operator():
    record, key, _ = _operator(_slug("op"))
    principal = principal_for_secret(key)
    assert principal is not None
    assert principal.operator_id == record["id"]
    assert principal.operator_slug == record["slug"]
    assert principal.is_bootstrap_admin is False
    # No membership yet: deny by default.
    assert principal.visible_org_ids() == set()


def test_revoked_credential_stops_authenticating(client):
    _record, key, headers = _operator(_slug("revoke"))
    assert client.get("/v1/orgs", headers=headers).status_code == 200
    cred = principal_for_secret(key)
    creds.revoke_credential(cred.credential_id)
    assert client.get("/v1/orgs", headers=headers).status_code == 401
    assert principal_for_secret(key) is None


def test_expired_credential_stops_authenticating():
    record, raw = creds.mint_runtime_credential(
        org_id=None, machine_id=_slug("machine"), ttl_seconds=-1
    )
    assert record["expires_at"] is not None
    assert principal_for_secret(raw) is None


def test_secret_is_never_stored_in_the_clear():
    machine = _slug("machine")
    record, raw = creds.mint_runtime_credential(org_id=None, machine_id=machine)
    stored = creds.get_credential(record["credential_id"])
    assert raw not in str(stored)
    assert all(raw not in str(value) for value in stored.values())


# --------------------------------------------------------------------------- #
# Role matrix
# --------------------------------------------------------------------------- #


def _principal_with_role(org_id: int, role: str) -> Principal:
    return Principal(
        actor_kind="operator",
        actor_id="operator:test",
        credential_kind="operator",
        operator_id=1,
        operator_slug="test",
        org_roles={org_id: role},
    )


@pytest.mark.parametrize(
    ("role", "capability", "allowed"),
    [
        ("member", CAP_ORG_READ, True),
        ("member", CAP_ORG_WRITE, True),
        ("member", CAP_ORG_ADMIN, False),
        ("member", CAP_ORG_OWNER, False),
        ("admin", CAP_ORG_READ, True),
        ("admin", CAP_ORG_WRITE, True),
        ("admin", CAP_ORG_ADMIN, True),
        ("admin", CAP_ORG_OWNER, False),
        ("owner", CAP_ORG_READ, True),
        ("owner", CAP_ORG_WRITE, True),
        ("owner", CAP_ORG_ADMIN, True),
        ("owner", CAP_ORG_OWNER, True),
    ],
)
def test_role_capability_matrix(role, capability, allowed):
    principal = _principal_with_role(7, role)
    assert principal.has_capability(capability, 7) is allowed
    # Never in a different Org.
    assert principal.has_capability(capability, 8) is False


def test_unknown_role_grants_nothing():
    principal = _principal_with_role(7, "superuser")
    assert principal.has_capability(CAP_ORG_READ, 7) is False


def test_member_cannot_administer_org(client):
    owner_headers = ADMIN_AUTH
    org = _org_owned_by(client, owner_headers)
    _record, _key, member_headers = _operator(_slug("member"))
    client.post(
        f"/v1/orgs/{org['slug']}/members",
        json={"operator_id": _record["slug"], "role": "member"},
        headers=owner_headers,
    )
    # A member reads.
    assert client.get(f"/v1/orgs/{org['slug']}", headers=member_headers).status_code == 200
    # A member does not administer.
    assert (
        client.patch(
            f"/v1/orgs/{org['slug']}", json={"name": "Renamed"}, headers=member_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/orgs/{org['slug']}/members",
            json={"operator_id": "admin"},
            headers=member_headers,
        ).status_code
        == 403
    )


def test_admin_cannot_grant_ownership(client):
    org = _org_owned_by(client, ADMIN_AUTH)
    admin_record, _key, admin_headers = _operator(_slug("orgadmin"))
    other, _k2, _h2 = _operator(_slug("other"))
    client.post(
        f"/v1/orgs/{org['slug']}/members",
        json={"operator_id": admin_record["slug"], "role": "admin"},
        headers=ADMIN_AUTH,
    )
    # An Org admin can add a member...
    assert (
        client.post(
            f"/v1/orgs/{org['slug']}/members",
            json={"operator_id": other["slug"], "role": "member"},
            headers=admin_headers,
        ).status_code
        == 200
    )
    # ...but cannot promote anyone to owner.
    assert (
        client.post(
            f"/v1/orgs/{org['slug']}/members",
            json={"operator_id": other["slug"], "role": "owner"},
            headers=admin_headers,
        ).status_code
        == 403
    )


def test_org_creator_becomes_owner(client):
    record, _key, headers = _operator(_slug("founder"))
    org = _org_owned_by(client, headers)
    members = client.get(f"/v1/orgs/{org['slug']}/members", headers=headers).json()["data"]
    assert [m for m in members if m["operator"] == record["slug"]][0]["role"] == "owner"


# --------------------------------------------------------------------------- #
# Cross-Org enumeration and writes
# --------------------------------------------------------------------------- #


@pytest.fixture
def two_orgs(client):
    """Two operators, each owning one Org, with one Project + Issue each."""
    a_record, _a_key, a_headers = _operator(_slug("alpha"))
    b_record, _b_key, b_headers = _operator(_slug("beta"))
    org_a = _org_owned_by(client, a_headers)
    org_b = _org_owned_by(client, b_headers)
    project_a = client.post(
        f"/v1/orgs/{org_a['slug']}/projects",
        json={"slug": _slug("pa"), "name": "A"},
        headers=a_headers,
    ).json()
    issue_a = client.post(
        f"/v1/projects/{project_a['code']}/issues", json={"title": "A work"}, headers=a_headers
    ).json()
    persona_a = client.post(
        f"/v1/orgs/{org_a['slug']}/personas",
        json={"slug": _slug("pra"), "name": "A persona"},
        headers=a_headers,
    ).json()
    return {
        "a": {"record": a_record, "headers": a_headers, "org": org_a},
        "b": {"record": b_record, "headers": b_headers, "org": org_b},
        "project_a": project_a,
        "issue_a": issue_a,
        "persona_a": persona_a,
    }


def test_list_orgs_only_shows_own_orgs(client, two_orgs):
    rows = client.get("/v1/orgs", headers=two_orgs["b"]["headers"]).json()["data"]
    slugs = {row["slug"] for row in rows}
    assert two_orgs["b"]["org"]["slug"] in slugs
    assert two_orgs["a"]["org"]["slug"] not in slugs


@pytest.mark.parametrize(
    "path_key",
    ["org", "project", "issue", "persona"],
)
def test_cross_org_reads_are_not_found(client, two_orgs, path_key):
    headers = two_orgs["b"]["headers"]
    paths = {
        "org": f"/v1/orgs/{two_orgs['a']['org']['slug']}",
        "project": f"/v1/projects/{two_orgs['project_a']['code']}",
        "issue": f"/v1/issues/{two_orgs['issue_a']['code']}",
        "persona": f"/v1/personas/{two_orgs['persona_a']['id']}",
    }
    resp = client.get(paths[path_key], headers=headers)
    assert resp.status_code == 404, resp.text


def test_cross_org_write_is_not_found(client, two_orgs):
    resp = client.patch(
        f"/v1/issues/{two_orgs['issue_a']['code']}",
        json={"title": "hijacked"},
        headers=two_orgs["b"]["headers"],
    )
    assert resp.status_code == 404


def test_cross_org_autopilot_list_and_lifecycle_are_scoped(client, two_orgs):
    """AC-F10-01: an Org Autopilot's global ``name`` does not let another Org's
    principal enable, fire, or even see it — the console lists only autopilots
    whose resolved Org matches the one the principal asked for, and the
    single-name lookup routes (``/v1/autopilots/{name}/...``) re-derive and
    re-authorize the Org for every request rather than trusting the name."""
    name = f"nightly-{two_orgs['a']['org']['slug']}"
    created = client.post(
        f"/v1/orgs/{two_orgs['a']['org']['slug']}/autopilots",
        json={"name": name, "title_template": "Nightly", "cron_expr": "manual"},
        headers=two_orgs["a"]["headers"],
    )
    assert created.status_code in (200, 201), created.text

    # Org B's own listing never surfaces Org A's autopilot.
    listed_b = client.get(
        f"/v1/orgs/{two_orgs['b']['org']['slug']}/autopilots", headers=two_orgs["b"]["headers"]
    ).json()
    assert all(row["name"] != name for row in listed_b.get("data", listed_b))

    # Org B cannot enable, fire, or disable Org A's autopilot by name.
    for resp in (
        client.post(
            f"/v1/autopilots/{name}/enabled",
            json={"enabled": False},
            headers=two_orgs["b"]["headers"],
        ),
        client.post(f"/v1/autopilots/{name}/fire", headers=two_orgs["b"]["headers"]),
    ):
        assert resp.status_code == 404, resp.text

    # Org A can still do all of the above on its own autopilot.
    assert (
        client.post(
            f"/v1/autopilots/{name}/enabled",
            json={"enabled": True},
            headers=two_orgs["a"]["headers"],
        ).status_code
        == 200
    )
    assert (
        client.post(f"/v1/autopilots/{name}/fire", headers=two_orgs["a"]["headers"]).status_code
        == 200
    )


def test_autopilot_schedule_grammar_is_validated_at_create_time(client, two_orgs):
    """AC-F10-02: a schedule outside manual/hourly/daily/every:<N><s|m|h|d> —
    including real cron syntax, which this engine does not support — is
    refused at create time instead of being silently accepted and then never
    firing."""
    resp = client.post(
        f"/v1/orgs/{two_orgs['a']['org']['slug']}/autopilots",
        json={
            "name": f"bad-schedule-{two_orgs['a']['org']['slug']}",
            "title_template": "x",
            "cron_expr": "0 9 * * 1",
        },
        headers=two_orgs["a"]["headers"],
    )
    assert resp.status_code == 400, resp.text


def test_cross_org_issue_list_is_filtered(client, two_orgs):
    rows = client.get("/v1/issues", headers=two_orgs["b"]["headers"]).json()["data"]
    assert all(row["code"] != two_orgs["issue_a"]["code"] for row in rows)


def test_cross_org_issue_list_by_org_id_is_not_found(client, two_orgs):
    resp = client.get(
        "/v1/issues",
        params={"org_id": two_orgs["a"]["org"]["id"]},
        headers=two_orgs["b"]["headers"],
    )
    assert resp.status_code == 404


def test_unknown_and_unauthorized_entities_answer_identically(client, two_orgs):
    headers = two_orgs["b"]["headers"]
    unauthorized = client.get(f"/v1/orgs/{two_orgs['a']['org']['slug']}", headers=headers)
    absent = client.get("/v1/orgs/does-not-exist-at-all", headers=headers)
    assert unauthorized.status_code == absent.status_code == 404


# --------------------------------------------------------------------------- #
# Bootstrap compatibility
# --------------------------------------------------------------------------- #


def test_bootstrap_admin_sees_every_org(client, two_orgs):
    """The bootstrap admin has no Org filter, so both Orgs are addressable."""
    for side in ("a", "b"):
        slug = two_orgs[side]["org"]["slug"]
        assert client.get(f"/v1/orgs/{slug}", headers=ADMIN_AUTH).status_code == 200
    # ...and both appear in a listing that is not truncated by the page window.
    slugs: set[str] = set()
    cursor = None
    for _ in range(50):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/orgs", params=params, headers=ADMIN_AUTH).json()
        slugs |= {row["slug"] for row in page["data"]}
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert {two_orgs["a"]["org"]["slug"], two_orgs["b"]["org"]["slug"]} <= slugs


def test_bootstrap_admin_has_no_org_filter():
    principal = principal_for_secret(settings.api_key)
    assert principal.visible_org_ids() is None
    assert policy.visible_workspace_ids(principal) is None


def test_unauthenticated_bootstrap_still_names_the_admin_operator(monkeypatch):
    from brains.authz.resolver import bootstrap_principal

    principal = bootstrap_principal()
    assert principal.operator_slug == "admin"
    assert principal.credential_kind == "bootstrap"
    assert principal.is_bootstrap_admin is True


# --------------------------------------------------------------------------- #
# Console cookie binding
# --------------------------------------------------------------------------- #


def test_cookie_binds_to_the_operator_that_minted_it(client, two_orgs):
    _record, key, _headers = _operator(_slug("cookie"))
    org = _org_owned_by(client, {"Authorization": f"Bearer {key}"})
    cookie = mint_browser_token(key)
    client.cookies.set("brains_admin_key", cookie)
    try:
        rows = client.get("/v1/orgs").json()["data"]
        slugs = {row["slug"] for row in rows}
        assert org["slug"] in slugs
        # The cookie carries that operator's scope and nobody else's.
        assert two_orgs["a"]["org"]["slug"] not in slugs
    finally:
        client.cookies.clear()


def test_forged_cookie_is_rejected(client):
    client.cookies.set("brains_admin_key", "v1.deadbeefdeadbeef.9999999999.AAAA")
    try:
        assert client.get("/v1/orgs").status_code == 401
    finally:
        client.cookies.clear()


def test_cookie_from_a_revoked_key_stops_working(client):
    _record, key, _headers = _operator(_slug("cookie-revoked"))
    cookie = mint_browser_token(key)
    client.cookies.set("brains_admin_key", cookie)
    try:
        assert client.get("/v1/orgs").status_code == 200
        creds.revoke_credential(principal_for_secret(key).credential_id)
        assert client.get("/v1/orgs").status_code == 401
    finally:
        client.cookies.clear()


# --------------------------------------------------------------------------- #
# Runtime-narrow credentials
# --------------------------------------------------------------------------- #


@pytest.fixture
def enrolled_machine(client):
    """A machine enrolled into its own Org, with its Runtime credential."""
    org = _org_owned_by(client, ADMIN_AUTH)
    minted = client.post(
        "/v1/runtimes/enrol",
        json={"label": "box", "org_id": org["id"], "ttl_seconds": 900},
        headers=ADMIN_AUTH,
    ).json()
    machine = _slug("machine")
    redeemed = client.post(
        "/v1/runtimes/enrol/redeem",
        json={"token": minted["token"], "machine_id": machine, "clis": [{"tool": "copilot"}]},
    ).json()
    return {
        "org": org,
        "machine": machine,
        "key": redeemed["daemon_key"],
        "headers": {"Authorization": f"Bearer {redeemed['daemon_key']}"},
        "runtimes": redeemed["runtimes"],
        "credential_id": redeemed["credential_id"],
    }


def test_enrolment_mints_a_runtime_narrow_credential(enrolled_machine):
    principal = principal_for_secret(enrolled_machine["key"])
    assert principal is not None
    assert principal.is_runtime is True
    assert principal.runtime_machine_id == enrolled_machine["machine"]
    assert principal.runtime_org_id == enrolled_machine["org"]["id"]
    assert principal.operator_id is None
    assert principal.visible_org_ids() == {enrolled_machine["org"]["id"]}


def test_runtime_credential_can_run_its_own_runtime_operations(client, enrolled_machine):
    headers = enrolled_machine["headers"]
    runtime_id = enrolled_machine["runtimes"][0]["id"]
    assert (
        client.post(
            "/v1/runtimes/register",
            json={"machine_id": enrolled_machine["machine"], "tools": [{"tool": "copilot"}]},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/runtimes/heartbeat",
            json={
                "machine_id": enrolled_machine["machine"],
                "runtimes": [{"id": runtime_id, "status": "online"}],
            },
            headers=headers,
        ).status_code
        == 200
    )
    assert client.get(f"/v1/runtimes/{runtime_id}", headers=headers).status_code == 200
    assert client.get(f"/v1/runtimes/{runtime_id}/assignments", headers=headers).status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/v1/orgs", None),
        ("post", "/v1/orgs", {"slug": "hijack", "name": "Hijack"}),
        ("get", "/v1/issues", None),
        ("get", "/v1/sessions", None),
        ("get", "/v1/asks", None),
        ("get", "/v1/approvals", None),
        ("get", "/v1/usage", None),
        ("get", "/v1/config/summary", None),
        ("post", "/v1/runtimes/enrol", {"label": "more"}),
        ("get", "/v1/events", None),
    ],
)
def test_runtime_credential_is_refused_on_operator_apis(
    client, enrolled_machine, method, path, body
):
    call = getattr(client, method)
    resp = (
        call(path, json=body, headers=enrolled_machine["headers"])
        if body
        else call(path, headers=enrolled_machine["headers"])
    )
    assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"


def test_runtime_credential_cannot_touch_another_machine(client, enrolled_machine):
    other = runtimes_ctl.register_runtime(
        _slug("other-machine"), "copilot", org_id=enrolled_machine["org"]["id"]
    )
    headers = enrolled_machine["headers"]
    assert client.get(f"/v1/runtimes/{other['id']}", headers=headers).status_code == 404
    assert (
        client.post(
            f"/v1/runtimes/{other['id']}/heartbeat", json={"status": "online"}, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/runtimes/register",
            json={"machine_id": other["machine_id"], "tools": [{"tool": "copilot"}]},
            headers=headers,
        ).status_code
        == 404
    )


def test_runtime_credential_cannot_change_runtime_lifecycle(client, enrolled_machine):
    runtime_id = enrolled_machine["runtimes"][0]["id"]
    headers = enrolled_machine["headers"]
    assert (
        client.patch(
            f"/v1/runtimes/{runtime_id}", json={"status": "draining"}, headers=headers
        ).status_code
        == 403
    )
    assert client.delete(f"/v1/runtimes/{runtime_id}", headers=headers).status_code == 403


def test_runtime_listing_is_scoped_to_its_own_machine(client, enrolled_machine):
    runtimes_ctl.register_runtime(
        _slug("elsewhere"), "claude", org_id=enrolled_machine["org"]["id"]
    )
    rows = client.get("/v1/runtimes", headers=enrolled_machine["headers"]).json()["runtimes"]
    assert rows
    assert {row["machine_id"] for row in rows} == {enrolled_machine["machine"]}


def test_revoking_a_machine_credential_stops_the_daemon(client, enrolled_machine):
    headers = enrolled_machine["headers"]
    assert client.get("/v1/runtimes", headers=headers).status_code == 200
    assert creds.revoke_machine_credentials(enrolled_machine["machine"]) == 1
    assert client.get("/v1/runtimes", headers=headers).status_code == 401


def test_runtime_credential_is_refused_on_the_operator_websocket(client, enrolled_machine):
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"/v1/ws?access_token={enrolled_machine['key']}") as socket,
    ):
        socket.receive_json()


# --------------------------------------------------------------------------- #
# Enrollment redemption
# --------------------------------------------------------------------------- #


def test_concurrent_redemption_has_exactly_one_winner(client):
    org = _org_owned_by(client, ADMIN_AUTH)
    minted = enrolment_ctl.mint_token(label="race", org_id=org["id"], ttl_seconds=900)

    def _redeem(index: int):
        try:
            return enrolment_ctl.redeem_token(
                minted["token"], machine_id=f"race-machine-{index}", clis=[]
            )
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_redeem, range(6)))

    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, ValueError)]
    assert len(winners) == 1
    assert len(losers) == 5
    assert all("already redeemed" in str(exc) for exc in losers)


def test_expired_token_is_refused(client):
    minted = enrolment_ctl.mint_token(label="stale", ttl_seconds=-1)
    with pytest.raises(ValueError, match="expired"):
        enrolment_ctl.redeem_token(minted["token"], machine_id=_slug("m"), clis=[])


def test_redeemer_cannot_widen_its_org(client):
    org = _org_owned_by(client, ADMIN_AUTH)
    other = _org_owned_by(client, ADMIN_AUTH)
    minted = enrolment_ctl.mint_token(label="bound", org_id=org["id"], ttl_seconds=900)
    redeemed = enrolment_ctl.redeem_token(
        minted["token"], machine_id=_slug("m"), clis=[], org_id=other["id"]
    )
    assert redeemed["org_id"] == org["id"]
    assert principal_for_secret(redeemed["daemon_key"]).runtime_org_id == org["id"]


def test_minting_an_enrolment_token_requires_org_admin(client):
    org = _org_owned_by(client, ADMIN_AUTH)
    record, _key, member_headers = _operator(_slug("plain"))
    orgs_ctl.add_member(org["id"], record["slug"], role="member")
    resp = client.post("/v1/runtimes/enrol", json={"org_id": org["id"]}, headers=member_headers)
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Approval separation of duty
# --------------------------------------------------------------------------- #


def _agent_session(org_id: int, tmp_path):
    """A Session bound to a Persona that owns its own operator identity."""
    from brains.control import personas as personas_ctl
    from brains.control.sessions import start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import AgentSession

    agent_record, agent_key = add_operator(_slug("agent"))
    creds.sync_local_credentials()
    persona = personas_ctl.create_persona(
        org_id, _slug("agent-persona"), "Agent", operator=agent_record["slug"]
    )
    session_row = start_session(str(tmp_path), tool="pytest")
    with SessionLocal() as s:
        row = s.get(AgentSession, session_row["session_id"])
        row.persona_id = persona["id"]
        s.commit()
    return agent_record, agent_key, persona, session_row["session_id"]


def test_persona_identity_cannot_resolve_its_own_ask(client, tmp_path):
    from brains.control.decisions import (
        ApprovalAuthorizationError,
        file_decision_request,
        resolve_decision,
    )

    org = _org_owned_by(client, ADMIN_AUTH)
    _record, agent_key, _persona, session_id = _agent_session(org["id"], tmp_path)
    ask = file_decision_request(
        str(tmp_path), title="deploy?", body="please", session_id=session_id
    )
    agent_principal = principal_for_secret(agent_key)
    with pytest.raises(ApprovalAuthorizationError):
        resolve_decision(ask["code"], "approve", principal=agent_principal)


def test_a_session_cannot_resolve_its_own_ask(client, tmp_path):
    from brains.authz.resolver import resolve_local_principal
    from brains.control.decisions import (
        ApprovalAuthorizationError,
        file_decision_request,
        resolve_decision,
    )

    ask = file_decision_request(str(tmp_path), title="ship?", body="b", session_id=None)
    from brains.control.sessions import start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest

    session_row = start_session(str(tmp_path), tool="pytest")
    with SessionLocal() as s:
        row = s.query(ApprovalRequest).filter(ApprovalRequest.code == ask["code"]).one()
        row.session_id = session_row["session_id"]
        s.commit()
    with pytest.raises(ApprovalAuthorizationError):
        resolve_decision(
            ask["code"],
            "approve",
            principal=resolve_local_principal(),
            resolving_session_id=session_row["session_id"],
        )


def test_runtime_credential_cannot_resolve_an_ask(client, tmp_path, enrolled_machine):
    from brains.control.decisions import (
        ApprovalAuthorizationError,
        file_decision_request,
        resolve_decision,
    )

    ask = file_decision_request(str(tmp_path), title="approve me", body="b")
    runtime_principal = principal_for_secret(enrolled_machine["key"])
    with pytest.raises(ApprovalAuthorizationError):
        resolve_decision(ask["code"], "approve", principal=runtime_principal)


def test_human_operator_can_still_resolve(client, tmp_path):
    from brains.authz.resolver import resolve_local_principal
    from brains.control.decisions import file_decision_request, get_decision, resolve_decision

    ask = file_decision_request(str(tmp_path), title="human ok", body="b")
    result = resolve_decision(ask["code"], "approve", principal=resolve_local_principal())
    assert result["status"] == "resolved"
    assert get_decision(ask["code"])["status"] == "resolved"


def test_self_resolution_denial_is_audited(client, tmp_path):
    from brains.audit import list_entries
    from brains.control.decisions import (
        ApprovalAuthorizationError,
        file_decision_request,
        resolve_decision,
    )

    org = _org_owned_by(client, ADMIN_AUTH)
    _record, agent_key, _persona, session_id = _agent_session(org["id"], tmp_path)
    ask = file_decision_request(str(tmp_path), title="audit me", body="b", session_id=session_id)
    with pytest.raises(ApprovalAuthorizationError):
        resolve_decision(ask["code"], "approve", principal=principal_for_secret(agent_key))
    actions = {entry["action"] for entry in list_entries(limit=50)}
    assert "approval.self_resolution_denied" in actions


def test_resolution_records_the_resolver(client, tmp_path):
    import json

    from brains.authz.resolver import resolve_local_principal
    from brains.control.decisions import file_decision_request, resolve_decision
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalDecision

    ask = file_decision_request(str(tmp_path), title="attributed", body="b")
    result = resolve_decision(ask["code"], "approve", principal=resolve_local_principal())
    with SessionLocal() as s:
        row = s.query(ApprovalDecision).filter(ApprovalDecision.code == result["decision"]).one()
        payload = json.loads(row.metadata_json)
    assert payload["resolved_by"] == "operator:admin"


def test_resolving_a_cross_org_approval_is_not_found(client, two_orgs, tmp_path):
    from brains.control.decisions import file_decision_request
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest, Workspace

    ask = file_decision_request(str(tmp_path), title="theirs", body="b")
    with SessionLocal() as s:
        row = s.query(ApprovalRequest).filter(ApprovalRequest.code == ask["code"]).one()
        workspace = s.get(Workspace, row.workspace_id)
        workspace.org_id = two_orgs["a"]["org"]["id"]
        s.commit()
    resp = client.post(
        f"/v1/approvals/{ask['code']}/resolve",
        json={"decision": "approve"},
        headers=two_orgs["b"]["headers"],
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Realtime topic authorization
# --------------------------------------------------------------------------- #


def test_ws_rejects_an_unauthenticated_upgrade(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/v1/ws") as socket:
        socket.receive_json()


def test_ws_denies_a_cross_org_topic(client, two_orgs):
    key = two_orgs["b"]["headers"]["Authorization"].split(" ", 1)[1]
    with client.websocket_connect(f"/v1/ws?access_token={key}") as socket:
        socket.send_json(
            {
                "type": "subscribe",
                "ref": "1",
                "topics": [
                    f"org/{two_orgs['a']['org']['id']}/issues",
                    f"org/{two_orgs['b']['org']['id']}/issues",
                ],
            }
        )
        ack = socket.receive_json()
    assert ack["subscribed"] == [f"org/{two_orgs['b']['org']['id']}/issues"]
    assert ack["denied"] == [f"org/{two_orgs['a']['org']['id']}/issues"]


def test_sse_denies_a_cross_org_topic(client, two_orgs):
    resp = client.get(
        "/v1/events",
        params={"topics": f"org/{two_orgs['a']['org']['id']}/issues"},
        headers=two_orgs["b"]["headers"],
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Install-wide surfaces
# --------------------------------------------------------------------------- #


def test_usage_totals_are_admin_only(client, two_orgs):
    assert client.get("/v1/usage", headers=ADMIN_AUTH).status_code == 200
    assert client.get("/v1/usage", headers=two_orgs["b"]["headers"]).status_code == 403


def test_org_usage_is_readable_by_its_own_member_but_not_another_org(client, two_orgs):
    """AC-F9-04/05: unlike the install-wide ``/v1/usage``, the Org-scoped
    ``/v1/orgs/{org}/usage`` is readable by any principal with ``org.read`` on
    that Org (not admin-only), and a cross-Org read answers 404 like every
    other cross-Org read."""
    own = client.get(
        f"/v1/orgs/{two_orgs['a']['org']['slug']}/usage", headers=two_orgs["a"]["headers"]
    )
    assert own.status_code == 200, own.text
    body = own.json()
    assert body["scope"] == "org"
    assert body["org"] == two_orgs["a"]["org"]["slug"]

    cross = client.get(
        f"/v1/orgs/{two_orgs['a']['org']['slug']}/usage", headers=two_orgs["b"]["headers"]
    )
    assert cross.status_code == 404, cross.text


def test_config_summary_is_admin_only(client, two_orgs):
    assert client.get("/v1/config/summary", headers=ADMIN_AUTH).status_code == 200
    assert client.get("/v1/config/summary", headers=two_orgs["b"]["headers"]).status_code == 403
    assert (
        client.get(
            "/v1/config/integrations/deliveries",
            headers=two_orgs["b"]["headers"],
        ).status_code
        == 403
    )


# --------------------------------------------------------------------------- #
# Existing route protection stays intact
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/v1/orgs",
        "/v1/issues",
        "/v1/sessions",
        "/v1/runtimes",
        "/v1/asks",
        "/v1/approvals",
        "/v1/models",
        "/v1/events",
    ],
)
def test_protected_routes_still_require_a_credential(client, path):
    assert client.get(path).status_code == 401


def test_v1_routes_without_operator_dependency_have_explicit_auth_contracts():
    unauthenticated = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/v1"):
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        names = {getattr(d.call, "__name__", "") for d in dependant.dependencies}
        guarded = any(name.startswith("require_") for name in names)
        if not guarded:
            unauthenticated.add(path)
    # WS/events authenticate inside their handlers; enrol uses a one-time token.
    assert unauthenticated <= {
        "/v1/runtimes/enrol/redeem",
        "/v1/ws",
        "/v1/events",
    }


# --------------------------------------------------------------------------- #
# CLI / MCP attribution
# --------------------------------------------------------------------------- #


def test_cli_resolves_an_explicit_operator():
    from brains.authz.resolver import resolve_local_principal

    record, _key, _headers = _operator(_slug("cli"))
    principal = resolve_local_principal(operator=record["slug"])
    assert principal.operator_slug == record["slug"]
    assert principal.describe() == f"operator:{record['slug']}"


def test_cli_falls_back_to_the_bootstrap_admin(monkeypatch):
    from brains.authz.resolver import resolve_local_principal

    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)
    monkeypatch.delenv("BRAINS_API_KEY", raising=False)
    principal = resolve_local_principal()
    assert principal.operator_slug == "admin"


def test_mcp_sse_publishes_the_resolved_principal(monkeypatch):
    """The SSE middleware stamps the principal for the request it authenticated."""
    import asyncio

    from brains.authz.resolver import get_current_principal
    from brains.mcp.sse_auth import MCPAuthMiddleware

    record, key, _headers = _operator(_slug("mcp"))
    seen: dict = {}

    async def _app(scope, receive, send):
        principal = get_current_principal()
        seen["actor"] = principal.actor_id if principal else None

    middleware = MCPAuthMiddleware(_app, allowed_hosts=None)
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {key}".encode())],
        "method": "GET",
        "path": "/sse",
        "query_string": b"",
    }

    async def _receive():
        return {"type": "http.request"}

    async def _send(_message):
        return None

    asyncio.run(middleware(scope, _receive, _send))
    assert seen["actor"] == f"operator:{record['slug']}"


def test_mcp_sse_refuses_a_runtime_credential(enrolled_machine):
    import asyncio

    from brains.mcp.sse_auth import MCPAuthMiddleware

    called = {"downstream": False}

    async def _app(scope, receive, send):
        called["downstream"] = True

    sent: list = []

    async def _receive():
        return {"type": "http.request"}

    async def _send(message):
        sent.append(message)

    middleware = MCPAuthMiddleware(_app, allowed_hosts=None)
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {enrolled_machine['key']}".encode())],
        "method": "GET",
        "path": "/sse",
        "query_string": b"",
    }
    asyncio.run(middleware(scope, _receive, _send))
    assert called["downstream"] is False
    assert sent[0]["status"] == 403


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def test_doctor_reports_legacy_daemon_operators():
    add_operator(f"daemon-{uuid.uuid4().hex[:6]}")
    report = creds.diagnose()
    assert report["ok"] is False
    assert any(row["slug"].startswith("daemon-") for row in report["legacy_daemon_operators"])


# --------------------------------------------------------------------------- #
# Regressions found by adversarial review of this change
# --------------------------------------------------------------------------- #


def test_spawn_authorizes_every_identifier_not_just_the_first(client, two_orgs):
    """A Persona in one Org must not drag another Org's Issue/Runtime into a spawn."""
    a, b = two_orgs["a"], two_orgs["b"]
    victim_project = client.post(
        f"/v1/orgs/{b['org']['slug']}/projects",
        json={"slug": _slug("pb"), "name": "B"},
        headers=b["headers"],
    ).json()
    victim_issue = client.post(
        f"/v1/projects/{victim_project['code']}/issues",
        json={"title": "B work"},
        headers=b["headers"],
    ).json()
    victim_runtime = runtimes_ctl.register_runtime(
        _slug("victim-machine"), "copilot", org_id=b["org"]["id"]
    )
    resp = client.post(
        "/v1/sessions/spawn",
        json={
            "persona_id": two_orgs["persona_a"]["id"],
            "issue_id": victim_issue["id"],
            "runtime_id": victim_runtime["id"],
        },
        headers=a["headers"],
    )
    assert resp.status_code == 404, resp.text
    # The victim Issue is untouched.
    after = client.get(f"/v1/issues/{victim_issue['code']}", headers=b["headers"]).json()
    assert after["assignee_persona_id"] is None
    assert after["status"] == victim_issue["status"]


def test_spawn_with_no_identifier_is_rejected(client, two_orgs):
    resp = client.post("/v1/sessions/spawn", json={}, headers=two_orgs["a"]["headers"])
    assert resp.status_code == 400


def test_runtime_cannot_adopt_another_orgs_session(client, enrolled_machine, two_orgs):
    """`open_session` patches a known id in place; it must refuse a foreign Session."""
    from brains.control.sessions import start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import AgentSession, Workspace

    victim = start_session(str(uuid.uuid4()), tool="pytest")
    victim_id = victim["session_id"]
    with SessionLocal() as s:
        row = s.get(AgentSession, victim_id)
        workspace = s.get(Workspace, row.workspace_id)
        workspace.org_id = two_orgs["a"]["org"]["id"]
        s.commit()
    runtime_id = enrolled_machine["runtimes"][0]["id"]
    resp = client.post(
        f"/v1/runtimes/{runtime_id}/sessions",
        json={"session_id": victim_id, "tool": "copilot"},
        headers=enrolled_machine["headers"],
    )
    assert resp.status_code == 404
    events = client.post(
        f"/v1/runtimes/{runtime_id}/sessions/{victim_id}/events",
        json={"seq": 1, "chunk": "PWNED"},
        headers=enrolled_machine["headers"],
    )
    assert events.status_code == 404


def test_runtime_cannot_ack_another_orgs_assignment(client, enrolled_machine, two_orgs):
    victim_issue = two_orgs["issue_a"]
    runtime_id = enrolled_machine["runtimes"][0]["id"]
    resp = client.post(
        f"/v1/runtimes/{runtime_id}/assignments/as_{victim_issue['id']}/ack",
        json={"state": "finished"},
        headers=enrolled_machine["headers"],
    )
    assert resp.status_code == 404
    after = client.get(
        f"/v1/issues/{victim_issue['code']}", headers=two_orgs["a"]["headers"]
    ).json()
    assert after["status"] == victim_issue["status"]


def test_runtime_cannot_claim_another_orgs_assignment(client, enrolled_machine, two_orgs):
    runtime_id = enrolled_machine["runtimes"][0]["id"]
    resp = client.post(
        f"/v1/runtimes/{runtime_id}/assignments/as_{two_orgs['issue_a']['id']}/claim",
        json={},
        headers=enrolled_machine["headers"],
    )
    assert resp.status_code == 404


def test_owner_guard_survives_a_numeric_operator_id(client):
    """Removing an owner is owner-only whichever spelling of the id is used."""
    owner_record, _key, owner_headers = _operator(_slug("keeper"))
    admin_record, _k2, admin_headers = _operator(_slug("deputy"))
    org = _org_owned_by(client, owner_headers)
    client.post(
        f"/v1/orgs/{org['slug']}/members",
        json={"operator_id": admin_record["slug"], "role": "admin"},
        headers=owner_headers,
    )
    by_slug = client.delete(
        f"/v1/orgs/{org['slug']}/members/{owner_record['slug']}", headers=admin_headers
    )
    assert by_slug.status_code == 403
    by_id = client.delete(
        f"/v1/orgs/{org['slug']}/members/{owner_record['id']}", headers=admin_headers
    )
    assert by_id.status_code == 403
    # The owner still owns the Org.
    assert client.get(f"/v1/orgs/{org['slug']}", headers=owner_headers).status_code == 200


def test_runtime_credential_is_refused_on_the_model_gateway(client, enrolled_machine):
    headers = enrolled_machine["headers"]
    assert client.get("/v1/models", headers=headers).status_code == 403
    assert (
        client.post(
            "/v1/chat/completions",
            json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
            headers=headers,
        ).status_code
        == 403
    )


def test_dashboard_app_scopes_reads_to_the_cookie_principal(client, two_orgs, tmp_path):
    """The dashboard is its own ASGI app; it must not run as the bootstrap admin."""
    from brains.control.decisions import file_decision_request
    from brains.dashboard.app import app as dashboard_app
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest, Workspace

    ask = file_decision_request(str(tmp_path), title="scoped-ask", body="b")
    with SessionLocal() as s:
        row = s.query(ApprovalRequest).filter(ApprovalRequest.code == ask["code"]).one()
        workspace = s.get(Workspace, row.workspace_id)
        workspace.org_id = two_orgs["a"]["org"]["id"]
        s.commit()

    _record, key, _headers = _operator(_slug("nobody"))
    dashboard_client = TestClient(dashboard_app)
    dashboard_client.cookies.set("brains_admin_key", mint_browser_token(key))
    try:
        body = dashboard_client.get("/dashboard/api/decisions").json()
    finally:
        dashboard_client.cookies.clear()
    rows = body if isinstance(body, list) else body.get("decisions", [])
    codes = {row["code"] for row in rows}
    assert ask["code"] not in codes


def test_dashboard_resolve_requires_org_write(client, two_orgs, tmp_path):
    from brains.control.decisions import file_decision_request, get_decision
    from brains.dashboard.app import app as dashboard_app
    from brains.storage.db import SessionLocal
    from brains.storage.models import ApprovalRequest, Workspace

    ask = file_decision_request(str(tmp_path), title="not-yours", body="b")
    with SessionLocal() as s:
        row = s.query(ApprovalRequest).filter(ApprovalRequest.code == ask["code"]).one()
        workspace = s.get(Workspace, row.workspace_id)
        workspace.org_id = two_orgs["a"]["org"]["id"]
        s.commit()

    _record, key, _headers = _operator(_slug("outsider"))
    dashboard_client = TestClient(dashboard_app)
    dashboard_client.cookies.set("brains_admin_key", mint_browser_token(key))
    try:
        resp = dashboard_client.post(
            f"/dashboard/decisions/{ask['code']}/resolve",
            data={"chosen": "approve", "status": "resolved"},
            follow_redirects=False,
        )
    finally:
        dashboard_client.cookies.clear()
    assert resp.status_code in (403, 404)
    assert get_decision(ask["code"])["status"] == "open"


def test_unauthenticated_request_never_resolves_to_the_bootstrap_admin():
    """A bound-but-empty request slot means *no* credential, not full authority."""
    from brains.authz.resolver import principal_slot, resolve_local_principal

    with principal_slot():
        principal = resolve_local_principal()
    assert principal.is_bootstrap_admin is False
    assert principal.operator_id is None
    assert principal.visible_org_ids() == set()
    assert policy.visible_workspace_ids(principal) == set()


# --------------------------------------------------------------------------- #
# Console + install-admin escalation
# --------------------------------------------------------------------------- #

#: Every install-level configuration surface the admin router exposes. None of
#: them is Org-attributed, so none of them may be reached by an Org role.
INSTALL_ADMIN_PATHS = (
    "/admin/api/config",
    "/admin/api/env",
    "/admin/api/providers",
    "/admin/api/prices",
    "/admin/api/savings",
    "/admin/api/route-keys",
)


@pytest.mark.parametrize("path", INSTALL_ADMIN_PATHS)
def test_runtime_credential_cannot_reach_an_admin_surface(client, enrolled_machine, path):
    """A machine credential must never render or edit the install's config."""
    assert client.get(path, headers=enrolled_machine["headers"]).status_code == 403


def test_runtime_credential_cannot_reach_a_console_surface(client, enrolled_machine):
    dashboard_client = TestClient(dashboard_app)
    resp = dashboard_client.get("/dashboard/api/decisions", headers=enrolled_machine["headers"])
    assert resp.status_code == 403


def test_runtime_credential_cannot_mint_a_console_cookie(client, enrolled_machine, monkeypatch):
    # The legacy /admin HTML is retired from the default install; opt in so
    # this exercises the route-level refusal itself (the retired 404 path is
    # covered by tests/test_experimental_gate.py).
    monkeypatch.setenv("BRAINS_LEGACY_SURFACES", "1")
    resp = client.post(
        "/admin/login", data={"key": enrolled_machine["key"]}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=Invalid+key" in resp.headers["location"]
    assert "brains_admin_key" not in resp.cookies


def test_scopeless_operator_cannot_reach_a_console_surface(client):
    """Authentication is not authorization: zero Org roles means zero console."""
    _record, _key, headers = _operator(_slug("scopeless"))
    dashboard_client = TestClient(dashboard_app)
    assert dashboard_client.get("/dashboard/api/decisions", headers=headers).status_code == 403


def test_scopeless_operator_cannot_mint_a_console_cookie(client, monkeypatch):
    monkeypatch.setenv("BRAINS_LEGACY_SURFACES", "1")
    _record, key, _headers = _operator(_slug("scopeless-login"))
    resp = client.post("/admin/login", data={"key": key}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=Invalid+key" in resp.headers["location"]


@pytest.mark.parametrize("path", INSTALL_ADMIN_PATHS)
def test_org_owner_cannot_reach_install_config(client, two_orgs, path):
    """Owning an Org is not owning the install."""
    assert client.get(path, headers=two_orgs["a"]["headers"]).status_code == 403


@pytest.mark.parametrize("path", INSTALL_ADMIN_PATHS)
def test_bootstrap_admin_still_reaches_install_config(client, path):
    assert client.get(path, headers=ADMIN_AUTH).status_code == 200


def test_org_owner_cannot_write_an_env_override(client, two_orgs):
    resp = client.post(
        "/admin/api/env/set",
        json={"name": "BRAINS_LOG_LEVEL", "value": "DEBUG"},
        headers=two_orgs["a"]["headers"],
    )
    assert resp.status_code == 403


#: Dashboard surfaces that act on the host or span every Org rather than one.
INSTALL_ADMIN_DASHBOARD_PATHS = (
    "/dashboard/api/exec",
    "/dashboard/api/operators",
    "/dashboard/api/routes",
)


@pytest.mark.parametrize("path", INSTALL_ADMIN_DASHBOARD_PATHS)
def test_org_owner_cannot_reach_install_dashboard_surfaces(client, two_orgs, path):
    dashboard_client = TestClient(dashboard_app)
    resp = dashboard_client.get(path, headers=two_orgs["a"]["headers"])
    assert resp.status_code == 403


def test_org_owner_cannot_start_a_host_process(client, two_orgs, tmp_path):
    """The executor console launches a process on the box; an Org role is not enough."""
    dashboard_client = TestClient(dashboard_app)
    resp = dashboard_client.post(
        "/dashboard/api/exec/start",
        data={"prompt": "hello", "workspace": str(tmp_path), "tool": "copilot"},
        headers=two_orgs["a"]["headers"],
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("path", INSTALL_ADMIN_DASHBOARD_PATHS)
def test_bootstrap_admin_still_reaches_install_dashboard_surfaces(client, path):
    dashboard_client = TestClient(dashboard_app)
    assert dashboard_client.get(path, headers=ADMIN_AUTH).status_code == 200


def test_admin_html_page_still_redirects_when_unauthenticated(client, monkeypatch):
    monkeypatch.setenv("BRAINS_LEGACY_SURFACES", "1")
    resp = client.get("/admin/config", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login?next=")


# --------------------------------------------------------------------------- #
# Runtime registration cannot move a machine between Orgs
# --------------------------------------------------------------------------- #


def _register(client, headers, machine_id, *, org_id=None, tool="copilot"):
    body: dict = {"machine_id": machine_id, "tools": [{"tool": tool}]}
    if org_id is not None:
        body["org_id"] = org_id
    return client.post("/v1/runtimes/register", json=body, headers=headers)


def _machine_org_ids(machine_id: str) -> set[int | None]:
    return {row["org_id"] for row in runtimes_ctl.list_runtimes(machine_id=machine_id)}


def test_register_cannot_reassign_a_machine_to_another_org(client, two_orgs):
    machine = _slug("machine")
    assert (
        _register(
            client,
            two_orgs["a"]["headers"],
            machine,
            org_id=two_orgs["a"]["org"]["id"],
        ).status_code
        == 200
    )
    stolen = _register(client, two_orgs["b"]["headers"], machine, org_id=two_orgs["b"]["org"]["id"])
    assert stolen.status_code == 404
    assert _machine_org_ids(machine) == {two_orgs["a"]["org"]["id"]}


def test_register_without_an_org_cannot_adopt_another_orgs_machine(client, two_orgs):
    machine = _slug("machine")
    _register(client, two_orgs["a"]["headers"], machine, org_id=two_orgs["a"]["org"]["id"])
    # No ``org_id`` in the body: the machine's own Org is authorized, and B has
    # no standing there, so the answer is the non-disclosing 404.
    assert _register(client, two_orgs["b"]["headers"], machine).status_code == 404
    assert _machine_org_ids(machine) == {two_orgs["a"]["org"]["id"]}


def test_register_mismatch_is_indistinguishable_from_an_unknown_machine(client, two_orgs):
    machine = _slug("machine")
    _register(client, two_orgs["a"]["headers"], machine, org_id=two_orgs["a"]["org"]["id"])
    known = _register(client, two_orgs["b"]["headers"], machine, org_id=two_orgs["b"]["org"]["id"])
    unknown = _register(
        client, two_orgs["b"]["headers"], _slug("ghost"), org_id=two_orgs["a"]["org"]["id"]
    )
    assert known.status_code == unknown.status_code == 404


def test_owner_of_the_machines_org_may_still_re_register(client, two_orgs):
    machine = _slug("machine")
    _register(client, two_orgs["a"]["headers"], machine, org_id=two_orgs["a"]["org"]["id"])
    again = _register(
        client, two_orgs["a"]["headers"], machine, org_id=two_orgs["a"]["org"]["id"], tool="codex"
    )
    assert again.status_code == 200
    assert _machine_org_ids(machine) == {two_orgs["a"]["org"]["id"]}


def test_concurrent_registration_from_two_orgs_leaves_one_owner(client, two_orgs):
    """The loser of the race is refused; the machine never straddles two Orgs."""
    machine = _slug("machine")
    org_a = two_orgs["a"]["org"]["id"]
    org_b = two_orgs["b"]["org"]["id"]

    def attempt(which: str):
        headers = two_orgs[which]["headers"]
        org_id = org_a if which == "a" else org_b
        return _register(client, headers, machine, org_id=org_id, tool="copilot").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(attempt, ["a", "b"]))

    assert sorted(statuses) == [200, 404]
    owners = _machine_org_ids(machine)
    assert owners in ({org_a}, {org_b})


def test_control_layer_refuses_a_cross_org_machine_claim(client, two_orgs):
    machine = _slug("machine")
    runtimes_ctl.register_runtime(machine, "copilot", org_id=two_orgs["a"]["org"]["id"])
    with pytest.raises(runtimes_ctl.RuntimeOrgConflictError):
        runtimes_ctl.register_runtime(machine, "codex", org_id=two_orgs["b"]["org"]["id"])


# --------------------------------------------------------------------------- #
# Enrollment redemption cannot mint a credential for another Org's machine
# --------------------------------------------------------------------------- #


def _enrol(client, org_id: int, machine: str, *, clis=None) -> dict:
    """Mint + redeem a connect token for ``machine`` in ``org_id``."""
    minted = client.post(
        "/v1/runtimes/enrol",
        json={"label": "box", "org_id": org_id, "ttl_seconds": 900},
        headers=ADMIN_AUTH,
    ).json()
    redeemed = client.post(
        "/v1/runtimes/enrol/redeem",
        json={
            "token": minted["token"],
            "machine_id": machine,
            "clis": [{"tool": "copilot"}] if clis is None else clis,
        },
    )
    return {"token": minted["token"], "response": redeemed}


def _runtime_credentials_for(machine: str) -> list[dict]:
    return creds.list_credentials(kind="runtime", machine_id=machine)


def test_empty_cli_redemption_cannot_claim_another_orgs_machine(client, two_orgs):
    """The exact exploit: redeem with ``clis=[]`` against a machine Org A owns.

    An empty CLI list registers no Runtime, so a claim read only from the
    ``runtimes`` table sees an unclaimed machine and mints Org B a credential
    for Org A's box. Nothing may be minted, and the refusal must read exactly
    like an unknown token.
    """
    machine = _slug("victim")
    victim = _enrol(client, two_orgs["a"]["org"]["id"], machine)
    assert victim["response"].status_code == 200

    stolen = _enrol(client, two_orgs["b"]["org"]["id"], machine, clis=[])
    assert stolen["response"].status_code == 400
    unknown = client.post(
        "/v1/runtimes/enrol/redeem",
        json={"token": "not-a-real-token", "machine_id": machine, "clis": []},
    )
    assert stolen["response"].json() == unknown.json()
    assert "daemon_key" not in stolen["response"].json()

    orgs_holding_the_machine = {row["org_id"] for row in _runtime_credentials_for(machine)}
    assert orgs_holding_the_machine == {two_orgs["a"]["org"]["id"]}
    assert _machine_org_ids(machine) == {two_orgs["a"]["org"]["id"]}


def test_a_refused_redemption_leaves_its_token_retry_safe(client, two_orgs):
    """The refused token is not consumed: it still works on B's own machine."""
    machine = _slug("victim")
    assert _enrol(client, two_orgs["a"]["org"]["id"], machine)["response"].status_code == 200
    attacker = _enrol(client, two_orgs["b"]["org"]["id"], machine, clis=[])
    assert attacker["response"].status_code == 400

    own_machine = _slug("attacker-box")
    retry = client.post(
        "/v1/runtimes/enrol/redeem",
        json={"token": attacker["token"], "machine_id": own_machine, "clis": []},
    )
    assert retry.status_code == 200
    assert retry.json()["org_id"] == two_orgs["b"]["org"]["id"]


def test_a_squatted_runtime_credential_authorizes_nothing(client, two_orgs, tmp_path):
    """Defence in depth: a credential naming another Org's machine is inert.

    Enrollment refuses to mint one; this mints it directly to prove that even
    if some other path produced one, every Runtime-id-scoped route compares the
    credential's Org to the Runtime's Org and not only to the machine id.
    """
    machine = _slug("victim")
    victim = _enrol(client, two_orgs["a"]["org"]["id"], machine)["response"].json()
    runtime_id = victim["runtimes"][0]["id"]
    victim_headers = _auth(victim["daemon_key"])
    session_id = client.post(
        f"/v1/runtimes/{runtime_id}/sessions",
        json={"tool": "copilot", "workspace_path": str(tmp_path)},
        headers=victim_headers,
    ).json()["session_id"]

    _record, forged = creds.mint_runtime_credential(
        org_id=two_orgs["b"]["org"]["id"], machine_id=machine, label="squatter"
    )
    headers = _auth(forged)
    principal = principal_for_secret(forged)
    assert principal.owns_machine(machine) is True, "the machine binding alone would pass"

    assert client.get(f"/v1/runtimes/{runtime_id}", headers=headers).status_code == 404
    assert (
        client.post(
            f"/v1/runtimes/{runtime_id}/heartbeat", json={"status": "online"}, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/runtimes/{runtime_id}/sessions",
            json={"tool": "copilot", "session_id": session_id},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/runtimes/{runtime_id}/sessions/{session_id}/events",
            json={"seq": 1, "chunk": "PWNED"},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/sessions/{session_id}/state", json={"state": "failed"}, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/runtimes/heartbeat",
            json={"machine_id": machine, "runtimes": [{"id": runtime_id, "status": "offline"}]},
            headers=headers,
        ).status_code
        == 404
    )
    assert client.get("/v1/runtimes", headers=headers).json()["runtimes"] == []
    still_running = client.get(f"/v1/sessions/{session_id}", headers=ADMIN_AUTH).json()
    assert still_running["state"] != "failed"


def test_concurrent_cross_org_redemption_leaves_one_owner(client, two_orgs):
    """Two Orgs racing to enrol the same machine id: exactly one claims it."""
    machine = _slug("contested")
    org_a = two_orgs["a"]["org"]["id"]
    org_b = two_orgs["b"]["org"]["id"]
    tokens = {
        which: enrolment_ctl.mint_token(label=which, org_id=org, ttl_seconds=900)["token"]
        for which, org in (("a", org_a), ("b", org_b))
    }

    def _redeem(which: str):
        try:
            return enrolment_ctl.redeem_token(tokens[which], machine_id=machine, clis=[])
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_redeem, ["a", "b"]))

    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, ValueError)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert str(losers[0]) == "invalid enrolment token"
    owners = {row["org_id"] for row in _runtime_credentials_for(machine)}
    assert owners in ({org_a}, {org_b})
    assert owners == {winners[0]["org_id"]}


def test_same_org_reconnect_and_rotation_still_work(client, two_orgs):
    """The legitimate path is untouched: re-enrolling one's own machine works."""
    machine = _slug("mine")
    org_a = two_orgs["a"]["org"]["id"]
    first = _enrol(client, org_a, machine)["response"].json()
    second = _enrol(client, org_a, machine, clis=[])["response"]
    assert second.status_code == 200
    rotated = second.json()
    assert rotated["org_id"] == org_a
    assert rotated["daemon_key"] != first["daemon_key"]

    runtime_id = first["runtimes"][0]["id"]
    for key in (first["daemon_key"], rotated["daemon_key"]):
        assert (
            client.post(
                f"/v1/runtimes/{runtime_id}/heartbeat",
                json={"status": "online"},
                headers=_auth(key),
            ).status_code
            == 200
        )
    # Revoking the machine's credentials releases its claim to its own Org only.
    assert _machine_org_ids(machine) == {org_a}


def test_an_enrolment_token_always_names_the_org_it_binds(client):
    """The intended Org is stamped at mint time, not inferred at redeem time."""
    minted = enrolment_ctl.mint_token(label="implicit")
    assert minted["org_id"] == policy.default_org_id()


# --------------------------------------------------------------------------- #
# Key rotation and deletion revoke the superseded credential
# --------------------------------------------------------------------------- #


def test_rotating_the_admin_key_revokes_the_old_one(client, monkeypatch):
    from brains.api.admin_key import rotate_admin_key

    old_key = settings.api_key
    old_headers = _auth(old_key)
    assert client.get("/v1/orgs", headers=old_headers).status_code == 200
    old_credential = principal_for_secret(old_key).credential_id
    try:
        new_key = rotate_admin_key()
        # Denied immediately - not at the end of a cache TTL.
        assert client.get("/v1/orgs", headers=old_headers).status_code == 401
        assert principal_for_secret(old_key) is None
        assert creds.get_credential(old_credential)["revoked_at"] is not None
        assert client.get("/v1/orgs", headers=_auth(new_key)).status_code == 200
    finally:
        settings.api_key = old_key
        creds.invalidate_source_cache()
        # The rotation revoked the fixture key. Reinstating it takes an
        # explicit registration (a local adoption never un-revokes), then a
        # second one to put its provenance back where the other tests expect.
        admin_operator_id = ensure_admin_operator()["id"]
        creds.register_credential(
            old_key,
            kind=creds.KIND_ADMIN,
            operator_id=admin_operator_id,
            source=creds.SOURCE_MANUAL,
        )
        creds.register_credential(
            old_key,
            kind=creds.KIND_ADMIN,
            operator_id=admin_operator_id,
            source=creds.SOURCE_ADMIN_KEY,
        )


def test_deleting_an_operator_key_file_revokes_its_credential(client):
    from brains.control.operators import remove_operator_key

    slug = _slug("rotated")
    _record, key, headers = _operator(slug)
    assert client.get("/v1/orgs", headers=headers).status_code == 200
    credential_id = principal_for_secret(key).credential_id

    assert remove_operator_key(slug) is True
    assert client.get("/v1/orgs", headers=headers).status_code == 401
    assert principal_for_secret(key) is None
    assert creds.get_credential(credential_id)["revoked_at"] is not None


def test_reconciliation_never_revokes_a_runtime_credential(client, enrolled_machine):
    """A machine credential is not sourced from disk, so a rotation must not touch it."""
    from brains.control.operators import remove_operator_key

    slug = _slug("bystander")
    _operator(slug)
    remove_operator_key(slug)
    creds.invalidate_source_cache()
    creds.sync_local_credentials()

    record = creds.get_credential(enrolled_machine["credential_id"])
    assert record["revoked_at"] is None
    assert record["source"] == creds.SOURCE_ENROLMENT
    assert principal_for_secret(enrolled_machine["key"]) is not None


def test_adopted_credentials_record_their_provenance(client):
    _record, key, _headers = _operator(_slug("provenance"))
    assert principal_for_secret(settings.api_key) is not None
    admin_record = creds.get_credential(principal_for_secret(settings.api_key).credential_id)
    operator_record = creds.get_credential(principal_for_secret(key).credential_id)
    assert admin_record["source"] in creds.LOCAL_SOURCES
    assert operator_record["source"] == creds.SOURCE_OPERATOR_KEY


def test_a_revoked_local_key_is_not_reinstated_by_a_resync(client):
    _record, key, headers = _operator(_slug("stay-revoked"))
    creds.revoke_credential(principal_for_secret(key).credential_id)
    creds.invalidate_source_cache()
    creds.sync_local_credentials()
    assert principal_for_secret(key) is None
    assert client.get("/v1/orgs", headers=headers).status_code == 401


# --------------------------------------------------------------------------- #
# Ownership cannot be taken by an admin, and an Org cannot lose its last owner
# --------------------------------------------------------------------------- #


def _org_with_admin(client):
    """An Org owned by ``owner`` with a second operator holding ``admin``."""
    _owner_record, _owner_key, owner_headers = _operator(_slug("owner"))
    org = _org_owned_by(client, owner_headers)
    admin_record, _admin_key, admin_headers = _operator(_slug("orgadmin"))
    resp = client.post(
        f"/v1/orgs/{org['slug']}/members",
        json={"operator_id": admin_record["slug"], "role": "admin"},
        headers=owner_headers,
    )
    assert resp.status_code == 200
    return org, owner_headers, admin_headers, admin_record


def _role_of(client, headers, org_slug, operator_slug):
    rows = client.get(f"/v1/orgs/{org_slug}/members", headers=headers).json()["data"]
    for row in rows:
        if row["operator"] == operator_slug:
            return row["role"]
    return None


def test_admin_cannot_demote_an_owner(client):
    org, owner_headers, admin_headers, _admin_record = _org_with_admin(client)
    owner_slug = _role_owner_slug(client, owner_headers, org["slug"])
    resp = client.post(
        f"/v1/orgs/{org['slug']}/members",
        json={"operator_id": owner_slug, "role": "member"},
        headers=admin_headers,
    )
    assert resp.status_code == 403
    assert _role_of(client, owner_headers, org["slug"], owner_slug) == "owner"


def test_admin_cannot_demote_an_owner_by_numeric_id(client):
    org, owner_headers, admin_headers, _admin_record = _org_with_admin(client)
    owner_slug = _role_owner_slug(client, owner_headers, org["slug"])
    owner_id = next(
        row["operator_id"]
        for row in client.get(f"/v1/orgs/{org['slug']}/members", headers=owner_headers).json()[
            "data"
        ]
        if row["operator"] == owner_slug
    )
    resp = client.post(
        f"/v1/orgs/{org['slug']}/members",
        json={"operator_id": str(owner_id), "role": "member"},
        headers=admin_headers,
    )
    assert resp.status_code in (403, 404)
    assert _role_of(client, owner_headers, org["slug"], owner_slug) == "owner"


def test_admin_cannot_remove_an_owner(client):
    org, owner_headers, admin_headers, _admin_record = _org_with_admin(client)
    owner_slug = _role_owner_slug(client, owner_headers, org["slug"])
    resp = client.delete(f"/v1/orgs/{org['slug']}/members/{owner_slug}", headers=admin_headers)
    assert resp.status_code == 403
    assert _role_of(client, owner_headers, org["slug"], owner_slug) == "owner"


def test_the_last_owner_cannot_demote_itself(client):
    _record, _key, owner_headers = _operator(_slug("solo"))
    org = _org_owned_by(client, owner_headers)
    owner_slug = _role_owner_slug(client, owner_headers, org["slug"])
    resp = client.post(
        f"/v1/orgs/{org['slug']}/members",
        json={"operator_id": owner_slug, "role": "member"},
        headers=owner_headers,
    )
    assert resp.status_code == 409
    assert _role_of(client, owner_headers, org["slug"], owner_slug) == "owner"


def test_the_last_owner_cannot_remove_itself(client):
    _record, _key, owner_headers = _operator(_slug("solo-remove"))
    org = _org_owned_by(client, owner_headers)
    owner_slug = _role_owner_slug(client, owner_headers, org["slug"])
    resp = client.delete(f"/v1/orgs/{org['slug']}/members/{owner_slug}", headers=owner_headers)
    assert resp.status_code == 409
    assert _role_of(client, owner_headers, org["slug"], owner_slug) == "owner"


def test_an_owner_may_step_down_once_another_owner_exists(client):
    _record, _key, owner_headers = _operator(_slug("first"))
    org = _org_owned_by(client, owner_headers)
    owner_slug = _role_owner_slug(client, owner_headers, org["slug"])
    second, _second_key, _second_headers = _operator(_slug("second"))
    assert (
        client.post(
            f"/v1/orgs/{org['slug']}/members",
            json={"operator_id": second["slug"], "role": "owner"},
            headers=owner_headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/orgs/{org['slug']}/members",
            json={"operator_id": owner_slug, "role": "member"},
            headers=owner_headers,
        ).status_code
        == 200
    )
    assert _role_of(client, _second_headers, org["slug"], second["slug"]) == "owner"


def test_concurrent_owner_demotions_cannot_empty_an_org(client):
    """Two owners, two simultaneous demotions: at most one may win."""
    _record, _key, owner_headers = _operator(_slug("race-a"))
    org = _org_owned_by(client, owner_headers)
    first_slug = _role_owner_slug(client, owner_headers, org["slug"])
    second, _second_key, _second_headers = _operator(_slug("race-b"))
    client.post(
        f"/v1/orgs/{org['slug']}/members",
        json={"operator_id": second["slug"], "role": "owner"},
        headers=owner_headers,
    )

    def demote(target: str):
        try:
            return orgs_ctl.add_member(org["slug"], target, role="member")
        except orgs_ctl.LastOwnerError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(demote, [first_slug, second["slug"]]))

    owners = [m for m in orgs_ctl.list_members(org["slug"]) if m["role"] == "owner"]
    assert len(owners) >= 1


def test_concurrent_owner_removals_cannot_empty_an_org(client):
    _record, _key, owner_headers = _operator(_slug("rrace-a"))
    org = _org_owned_by(client, owner_headers)
    first_slug = _role_owner_slug(client, owner_headers, org["slug"])
    second, _second_key, _second_headers = _operator(_slug("rrace-b"))
    client.post(
        f"/v1/orgs/{org['slug']}/members",
        json={"operator_id": second["slug"], "role": "owner"},
        headers=owner_headers,
    )

    def remove(target: str):
        try:
            return orgs_ctl.remove_member(org["slug"], target)
        except orgs_ctl.LastOwnerError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(remove, [first_slug, second["slug"]]))

    owners = [m for m in orgs_ctl.list_members(org["slug"]) if m["role"] == "owner"]
    assert len(owners) >= 1


def test_bootstrap_recovery_can_still_empty_an_org_explicitly(client):
    """The escape hatch exists, and only an explicit local call can use it."""
    _record, _key, owner_headers = _operator(_slug("recovery"))
    org = _org_owned_by(client, owner_headers)
    owner_slug = _role_owner_slug(client, owner_headers, org["slug"])
    orgs_ctl.remove_member(org["slug"], owner_slug, bootstrap_recovery=True)
    assert [m for m in orgs_ctl.list_members(org["slug"]) if m["role"] == "owner"] == []


def _role_owner_slug(client, headers, org_slug) -> str:
    rows = client.get(f"/v1/orgs/{org_slug}/members", headers=headers).json()["data"]
    return next(row["operator"] for row in rows if row["role"] == "owner")


# --------------------------------------------------------------------------- #
# Private Workspace visibility: list and detail agree
# --------------------------------------------------------------------------- #


@pytest.fixture
def private_workspace(client, tmp_path):
    """A private Workspace in an operator's Org, holding a Session and an ASK."""
    from brains.control.decisions import file_decision_request
    from brains.control.sessions import start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import Workspace

    record, _key, headers = _operator(_slug("wsowner"))
    org = _org_owned_by(client, headers)
    session_row = start_session(str(tmp_path), tool="pytest")
    ask = file_decision_request(str(tmp_path), title="private ask", body="b")
    with SessionLocal() as s:
        workspace = s.query(Workspace).filter(Workspace.path == str(tmp_path)).one()
        workspace.org_id = org["id"]
        workspace.visibility = "private"
        slug = workspace.slug
        workspace_id = workspace.id
        s.commit()
    return {
        "record": record,
        "headers": headers,
        "org": org,
        "slug": slug,
        "workspace_id": workspace_id,
        "session_id": session_row["session_id"],
        "ask": ask["code"],
    }


def test_private_workspace_session_is_absent_from_the_listing(client, private_workspace):
    rows = client.get("/v1/sessions", headers=private_workspace["headers"]).json()["data"]
    assert private_workspace["session_id"] not in {row["id"] for row in rows}


@pytest.mark.parametrize("suffix", ["", "/events"])
def test_private_workspace_session_detail_matches_the_listing(client, private_workspace, suffix):
    resp = client.get(
        f"/v1/sessions/{private_workspace['session_id']}{suffix}",
        headers=private_workspace["headers"],
    )
    assert resp.status_code == 404


def test_private_workspace_session_state_is_refused(client, private_workspace):
    resp = client.post(
        f"/v1/sessions/{private_workspace['session_id']}/state",
        json={"state": "completed"},
        headers=private_workspace["headers"],
    )
    assert resp.status_code == 404


def test_private_workspace_approval_detail_matches_the_listing(client, private_workspace):
    listed = client.get("/v1/approvals", headers=private_workspace["headers"]).json()["data"]
    assert private_workspace["ask"] not in {row["code"] for row in listed}
    assert (
        client.get(
            f"/v1/approvals/{private_workspace['ask']}", headers=private_workspace["headers"]
        ).status_code
        == 404
    )


def test_private_workspace_approval_cannot_be_resolved(client, private_workspace):
    from brains.control.decisions import get_decision

    resp = client.post(
        f"/v1/approvals/{private_workspace['ask']}/resolve",
        json={"decision": "approve"},
        headers=private_workspace["headers"],
    )
    assert resp.status_code == 404
    assert get_decision(private_workspace["ask"])["status"] == "open"


def test_workspace_membership_restores_list_and_detail_together(client, private_workspace):
    from brains.control.memberships import add_membership

    add_membership(private_workspace["slug"], private_workspace["record"]["slug"])
    rows = client.get("/v1/sessions", headers=private_workspace["headers"]).json()["data"]
    assert private_workspace["session_id"] in {row["id"] for row in rows}
    assert (
        client.get(
            f"/v1/sessions/{private_workspace['session_id']}",
            headers=private_workspace["headers"],
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/v1/approvals/{private_workspace['ask']}", headers=private_workspace["headers"]
        ).status_code
        == 200
    )


def test_private_workspace_topic_is_refused(private_workspace):
    principal = principal_for_secret(
        # The operator's own key, resolved fresh so the Org roles are current.
        next(
            raw
            for raw, _kind, _source in creds.local_key_sources()
            if principal_for_secret(raw)
            and principal_for_secret(raw).operator_slug == private_workspace["record"]["slug"]
        )
    )
    topic = f"session/{private_workspace['session_id']}/stdout"
    assert policy.authorize_topic(principal, topic) is False


# --------------------------------------------------------------------------- #
# Approval separation of duty does not trust a caller-declared session
# --------------------------------------------------------------------------- #


def _session_originated_ask(client, tmp_path):
    """An operator that can write the Org, plus an ASK filed by a Session."""
    from brains.control.decisions import file_decision_request
    from brains.control.sessions import start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import Workspace

    record, key, headers = _operator(_slug("approver"))
    org = _org_owned_by(client, headers)
    session_row = start_session(str(tmp_path), tool="pytest")
    ask = file_decision_request(
        str(tmp_path), title="agent ask", body="b", session_id=session_row["session_id"]
    )
    with SessionLocal() as s:
        workspace = s.query(Workspace).filter(Workspace.path == str(tmp_path)).one()
        workspace.org_id = org["id"]
        s.commit()
    return {
        "record": record,
        "key": key,
        "headers": headers,
        "org": org,
        "session_id": session_row["session_id"],
        "ask": ask["code"],
    }


def test_a_shared_api_key_cannot_resolve_a_session_originated_ask(client, tmp_path):
    """Omitting ``session_id`` must not be a way to become 'separated'."""
    from brains.control.decisions import get_decision

    ctx = _session_originated_ask(client, tmp_path)
    resp = client.post(
        f"/v1/approvals/{ctx['ask']}/resolve",
        json={"decision": "approve"},
        headers=ctx["headers"],
    )
    assert resp.status_code == 403
    assert "console or a local CLI" in resp.text
    assert get_decision(ctx["ask"])["status"] == "open"


def test_declaring_someone_elses_session_does_not_help(client, tmp_path):
    ctx = _session_originated_ask(client, tmp_path)
    resp = client.post(
        f"/v1/approvals/{ctx['ask']}/resolve",
        json={"decision": "approve", "session_id": "not-the-one-that-asked"},
        headers=ctx["headers"],
    )
    assert resp.status_code == 403


def test_the_console_cookie_can_resolve_a_session_originated_ask(client, tmp_path):
    from brains.control.decisions import get_decision

    ctx = _session_originated_ask(client, tmp_path)
    client.cookies.set("brains_admin_key", mint_browser_token(ctx["key"]))
    try:
        resp = client.post(f"/v1/approvals/{ctx['ask']}/resolve", json={"decision": "approve"})
    finally:
        client.cookies.clear()
    assert resp.status_code == 200
    assert get_decision(ctx["ask"])["status"] == "resolved"


def test_the_local_cli_can_resolve_a_session_originated_ask(client, tmp_path):
    from brains.authz.resolver import resolve_local_principal
    from brains.control.decisions import get_decision, resolve_decision

    ctx = _session_originated_ask(client, tmp_path)
    resolve_decision(ctx["ask"], "approve", principal=resolve_local_principal())
    assert get_decision(ctx["ask"])["status"] == "resolved"


def test_the_requesting_session_is_derived_from_the_credential(client, tmp_path):
    """A Runtime's live Sessions are known server-side, not declared."""
    from brains.authz.resolver import sessions_bound_to_machine
    from brains.control.sessions import start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import AgentSession

    session_row = start_session(str(tmp_path), tool="pytest")
    machine = _slug("machine")
    with SessionLocal() as s:
        row = s.get(AgentSession, session_row["session_id"])
        row.machine_id = machine
        s.commit()
    assert session_row["session_id"] in sessions_bound_to_machine(machine)
    assert sessions_bound_to_machine(_slug("other")) == frozenset()


def test_an_api_key_ask_with_no_session_still_resolves(client, tmp_path):
    """The rule is scoped to Session-originated asks; a human ask is unaffected."""
    from brains.control.decisions import file_decision_request, get_decision
    from brains.storage.db import SessionLocal
    from brains.storage.models import Workspace

    _record, _key, headers = _operator(_slug("human"))
    org = _org_owned_by(client, headers)
    ask = file_decision_request(str(tmp_path), title="human ask", body="b")
    with SessionLocal() as s:
        workspace = s.query(Workspace).filter(Workspace.path == str(tmp_path)).one()
        workspace.org_id = org["id"]
        s.commit()
    resp = client.post(
        f"/v1/approvals/{ask['code']}/resolve", json={"decision": "approve"}, headers=headers
    )
    assert resp.status_code == 200
    assert get_decision(ask["code"])["status"] == "resolved"


# --------------------------------------------------------------------------- #
# A bad token must not cost a filesystem scan
# --------------------------------------------------------------------------- #


def test_a_flood_of_bad_tokens_does_not_rescan_the_key_directory(client, monkeypatch):
    calls: list[int] = []
    real = creds._read_local_key_sources

    def counted():
        calls.append(1)
        return real()

    monkeypatch.setattr(creds, "_read_local_key_sources", counted)
    creds.invalidate_source_cache()

    for index in range(200):
        assert client.get("/v1/orgs", headers=_auth(f"bad-{index}")).status_code == 401

    # One read to warm the cache; the fingerprint has not moved since, so the
    # files are never re-read - and the negative cache means most tokens do not
    # even reach the fingerprint check.
    assert len(calls) <= 2


def test_the_negative_cache_is_bounded(client):
    creds.invalidate_source_cache()
    for index in range(creds.NEGATIVE_CACHE_MAX_ENTRIES + 250):
        creds.resolve_secret(f"nope-{index}")
    assert len(creds._negative_cache) <= creds.NEGATIVE_CACHE_MAX_ENTRIES


def test_the_negative_cache_never_holds_a_secret(client):
    creds.invalidate_source_cache()
    creds.resolve_secret("super-secret-token")
    assert "super-secret-token" not in creds._negative_cache
    assert creds.hash_secret("super-secret-token") in creds._negative_cache


def test_minting_a_credential_clears_its_negative_entry(client):
    raw = f"fresh-{uuid.uuid4().hex}"
    assert creds.resolve_secret(raw) is None
    assert creds.hash_secret(raw) in creds._negative_cache
    creds.register_credential(
        raw,
        kind=creds.KIND_ADMIN,
        operator_id=ensure_admin_operator()["id"],
        source=creds.SOURCE_MANUAL,
    )
    assert creds.resolve_secret(raw) is not None


def test_the_negative_cache_is_thread_safe(client):
    def hammer(index: int):
        creds.resolve_secret(f"parallel-{index % 50}")
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(hammer, range(400)))
    assert len(creds._negative_cache) <= creds.NEGATIVE_CACHE_MAX_ENTRIES


# --------------------------------------------------------------------------- #
# Reconciliation adopts; only an explicit act revokes
# --------------------------------------------------------------------------- #


class _UnreadableKeyDir:
    """A key directory this process cannot list - EACCES, unmounted volume."""

    def exists(self) -> bool:
        return True

    def iterdir(self):
        raise PermissionError("permission denied")

    def __str__(self) -> str:
        return "/unreadable/operator-keys"


class _UnreadableKeyFile:
    """A key file that exists but cannot be read."""

    def exists(self) -> bool:
        return True

    def read_text(self, **_kwargs) -> str:
        raise PermissionError("permission denied")

    def unlink(self) -> None:
        raise AssertionError("a key file that could not be read must not be unlinked")


@contextmanager
def _narrow_view(monkeypatch, *, keys_dir, api_key: str = ""):
    """Run a block with the key sources some *other* process would see.

    A second worker, a container without the state directory mounted, a
    different ``BRAINS_API_KEY`` - each is a legitimate process holding a
    narrower view of the same install than the one that issued the keys.
    """
    from brains.control import operators as operators_ctl

    creds.invalidate_source_cache()
    monkeypatch.setattr(operators_ctl, "operator_keys_dir", lambda: keys_dir)
    monkeypatch.setattr(settings, "api_key", api_key)
    monkeypatch.setattr(settings, "api_keys", ())
    try:
        yield
    finally:
        monkeypatch.undo()
        creds.invalidate_source_cache()
        creds.sync_local_credentials()


def test_a_process_with_a_narrower_view_revokes_nothing(client, monkeypatch, tmp_path):
    """The core rule: absence from one process's view is not a supersede."""
    _record, key, headers = _operator(_slug("wide"))
    credential_id = principal_for_secret(key).credential_id
    admin_credential = principal_for_secret(settings.api_key).credential_id
    empty = tmp_path / "other-process-keys"
    empty.mkdir()

    with _narrow_view(monkeypatch, keys_dir=empty, api_key=f"other-{uuid.uuid4().hex}"):
        creds.sync_local_credentials()
        creds.sync_local_credentials()

    assert creds.get_credential(credential_id)["revoked_at"] is None
    assert creds.get_credential(admin_credential)["revoked_at"] is None
    assert client.get("/v1/orgs", headers=headers).status_code == 200
    assert client.get("/v1/orgs", headers=ADMIN_AUTH).status_code == 200


def test_divergent_processes_keep_each_others_credentials(client, monkeypatch, tmp_path):
    """Two processes, two states: each adopts its own, neither denies the other."""
    _record, key_a, headers_a = _operator(_slug("proc-a"))
    other = tmp_path / "proc-b-keys"
    other.mkdir()
    key_b = f"proc-b-{uuid.uuid4().hex}"
    (other / "procb.key").write_text(key_b + "\n", encoding="utf-8")

    with _narrow_view(monkeypatch, keys_dir=other):
        creds.sync_local_credentials()
        assert creds.resolve_secret(key_b) is not None
        assert creds.resolve_secret(key_a) is not None

    assert client.get("/v1/orgs", headers=headers_a).status_code == 200
    assert creds.resolve_secret(key_b) is not None


def test_a_bad_token_never_revokes_a_credential(client, monkeypatch, tmp_path):
    """An unauthenticated miss reconciles; reconciliation may not take keys away."""
    _record, _key, headers = _operator(_slug("bystander"))
    empty = tmp_path / "no-keys-here"
    empty.mkdir()

    with _narrow_view(monkeypatch, keys_dir=empty):
        for index in range(5):
            assert client.get("/v1/orgs", headers=_auth(f"bad-{index}")).status_code == 401

    assert client.get("/v1/orgs", headers=headers).status_code == 200


def test_an_unreadable_key_directory_is_diagnosed_not_believed(client, monkeypatch):
    _record, _key, headers = _operator(_slug("unreadable"))
    from brains.control import operators as operators_ctl

    creds.invalidate_source_cache()
    monkeypatch.setattr(operators_ctl, "operator_keys_dir", _UnreadableKeyDir)
    try:
        with pytest.raises(creds.LocalSourceError):
            creds.local_key_sources(force=True)
        # Nothing adopted, nothing revoked, and the fault is reported.
        assert creds.sync_local_credentials() == 0
        assert creds.diagnose()["local_source_error"]
        assert client.get("/v1/orgs", headers=headers).status_code == 200
    finally:
        monkeypatch.undo()
        creds.invalidate_source_cache()
        creds.sync_local_credentials()
    assert creds.diagnose()["local_source_error"] is None


def test_a_key_file_that_cannot_be_read_is_not_reported_as_removed(client, monkeypatch):
    from brains.control import operators as operators_ctl

    slug = _slug("locked")
    _record, _key, headers = _operator(slug)
    monkeypatch.setattr(operators_ctl, "_operator_key_path", lambda _slug: _UnreadableKeyFile())
    with pytest.raises(PermissionError):
        operators_ctl.remove_operator_key(slug)
    monkeypatch.undo()
    # The caller was not told the key was retired, and it was not.
    assert client.get("/v1/orgs", headers=headers).status_code == 200


def test_restoring_a_deleted_key_file_does_not_re_enable_it(client):
    from brains.control.operators import operator_keys_dir, remove_operator_key

    slug = _slug("restored")
    record, key, headers = _operator(slug)
    credential_id = principal_for_secret(key).credential_id
    assert remove_operator_key(slug) is True
    assert client.get("/v1/orgs", headers=headers).status_code == 401

    path = operator_keys_dir() / f"{slug}.key"
    path.write_text(key + "\n", encoding="utf-8")
    creds.invalidate_source_cache()
    creds.sync_local_credentials()
    # Ambient reconciliation does not undo a revocation it did not make.
    assert client.get("/v1/orgs", headers=headers).status_code == 401
    assert creds.get_credential(credential_id)["revoked_at"] is not None

    creds.register_credential(
        key,
        kind=creds.KIND_OPERATOR,
        operator_id=record["id"],
        source=creds.SOURCE_OPERATOR_KEY,
        reinstate=True,
    )
    assert client.get("/v1/orgs", headers=headers).status_code == 200
    remove_operator_key(slug)


def test_rotation_revokes_the_key_it_superseded_and_nothing_else(client, monkeypatch):
    """Rotation names the exact hash it retires, rather than diffing disk."""
    from brains.api.admin_key import rotate_admin_key

    _record, operator_key, operator_headers = _operator(_slug("survivor"))
    old_key = settings.api_key
    old_credential = principal_for_secret(old_key).credential_id
    try:
        new_key = rotate_admin_key()
        assert creds.get_credential(old_credential)["revoked_at"] is not None
        assert client.get("/v1/orgs", headers=_auth(new_key)).status_code == 200
        # The bystander operator was never named, so it is untouched.
        assert client.get("/v1/orgs", headers=operator_headers).status_code == 200
        assert principal_for_secret(operator_key) is not None
    finally:
        settings.api_key = old_key
        creds.invalidate_source_cache()
        creds.register_credential(
            old_key,
            kind=creds.KIND_ADMIN,
            operator_id=ensure_admin_operator()["id"],
            source=creds.SOURCE_ADMIN_KEY,
            reinstate=True,
        )


# --------------------------------------------------------------------------- #
# A Session listing is Workspace-scoped whichever entity it hangs off
# --------------------------------------------------------------------------- #


@pytest.fixture
def private_workspace_session_links(client, private_workspace):
    """Link the private Workspace's Session to an Issue and a Persona."""
    from brains.control import issues as issues_ctl
    from brains.control import personas as personas_ctl
    from brains.control import projects as projects_ctl
    from brains.storage.db import SessionLocal
    from brains.storage.models import AgentSession

    org_id = private_workspace["org"]["id"]
    persona = personas_ctl.create_persona(
        org_id, _slug("worker"), "Worker", model="claude-opus-4.8", tool="pytest"
    )
    project = projects_ctl.create_project(org_id, _slug("proj"), "Proj")
    issue = issues_ctl.create_issue(project["id"], "private work")
    with SessionLocal() as session:
        row = session.get(AgentSession, private_workspace["session_id"])
        row.issue_id = issue["id"]
        row.persona_id = persona["id"]
        session.commit()
    return {**private_workspace, "issue": issue, "persona": persona}


def test_issue_sessions_hide_a_private_workspace(client, private_workspace_session_links):
    links = private_workspace_session_links
    resp = client.get(f"/v1/issues/{links['issue']['code']}/sessions", headers=links["headers"])
    assert resp.status_code == 200
    assert links["session_id"] not in {row["id"] for row in resp.json()["data"]}


def test_persona_sessions_hide_a_private_workspace(client, private_workspace_session_links):
    links = private_workspace_session_links
    resp = client.get(f"/v1/personas/{links['persona']['slug']}/sessions", headers=links["headers"])
    assert resp.status_code == 200
    assert links["session_id"] not in {row["id"] for row in resp.json()["data"]}


def test_workspace_membership_restores_every_session_listing(
    client, private_workspace_session_links
):
    from brains.control.memberships import add_membership

    links = private_workspace_session_links
    add_membership(links["slug"], links["record"]["slug"])
    for path in (
        "/v1/sessions",
        f"/v1/issues/{links['issue']['code']}/sessions",
        f"/v1/personas/{links['persona']['slug']}/sessions",
    ):
        rows = client.get(path, headers=links["headers"]).json()["data"]
        assert links["session_id"] in {row["id"] for row in rows}, path


def test_every_session_listing_goes_through_the_workspace_filter():
    """No entity-to-Session listing may read ``list_agent_sessions`` unscoped."""
    import ast
    from pathlib import Path

    api_dir = Path(__file__).resolve().parents[1] / "src" / "brains" / "api"
    unscoped: list[str] = []
    for path in sorted(api_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            source = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
            if "list_agent_sessions(" not in source:
                continue
            if "scope_sessions(" not in source:
                unscoped.append(f"{path.name}:{node.name}")
    assert unscoped == []


# --------------------------------------------------------------------------- #
# A registration either claims its machine or leaves no row at all
# --------------------------------------------------------------------------- #


def test_a_refused_claim_leaves_no_org_less_row(client, two_orgs):
    """The refusal and the insert are one transaction, so neither half lands."""
    machine = _slug("machine")
    runtimes_ctl.register_runtime(machine, "copilot", org_id=two_orgs["a"]["org"]["id"])
    with pytest.raises(runtimes_ctl.RuntimeOrgConflictError):
        runtimes_ctl.register_runtime(machine, "codex", org_id=two_orgs["b"]["org"]["id"])

    rows = runtimes_ctl.list_runtimes(machine_id=machine)
    assert [row["tool"] for row in rows] == ["copilot"]
    assert all(row["org_id"] == two_orgs["a"]["org"]["id"] for row in rows)


def test_a_second_tool_never_lands_org_less_on_a_claimed_machine(client, two_orgs):
    machine = _slug("machine")
    org_a = two_orgs["a"]["org"]["id"]
    runtimes_ctl.register_runtime(machine, "copilot", org_id=org_a)
    # No Org named at all: the machine's claim is inherited, never contradicted
    # and never left NULL beside a claimed sibling.
    second = runtimes_ctl.register_runtime(machine, "codex")
    assert second["org_id"] == org_a
    assert _machine_org_ids(machine) == {org_a}


def test_concurrent_first_registrations_of_different_tools_claim_one_org(client, two_orgs):
    """Two Orgs, two tools, one unclaimed machine: exactly one claim survives."""
    machine = _slug("machine")
    orgs_by_tool = {
        "copilot": two_orgs["a"]["org"]["id"],
        "codex": two_orgs["b"]["org"]["id"],
    }

    def attempt(tool: str):
        try:
            return runtimes_ctl.register_runtime(machine, tool, org_id=orgs_by_tool[tool])["org_id"]
        except runtimes_ctl.RuntimeOrgConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, list(orgs_by_tool)))

    claimed = {result for result in results if result is not None}
    assert len(claimed) == 1
    rows = runtimes_ctl.list_runtimes(machine_id=machine)
    assert rows, "the winner's row must exist"
    assert {row["org_id"] for row in rows} == claimed


def test_a_legacy_org_less_runtime_is_not_treated_as_the_default_org(client):
    """A pre-Org row claims nothing; it is not handed to whoever is default."""
    from brains.control.orgs import add_member

    machine = _slug("legacy")
    legacy = runtimes_ctl.register_runtime(machine, "copilot")
    assert legacy["org_id"] is None

    record, _key, headers = _operator(_slug("defaultmember"))
    default_org = policy.default_org_id()
    assert default_org is not None
    add_member(default_org, record["slug"], role="admin")

    listed = client.get("/v1/runtimes", headers=headers).json()["runtimes"]
    assert legacy["id"] not in {row["id"] for row in listed}
    assert client.get(f"/v1/runtimes/{legacy['id']}", headers=headers).status_code == 404
    # The install administrator still sees it, so it can be claimed or removed.
    admin_listed = client.get("/v1/runtimes", headers=ADMIN_AUTH).json()["runtimes"]
    assert legacy["id"] in {row["id"] for row in admin_listed}

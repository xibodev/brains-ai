"""Persona-oriented Pods: roster, leadership, lifecycle, routing (F5, BL-P1-03).

These cover AC-F5-01 through AC-F5-04: a Pod's members are Personas in the
Pod's own Org, exactly one of them leads, add/remove/archive are real
operations with refusals that say why, and dispatch resolution is deterministic
and Runtime-compatibility aware. The legacy-membership classification is
asserted here too, because a silent drop would be the easy wrong answer.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from fastapi.testclient import TestClient

from brains.control import orgs as orgs_ctl
from brains.control import personas as personas_ctl
from brains.control import pods as pods_ctl
from brains.control import runtimes as runtimes_ctl
from brains.control import squads as squads_ctl
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


def _persona(org, name="Forge", *, runtime_id=None, tool="copilot"):
    return personas_ctl.create_persona(
        org["id"], _slug("p"), name, tool=tool, default_runtime_id=runtime_id
    )


# --------------------------------------------------------------------------- #
# Roster and leadership (AC-F5-01, AC-F5-02, AC-F5-03)
# --------------------------------------------------------------------------- #


def test_pod_membership_is_personas(org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    member = _persona(org, "Member", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    pod = pods_ctl.add_member(pod["id"], member["id"])

    assert pod["leader_persona_id"] == leader["id"]
    assert {m["persona_id"] for m in pod["members"]} == {leader["id"], member["id"]}
    assert [m["persona_id"] for m in pod["members"]][0] == leader["id"], "leader is listed first"
    assert pod["org_id"] == org["id"]


def test_pod_refuses_a_persona_from_another_org(org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    other = orgs_ctl.create_org(_slug("other"), "Other")
    stranger = personas_ctl.create_persona(other["id"], _slug("p"), "Stranger")

    with pytest.raises(pods_ctl.PodError, match="must share its Org"):
        pods_ctl.add_member(pod["id"], stranger["id"])
    with pytest.raises(pods_ctl.PodError, match="must share its Org"):
        pods_ctl.set_leader(pod["id"], stranger["id"])


def test_leader_replacement_keeps_exactly_one_leader(org, runtime):
    first = _persona(org, "First", runtime_id=runtime["id"])
    second = _persona(org, "Second", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=first["id"])

    pod = pods_ctl.set_leader(pod["id"], second["id"])

    assert pod["leader_persona_id"] == second["id"]
    leaders = [m for m in pod["members"] if m["is_leader"]]
    assert len(leaders) == 1 and leaders[0]["persona_id"] == second["id"]
    # The previous leader is kept on the roster, demoted rather than dropped.
    previous = next(m for m in pod["members"] if m["persona_id"] == first["id"])
    assert previous["role"] == "member"


def test_archived_persona_cannot_lead_or_join(org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    retired = _persona(org, "Retired", runtime_id=runtime["id"])
    personas_ctl.archive(retired["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])

    with pytest.raises(pods_ctl.PodError, match="archived"):
        pods_ctl.add_member(pod["id"], retired["id"])
    with pytest.raises(pods_ctl.PodError, match="archived"):
        pods_ctl.set_leader(pod["id"], retired["id"])


def test_leader_cannot_be_removed(org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])

    with pytest.raises(pods_ctl.PodError, match="assign a new leader"):
        pods_ctl.remove_member(pod["id"], leader["id"])


def test_member_removal_is_explicit_about_a_non_member(org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    outsider = _persona(org, "Outsider", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])

    with pytest.raises(pods_ctl.PodError, match="not a member"):
        pods_ctl.remove_member(pod["id"], outsider["id"])


def test_archive_hides_the_pod_and_freezes_its_roster(org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    member = _persona(org, "Member", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    pods_ctl.add_member(pod["id"], member["id"])

    archived = pods_ctl.archive_pod(pod["id"])
    assert archived["status"] == "archived"
    assert archived["archived_at"] is not None
    assert pod["id"] not in {p["id"] for p in pods_ctl.list_pods(org["id"])}
    assert pod["id"] in {p["id"] for p in pods_ctl.list_pods(org["id"], include_archived=True)}
    with pytest.raises(pods_ctl.PodError, match="archived"):
        pods_ctl.add_member(pod["id"], member["id"])
    # Archiving twice is not an error, and does not re-stamp the timestamp.
    assert pods_ctl.archive_pod(pod["id"])["archived_at"][:19] == archived["archived_at"][:19]


# --------------------------------------------------------------------------- #
# Routing and Runtime compatibility (AC-F5-04, AC-F4-06)
# --------------------------------------------------------------------------- #


def test_routing_prefers_the_leader_then_members_by_id(org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    member = _persona(org, "Member", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    pods_ctl.add_member(pod["id"], member["id"])

    plan = pods_ctl.resolve_dispatch(pod["id"])
    assert plan["persona_id"] == leader["id"]
    assert plan["blocked_reason"] is None
    assert plan["candidates"][0]["is_leader"] is True


def test_routing_skips_an_incompatible_leader_with_its_reason(org, runtime):
    leader = _persona(org, "Lead", tool="claude", runtime_id=runtime["id"])
    member = _persona(org, "Member", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    pods_ctl.add_member(pod["id"], member["id"])

    plan = pods_ctl.resolve_dispatch(pod["id"])
    assert plan["candidates"][0]["blocked_reason"] == "runtime_tool_mismatch"
    assert plan["persona_id"] == member["id"]


def test_routing_refuses_a_runtime_from_another_org(org, tmp_path):
    other = orgs_ctl.create_org(_slug("other"), "Other")
    foreign = runtimes_ctl.register_runtime(
        f"machine-{uuid.uuid4().hex[:8]}",
        "copilot",
        org_id=other["id"],
        working_root=str(tmp_path),
        status="online",
    )
    leader = _persona(org, "Lead", runtime_id=foreign["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])

    plan = pods_ctl.resolve_dispatch(pod["id"])
    assert plan["blocked_reason"] == "pod_no_capable_member"
    assert plan["candidates"][0]["blocked_reason"] == "runtime_other_org"


def test_routing_reports_an_offline_runtime(org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    runtimes_ctl.mark_offline(runtime["id"])

    plan = pods_ctl.resolve_dispatch(pod["id"])
    assert plan["candidates"][0]["blocked_reason"] == "runtime_offline"


def test_leaderless_and_empty_pods_report_their_own_reason(org):
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core")
    assert pods_ctl.resolve_dispatch(pod["id"])["blocked_reason"] == "pod_empty"

    member = _persona(org, "Member")
    pods_ctl.add_member(pod["id"], member["id"])
    assert pods_ctl.resolve_dispatch(pod["id"])["blocked_reason"] == "pod_no_leader"


def test_archived_pod_routes_nowhere(org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    pods_ctl.archive_pod(pod["id"])
    assert pods_ctl.resolve_dispatch(pod["id"])["blocked_reason"] == "pod_archived"


def test_runtime_assignment_poll_resolves_a_pod_through_its_persona_roster(org, runtime, tmp_path):
    """A Runtime polling for work sees a Pod-assigned Issue through the roster."""
    from brains.control import assignments as assignments_ctl
    from brains.control import issues as issues_ctl
    from brains.control import projects as projects_ctl

    leader = _persona(org, "Lead")  # no runtime — must be skipped
    member = _persona(org, "Member", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    pods_ctl.add_member(pod["id"], member["id"])
    project = projects_ctl.create_project(org["id"], _slug("proj"), "Proj")
    issue = issues_ctl.create_issue(project["id"], "Pod work")
    issues_ctl.assign(issue["code"], pod_id=pod["id"])

    pending = assignments_ctl.list_assignments_for_runtime(runtime["id"])
    assert [row["issue_code"] for row in pending] == [issue["code"]]
    assert pending[0]["persona_id"] == member["id"]


# --------------------------------------------------------------------------- #
# Legacy migration and classification
# --------------------------------------------------------------------------- #


def test_legacy_operator_membership_is_classified_not_dropped(org, runtime, tmp_path):
    """A legacy squad member with no single Persona is reported, never invented."""
    from brains.control.sessions import register_workspace
    from brains.storage.db import SessionLocal
    from brains.storage.models import Workspace

    with contextlib.suppress(Exception):
        add_operator("legacy-lead")
    with contextlib.suppress(Exception):
        add_operator("legacy-hand")

    workspace = register_workspace(str(tmp_path))
    with SessionLocal() as session:
        row = session.get(Workspace, workspace.id)
        row.org_id = org["id"]
        session.commit()

    slug = _slug("legacy")
    squads_ctl.create_squad(str(tmp_path), slug, "Legacy Pod", "legacy-lead")
    squads_ctl.add_member(str(tmp_path), slug, "legacy-hand")

    pod = next(p for p in pods_ctl.list_pods(org["id"]) if p["slug"] == slug)
    assert pod["members"] == [], "an operator with no Persona must not become a member"
    assert pod["legacy_leader_operator"] == "legacy-lead"
    legacy = {m["operator"] for m in pod["legacy_operator_members"]}
    assert {"legacy-lead", "legacy-hand"} <= legacy
    assert all(m["dispatchable"] is False for m in pod["legacy_operator_members"])
    assert all("no active Persona" in m["reason"] for m in pod["legacy_operator_members"])


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


def test_pod_http_lifecycle(client, org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    member = _persona(org, "Member", runtime_id=runtime["id"])

    created = client.post(
        f"/v1/orgs/{org['slug']}/pods",
        json={"slug": _slug("ops"), "name": "Ops Pod", "leader_persona_id": str(leader["id"])},
        headers=AUTH,
    )
    assert created.status_code == 200, created.text
    pod_id = created.json()["id"]
    assert created.json()["leader_persona"] == leader["slug"]

    added = client.post(
        f"/v1/pods/{pod_id}/members", json={"persona_id": str(member["id"])}, headers=AUTH
    )
    assert added.status_code == 200, added.text
    assert {m["persona_id"] for m in added.json()["members"]} == {leader["id"], member["id"]}

    plan = client.get(f"/v1/pods/{pod_id}/dispatch-plan", headers=AUTH)
    assert plan.status_code == 200
    assert plan.json()["persona_id"] == leader["id"]

    releader = client.patch(
        f"/v1/pods/{pod_id}", json={"leader_persona_id": str(member["id"])}, headers=AUTH
    )
    assert releader.status_code == 200, releader.text
    assert releader.json()["leader_persona"] == member["slug"]

    removed = client.delete(f"/v1/pods/{pod_id}/members/{leader['id']}", headers=AUTH)
    assert removed.status_code == 200, removed.text
    assert {m["persona_id"] for m in removed.json()["members"]} == {member["id"]}

    archived = client.patch(f"/v1/pods/{pod_id}", json={"status": "archived"}, headers=AUTH)
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    listed = client.get(f"/v1/orgs/{org['slug']}/pods", headers=AUTH).json()["data"]
    assert pod_id not in {p["id"] for p in listed}
    with_archived = client.get(
        f"/v1/orgs/{org['slug']}/pods", params={"status": "all"}, headers=AUTH
    ).json()["data"]
    assert pod_id in {p["id"] for p in with_archived}


def test_pod_member_route_refuses_an_operator_body(client, org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    resp = client.post(f"/v1/pods/{pod['id']}/members", json={"operator": "admin"}, headers=AUTH)
    assert resp.status_code == 400
    assert "Personas" in resp.text


def test_cross_org_pod_is_not_found(client, org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    _record, key = add_operator(_slug("outsider"))
    outsider = {"Authorization": f"Bearer {key}"}

    assert client.get(f"/v1/pods/{pod['id']}", headers=outsider).status_code == 404
    assert client.get(f"/v1/pods/{pod['id']}/dispatch-plan", headers=outsider).status_code == 404
    assert (
        client.delete(f"/v1/pods/{pod['id']}/members/{leader['id']}", headers=outsider).status_code
        == 404
    )
    assert (
        client.patch(
            f"/v1/pods/{pod['id']}", json={"status": "archived"}, headers=outsider
        ).status_code
        == 404
    )


def test_pod_routes_require_auth(client, org, runtime):
    leader = _persona(org, "Lead", runtime_id=runtime["id"])
    pod = pods_ctl.create_pod(org["id"], _slug("pod"), "Core", leader_persona=leader["id"])
    assert client.get(f"/v1/pods/{pod['id']}").status_code in (401, 403)
    assert client.delete(f"/v1/pods/{pod['id']}/members/{leader['id']}").status_code in (401, 403)

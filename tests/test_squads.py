"""Tests for squads — leader-routed team assignment.

A squad groups operators under a leader; assigning work to the squad produces a
leader brief, and the leader delegates to a member. These tests pin the squad
lifecycle (create / membership / roster), the assign→brief flow, and the
delegate path including the anti-loop guard.
"""

from __future__ import annotations

import contextlib

import pytest

from brains.control import squads
from brains.control.operators import add_operator, ensure_admin_operator
from brains.control.tasks import get_task


@pytest.fixture
def workspace(tmp_path):
    # A real on-disk dir so register_workspace can canonicalise it.
    d = tmp_path / "ws"
    d.mkdir()
    return str(d)


def _ensure_operator(slug: str) -> None:
    # Operators persist in the shared test DB; tolerate re-creation across tests.
    with contextlib.suppress(Exception):
        add_operator(slug)


@pytest.fixture(autouse=True)
def _operators():
    ensure_admin_operator()
    for slug in ("alice", "bob", "carol"):
        _ensure_operator(slug)
    yield


def test_create_squad_adds_leader_to_roster(workspace):
    squad = squads.create_squad(workspace, "frontend", "Frontend Team", leader="alice")
    assert squad["slug"] == "frontend"
    assert squad["leader"] == "alice"
    r = squads.roster(workspace, "frontend")
    leaders = [m for m in r["members"] if m["is_leader"]]
    assert len(leaders) == 1
    assert leaders[0]["operator"] == "alice"
    assert leaders[0]["role"] == "leader"


def test_create_squad_rejects_unknown_leader(workspace):
    with pytest.raises(ValueError, match="unknown leader"):
        squads.create_squad(workspace, "x", "X", leader="nobody")


def test_create_squad_rejects_bad_slug(workspace):
    with pytest.raises(ValueError, match="slug"):
        squads.create_squad(workspace, "Not A Slug", "X", leader="alice")


def test_create_squad_rejects_duplicate(workspace):
    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    with pytest.raises(ValueError, match="already exists"):
        squads.create_squad(workspace, "frontend", "Frontend", leader="bob")


def test_add_and_remove_members(workspace):
    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    squads.add_member(workspace, "frontend", "bob", role="react")
    squads.add_member(workspace, "frontend", "carol", role="css")
    r = squads.roster(workspace, "frontend")
    slugs = {m["operator"] for m in r["members"]}
    assert slugs == {"alice", "bob", "carol"}
    # role update is idempotent on (squad, operator)
    squads.add_member(workspace, "frontend", "bob", role="senior-react")
    r = squads.roster(workspace, "frontend")
    bob = next(m for m in r["members"] if m["operator"] == "bob")
    assert bob["role"] == "senior-react"
    # remove
    squads.remove_member(workspace, "frontend", "bob")
    r = squads.roster(workspace, "frontend")
    assert "bob" not in {m["operator"] for m in r["members"]}


def test_cannot_remove_leader(workspace):
    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    with pytest.raises(ValueError, match="leader"):
        squads.remove_member(workspace, "frontend", "alice")


def test_list_squads(workspace):
    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    squads.create_squad(workspace, "backend", "Backend", leader="bob")
    listed = {s["slug"] for s in squads.list_squads(workspace)}
    assert listed == {"frontend", "backend"}


def test_assign_to_squad_creates_tagged_task_and_brief(workspace):
    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    squads.add_member(workspace, "frontend", "bob", role="react")
    out = squads.assign_task_to_squad(
        workspace, "frontend", "Build the login page", body="OAuth + form"
    )
    code = out["task"]["code"]
    assert out["leader"] == "alice"
    # Task is tagged to the squad
    task = get_task(code)
    assert "squad:frontend" in (task["tags"] or "")
    # Brief names the leader, the task, and the member to route to — but not a
    # hardcoded pick (the leader decides).
    brief = out["brief"]
    assert "@alice" in brief
    assert code in brief
    assert "@bob" in brief
    assert "squad_delegate" in brief


def test_assign_to_unknown_squad_raises(workspace):
    with pytest.raises(ValueError, match="unknown squad"):
        squads.assign_task_to_squad(workspace, "ghost", "x")


def test_delegate_tags_task_and_records(workspace):
    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    squads.add_member(workspace, "frontend", "bob", role="react")
    out = squads.assign_task_to_squad(workspace, "frontend", "Build login")
    code = out["task"]["code"]
    res = squads.delegate_task(code, "bob", note="bob owns auth UI")
    assert res["delegated_to"] == "bob"
    task = get_task(code)
    assert "delegated-to:bob" in (task["tags"] or "")


def test_delegate_anti_loop_guard(workspace):
    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    squads.add_member(workspace, "frontend", "bob")
    squads.add_member(workspace, "frontend", "carol")
    out = squads.assign_task_to_squad(workspace, "frontend", "Build login")
    code = out["task"]["code"]
    squads.delegate_task(code, "bob")
    # A leader re-reading its inbox must not be able to re-delegate the same task.
    with pytest.raises(ValueError, match="already delegated"):
        squads.delegate_task(code, "carol")


def test_delegate_unknown_task_or_operator(workspace):
    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    out = squads.assign_task_to_squad(workspace, "frontend", "x")
    with pytest.raises(ValueError, match="unknown task"):
        squads.delegate_task("TASK-99999", "alice")
    with pytest.raises(ValueError, match="unknown operator"):
        squads.delegate_task(out["task"]["code"], "nobody")


def test_roster_includes_skill_signal(workspace):
    # carol contributes knowledge in this workspace → shows up as a skill.
    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    squads.add_member(workspace, "frontend", "carol")
    r = squads.roster(workspace, "frontend")
    carol = next(m for m in r["members"] if m["operator"] == "carol")
    # No knowledge yet → empty skills list (not an error).
    assert carol["skills"] == []


def test_recurring_task_routes_to_squad(workspace):
    """A recurring task bound to a squad fires a squad-tagged task plus a leader
    brief — wiring scheduled/triggered work into leader-routed delegation."""
    from brains.control import recurring

    squads.create_squad(workspace, "frontend", "Frontend", leader="alice")
    squads.add_member(workspace, "frontend", "bob", role="react")
    recurring.create_recurring_task(
        workspace, "daily-standup", "Standup {date}", cron_expr="manual", squad="frontend"
    )
    res = recurring.fire_recurring_task("daily-standup")
    assert res["squad"] == "frontend"
    assert "squad:frontend" in (res["task"]["tags"] or "")
    # Leader brief names a member to route to.
    assert "@bob" in (res["brief"] or "")
    # The fire was recorded as an audit run.
    runs = recurring.list_recurring_runs("daily-standup")
    assert len(runs) == 1
    assert runs[0]["source"] == "manual"
    assert runs[0]["status"] == "created"
    assert runs[0]["task_code"] == res["task"]["code"]


def test_recurring_task_rejects_unknown_squad(workspace):
    from brains.control import recurring

    with pytest.raises(ValueError, match="unknown squad"):
        recurring.create_recurring_task(workspace, "bad", "x", squad="ghost")

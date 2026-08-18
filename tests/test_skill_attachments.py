"""Skill attachment tests (BL-P1-08 / AC-F10-05).

Covers the durable ``persona_skills``/``project_skills`` attachment surface
(migration 138): CRUD-adjacent attach/detach control functions, the protected
HTTP routes, cross-Org and unauthorized refusal, and the actual Session
context injection path — both the ``build_welcome`` API-visible packet and the
real launch-time prompt a spawned agent receives (``exec.runner.run_session``).
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from brains.control import issues as issues_ctl
from brains.control import orgs as orgs_ctl
from brains.control import personas as personas_ctl
from brains.control import projects as projects_ctl
from brains.control import sessions as sessions_ctl
from brains.control import skills as skills_ctl
from brains.control.operators import add_operator, ensure_admin_operator
from brains.control.welcome import build_welcome
from brains.main import app
from brains.storage.migrations import init_db

AUTH = {"Authorization": "Bearer local-dev-key"}


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _bootstrap():
    init_db()
    ensure_admin_operator()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def org():
    return orgs_ctl.create_org(_slug("org"), "Acme")


@pytest.fixture
def persona(org):
    return personas_ctl.create_persona(org["id"], _slug("persona"), "Forge")


@pytest.fixture
def project(org):
    return projects_ctl.create_project(org["id"], _slug("proj"), "Proj")


@pytest.fixture
def skill(org):
    return skills_ctl.create_skill(org["id"], _slug("skill"), "Skill", content="Do the thing.")


# --------------------------------------------------------------------------- #
# Control-layer attach/detach
# --------------------------------------------------------------------------- #


def test_attach_to_persona_and_list(persona, skill):
    attached = skills_ctl.attach_to_persona(persona["id"], skill["id"])
    assert attached["skill_id"] == skill["id"]
    assert attached["source"] == "persona"
    rows = skills_ctl.list_persona_skills(persona["id"])
    assert [r["skill_id"] for r in rows] == [skill["id"]]


def test_attach_to_persona_is_idempotent(persona, skill):
    """Attaching an already-attached Skill does not duplicate the row."""
    first = skills_ctl.attach_to_persona(persona["id"], skill["id"])
    second = skills_ctl.attach_to_persona(persona["id"], skill["id"])
    assert first["id"] == second["id"]
    assert len(skills_ctl.list_persona_skills(persona["id"])) == 1


def test_concurrent_duplicate_persona_attach_is_idempotent(persona, skill):
    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(
            executor.map(
                lambda _index: skills_ctl.attach_to_persona(persona["id"], skill["id"]),
                range(2),
            )
        )
    assert rows[0]["id"] == rows[1]["id"]
    assert len(skills_ctl.list_persona_skills(persona["id"])) == 1


def test_attach_to_project_and_list(project, skill):
    attached = skills_ctl.attach_to_project(project["id"], skill["id"])
    assert attached["source"] == "project"
    rows = skills_ctl.list_project_skills(project["id"])
    assert [r["skill_id"] for r in rows] == [skill["id"]]


def test_attach_cross_org_is_refused(project, org):
    other_org = orgs_ctl.create_org(_slug("other"), "Other")
    other_skill = skills_ctl.create_skill(other_org["id"], _slug("skill"), "Other skill")
    with pytest.raises(skills_ctl.SkillAttachmentError, match="another Org"):
        skills_ctl.attach_to_project(project["id"], other_skill["id"])


def test_detach_unknown_attachment_raises(persona, skill):
    with pytest.raises(skills_ctl.SkillAttachmentError):
        skills_ctl.detach_from_persona(persona["id"], skill["id"])


def test_detach_removes_the_attachment(persona, skill):
    skills_ctl.attach_to_persona(persona["id"], skill["id"])
    skills_ctl.detach_from_persona(persona["id"], skill["id"])
    assert skills_ctl.list_persona_skills(persona["id"]) == []


# --------------------------------------------------------------------------- #
# HTTP routes: authorization + cross-Org
# --------------------------------------------------------------------------- #


def test_api_attach_list_detach_persona_skill(client, org, persona, skill):
    attach = client.post(
        f"/v1/personas/{persona['id']}/skills", json={"skill_id": skill["id"]}, headers=AUTH
    )
    assert attach.status_code == 200, attach.text

    listed = client.get(f"/v1/personas/{persona['id']}/skills", headers=AUTH).json()
    assert any(r["skill_id"] == skill["id"] for r in listed["data"])

    # Duplicate attach via HTTP is still idempotent — one row.
    dup = client.post(
        f"/v1/personas/{persona['id']}/skills", json={"skill_id": skill["id"]}, headers=AUTH
    )
    assert dup.status_code == 200, dup.text
    listed_again = client.get(f"/v1/personas/{persona['id']}/skills", headers=AUTH).json()
    assert len(listed_again["data"]) == 1

    detach = client.delete(f"/v1/personas/{persona['id']}/skills/{skill['id']}", headers=AUTH)
    assert detach.status_code == 200, detach.text
    empty = client.get(f"/v1/personas/{persona['id']}/skills", headers=AUTH).json()
    assert empty["data"] == []


def test_api_attach_list_detach_project_skill(client, org, project, skill):
    attach = client.post(
        f"/v1/projects/{project['code']}/skills", json={"skill_id": skill["id"]}, headers=AUTH
    )
    assert attach.status_code == 200, attach.text
    listed = client.get(f"/v1/projects/{project['code']}/skills", headers=AUTH).json()
    assert any(r["skill_id"] == skill["id"] for r in listed["data"])

    detach = client.delete(f"/v1/projects/{project['code']}/skills/{skill['id']}", headers=AUTH)
    assert detach.status_code == 200, detach.text
    empty = client.get(f"/v1/projects/{project['code']}/skills", headers=AUTH).json()
    assert empty["data"] == []


def test_api_attach_cross_org_skill_is_not_found(client, org, persona):
    other_org = orgs_ctl.create_org(_slug("other"), "Other")
    other_skill = skills_ctl.create_skill(other_org["id"], _slug("skill"), "Other skill")
    resp = client.post(
        f"/v1/personas/{persona['id']}/skills",
        json={"skill_id": other_skill["id"]},
        headers=AUTH,
    )
    assert resp.status_code == 404, resp.text


def test_api_detach_unknown_attachment_is_not_found(client, org, persona, skill):
    resp = client.delete(f"/v1/personas/{persona['id']}/skills/{skill['id']}", headers=AUTH)
    assert resp.status_code == 404, resp.text


def test_api_attach_by_a_principal_with_no_role_in_the_org_is_not_found(
    client, org, persona, skill
):
    """An operator with no membership in the Persona's Org cannot see the Org
    at all, so an attach/detach attempt answers 404 like every other
    unauthorized native route (never revealing the Org/Persona exists)."""
    _record, key = add_operator(_slug("stranger"))
    from brains.authz import credentials as creds

    creds.sync_local_credentials()
    stranger_headers = {"Authorization": f"Bearer {key}"}

    attach = client.post(
        f"/v1/personas/{persona['id']}/skills",
        json={"skill_id": skill["id"]},
        headers=stranger_headers,
    )
    assert attach.status_code == 404, attach.text

    skills_ctl.attach_to_persona(persona["id"], skill["id"])
    detach = client.delete(
        f"/v1/personas/{persona['id']}/skills/{skill['id']}", headers=stranger_headers
    )
    assert detach.status_code == 404, detach.text


# --------------------------------------------------------------------------- #
# Context injection (AC-F10-05)
# --------------------------------------------------------------------------- #


def test_resolve_context_for_session_dedupes_persona_and_project_with_provenance(
    org, persona, project, skill, tmp_path
):
    other_skill = skills_ctl.create_skill(org["id"], _slug("skill2"), "Second skill")
    skills_ctl.attach_to_persona(persona["id"], skill["id"])
    skills_ctl.attach_to_project(project["id"], skill["id"])
    skills_ctl.attach_to_project(project["id"], other_skill["id"])

    issue = issues_ctl.create_issue(project["id"], "Do work")
    session_row = sessions_ctl.open_spawn_session(
        persona_id=persona["id"],
        tool="copilot",
        issue_id=issue["id"],
        org_id=org["id"],
        workspace_path=str(tmp_path),
    )

    context = skills_ctl.resolve_context_for_session(session_row["id"])
    assert context is not None
    by_id = {entry["skill_id"]: entry for entry in context["skills"]}
    assert set(by_id[skill["id"]]["sources"]) == {"persona", "project"}
    assert by_id[other_skill["id"]]["sources"] == ["project"]
    # Persona-sourced entries are ordered before project-only ones.
    assert context["skills"][0]["skill_id"] == skill["id"]


def test_resolve_context_for_session_returns_none_when_nothing_attached(persona, tmp_path):
    session_row = sessions_ctl.open_spawn_session(
        persona_id=persona["id"], tool="copilot", workspace_path=str(tmp_path)
    )
    assert skills_ctl.resolve_context_for_session(session_row["id"]) is None


def test_render_skill_context_block_marks_empty_as_a_comment():
    block = skills_ctl.render_skill_context_block(None)
    assert block.startswith("<!--")


def test_render_skill_context_block_includes_name_and_content(org, persona, skill):
    skills_ctl.attach_to_persona(persona["id"], skill["id"])
    # Build a fake session row by going through the real resolver via a
    # spawn session, so the render path is exercised against real data.
    import brains.storage.db as db_module
    from brains.storage.models import AgentSession

    with db_module.SessionLocal() as db_session:
        session_id = f"ses_{uuid.uuid4().hex[:12]}"
        from brains.control.sessions import register_workspace

        ws = register_workspace(__file__.rsplit("\\", 1)[0], org_id=persona["org_id"])
        db_session.add(
            AgentSession(
                id=session_id, workspace_id=ws.id, tool="copilot", persona_id=persona["id"]
            )
        )
        db_session.commit()

    context = skills_ctl.resolve_context_for_session(session_id)
    block = skills_ctl.render_skill_context_block(context)
    assert skill["name"] in block
    assert skill["content"] in block


def test_welcome_packet_includes_attached_skills(org, persona, skill, tmp_path):
    skills_ctl.attach_to_persona(persona["id"], skill["id"])
    session_row = sessions_ctl.open_spawn_session(
        persona_id=persona["id"], tool="copilot", org_id=org["id"], workspace_path=str(tmp_path)
    )
    workspace = sessions_ctl.get_workspace(path=str(tmp_path))
    welcome = build_welcome(workspace, session_row["id"])
    assert any(s["skill_id"] == skill["id"] for s in welcome["skills"])


def test_run_session_injects_skill_context_into_the_actual_prompt(
    monkeypatch, org, persona, skill, tmp_path
):
    """The agent receives Skill context through the real launch path
    (``run_session``'s prompt fed on stdin), not merely an API response."""
    from brains.exec import guard as guard_module
    from brains.exec import runner

    skills_ctl.attach_to_persona(persona["id"], skill["id"])
    session_row = sessions_ctl.open_spawn_session(
        persona_id=persona["id"],
        tool="unit-test-tool",
        org_id=org["id"],
        workspace_path=str(tmp_path),
    )

    captured: dict = {}

    def _fake_run(argv, **kwargs):
        captured["input_text"] = kwargs.get("input_text")
        return guard_module.GovernedRun(
            allowed=True, action_id="a1", status="applied", tier="local", returncode=0
        )

    monkeypatch.setattr(guard_module, "run", _fake_run)

    runner.run_session(
        ["echo", "hi"],
        str(tmp_path),
        prompt="do the assigned work",
        tool="unit-test-tool",
        session_id=session_row["id"],
    )

    assert captured["input_text"] is not None
    assert skill["name"] in captured["input_text"]
    assert skill["content"] in captured["input_text"]
    assert "do the assigned work" in captured["input_text"]


def test_run_session_injects_skill_context_into_copilot_prompt_argv(
    monkeypatch, org, persona, skill, tmp_path
):
    from brains.exec import guard as guard_module
    from brains.exec import runner

    skills_ctl.attach_to_persona(persona["id"], skill["id"])
    session_row = sessions_ctl.open_spawn_session(
        persona_id=persona["id"],
        tool="copilot",
        org_id=org["id"],
        workspace_path=str(tmp_path),
    )
    captured: dict = {}

    def _fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return guard_module.GovernedRun(
            allowed=True, action_id="a2", status="applied", tier="local", returncode=0
        )

    monkeypatch.setattr(guard_module, "run", _fake_run)
    argv, feed = runner._build_tool_argv("copilot", "do the assigned work", None)
    assert feed is None
    runner.run_session(
        argv,
        str(tmp_path),
        prompt=feed,
        tool="copilot",
        session_id=session_row["id"],
    )

    prompt = captured["argv"][captured["argv"].index("-p") + 1]
    assert skill["name"] in prompt
    assert skill["content"] in prompt
    assert "do the assigned work" in prompt

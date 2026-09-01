"""Tests for the start-session welcome packet.

The welcome packet is the discoverability nudge that helps a fresh agent
realize there are unread messages, applicable patterns, workspace
memories, tool issues and indexed sources to look at — without having
to read the skill doc from scratch every time.

These tests live alongside ``test_coordination_plane.py`` style fixtures
and rely on the shared dev DB the rest of the suite uses (no
isolation), so they namespace everything by a per-test ``tmp_path`` slug.
"""

from __future__ import annotations

import uuid

from brains.control.knowledge import add_knowledge_entry
from brains.control.mailbox import send_message
from brains.control.patterns import approve_pattern, propose_pattern
from brains.control.sessions import register_workspace, start_session
from brains.control.tool_registry import register_tool
from brains.control.welcome import build_welcome
from brains.storage.repositories import store_memory


def test_start_session_returns_welcome_block(tmp_path):
    started = start_session(str(tmp_path), tool="pytest")
    assert "welcome" in started
    welcome = started["welcome"]
    assert welcome is not None
    # Top-level shape is fixed — the skill doc tells agents what to look
    # for, so the shape must not silently change.
    for key in (
        "unread_messages",
        "applicable_patterns",
        "knowledge",
        "relevant_memories",
        "tool_status",
        "index_status",
        "hints",
        "brain_version",
        "skills",
    ):
        assert key in welcome, f"welcome missing {key}: {welcome}"
    assert welcome["unread_messages"] == {"count": 0, "subjects": []}
    assert welcome["knowledge"] == {"count": 0, "entries": []}
    assert welcome["index_status"] == {"sources": 0, "indexed": 0}
    assert welcome["skills"] == []
    # tool_status sub-keys must always be present.
    for k in ("registered", "available", "missing", "unverified"):
        assert k in welcome["tool_status"]
    # brain_version is the installed package version — never empty so
    # operators and agents can confirm which build is serving them.
    from brains import __version__

    assert welcome["brain_version"] == __version__
    assert welcome["brain_version"]


def test_welcome_surfaces_unread_mail_for_session(tmp_path):
    workspace = register_workspace(str(tmp_path))
    session = start_session(str(tmp_path), tool="pytest")
    send_message(
        subject="please review",
        body="thx",
        to_session_id=session["session_id"],
        workspace_path=str(tmp_path),
    )
    welcome = build_welcome(workspace, session["session_id"])
    assert welcome["unread_messages"]["count"] >= 1
    assert "please review" in welcome["unread_messages"]["subjects"]
    # Mail-related hint must be present so the agent knows to act on it.
    assert any("unread message" in h for h in welcome["hints"])


def test_welcome_surfaces_applicable_pattern_by_glob(tmp_path):
    # Workspace slug is derived from the leaf directory name; we name the
    # tmp dir so we can match it with a glob. The leaf uses a unique
    # per-run prefix so stale patterns left in the shared dev DB by
    # earlier runs (which used a broader ``welcome-glob-*`` glob) cannot
    # incidentally match this workspace and crowd the new pattern out of
    # the welcome packet's top-N sort.
    token = uuid.uuid4().hex[:8]
    leaf = f"welcome-pat-target-{token}"
    target = tmp_path / leaf
    target.mkdir()
    workspace = register_workspace(str(target))
    session = start_session(str(target), tool="pytest")
    name = f"welcome-pat-{uuid.uuid4().hex}"
    propose_pattern(
        name=name,
        category="testing",
        description="welcome-packet visibility check",
        applies_to=f"welcome-pat-target-{token}-*,{workspace.slug}",
    )
    approve_pattern(name)
    welcome = build_welcome(workspace, session["session_id"])
    names = [p["name"] for p in welcome["applicable_patterns"]]
    assert name in names
    assert any("matching pattern" in h for h in welcome["hints"])


def test_start_session_welcome_surfaces_active_workspace_knowledge(tmp_path):
    title = f"Known blocker {uuid.uuid4().hex}"
    entry = add_knowledge_entry(
        str(tmp_path),
        "blocker",
        title,
        body="Seeded before session start so welcome can surface it.",
        scope="workspace",
    )

    started = start_session(str(tmp_path), tool="pytest")
    knowledge = started["welcome"]["knowledge"]

    assert knowledge["count"] >= 1
    assert {
        "code": entry["code"],
        "type": "blocker",
        "title": title,
        "scope": "workspace",
    } in knowledge["entries"]
    assert any("active knowledge" in h for h in started["welcome"]["hints"])


def test_welcome_surfaces_workspace_memory_keys(tmp_path):
    leaf = f"welcome-mem-{uuid.uuid4().hex[:6]}"
    target = tmp_path / leaf
    target.mkdir()
    workspace = register_workspace(str(target))
    session = start_session(str(target), tool="pytest")
    key = f"{workspace.slug}.build.cmd"
    store_memory(key, "make test")
    welcome = build_welcome(workspace, session["session_id"])
    assert key in welcome["relevant_memories"]
    assert any("workspace memory" in h for h in welcome["hints"])


def test_welcome_tool_status_counts(tmp_path, monkeypatch):
    workspace = register_workspace(str(tmp_path))
    session = start_session(str(tmp_path), tool="pytest")
    monkeypatch.setattr(
        "brains.control.tool_registry.shutil.which",
        lambda c: None,  # force missing
    )
    name = f"welcome-tool-{uuid.uuid4().hex}"
    register_tool(
        name=name,
        display_name="Welcome Tool",
        cli_command="welcome-fake-bin",
    )
    welcome = build_welcome(workspace, session["session_id"])
    assert welcome["tool_status"]["registered"] >= 1
    # We registered a missing tool — the missing count must include it.
    assert welcome["tool_status"]["missing"] >= 1


def test_welcome_auto_verifies_local_session_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "brains.control.tool_registry.shutil.which",
        lambda command: "/bin/local-tool" if command == "local-tool" else None,
    )
    register_tool("local-tool", "Local Tool", "local-tool", verify=False)
    started = start_session(str(tmp_path), tool="local-tool")
    status = started["welcome"]["tool_status"]
    assert status["verification_scope"] == "control_plane"
    assert status["session_ready"] is True
    assert status["unverified"] == 0


def test_welcome_uses_bound_runtime_readiness_not_hub_path(tmp_path, monkeypatch):
    from brains.control.sessions import current_machine_id
    from brains.storage.db import SessionLocal
    from brains.storage.models import AgentSession, Runtime

    monkeypatch.setattr(
        "brains.control.tool_registry.shutil.which",
        lambda _command: None,
    )
    register_tool("remote-tool", "Remote Tool", "remote-tool", verify=False)
    workspace = register_workspace(str(tmp_path))
    with SessionLocal() as session:
        runtime = Runtime(
            slug=f"remote-runtime-{uuid.uuid4().hex[:8]}",
            machine_id=f"remote-{current_machine_id()}",
            tool="remote-tool",
            status="online",
            health="healthy",
        )
        session.add(runtime)
        session.flush()
        agent = AgentSession(
            id=f"ses_{uuid.uuid4().hex[:12]}",
            workspace_id=workspace.id,
            tool="remote-tool",
            machine_id=runtime.machine_id,
            runtime_id=runtime.id,
        )
        session.add(agent)
        session.commit()
        session_id = agent.id

    status = build_welcome(workspace, session_id)["tool_status"]
    assert status["verification_scope"] == "runtime"
    assert status["session_ready"] is True
    assert status["unverified"] >= 1

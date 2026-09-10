"""Tests for the start-session welcome packet.

The welcome packet is the discoverability nudge that helps a fresh agent
realize there are unread messages, applicable patterns, workspace
memories, tool issues and indexed sources to look at — without having
to read the skill doc from scratch every time.

The suite's ``conftest.py`` redirects storage to a process-local temporary
SQLite database by default. These tests seed synthetic records, namespaced
by per-test ``tmp_path`` workspaces and unique names within that database.
"""

from __future__ import annotations

import re
import uuid

import pytest

from brains.capabilities import CORE_MCP_TOOLS
from brains.control.knowledge import add_knowledge_entry
from brains.control.mailbox import send_message
from brains.control.patterns import approve_pattern, propose_pattern
from brains.control.sessions import register_workspace, start_session
from brains.control.tool_registry import register_tool
from brains.control.welcome import build_welcome
from brains.storage.repositories import store_memory


def _assert_supported_hint_tools(welcome):
    # Match actionable tool identifiers, not informational prose or the
    # quoted 'skills' payload key. Accept both bare and MCP-prefixed names.
    recommended = {
        match.removeprefix("brains_")
        for hint in welcome["hints"]
        for match in re.findall(
            r"\b(?:call|see|consider)\s+[`'\"]?([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b",
            hint,
        )
    }
    assert recommended <= CORE_MCP_TOOLS, (
        f"Unavailable tool recommendations: {recommended - CORE_MCP_TOOLS}; "
        f"hints: {welcome['hints']}"
    )
    return recommended


def test_start_session_returns_welcome_block(tmp_path):
    started = start_session(str(tmp_path), tool="pytest")
    assert "welcome" in started
    welcome = started["welcome"]
    assert welcome is not None
    assert "search_repo" in _assert_supported_hint_tools(welcome)
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
    _assert_supported_hint_tools(welcome)
    assert welcome["unread_messages"]["count"] >= 1
    assert "please review" in welcome["unread_messages"]["subjects"]
    # Retained mail remains visible as an informational notice.
    assert any("unread message" in h for h in welcome["hints"])


def test_welcome_surfaces_applicable_pattern_by_glob(tmp_path):
    # Workspace slug is derived from the leaf directory name; we name the
    # tmp dir so we can match it with a glob. The leaf uses a unique
    # per-test prefix so other synthetic patterns in the suite's temporary
    # DB cannot match and crowd this pattern out of the top-N preview.
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
    _assert_supported_hint_tools(welcome)
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
    assert "knowledge_search" in _assert_supported_hint_tools(started["welcome"])
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
    _assert_supported_hint_tools(welcome)
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
    _assert_supported_hint_tools(welcome)
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
    _assert_supported_hint_tools(started["welcome"])
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

    welcome = build_welcome(workspace, session_id)
    _assert_supported_hint_tools(welcome)
    status = welcome["tool_status"]
    assert status["verification_scope"] == "runtime"
    assert status["session_ready"] is True
    assert status["unverified"] >= 1


@pytest.mark.parametrize("indexed", [False, True], ids=["no-index", "retained-index"])
def test_welcome_preserves_historical_context_and_recommends_only_core_tools(
    tmp_path, monkeypatch, indexed
):
    from brains.storage.db import SessionLocal
    from brains.storage.models import (
        KnowledgeEntry,
        KnowledgePattern,
        MailboxMessage,
        Memory,
        RegisteredTool,
        Source,
    )

    monkeypatch.setattr("brains.control.tool_registry.shutil.which", lambda _command: None)
    workspace = register_workspace(str(tmp_path))
    started = start_session(str(tmp_path), tool="pytest")
    session_id = started["session_id"]
    token = uuid.uuid4().hex[:12]
    tool_name = f"welcome-missing-{token}"
    register_tool(tool_name, "Synthetic missing tool", tool_name, verify=False)

    # Persist historical rows directly: no retired MCP tool is needed to
    # represent an existing store. Include both direct and workspace mail.
    rows = [
        MailboxMessage(
            workspace_id=workspace.id,
            to_session_id=recipient,
            subject=f"Historical {label} {token}",
            body="Retained message body",
        )
        for label, recipient in (("direct", session_id), ("workspace", None))
    ]
    pattern = KnowledgePattern(
        name=f"welcome-history-{token}",
        category="testing",
        description="Retained pattern description",
        example="Retained example",
        applies_to=workspace.slug,
        status="approved",
        usage_count=7,
    )
    memory = Memory(key=f"{workspace.slug}.history", value="Retained memory value")
    knowledge = KnowledgeEntry(
        code=f"KNOW-{token}",
        workspace_id=workspace.id,
        type="caveat",
        title=f"Historical knowledge {token}",
        body="Retained knowledge body",
        status="active",
        scope="workspace",
    )
    rows.extend([pattern, memory, knowledge])
    if indexed:
        rows.extend(
            Source(
                workspace_id=workspace.id,
                source_type="repo",
                uri=f"{tmp_path.as_uri()}/{status}",
                title=f"Retained {status} source",
                status=status,
                metadata_json='{"synthetic": true}',
            )
            for status in ("active", "archived")
        )
    with SessionLocal() as session:
        session.add_all(rows)
        session.commit()
        identities = [(type(row), row.id) for row in rows]

    def snapshot():
        # Compare every stored column, including bodies, counters and dates.
        # RegisteredTool is intentionally excluded: welcome refreshes PATH
        # readiness, so its last_verified_at/is_available may legitimately change.
        with SessionLocal() as session:
            result = {}
            for model, row_id in identities:
                row = session.get(model, row_id)
                assert row is not None, f"Welcome deleted {model.__name__} {row_id}"
                result[(model, row_id)] = {
                    column.key: getattr(row, column.key) for column in model.__table__.columns
                }
            return result

    before = snapshot()
    # A repeated build must neither consume previews nor change historical rows.
    for _ in range(2):
        welcome = build_welcome(workspace, session_id)
        assert welcome["unread_messages"]["count"] == 2
        assert set(welcome["unread_messages"]["subjects"]) == {row.subject for row in rows[:2]}
        assert {
            "name": pattern.name,
            "category": pattern.category,
            "description": pattern.description,
            "usage_count": 7,
        } in welcome["applicable_patterns"]
        assert welcome["relevant_memories"] == [memory.key]
        assert welcome["knowledge"] == {
            "count": 1,
            "entries": [
                {
                    "code": knowledge.code,
                    "type": knowledge.type,
                    "title": knowledge.title,
                    "scope": knowledge.scope,
                }
            ],
        }
        assert welcome["tool_status"]["registered"] >= 1
        assert welcome["tool_status"]["missing"] >= 1
        assert welcome["skills"] == []
        assert welcome["index_status"] == (
            {"sources": 2, "indexed": 1} if indexed else {"sources": 0, "indexed": 0}
        )
        for notice in ("unread message", "matching pattern", "workspace memory", "missing on PATH"):
            assert any(notice in hint for hint in welcome["hints"])
        recommended = _assert_supported_hint_tools(welcome)
        expected_tools = {"knowledge_search"}
        if not indexed:
            expected_tools.add("search_repo")
        assert recommended == expected_tools
        assert snapshot() == before
        with SessionLocal() as session:
            for row in rows[:2]:
                assert session.get(MailboxMessage, row.id).read_at is None
            tool = session.get(RegisteredTool, tool_name)
            assert tool is not None
            assert tool.last_verified_at is not None
            assert not tool.is_available

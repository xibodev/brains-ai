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


# ------------------------------------------------------- adoption telemetry
#
# The ``session_start`` event must carry a snapshot of the welcome
# packet's counts in its ``metadata_json`` so adoption queries (did this
# session actually call ``read_messages`` after being told there was
# unread mail?) can be derived from the events table with a single
# self-join — no new telemetry tables required.


def _read_session_start_metadata(session_id: str) -> dict:
    import json as _json

    from brains.storage.db import SessionLocal as _SessionLocal
    from brains.storage.models import Event as _Event

    with _SessionLocal() as db:
        row = (
            db.query(_Event)
            .filter(_Event.session_id == session_id, _Event.kind == "session_start")
            .order_by(_Event.created_at.desc(), _Event.id.desc())
            .first()
        )
        assert row is not None, "session_start event must exist"
        return _json.loads(row.metadata_json or "{}")


def test_session_start_event_stamps_welcome_counts(tmp_path):
    workspace = register_workspace(str(tmp_path))
    # Seed one unread message so unread_messages count is non-zero.
    session = start_session(str(tmp_path), tool="pytest")
    sid = session["session_id"]
    send_message(
        subject=f"adopt-{uuid.uuid4().hex}",
        body="hi",
        to_session_id=sid,
        workspace_path=str(tmp_path),
    )
    # Open a second session — its welcome packet sees zero unread (the
    # mail is addressed to sid, not this one) but the metadata shape
    # must be intact regardless.
    second = start_session(str(tmp_path), tool="pytest")
    meta = _read_session_start_metadata(second["session_id"])
    assert meta.get("tool") == "pytest"
    welcome_meta = meta.get("welcome")
    assert welcome_meta is not None, "session_start event must carry welcome snapshot"
    for key in (
        "unread_messages",
        "applicable_patterns",
        "knowledge",
        "relevant_memories",
        "tools_missing",
        "tools_unverified",
        "index_sources",
        "hints",
    ):
        assert key in welcome_meta, f"welcome metadata missing {key}: {welcome_meta}"
        assert isinstance(welcome_meta[key], int)
    # Workspace was just registered — index is empty.
    assert welcome_meta["index_sources"] == 0
    # Silence linter: workspace fixture exists for parity with siblings.
    assert workspace.slug


def test_session_start_metadata_reflects_unread_mail_for_recipient(tmp_path):
    leaf = f"adopt-mail-{uuid.uuid4().hex[:6]}"
    target = tmp_path / leaf
    target.mkdir()
    register_workspace(str(target))
    # First session is the recipient.
    recipient = start_session(str(target), tool="pytest")
    rid = recipient["session_id"]
    send_message(
        subject=f"please-read-{uuid.uuid4().hex}",
        body="action required",
        to_session_id=rid,
        workspace_path=str(target),
    )
    # Restart the same recipient by opening a fresh session in the same
    # workspace addressed to nobody specific — mail addressed to ``rid``
    # remains targeted at ``rid``, so the FRESH session's welcome shows
    # zero unread. The recipient's welcome (built at THEIR start_session
    # call) was built BEFORE the mail was sent, so its metadata shows
    # zero too. This test asserts the shape stays honest: the count is
    # the count at start time, not a backfilled value.
    meta = _read_session_start_metadata(rid)
    welcome_meta = meta.get("welcome") or {}
    assert welcome_meta.get("unread_messages") == 0


def test_session_start_metadata_counts_pattern_offer(tmp_path):
    leaf = f"adopt-pat-{uuid.uuid4().hex[:6]}"
    target = tmp_path / leaf
    target.mkdir()
    workspace = register_workspace(str(target))
    # Seed an approved pattern that matches this workspace BEFORE
    # starting the session so the welcome packet picks it up.
    name = f"adopt-pat-{uuid.uuid4().hex}"
    propose_pattern(
        name=name,
        category="testing",
        description="adoption telemetry probe",
        applies_to=f"adopt-pat-*,{workspace.slug}",
    )
    approve_pattern(name)
    session = start_session(str(target), tool="pytest")
    meta = _read_session_start_metadata(session["session_id"])
    welcome_meta = meta.get("welcome") or {}
    assert welcome_meta.get("applicable_patterns", 0) >= 1
    # At least one hint should have been generated.
    assert welcome_meta.get("hints", 0) >= 1

"""Agent-to-agent comms slice 1 — the four validated scenarios.

* Discovery (scenario 1): ``live_agent_sessions`` sees every live agent
  across workspaces, with its harness, and drops ended sessions.
* Workspace inbox (scenario 2): mailbox messages addressed to a workspace
  reach whatever session is alive there.
* Harness-matched help (scenario 6): a Copilot session can file an ask no
  Copilot may claim; a Claude peer claims and answers it without either
  side sharing context.
* Topic boards (scenarios 3/5): posting creates one durable announcement,
  subscribed Sessions wake without mailbox copies, the board is the archive,
  and replies thread via ``reply_to``.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

import pytest

# Tighten the long-poll loops before importing the modules under test.
os.environ.setdefault("BRAINS_HELP_POLL_INTERVAL_MS", "10")

from brains.control.help import (  # noqa: E402
    answer_request,
    ask_peer,
    file_help_request,
    normalize_required_tool,
    tool_matches_requirement,
    wait_for_request,
)
from brains.control.mailbox import inbox_wait, read_messages, send_message  # noqa: E402
from brains.control.sessions import end_session, register_workspace, start_session  # noqa: E402
from brains.control.topics import (  # noqa: E402
    list_topics,
    live_agent_sessions,
    post_topic,
    read_topic,
    subscribe_topic,
)


def _run_ask_in_thread(*args, **kwargs):
    box: dict = {}

    def _runner():
        try:
            box["result"] = ask_peer(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            box["error"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return box, t


@pytest.fixture
def start_tracked_session():
    """``start_session`` that always ends what it started on teardown.

    Live-agent discovery and topic delivery read real liveness, so leaked
    sessions from an earlier test would legitimately appear as peers.
    """
    created: list[str] = []

    def _start(path: str, tool: str) -> dict:
        ses = start_session(path, tool=tool)
        created.append(ses["session_id"])
        return ses

    yield _start
    for sid in created:
        with contextlib.suppress(Exception):
            end_session(sid)


# --- harness grammar -------------------------------------------------------


def test_required_tool_grammar_normalization():
    assert normalize_required_tool(None) is None
    assert normalize_required_tool("") is None
    assert normalize_required_tool(" Claude ") == "claude"
    assert normalize_required_tool("NOT:Copilot") == "not:copilot"
    with pytest.raises(ValueError):
        normalize_required_tool("not:")


def test_tool_matches_requirement_matrix():
    assert tool_matches_requirement(None, "copilot")
    assert tool_matches_requirement("claude", "Claude")
    assert not tool_matches_requirement("claude", "codex")
    assert tool_matches_requirement("not:copilot", "codex")
    assert not tool_matches_requirement("not:copilot", "Copilot")
    # An unnamed harness cannot prove it satisfies any constraint.
    assert not tool_matches_requirement("claude", None)
    assert not tool_matches_requirement("not:copilot", "")


# --- scenario 1: cross-workspace discovery ---------------------------------


def test_live_agents_spans_workspaces_and_excludes_ended(tmp_path, start_tracked_session):
    ws_a = register_workspace(str(tmp_path / "alpha"))
    ws_b = register_workspace(str(tmp_path / "beta"))
    ses_a = start_tracked_session(str(tmp_path / "alpha"), tool="copilot")
    ses_b = start_tracked_session(str(tmp_path / "beta"), tool="claude")

    agents = {row["session_id"]: row for row in live_agent_sessions()}
    assert set(agents) >= {ses_a["session_id"], ses_b["session_id"]}
    assert agents[ses_a["session_id"]]["tool"] == "copilot"
    assert agents[ses_a["session_id"]]["workspace"] == ws_a.slug
    assert agents[ses_b["session_id"]]["workspace"] == ws_b.slug

    end_session(ses_a["session_id"], summary="done")
    remaining = {row["session_id"] for row in live_agent_sessions()}
    assert ses_b["session_id"] in remaining
    assert ses_a["session_id"] not in remaining


# --- scenario 2 + 6: workspace inbox and harness-matched help --------------


def test_workspace_addressed_mail_reaches_the_other_side(tmp_path, start_tracked_session):
    start_session(str(tmp_path / "alpha"), tool="copilot")
    ses_b = start_tracked_session(str(tmp_path / "beta"), tool="claude")

    send_to_beta = {
        "subject": "beta-bound",
        "body": "for whoever is alive in beta",
        "workspace_path": str(tmp_path / "beta"),
        "kind": "info",
    }
    from brains.control.mailbox import send_message

    sent = send_message(**send_to_beta)
    assert sent["to_session_id"] is None  # workspace-addressed

    mail = read_messages(ses_b["session_id"], include_read=True)
    assert any(m["subject"] == "beta-bound" for m in mail)


def test_harness_constrained_ask_routes_to_a_different_cli(tmp_path, start_tracked_session):
    register_workspace(str(tmp_path / "alpha"))
    ws_b = register_workspace(str(tmp_path / "beta"))
    ses_copilot = start_tracked_session(str(tmp_path / "alpha"), tool="copilot")
    ses_claude = start_tracked_session(str(tmp_path / "beta"), tool="claude")

    box, thread = _run_ask_in_thread(
        "Review this diff?",
        "Is the gate logic sound? Context: <the asker's own context>",
        from_session_id=ses_copilot["session_id"],
        to_workspace=ws_b.slug,
        required_tool="not:copilot",
        timeout_ms=8000,
    )

    # The wrong harness must NOT claim it: a copilot peer polls briefly
    # and comes away empty-handed while the request stays open.
    assert wait_for_request(session_id=ses_copilot["session_id"], timeout_ms=250) is None

    claimed = wait_for_request(session_id=ses_claude["session_id"], timeout_ms=5000)
    assert claimed is not None, "claude never matched the not:copilot ask"
    assert claimed["required_tool"] == "not:copilot"

    answer_request(
        claimed["code"],
        "Gate logic holds; one edge case noted.",
        evidence="tests/test_help_requests.py::test_ask_peer_blocks_until_answered",
        session_id=ses_claude["session_id"],
    )
    thread.join(timeout=5)
    result = box.get("result") or {}
    assert result.get("status") == "answered"
    assert result.get("claimed_by_session_id") == ses_claude["session_id"]


def test_exact_harness_ask_refuses_the_wrong_claimer(tmp_path, start_tracked_session):
    ws_b = register_workspace(str(tmp_path / "beta"))
    ses_copilot = start_tracked_session(str(tmp_path / "alpha"), tool="copilot")
    ses_claude = start_tracked_session(str(tmp_path / "beta"), tool="claude")

    box, thread = _run_ask_in_thread(
        "Plan check",
        "Does this ordering avoid the race?",
        from_session_id=ses_claude["session_id"],
        to_workspace=ws_b.slug,
        required_tool="claude",
        timeout_ms=6000,
    )

    # The copilot peer polls briefly and must come away empty-handed.
    assert wait_for_request(session_id=ses_copilot["session_id"], timeout_ms=300) is None

    claimed = wait_for_request(session_id=ses_claude["session_id"], timeout_ms=5000)
    assert claimed is not None
    answer_request(
        claimed["code"],
        "Ordering is safe.",
        evidence="e3 race matrix",
        session_id=ses_claude["session_id"],
    )
    thread.join(timeout=5)
    assert box["result"]["status"] == "answered"


# --- scenarios 3/5: interest-scoped topic delivery -------------------------


def test_topic_post_wakes_other_subscribed_sessions_only(tmp_path, start_tracked_session):
    ses_a = start_tracked_session(str(tmp_path / "alpha"), tool="copilot")
    ses_b = start_tracked_session(str(tmp_path / "beta"), tool="claude")
    ws_alpha = register_workspace(str(tmp_path / "alpha"))
    ws_beta = register_workspace(str(tmp_path / "beta"))
    subscribe_topic("review-queue", ses_b["session_id"])

    posted = post_topic(
        "review-queue",
        "PR review needed for the gate change",
        body="Diff summary inside.",
        from_session_id=ses_a["session_id"],
        workspace_path=str(tmp_path / "alpha"),
    )
    assert posted["topic"] == "review-queue"
    assert ses_b["session_id"] in posted["notified_sessions"]
    assert ws_beta.slug in posted["notified_workspaces"]
    assert ws_alpha.slug not in posted["notified_workspaces"], (
        "delivery must never notify the poster's own workspace"
    )

    # Topic delivery no longer creates one mailbox row per Workspace.
    assert read_messages(ses_b["session_id"]) == []
    wake = inbox_wait(ses_b["session_id"], timeout_ms=250)
    assert wake["wakeup"] == "topic"
    assert wake["posts"][0]["id"] == posted["id"]

    # The poster's own workspace stays quiet.
    assert read_messages(ses_a["session_id"]) == []

    board = read_topic("review-queue", session_id=ses_b["session_id"])
    assert [row["id"] for row in board] == [posted["id"]]
    assert inbox_wait(ses_b["session_id"], timeout_ms=100)["wakeup"] is None
    topics = {row["topic"]: row for row in list_topics()}
    assert topics["review-queue"]["posts"] == 1


def test_topic_replies_thread_and_validate_inputs(tmp_path, start_tracked_session):
    start_session(str(tmp_path / "alpha"), tool="copilot")
    ses_b = start_tracked_session(str(tmp_path / "beta"), tool="claude")

    root = post_topic(
        "plan-validation",
        "Validate the migration order",
        from_session_id=ses_b["session_id"],
        workspace_path=str(tmp_path / "beta"),
    )
    reply = post_topic(
        "plan-validation",
        "Re: Validate the migration order",
        body="Step 3 depends on step 2 landing first.",
        from_session_id=None,
        workspace_path=str(tmp_path / "alpha"),
        reply_to=root["id"],
    )
    assert reply["reply_to_id"] == root["id"]

    threaded = read_topic("plan-validation", reply_to=root["id"])
    assert [row["id"] for row in threaded] == [reply["id"]]

    with pytest.raises(ValueError):
        post_topic("Bad Topic!", "nope")
    with pytest.raises(ValueError):
        post_topic("ok-topic", "")


# --- slice 2: inbox_wait unified long-poll -----------------------------------


def test_inbox_wait_wakes_on_mail_and_on_request(tmp_path, start_tracked_session, monkeypatch):
    monkeypatch.setenv("BRAINS_HELP_POLL_INTERVAL_MS", "10")
    import threading

    ws_b = register_workspace(str(tmp_path / "beta"))
    ses = start_tracked_session(str(tmp_path / "beta"), tool="claude")

    # Mail wake: a message lands shortly after the wait starts.
    def _send_later():
        time.sleep(0.2)
        send_message("wake up", workspace_path=str(tmp_path / "beta"))

    t = threading.Thread(target=_send_later)
    t.start()
    result = inbox_wait(ses["session_id"], timeout_ms=4000)
    t.join()
    assert result["wakeup"] == "mail"

    # Peer-request wake: an ask addressed to this workspace wakes the wait.
    asker = start_tracked_session(str(tmp_path / "alpha"), tool="copilot")
    box: dict = {}

    def _ask():
        box["result"] = ask_peer(
            "wake on request",
            "ready?",
            from_session_id=asker["session_id"],
            to_workspace=ws_b.slug,
            timeout_ms=3000,
        )

    t2 = threading.Thread(target=_ask)
    t2.start()
    time.sleep(0.1)
    woke = inbox_wait(ses["session_id"], timeout_ms=3000)
    assert woke["wakeup"] == "peer_request"
    assert woke["request"]["subject"] == "wake on request"
    claimed = wait_for_request(session_id=ses["session_id"], timeout_ms=2000)
    assert claimed is not None
    answer_request(claimed["code"], "yes", evidence="test", session_id=ses["session_id"])
    t2.join(timeout=5)


def test_inbox_wait_times_out_quietly(tmp_path, start_tracked_session):

    ses = start_tracked_session(str(tmp_path / "quiet"), tool="codex")
    started = time.monotonic()
    result = inbox_wait(ses["session_id"], timeout_ms=250)
    assert result == {"wakeup": None, "timeout": True}
    assert time.monotonic() - started < 5


def test_inbox_wait_skips_unclaimable_requests_before_limit(
    tmp_path, start_tracked_session, monkeypatch
):
    monkeypatch.setattr("brains.control.help_execution.schedule_help_review", lambda _code: True)
    workspace = register_workspace(str(tmp_path / "target"))
    peer = start_tracked_session(str(tmp_path / "target"), tool="claude")
    for index in range(12):
        file_help_request(
            f"wrong tool {index}",
            "inspect",
            to_workspace=workspace.slug,
            required_tool="copilot",
            execution_mode="existing",
            timeout_ms=5000,
        )
    file_help_request(
        "execution only",
        "inspect",
        to_workspace=workspace.slug,
        required_tool="claude",
        execution_mode="ephemeral",
        timeout_ms=5000,
    )
    expected = file_help_request(
        "claimable",
        "inspect",
        to_workspace=workspace.slug,
        required_tool="claude",
        execution_mode="existing",
        timeout_ms=5000,
    )

    wake = inbox_wait(peer["session_id"], timeout_ms=250)

    assert wake["wakeup"] == "peer_request"
    assert wake["request"]["code"] == expected["code"]

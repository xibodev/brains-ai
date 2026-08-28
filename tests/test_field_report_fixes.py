"""Regression tests from the 2026-08-24 six-issue field report.

Five coordinated sessions found real failures in the running install. Each
test here pins the fix for one of them:

1. Liveness: a session with fresh heartbeat activity is NEVER reaped for a
   dead PID alone (the recorded pid is often the brains stdio child, not
   the agent); quiet + pid-dead is what gets reaped.
2. Broadcasts: cross-workspace broadcast is interest-scoped topic delivery;
   workspace mail is local. NULL-workspace rows are direct-delivery only,
   never a cross-project firehose.
3. Evidence vs expiry: claiming stops the expiry clock (bounded by a claim
   grace), so gathering real evidence can't get the answer discarded.
4. Polling an ended/unknown session errors loudly instead of silent [].
5. Wake capability: live-agent discovery discloses interactive_input so an
   orchestrator knows before building on "message you mid-run".
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("BRAINS_HELP_POLL_INTERVAL_MS", "10")

from brains.control import help as help_mod  # noqa: E402
from brains.control.help import (  # noqa: E402
    answer_request,
    ask_peer,
    wait_for_request,
)
from brains.control.mailbox import read_messages, send_message  # noqa: E402
from brains.control.sessions import (  # noqa: E402
    reap_zombie_sessions,
    register_workspace,
    start_session,
)
from brains.control.topics import post_topic, read_topic, subscribe_topic  # noqa: E402
from brains.storage.db import SessionLocal  # noqa: E402
from brains.storage.models import AgentSession  # noqa: E402


@pytest.fixture
def tracked_session():
    """``start_session`` bound to an explicit path, force-ended on teardown."""
    created: list[str] = []

    def _start(path: str, tool: str):
        ses = start_session(path, tool=tool)
        created.append(ses["session_id"])
        return ses

    yield _start
    for sid in created:
        try:
            with SessionLocal() as session:
                row = session.get(AgentSession, sid)
                if row and row.ended_at is None:
                    row.ended_at = datetime.now(UTC)
                    session.commit()
        except Exception:
            pass


# --- issue 1: activity beats pid -------------------------------------------


def _set_activity(session_id: str, **kwargs) -> None:
    with SessionLocal() as session:
        row = session.get(AgentSession, session_id)
        for field, value in kwargs.items():
            setattr(row, field, value)
        session.commit()


def test_reaper_spares_active_session_with_dead_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(help_mod, "_claim_grace_seconds", 600)  # no-op guard
    ses = start_session(str(tmp_path / "reap-a"), tool="probe")
    dead_pid = 999_999_999
    _set_activity(ses["session_id"], pid=dead_pid)

    # The recorded pid is dead (nothing has that pid), but the session sent
    # a message seconds ago — that traffic IS liveness.
    send_message(
        "still working",
        workspace_path=str(tmp_path / "reap-a"),
        from_session_id=ses["session_id"],
    )
    assert reap_zombie_sessions() == []
    with SessionLocal() as session:
        row = session.get(AgentSession, ses["session_id"])
        assert row.ended_at is None
        assert row.state == "running"


def test_reaper_takes_quiet_session_with_dead_pid(monkeypatch, tmp_path):
    import brains.control.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "STALE_SESSION_TTL_SECONDS", 1)
    ses = start_session(str(tmp_path / "reap-b"), tool="probe")
    stale = datetime.now(UTC) - timedelta(seconds=120)
    _set_activity(ses["session_id"], pid=999_999_998, last_activity_at=stale)
    reaped = reap_zombie_sessions()
    assert ses["session_id"] in reaped


# --- issues 2 + 6: mailbox scoping ------------------------------------------


def test_null_workspace_message_is_direct_delivery_only(tmp_path, tracked_session):
    ses_b = tracked_session(str(tmp_path / "beta"), "claude")

    # A legacy/system row with neither anchor is refused at send time now…
    with pytest.raises(ValueError):
        send_message("orphan broadcast")
    # …and a direct-to-session row with null workspace reaches ONLY that
    # session — it must not leak to unrelated workspaces' inboxes.
    send_message("targeted note", to_session_id=ses_b["session_id"])
    assert [m["subject"] for m in read_messages(ses_b["session_id"])] == ["targeted note"]

    stranger = start_session(str(tmp_path / "gamma"), tool="codex")
    assert read_messages(stranger["session_id"]) == []


def test_workspace_mail_stays_local_broadcast_goes_to_topic_subscribers(tmp_path, tracked_session):
    ses_a = tracked_session(str(tmp_path / "alpha"), "copilot")
    ses_b = tracked_session(str(tmp_path / "beta"), "claude")

    # Workspace-addressed mail reaches that workspace's own session...
    send_message("alpha-local", workspace_path=str(tmp_path / "alpha"))
    subjects_a = [m["subject"] for m in read_messages(ses_a["session_id"])]
    assert "alpha-local" in subjects_a
    # ...but never leaks into another workspace's inbox.
    assert all(m["subject"] != "alpha-local" for m in read_messages(ses_b["session_id"]))

    # Cross-workspace broadcast is interest-scoped and does not create mailbox rows.
    subscribe_topic("field-report", ses_b["session_id"])
    posted = post_topic(
        "field-report",
        "broadcast check",
        from_session_id=ses_a["session_id"],
        workspace_path=str(tmp_path / "alpha"),
    )
    assert read_messages(ses_b["session_id"]) == []
    assert [row["id"] for row in read_topic(posted["topic"], session_id=ses_b["session_id"])] == [
        posted["id"]
    ]


# --- issue 4: polling a dead handle is loud ---------------------------------


def test_read_messages_refuses_ended_and_unknown_sessions(tmp_path, tracked_session):
    ses = tracked_session(str(tmp_path / "delta"), "probe")
    with SessionLocal() as session:
        row = session.get(AgentSession, ses["session_id"])
        row.ended_at = datetime.now(UTC)
        row.state = "completed"
        session.commit()

    with pytest.raises(ValueError, match="ended"):
        read_messages(ses["session_id"])
    with pytest.raises(ValueError, match="unknown session"):
        read_messages("ses_doesnotexist")


# --- issue 3: evidence time doesn't kill the answer -------------------------


def _file_open_request(from_session_id: str, to_workspace: str, timeout_ms: int):
    box: dict = {}

    def _runner():
        try:
            box["result"] = ask_peer(
                "Need evidence",
                "Which file holds the gate?",
                from_session_id=from_session_id,
                to_workspace=to_workspace,
                timeout_ms=timeout_ms,
            )
        except Exception as exc:  # pragma: no cover
            box["error"] = exc

    import threading

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return box, t


def test_claimed_request_survives_original_expiry(tmp_path, tracked_session, monkeypatch):
    monkeypatch.setenv("BRAINS_HELP_CLAIM_GRACE_SECONDS", "600")
    ws_b = register_workspace(str(tmp_path / "beta"))
    asker = tracked_session(str(tmp_path / "alpha"), "copilot")
    peer = tracked_session(str(tmp_path / "beta"), "claude")

    box, thread = _file_open_request(asker["session_id"], ws_b.slug, timeout_ms=300)
    claimed = wait_for_request(session_id=peer["session_id"], timeout_ms=3000)
    assert claimed is not None

    # The peer now takes longer than the ORIGINAL 300ms asker timeout to
    # gather evidence. The claim must hold the request open.
    time.sleep(0.6)
    answered = answer_request(
        claimed["code"],
        "The gate lives in exec/gate.py.",
        evidence="src/brains/exec/gate.py:1447",
        session_id=peer["session_id"],
    )
    assert answered["status"] == "answered"

    thread.join(timeout=10)
    result = box.get("result") or {}
    # The asker either saw the answer or was still waiting when it landed —
    # both prove the request was not expired out from under the working peer.
    assert result.get("status") in ("answered", None)


def test_unclaimed_request_still_expires_normally(tmp_path, tracked_session):
    ws_b = register_workspace(str(tmp_path / "beta"))
    asker = tracked_session(str(tmp_path / "alpha"), "copilot")

    result = ask_peer(
        "Nobody home",
        "expires quietly?",
        from_session_id=asker["session_id"],
        to_workspace=ws_b.slug,
        timeout_ms=200,
    )
    assert result["status"] == "expired"


# --- issue 5: wake capability disclosure ------------------------------------


def test_live_agents_disclose_interactive_input(tmp_path, tracked_session):
    ses = tracked_session(str(tmp_path / "echo"), "opencode")
    from brains.control.topics import live_agent_sessions

    agents = {row["session_id"]: row for row in live_agent_sessions()}
    assert agents[ses["session_id"]]["interactive_input"] is False


# --- addendum (a): attribution is validated against liveness ----------------


def test_cannot_send_as_a_reaped_session(tmp_path, tracked_session):
    ses = tracked_session(str(tmp_path / "alpha"), tool="copilot")
    with SessionLocal() as session:
        row = session.get(AgentSession, ses["session_id"])
        row.ended_at = datetime.now(UTC)
        row.state = "failed"
        session.commit()

    with pytest.raises(ValueError, match="refusing send_message"):
        send_message(
            "impersonation attempt",
            from_session_id=ses["session_id"],
            workspace_path=str(tmp_path / "alpha"),
        )


def test_cannot_ask_as_a_reaped_session(tmp_path, tracked_session):
    register_workspace(str(tmp_path / "beta"))
    ses = tracked_session(str(tmp_path / "alpha"), tool="copilot")
    with SessionLocal() as session:
        row = session.get(AgentSession, ses["session_id"])
        row.ended_at = datetime.now(UTC)
        session.commit()

    with pytest.raises(ValueError, match="refusing ask_peer"):
        ask_peer(
            "from the grave",
            "still there?",
            from_session_id=ses["session_id"],
            to_workspace="beta",
            timeout_ms=200,
        )


def test_read_error_names_live_replacement_candidates(tmp_path, tracked_session):
    dead = tracked_session(str(tmp_path / "alpha"), tool="copilot")
    live = tracked_session(str(tmp_path / "alpha"), tool="codex")
    with SessionLocal() as session:
        row = session.get(AgentSession, dead["session_id"])
        row.ended_at = datetime.now(UTC)
        row.summary = "zombie reaped: pid 1 dead and no activity"
        session.commit()

    with pytest.raises(ValueError) as excinfo:
        read_messages(dead["session_id"])
    message = str(excinfo.value)
    assert "ended_at=" in message
    assert "zombie reaped" in message
    assert live["session_id"] in message


# --- addendum: explicit-only rerouting ---------------------------------------


def test_send_to_ended_handle_refuses_by_default_and_reroutes_on_opt_in(tmp_path, tracked_session):
    register_workspace(str(tmp_path / "beta"))
    dead = tracked_session(str(tmp_path / "beta"), tool="copilot")
    live = tracked_session(str(tmp_path / "beta"), tool="claude")
    outsider = tracked_session(str(tmp_path / "gamma"), tool="codex")

    with SessionLocal() as session:
        row = session.get(AgentSession, dead["session_id"])
        row.ended_at = datetime.now(UTC)
        session.commit()

    # Default: refuse loudly, naming the single live candidate.
    with pytest.raises(ValueError, match=live["session_id"]):
        send_message("to a dead handle", to_session_id=dead["session_id"])

    # Explicit opt-in with EXACTLY one live candidate: reroute, and say so.
    sent = send_message(
        "to a dead handle",
        to_session_id=dead["session_id"],
        route_to_current=True,
        from_session_id=outsider["session_id"],
    )
    assert sent["routed_from"] == dead["session_id"]
    assert sent["routed_to"] == live["session_id"]
    assert f"[rerouted from {dead['session_id']}]" in sent["subject"]

    delivered = read_messages(live["session_id"])
    assert any("to a dead handle" in m["subject"] for m in delivered)


def test_route_to_current_refuses_when_candidates_are_ambiguous(tmp_path, tracked_session):
    dead = tracked_session(str(tmp_path / "beta2"), tool="copilot")
    live_a = tracked_session(str(tmp_path / "beta2"), tool="claude")
    live_b = tracked_session(str(tmp_path / "beta2"), tool="codex")
    with SessionLocal() as session:
        row = session.get(AgentSession, dead["session_id"])
        row.ended_at = datetime.now(UTC)
        session.commit()

    with pytest.raises(ValueError) as excinfo:
        send_message(
            "ambiguous",
            to_session_id=dead["session_id"],
            route_to_current=True,
        )
    assert live_a["session_id"] in str(excinfo.value)
    assert live_b["session_id"] in str(excinfo.value)

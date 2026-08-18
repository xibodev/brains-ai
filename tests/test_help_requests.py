"""Tests for the cross-session peer-help long-poll RPC.

The contract under test:

* ``ask_peer`` blocks until a peer answers OR until the timeout expires;
  expired requests come back with ``status="expired"``.
* ``wait_for_request`` matches by either ``to_session_id`` or
  ``to_workspace`` (the slug). It atomically claims the row so two
  waiters don't both grab it.
* ``answer_request`` requires non-empty ``evidence`` and refuses to
  answer requests claimed by another session.
* ``ask_depth`` is capped at ``MAX_ASK_DEPTH`` to prevent A↔B deadlock.

To keep test runtime tight we crank the poll interval down via
``BRAINS_HELP_POLL_INTERVAL_MS=10`` so the long-poll loop ticks every
~10 ms. Long-poll ``ask_peer`` is run on a worker thread so the main
test thread can simulate the peer claiming + answering.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

# Tighten the poll loop before importing the module under test so the
# constant is read on first call.
os.environ.setdefault("BRAINS_HELP_POLL_INTERVAL_MS", "10")

from brains.control.help import (  # noqa: E402
    MAX_ASK_DEPTH,
    HelpDeadlockError,
    answer_request,
    ask_peer,
    list_open_help_requests,
    wait_for_request,
)
from brains.control.sessions import register_workspace, start_session  # noqa: E402
from brains.storage.db import SessionLocal  # noqa: E402
from brains.storage.models import HelpRequest  # noqa: E402


def _run_ask_in_thread(**kwargs) -> dict:
    """Run ask_peer on a worker thread and return its result via container."""
    box: dict = {}

    def _runner():
        try:
            box["result"] = ask_peer(**kwargs)
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            box["error"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return box, t


def test_ask_peer_validates_inputs(tmp_path):
    workspace = register_workspace(str(tmp_path))
    with pytest.raises(ValueError, match="subject"):
        ask_peer("", "what?", to_workspace=workspace.slug, timeout_ms=200)
    with pytest.raises(ValueError, match="question"):
        ask_peer("subj", "", to_workspace=workspace.slug, timeout_ms=200)
    with pytest.raises(ValueError, match="to_workspace or to_session_id"):
        ask_peer("subj", "q?", timeout_ms=200)


def test_ask_peer_times_out_with_expired_status(tmp_path):
    workspace = register_workspace(str(tmp_path))
    started = start_session(str(tmp_path), tool="pytest")
    result = ask_peer(
        "ping",
        "anyone there?",
        from_session_id=started["session_id"],
        to_workspace=workspace.slug,
        timeout_ms=200,
    )
    assert result["status"] == "expired"
    assert result["answer"] is None


def test_full_ask_wait_answer_roundtrip(tmp_path):
    workspace = register_workspace(str(tmp_path))
    asker = start_session(str(tmp_path), tool="pytest-asker")
    answerer = start_session(str(tmp_path), tool="pytest-answerer")

    box, asker_thread = _run_ask_in_thread(
        subject="db question",
        question="where is the migration runner?",
        from_session_id=asker["session_id"],
        to_workspace=workspace.slug,
        timeout_ms=5000,
    )

    # Give the asker a moment to file the row before the answerer waits.
    deadline = time.monotonic() + 2.0
    claimed = None
    while time.monotonic() < deadline and claimed is None:
        claimed = wait_for_request(
            session_id=answerer["session_id"],
            workspace_slug=workspace.slug,
            timeout_ms=200,
        )
    assert claimed is not None, "wait_for_request never matched"
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by_session_id"] == answerer["session_id"]
    assert claimed["subject"] == "db question"

    answered = answer_request(
        code=claimed["code"],
        answer="src/brains/storage/migrations.py",
        evidence="src/brains/storage/migrations.py:1-200",
        session_id=answerer["session_id"],
    )
    assert answered["status"] == "answered"
    assert answered["evidence"].startswith("src/brains/storage/migrations.py")

    asker_thread.join(timeout=5.0)
    assert not asker_thread.is_alive(), "ask_peer never returned"
    result = box.get("result")
    assert result is not None, f"asker failed: {box.get('error')}"
    assert result["status"] == "answered"
    assert "migrations.py" in result["answer"]
    assert result["evidence"]


def test_answer_request_requires_evidence(tmp_path):
    workspace = register_workspace(str(tmp_path))
    asker = start_session(str(tmp_path), tool="pytest-asker")
    answerer = start_session(str(tmp_path), tool="pytest-answerer")

    box, asker_thread = _run_ask_in_thread(
        subject="ev test",
        question="anything",
        from_session_id=asker["session_id"],
        to_workspace=workspace.slug,
        timeout_ms=5000,
    )
    try:
        deadline = time.monotonic() + 2.0
        claimed = None
        while time.monotonic() < deadline and claimed is None:
            claimed = wait_for_request(
                session_id=answerer["session_id"],
                workspace_slug=workspace.slug,
                timeout_ms=200,
            )
        assert claimed is not None
        with pytest.raises(ValueError, match="evidence"):
            answer_request(
                code=claimed["code"],
                answer="here you go",
                evidence="",
                session_id=answerer["session_id"],
            )
    finally:
        # Always finish the asker — answer it so it returns instead of
        # waiting out its timeout, keeping the test suite fast.
        answer_request(
            code=claimed["code"],
            answer="done",
            evidence="see test",
            session_id=answerer["session_id"],
        )
        asker_thread.join(timeout=5.0)


def test_answer_request_rejects_wrong_session(tmp_path):
    workspace = register_workspace(str(tmp_path))
    asker = start_session(str(tmp_path), tool="pytest-asker")
    claimer = start_session(str(tmp_path), tool="pytest-claimer")
    stranger = start_session(str(tmp_path), tool="pytest-stranger")

    box, asker_thread = _run_ask_in_thread(
        subject="ownership",
        question="?",
        from_session_id=asker["session_id"],
        to_workspace=workspace.slug,
        timeout_ms=5000,
    )
    try:
        deadline = time.monotonic() + 2.0
        claimed = None
        while time.monotonic() < deadline and claimed is None:
            claimed = wait_for_request(
                session_id=claimer["session_id"],
                workspace_slug=workspace.slug,
                timeout_ms=200,
            )
        assert claimed is not None
        with pytest.raises(ValueError, match="claimed by another session"):
            answer_request(
                code=claimed["code"],
                answer="not yours",
                evidence="test",
                session_id=stranger["session_id"],
            )
    finally:
        answer_request(
            code=claimed["code"],
            answer="ok",
            evidence="test",
            session_id=claimer["session_id"],
        )
        asker_thread.join(timeout=5.0)


def test_wait_for_request_returns_none_on_timeout(tmp_path):
    workspace = register_workspace(str(tmp_path))
    session = start_session(str(tmp_path), tool="pytest-waiter")
    out = wait_for_request(
        session_id=session["session_id"],
        workspace_slug=workspace.slug,
        timeout_ms=150,
    )
    assert out is None


def test_ask_depth_guard_refuses_overflow(tmp_path):
    """An answerer that's mid-claim filing a fresh ask inherits depth+1;
    a chain beyond MAX_ASK_DEPTH must be refused."""
    workspace = register_workspace(str(tmp_path))
    asker = start_session(str(tmp_path), tool="pytest-asker")
    answerer = start_session(str(tmp_path), tool="pytest-answerer")

    box, asker_thread = _run_ask_in_thread(
        subject="depth seed",
        question="?",
        from_session_id=asker["session_id"],
        to_workspace=workspace.slug,
        timeout_ms=5000,
    )
    try:
        # Answerer claims first ask -> ask_depth on that row is 1.
        deadline = time.monotonic() + 2.0
        claimed = None
        while time.monotonic() < deadline and claimed is None:
            claimed = wait_for_request(
                session_id=answerer["session_id"],
                workspace_slug=workspace.slug,
                timeout_ms=200,
            )
        assert claimed is not None
        # While the answerer is still in 'claimed' state, its own
        # ask_peer should be at depth 2 — exactly MAX_ASK_DEPTH.
        nested = ask_peer(
            "nested-depth-2",
            "small ask",
            from_session_id=answerer["session_id"],
            to_workspace=workspace.slug,
            timeout_ms=100,  # let it expire fast
        )
        assert nested["ask_depth"] == MAX_ASK_DEPTH

        # Manually bump the seed row's depth to MAX so a nested ask
        # would exceed the cap and the guard fires.
        with SessionLocal() as session:
            row = session.query(HelpRequest).filter(HelpRequest.code == claimed["code"]).one()
            row.ask_depth = MAX_ASK_DEPTH
            session.commit()
        with pytest.raises(HelpDeadlockError):
            ask_peer(
                "would-be-depth-3",
                "?",
                from_session_id=answerer["session_id"],
                to_workspace=workspace.slug,
                timeout_ms=100,
            )
    finally:
        answer_request(
            code=claimed["code"],
            answer="done",
            evidence="test",
            session_id=answerer["session_id"],
        )
        asker_thread.join(timeout=5.0)


def test_list_open_help_requests_filters(tmp_path):
    workspace = register_workspace(str(tmp_path))
    asker = start_session(str(tmp_path), tool="pytest-asker")
    box, asker_thread = _run_ask_in_thread(
        subject="listing test",
        question="?",
        from_session_id=asker["session_id"],
        to_workspace=workspace.slug,
        timeout_ms=5000,
    )
    try:
        # The asker is blocked while the row is open — list should see it.
        # Window is generous so the test stays green under full-suite
        # load against a shared SQLite DB; on success the loop exits on
        # the first poll so happy-path cost is unchanged.
        deadline = time.monotonic() + 4.5
        found = False
        while time.monotonic() < deadline and not found:
            rows = list_open_help_requests(to_workspace=workspace.slug)
            found = any(r["subject"] == "listing test" for r in rows)
            if not found:
                time.sleep(0.05)
        assert found, "list_open_help_requests did not surface the open ask"
    finally:
        asker_thread.join(timeout=8.0)

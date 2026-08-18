"""Tests for :mod:`brains.control.jobs`.

The jobs module is the "ops digest" surface — recurring tasks that
summarize stale docs, open decisions, and route distribution into
decision requests an operator can review. Coverage was at 38% because
no test exercised the dispatch or the three concrete jobs.

We monkeypatch the storage-touching boundaries (``file_decision_request``,
``append_event``, ``index_docs``, ``list_open_decisions``, ``SessionLocal``,
``register_workspace``, ``init_db``) so each test is isolated from the
shared ``brains.db`` SQLite file the rest of the suite uses. This keeps
the test fast, deterministic, and free of cross-test contamination.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from brains.control import jobs


@pytest.fixture
def captured_decision(monkeypatch: pytest.MonkeyPatch):
    """Replace ``file_decision_request`` with a recorder.

    Returns a list of every (args, kwargs) tuple the job dispatched.
    """
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return {"code": f"ASK-{len(calls)}", "status": "open", "workspace": "test-ws"}

    monkeypatch.setattr(jobs, "file_decision_request", _fake)
    return calls


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch):
    """Replace ``append_event`` with a recorder."""
    events: list[tuple[str, str]] = []

    def _fake(kind: str, message: str, *, metadata: dict | None = None, **_: Any) -> None:
        events.append((kind, message))

    monkeypatch.setattr(jobs, "append_event", _fake)
    return events


# --- Dispatch ------------------------------------------------------------


def test_list_jobs_returns_all_registered_in_sorted_order() -> None:
    names = jobs.list_jobs()
    assert names == sorted(names)
    assert set(names) == {
        "stale-docs-digest",
        "open-decisions-digest",
        "route-audit",
    }


def test_run_job_unknown_name_raises_value_error(captured_events) -> None:
    with pytest.raises(ValueError, match="unknown job"):
        jobs.run_job("not-a-job")
    # Unknown name short-circuits before any event is emitted.
    assert captured_events == []


def test_run_job_success_path_appends_job_run_event(
    captured_events, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(jobs.JOBS, "stale-docs-digest", lambda _ws: {"ok": True})
    result = jobs.run_job("stale-docs-digest", "/tmp/ws")
    assert result == {"ok": True}
    kinds = [k for k, _ in captured_events]
    assert "job_run" in kinds
    assert "job_failed" not in kinds


def test_run_job_failure_appends_event_and_reraises(
    captured_events, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(_workspace: str) -> dict:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(jobs.JOBS, "stale-docs-digest", _explode)

    with pytest.raises(RuntimeError, match="kaboom"):
        jobs.run_job("stale-docs-digest", "/tmp/ws")

    kinds = [kind for kind, _ in captured_events]
    assert "job_failed" in kinds
    assert "job_run" not in kinds


# --- stale-docs-digest ---------------------------------------------------


def test_stale_docs_digest_with_no_stale_docs(
    captured_decision, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "index_docs", lambda _ws: {"records": []})
    out = jobs.stale_docs_digest("/tmp/ws")
    assert out["status"] == "open"
    assert len(captured_decision) == 1
    _args, kwargs = captured_decision[0]
    assert kwargs["proposed_answer"] == "No action needed"
    assert kwargs["metadata"] == {"job": "stale-docs-digest", "stale_count": 0}
    assert kwargs["body"] == "No stale docs found."


def test_stale_docs_digest_renders_first_50_stale_docs(
    captured_decision, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [
        {
            "rel_path": f"docs/page-{i}.md",
            "title": f"Page {i}",
            "mtime": datetime(2024, 1, 1 + (i % 28)),
            "stale": True,
        }
        for i in range(60)
    ]
    # Add a non-stale row to verify filtering.
    records.append(
        {"rel_path": "fresh.md", "title": "Fresh", "mtime": datetime(2026, 1, 1), "stale": False}
    )
    monkeypatch.setattr(jobs, "index_docs", lambda _ws: {"records": records})

    jobs.stale_docs_digest("/tmp/ws")
    assert len(captured_decision) == 1
    _args, kwargs = captured_decision[0]
    assert kwargs["metadata"]["stale_count"] == 60
    assert kwargs["proposed_answer"] == "Review 60 stale docs"
    # Body lists 50 of the 60 (the cap) and never includes the fresh row.
    body_lines = kwargs["body"].splitlines()
    assert len(body_lines) == 50
    assert "fresh.md" not in kwargs["body"]


# --- open-decisions-digest ----------------------------------------------


def test_open_decisions_digest_with_none_open(
    captured_decision, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "list_open_decisions", lambda _ws: [])
    jobs.open_decisions_digest("/tmp/ws")
    assert len(captured_decision) == 1
    _args, kwargs = captured_decision[0]
    assert kwargs["body"] == "No open decisions."
    assert kwargs["proposed_answer"] == "No action needed"
    assert kwargs["metadata"] == {"job": "open-decisions-digest", "open_count": 0}


def test_open_decisions_digest_lists_each_open_decision(
    captured_decision, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {"code": "ASK-1", "title": "Approve A", "created_at": "2026-05-30T12:00:00"},
        {"code": "ASK-2", "title": "Approve B", "created_at": "2026-05-30T12:05:00"},
    ]
    monkeypatch.setattr(jobs, "list_open_decisions", lambda _ws: rows)

    jobs.open_decisions_digest("/tmp/ws")
    assert len(captured_decision) == 1
    _args, kwargs = captured_decision[0]
    assert "ASK-1" in kwargs["body"]
    assert "ASK-2" in kwargs["body"]
    assert kwargs["proposed_answer"] == "Resolve 2 open decisions"
    assert kwargs["metadata"]["open_count"] == 2


# --- route-audit ---------------------------------------------------------


def test_route_audit_with_no_route_decisions(
    captured_decision, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub out every storage call route_audit makes.
    monkeypatch.setattr(jobs, "register_workspace", lambda _path: SimpleNamespace(path="/tmp/ws"))
    monkeypatch.setattr(jobs, "init_db", lambda: None)

    fake_session = MagicMock()
    fake_session.__enter__.return_value = fake_session
    fake_session.__exit__.return_value = False
    fake_session.query.return_value.order_by.return_value.limit.return_value.all.return_value = []
    monkeypatch.setattr(jobs, "SessionLocal", lambda: fake_session)

    jobs.route_audit("/tmp/ws")
    assert len(captured_decision) == 1
    _args, kwargs = captured_decision[0]
    assert kwargs["metadata"] == {"job": "route-audit", "counts": {}}
    assert kwargs["body"] == "No route decisions found."


def test_route_audit_counts_route_decisions_by_tier(
    captured_decision, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "register_workspace", lambda _path: SimpleNamespace(path="/tmp/ws"))
    monkeypatch.setattr(jobs, "init_db", lambda: None)

    rows = [
        SimpleNamespace(model_tier="local"),
        SimpleNamespace(model_tier="local"),
        SimpleNamespace(model_tier="frontier"),
    ]
    fake_session = MagicMock()
    fake_session.__enter__.return_value = fake_session
    fake_session.__exit__.return_value = False
    fake_session.query.return_value.order_by.return_value.limit.return_value.all.return_value = rows
    monkeypatch.setattr(jobs, "SessionLocal", lambda: fake_session)

    jobs.route_audit("/tmp/ws")
    assert len(captured_decision) == 1
    _args, kwargs = captured_decision[0]
    counts = kwargs["metadata"]["counts"]
    assert counts == {"local": 2, "frontier": 1}
    # Body must be sorted by tier name for deterministic rendering.
    assert kwargs["body"] == "- frontier: 1\n- local: 2"

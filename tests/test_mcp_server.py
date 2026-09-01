"""Tests for the MCP server's scheduler + tool-dispatch surface.

These do not start the SSE/stdio transport. We exercise the pure-Python
pieces directly:

* ``_parse_last_fired`` (timestamp normalization),
* ``_is_due`` (every supported schedule-grammar branch — manual/hourly/daily/
  ``every:<N><s|m|h|d>``; this engine does not support cron syntax),
* ``_scheduler_tick`` (with ``list_recurring_tasks`` + ``fire_recurring_task``
  monkeypatched so no DB hit happens),
* ``list_tools`` and ``call_tool`` (registry shape + ``brains.`` prefix
  normalization + unknown-tool error),
* the ``TOOL_REGISTRY`` itself, asserting every key points to a callable.

Scheduler-loop and ``run_mcp_server`` are deliberately not covered;
they spawn threads / start transports and are not unit-testable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brains.mcp import server as mcp_server

# --- _parse_last_fired ---------------------------------------------------


def test_parse_last_fired_returns_none_for_empty() -> None:
    assert mcp_server._parse_last_fired("") is None
    assert mcp_server._parse_last_fired(None) is None


def test_parse_last_fired_attaches_utc_when_naive() -> None:
    parsed = mcp_server._parse_last_fired("2026-01-02T03:04:05")
    assert parsed is not None
    assert parsed.tzinfo is UTC


def test_parse_last_fired_preserves_existing_tz() -> None:
    parsed = mcp_server._parse_last_fired("2026-01-02T03:04:05+02:00")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=2)


def test_parse_last_fired_returns_none_for_garbage() -> None:
    assert mcp_server._parse_last_fired("not-a-timestamp") is None


# --- _is_due -------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


def test_is_due_never_for_manual_or_unknown() -> None:
    now = _now()
    assert mcp_server._is_due("manual", None, now) is False
    assert mcp_server._is_due("never-heard-of-it", None, now) is False
    assert mcp_server._is_due("", None, now) is False


def test_is_due_true_on_first_run_for_known_expressions() -> None:
    """When last_fired is None and the expression is recognized, fire immediately."""
    now = _now()
    assert mcp_server._is_due("hourly", None, now) is True
    assert mcp_server._is_due("daily", None, now) is True
    assert mcp_server._is_due("every:5m", None, now) is True


def test_is_due_hourly_respects_one_hour_gap() -> None:
    now = _now()
    just_now = now - timedelta(minutes=30)
    one_hour_ago = now - timedelta(hours=1)
    assert mcp_server._is_due("hourly", just_now, now) is False
    assert mcp_server._is_due("hourly", one_hour_ago, now) is True


def test_is_due_daily_uses_calendar_date_not_24h_window() -> None:
    now = datetime(2026, 5, 30, 0, 5, tzinfo=UTC)
    yesterday_late = datetime(2026, 5, 29, 23, 59, tzinfo=UTC)
    today_earlier = datetime(2026, 5, 30, 0, 0, tzinfo=UTC)
    assert mcp_server._is_due("daily", yesterday_late, now) is True
    assert mcp_server._is_due("daily", today_earlier, now) is False


@pytest.mark.parametrize(
    "expr,delta,expected",
    [
        ("every:30s", timedelta(seconds=29), False),
        ("every:30s", timedelta(seconds=31), True),
        ("every:5m", timedelta(minutes=4, seconds=59), False),
        ("every:5m", timedelta(minutes=5, seconds=1), True),
        ("every:2h", timedelta(hours=1, minutes=59), False),
        ("every:2h", timedelta(hours=2, minutes=1), True),
        ("every:1d", timedelta(hours=23, minutes=59), False),
        ("every:1d", timedelta(hours=24, minutes=1), True),
    ],
)
def test_is_due_every_n_unit_branches(expr: str, delta: timedelta, expected: bool) -> None:
    now = _now()
    last = now - delta
    assert mcp_server._is_due(expr, last, now) is expected


def test_is_due_every_is_case_insensitive() -> None:
    now = _now()
    last = now - timedelta(minutes=10)
    assert mcp_server._is_due("EVERY:5M", last, now) is True


# --- _scheduler_tick -----------------------------------------------------


def test_scheduler_tick_fires_only_due_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAINS_MCP_EXPERIMENTAL", "1")
    now = _now()
    # Three recurring tasks: one due, one not-yet-due, one with no last_fired.
    monkeypatch.setattr(
        mcp_server,
        "list_recurring_tasks",
        lambda **_kw: [
            {
                "name": "hourly-due",
                "cron_expr": "hourly",
                "last_fired_at": (now - timedelta(hours=2)).isoformat(),
            },
            {
                "name": "hourly-not-due",
                "cron_expr": "hourly",
                "last_fired_at": (now - timedelta(minutes=10)).isoformat(),
            },
            {
                "name": "first-run",
                "cron_expr": "every:5m",
                "last_fired_at": None,
            },
        ],
    )
    fire_log: list[str] = []

    def _fire(name: str, source: str = "manual", **_kwargs) -> dict:
        fire_log.append(name)
        return {"task": {"code": f"task-{name}"}}

    monkeypatch.setattr(mcp_server, "fire_recurring_task", _fire)

    fired = mcp_server._scheduler_tick(now=now)
    fired_names = sorted(entry["name"] for entry in fired)
    assert fired_names == ["first-run", "hourly-due"]
    assert sorted(fire_log) == ["first-run", "hourly-due"]


def test_scheduler_tick_swallows_per_task_errors_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAINS_MCP_EXPERIMENTAL", "1")
    now = _now()
    monkeypatch.setattr(
        mcp_server,
        "list_recurring_tasks",
        lambda **_kw: [
            {"name": "boom", "cron_expr": "hourly", "last_fired_at": None},
            {"name": "ok", "cron_expr": "hourly", "last_fired_at": None},
        ],
    )

    def _fire(name: str, source: str = "manual", **_kwargs) -> dict:
        if name == "boom":
            raise RuntimeError("database unavailable")
        return {"task": {"code": "task-ok"}}

    monkeypatch.setattr(mcp_server, "fire_recurring_task", _fire)
    fired = mcp_server._scheduler_tick(now=now)
    # The failing one is logged + skipped; the healthy one still fires.
    assert [entry["name"] for entry in fired] == ["ok"]


def test_scheduler_tick_skips_entries_without_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAINS_MCP_EXPERIMENTAL", "1")
    now = _now()
    monkeypatch.setattr(
        mcp_server,
        "list_recurring_tasks",
        lambda **_kw: [{"cron_expr": "hourly", "last_fired_at": None}],
    )
    monkeypatch.setattr(
        mcp_server,
        "fire_recurring_task",
        lambda *_a, **_kw: pytest.fail("must not be called"),
    )
    assert mcp_server._scheduler_tick(now=now) == []


# --- Runtime staleness sweep — BL-P1-13 -----------------------------------
#
# ``brains.control.runtimes.sweep_stale`` existed with no periodic caller: a
# Runtime that stopped heartbeating stayed "online" forever unless an
# operator happened to read it. The scheduler tick is now that owner.


def test_scheduler_tick_calls_the_runtime_staleness_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _now()
    monkeypatch.setattr(mcp_server, "list_recurring_tasks", lambda **_kw: [])
    calls: list[int] = []
    monkeypatch.setattr(
        mcp_server,
        "_sweep_stale_runtimes",
        lambda: calls.append(1) or 0,
    )
    mcp_server._scheduler_tick(now=now)
    assert calls == [1]


def test_scheduler_tick_calls_the_session_lease_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "list_recurring_tasks", lambda **_kw: [])
    calls: list[int] = []
    monkeypatch.setattr(
        mcp_server,
        "_sweep_stale_sessions",
        lambda: calls.append(1) or 0,
    )
    mcp_server._scheduler_tick(now=_now())
    assert calls == [1]


def test_scheduler_tick_processes_mailbox_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "list_recurring_tasks", lambda **_kw: [])
    calls: list[int] = []
    monkeypatch.setattr(mcp_server, "_process_mailbox_smtp", lambda: calls.append(1) or 0)

    mcp_server._scheduler_tick(now=_now())

    assert calls == [1]


def test_scheduler_tick_runs_the_runtime_sweep_even_when_fire_list_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep is unconditional maintenance, not gated on any due task."""
    now = _now()
    monkeypatch.setattr(mcp_server, "list_recurring_tasks", lambda **_kw: [])
    from brains.control import runtimes as runtimes_mod

    swept: list[int] = []
    monkeypatch.setattr(
        runtimes_mod,
        "sweep_stale",
        lambda ttl_seconds, session_id=None: swept.append(ttl_seconds) or [],
    )
    mcp_server._scheduler_tick(now=now)
    assert swept == [mcp_server._runtime_stale_ttl_seconds()]


# --- Experimental gate: scheduled auto-fire + tool surface -----------------


def test_scheduler_tick_does_not_auto_fire_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduled auto-fire is experimental: the default install never fires."""
    monkeypatch.delenv("BRAINS_MCP_EXPERIMENTAL", raising=False)
    now = _now()
    monkeypatch.setattr(
        mcp_server,
        "list_recurring_tasks",
        lambda **_kw: [{"name": "due-hourly", "cron_expr": "hourly", "last_fired_at": None}],
    )
    monkeypatch.setattr(
        mcp_server,
        "fire_recurring_task",
        lambda *_a, **_kw: pytest.fail("scheduled auto-fire must be gated"),
    )
    assert mcp_server._scheduler_tick(now=now) == []


def test_runtime_stale_ttl_seconds_uses_documented_default(monkeypatch) -> None:
    monkeypatch.delenv("BRAINS_RUNTIME_STALE_TTL_SECONDS", raising=False)
    assert mcp_server._runtime_stale_ttl_seconds() == mcp_server.DEFAULT_RUNTIME_STALE_TTL_SECONDS


def test_runtime_stale_ttl_seconds_respects_env_override(monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_RUNTIME_STALE_TTL_SECONDS", "45")
    assert mcp_server._runtime_stale_ttl_seconds() == 45


def test_runtime_stale_ttl_seconds_falls_back_on_garbage(monkeypatch) -> None:
    monkeypatch.setenv("BRAINS_RUNTIME_STALE_TTL_SECONDS", "not-a-number")
    assert mcp_server._runtime_stale_ttl_seconds() == mcp_server.DEFAULT_RUNTIME_STALE_TTL_SECONDS


def test_sweep_stale_runtimes_swallows_errors(monkeypatch) -> None:
    from brains.control import runtimes as runtimes_mod

    def _boom(ttl_seconds, session_id=None):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(runtimes_mod, "sweep_stale", _boom)
    assert mcp_server._sweep_stale_runtimes() == 0


def test_sweep_stale_runtimes_flips_a_silent_online_runtime_offline(monkeypatch) -> None:
    """End-to-end through the real DB: register a runtime, age its heartbeat
    past the TTL, and confirm the scheduler-owned sweep flips it offline."""
    from datetime import timedelta

    from brains.control import runtimes as runtimes_mod
    from brains.control.common import utc_now
    from brains.storage.db import SessionLocal
    from brains.storage.models import Runtime

    registered = runtimes_mod.register_runtime("machine-sweep-test", "copilot")
    runtime_id = registered["id"]
    monkeypatch.setenv("BRAINS_RUNTIME_STALE_TTL_SECONDS", "60")
    with SessionLocal() as session:
        rt = session.get(Runtime, runtime_id)
        rt.status = "online"
        rt.last_heartbeat_at = utc_now() - timedelta(seconds=600)
        session.commit()

    flipped = mcp_server._sweep_stale_runtimes()

    # A full-suite run shares one DB across every test file, so other
    # already-registered online Runtimes may also be stale by the time this
    # runs; assert on our own runtime's outcome, not an exact global count.
    assert flipped >= 1
    with SessionLocal() as session:
        rt = session.get(Runtime, runtime_id)
        assert rt.status == "offline"


# --- Tool registry + dispatch -------------------------------------------


def test_list_tools_returns_brains_prefixed_names() -> None:
    names = mcp_server.list_tools()
    assert all(n.startswith("brains_") for n in names)
    assert "brains_plan_request" in names
    assert "brains_search_repo" in names
    assert "brains_learn_propose" in names
    assert "brains_retrieve_original" in names
    # Experimental tools are absent from the default advertised surface.
    experimental = {f"brains_{n}" for n in mcp_server.EXPERIMENTAL_MCP_TOOLS}
    assert not (experimental & set(names))
    # Registry shouldn't be empty - this catches accidental wipes.
    assert len(names) >= 30


def test_experimental_tools_opt_in_advertises_and_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAINS_MCP_EXPERIMENTAL", "1")
    selected = set(mcp_server._resolve_active_tools())
    assert selected >= mcp_server.EXPERIMENTAL_MCP_TOOLS

    # An explicit allowlist naming an experimental tool still needs the env.
    monkeypatch.delenv("BRAINS_MCP_EXPERIMENTAL", raising=False)
    monkeypatch.setenv("BRAINS_MCP_TOOLS", "search_semantic,start_session")
    allowlisted = set(mcp_server._resolve_active_tools())
    assert "start_session" in allowlisted
    assert "search_semantic" not in allowlisted


def test_call_tool_refuses_experimental_tool_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAINS_MCP_EXPERIMENTAL", raising=False)
    with pytest.raises(ValueError, match="BRAINS_MCP_EXPERIMENTAL"):
        mcp_server.call_tool("brains_graph_query", workspace_path=".", question="x")
    monkeypatch.setenv("BRAINS_MCP_EXPERIMENTAL", "1")
    # With the opt-in the dispatch reaches the registered tool; a stub keeps
    # this a gate test rather than a graph test.
    monkeypatch.setitem(mcp_server.TOOL_REGISTRY, "graph_query", lambda **_kw: {"ok": True})
    assert mcp_server.call_tool("brains_graph_query", workspace_path=".") == {"ok": True}


def test_registered_mcp_tool_names_are_anthropic_safe() -> None:
    """Every FastMCP-exposed tool name must match Anthropic's tool-name rule
    ``^[a-zA-Z0-9_-]+$``.

    Regression guard for the 0.1.0a12 MCP outage: tools were registered with a
    dotted ``brains.`` prefix. Claude Code sanitises the dot to ``brains_<x>``
    when surfacing the tool, then forwards that sanitised name back on call —
    a name the server never registered — so every Claude ``tools/call`` failed
    with JSON-RPC ``-32602``. Underscores round-trip cleanly.
    """
    import re

    from brains.mcp.server import mcp

    names = [t.name for t in mcp._tool_manager.list_tools()]
    offenders = [n for n in names if not re.fullmatch(r"[a-zA-Z0-9_-]+", n)]
    assert not offenders, (
        f"{len(offenders)} MCP tool name(s) violate Anthropic's "
        f"^[a-zA-Z0-9_-]+$ rule (dots break Claude Code): {offenders[:5]}"
    )
    assert "brains_start_session" in names


def test_tool_registry_every_value_is_callable() -> None:
    for name, fn in mcp_server.TOOL_REGISTRY.items():
        assert callable(fn), f"TOOL_REGISTRY[{name!r}] is not callable"


def test_topic_subscription_tools_are_registered() -> None:
    assert {
        "topic_subscribe",
        "topic_unsubscribe",
        "topic_subscriptions",
    } <= set(mcp_server.TOOL_REGISTRY)


def test_call_tool_normalizes_brains_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict] = []

    def _fake(**kw):
        captured.append(kw)
        return {"ok": True, "args": kw}

    monkeypatch.setitem(mcp_server.TOOL_REGISTRY, "pytest_probe", _fake)
    # Current underscore prefix:
    out1 = mcp_server.call_tool("brains_pytest_probe", a=1)
    # Without prefix:
    out2 = mcp_server.call_tool("pytest_probe", a=2)
    # Legacy dotted prefix still accepted for back-compat:
    out3 = mcp_server.call_tool("brains.pytest_probe", a=3)
    assert out1 == {"ok": True, "args": {"a": 1}}
    assert out2 == {"ok": True, "args": {"a": 2}}
    assert out3 == {"ok": True, "args": {"a": 3}}
    assert captured == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_call_tool_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown Brains tool"):
        mcp_server.call_tool("brains.does_not_exist")

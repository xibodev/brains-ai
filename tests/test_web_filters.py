"""Tests for the shared ``brains.web`` filter and macro layer.

These cross-cutting helpers are consumed by the server-rendered list pages.

Covers:
  * ``relative_time`` filter buckets (just-now / minutes / hours /
    days / months / years), naive-datetime safety, ISO-string input,
    falsy input, malformed input, future-timestamp politeness.
  * ``is_test_pollution`` predicate (UUID names, ``pytest`` /
    ``test-`` / ``test_`` / ``_test`` / ``/tmp/`` markers, empty +
    ``None`` falsy).
  * ``status_pill`` macro auto-tones the common status vocabulary
    used across sessions / tasks / decisions / handoffs / patterns /
    tools / routes / workspaces / recurring.
  * ``relative_time`` macro emits ``<abbr title="ISO">…</abbr>`` so
    the precise timestamp is one hover away, falls back to a dim em
    dash on empty input.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brains.web import env, render
from brains.web.filters import is_test_pollution, relative_time

# ---------------------------------------------------------------------------
# relative_time filter
# ---------------------------------------------------------------------------


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 6, 11, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "delta_seconds, expected",
    [
        (0, "just now"),
        (10, "just now"),
        (44, "just now"),
        (60, "1m ago"),
        (89, "1m ago"),
        (60 * 5, "5m ago"),
        (60 * 59, "59m ago"),
        (60 * 60, "1h ago"),
        (60 * 60 * 3, "3h ago"),
        (60 * 60 * 23, "23h ago"),
        (60 * 60 * 24, "1d ago"),
        (60 * 60 * 24 * 6, "6d ago"),
        (60 * 60 * 24 * 29, "29d ago"),
        (60 * 60 * 24 * 30, "1mo ago"),
        (60 * 60 * 24 * 200, "6mo ago"),
        (60 * 60 * 24 * 365, "1y ago"),
        (60 * 60 * 24 * 365 * 3, "3y ago"),
    ],
)
def test_relative_time_buckets(now: datetime, delta_seconds: int, expected: str):
    ts = now - timedelta(seconds=delta_seconds)
    assert relative_time(ts, now=now) == expected


def test_relative_time_naive_datetime_is_treated_as_utc(now: datetime):
    naive = (now - timedelta(hours=2)).replace(tzinfo=None)
    assert relative_time(naive, now=now) == "2h ago"


def test_relative_time_accepts_iso_string(now: datetime):
    iso = (now - timedelta(minutes=12)).isoformat()
    assert relative_time(iso, now=now) == "12m ago"


def test_relative_time_accepts_iso_string_with_z_suffix(now: datetime):
    iso = (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert relative_time(iso, now=now) == "3m ago"


@pytest.mark.parametrize("value", [None, "", 0])
def test_relative_time_falsy_returns_empty_string(value):
    assert relative_time(value) == ""


def test_relative_time_unparseable_returns_empty_string():
    assert relative_time("this is not a date") == ""


def test_relative_time_future_renders_politely(now: datetime):
    future = now + timedelta(minutes=5)
    assert relative_time(future, now=now) == "just now"


# ---------------------------------------------------------------------------
# is_test_pollution predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "a1b2c3d4-e5f6-7890-abcd-ef0123456789",  # uuid v-anything
        "A1B2C3D4-E5F6-7890-ABCD-EF0123456789",  # case-insensitive
        "pytest-of-runner/workspace",
        "test-fixture-1",
        "test_workspace_alpha",
        "session_pytest_42",
        "fixture_test",
        "/tmp/pytest-1234/workspace",
    ],
)
def test_is_test_pollution_positives(name: str):
    assert is_test_pollution(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "brains-v2",
        "project-alpha",
        "project-beta",
        "project-gamma",
        "Workspace · Production",
        "",
        None,
        "   ",
    ],
)
def test_is_test_pollution_negatives(name):
    assert is_test_pollution(name) is False


# ---------------------------------------------------------------------------
# Filter + test registration on the shared env
# ---------------------------------------------------------------------------


def test_env_registers_relative_time_filter():
    e = env()
    assert "relative_time" in e.filters
    assert e.filters["relative_time"] is relative_time


def test_env_registers_is_test_pollution_filter_and_test():
    e = env()
    assert "is_test_pollution" in e.filters
    # Also wired as a Jinja `is test_pollution` test so templates can
    # write the more natural `{% if name is test_pollution %}`.
    assert "test_pollution" in e.tests


# ---------------------------------------------------------------------------
# status_pill macro
# ---------------------------------------------------------------------------


def _render_macro_call(call_source: str) -> str:
    """Render an inline template that imports + calls a macro."""

    template_source = (
        '{% from "partials/_macros.html" import status_pill, relative_time %}' + call_source
    )
    return env().from_string(template_source).render()


@pytest.mark.parametrize(
    "status, expected_class",
    [
        ("active", "is-success"),
        ("approved", "is-success"),
        ("resolved", "is-success"),
        ("picked_up", "is-warning"),
        ("picked-up", "is-warning"),  # dash → underscore normalised
        ("Picked Up", "is-warning"),  # case + space normalised
        ("stale", "is-warning"),
        ("pending", "is-warning"),
        ("in_progress", "is-info"),
        ("open", "is-info"),
        ("zombie", "is-danger"),
        ("failed", "is-danger"),
        ("missing", "is-danger"),
        ("cleared", ""),  # neutral tone emits no `is-*` modifier
        ("archived", ""),
    ],
)
def test_status_pill_auto_tones_known_statuses(status: str, expected_class: str):
    html = _render_macro_call("{{ status_pill('" + status + "') }}")
    assert "pill" in html
    if expected_class:
        assert expected_class in html
    else:
        for tone in ("is-success", "is-warning", "is-danger", "is-info", "is-accent"):
            assert tone not in html


def test_status_pill_unknown_status_falls_through_to_neutral():
    html = _render_macro_call("{{ status_pill('frobnicating') }}")
    assert "pill" in html
    assert "frobnicating" in html
    for tone in ("is-success", "is-warning", "is-danger", "is-info"):
        assert tone not in html


def test_status_pill_renders_status_text_verbatim():
    html = _render_macro_call("{{ status_pill('active') }}")
    assert ">active<" in html or "active</span>" in html


# ---------------------------------------------------------------------------
# relative_time macro
# ---------------------------------------------------------------------------


def test_relative_time_macro_emits_abbr_with_iso_title():
    iso = "2026-06-10T12:00:00+00:00"
    html = _render_macro_call("{{ relative_time('" + iso + "') }}")
    assert "<abbr" in html
    assert f'title="{iso}"' in html
    assert "rel-time" in html
    # Body is a humanised string, not the raw ISO.
    assert iso not in html.split("</abbr>")[0].split(">")[-1]


def test_relative_time_macro_falsy_renders_dim_dash():
    html = _render_macro_call("{{ relative_time('') }}")
    assert "<abbr" not in html
    assert "dim" in html
    assert "—" in html


def test_relative_time_macro_custom_fallback():
    html = _render_macro_call("{{ relative_time(None, fallback='never') }}")
    assert "<abbr" not in html
    assert "never" in html


# ---------------------------------------------------------------------------
# CSS hook for the abbr element
# ---------------------------------------------------------------------------


def test_css_defines_rel_time_class():
    from brains.web import STATIC_DIR

    css = (STATIC_DIR / "brains.css").read_text(encoding="utf-8")
    assert ".rel-time" in css


# ---------------------------------------------------------------------------
# Render-smoke: existing admin overview still renders after wiring
# ---------------------------------------------------------------------------


def test_existing_admin_overview_still_renders():
    """Regression guard: adding filters + macros must not break the
    already-shipped admin overview template."""

    html = render(
        "admin/overview.html",
        page_title="Overview",
        active="overview",
        admin_nav=[
            ("overview", "Overview", "/admin/overview", "circle-dot"),
        ],
        overview={
            "greeting": "Hi",
            "tagline": "tag",
            "providers": [],
            "tier_count": 0,
            "route_count": 0,
            "models": {},
            "routes": {},
            "rate_limit_per_minute": 0,
            "api_key_set": False,
            "api_keys_count": 0,
            "openai_compatible_api_key_set": False,
            "allow_unauthenticated_api": False,
            "overlay_path": "/tmp/x",
        },
    )
    assert "stat-card" in html

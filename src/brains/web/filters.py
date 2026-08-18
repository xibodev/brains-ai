"""Jinja2 filters + utility predicates shared across templates.

Two concerns ride together because they are both single-purpose, both
consumed by every ported dashboard page, and both pure functions of
their input:

* ``relative_time(value)`` — humanise a timestamp into ``"2h ago"``
  style strings. Templates render these as ``<abbr title="ISO">…</abbr>``
  so the precise wall-clock is one hover away.
* ``is_test_pollution(name)`` — heuristic predicate that flags
  entities created by the test-suite (pytest fixtures, ``test-…``
  prefixes, bare UUID-shaped workspace names) so list pages can hide
  them by default. Extends the ``?hide_tests=1`` toggle that already
  exists on the Workspaces page across every list page.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

# Compile once. UUID-shaped names are the dominant pytest pollution
# pattern — ``test_workspace`` fixtures stamp UUIDs as workspace
# slugs/names.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Markers that recur in pytest-origin names. Match permissively — the
# false-positive cost is low (one extra row hidden behind a toggle)
# while the false-negative cost is exactly the pain this filter exists
# to solve.
_POLLUTION_SUBSTRINGS: tuple[str, ...] = (
    "pytest",
    "test-",
    "test_",
    "_test",
    "/tmp/",
    "tmp/pytest-",
)


def relative_time(value: Any, *, now: datetime | None = None) -> str:
    """Return ``"5m ago"`` / ``"2h ago"`` / ``"3d ago"`` for ``value``.

    Accepts a ``datetime``, an ISO-8601 string, or ``None`` /
    falsy input. Returns ``""`` for falsy / unparseable input so
    templates can write ``{{ ts|relative_time }}`` without guards.

    Naive datetimes are treated as UTC — every brains timestamp lands
    in the DB as UTC, so this preserves correctness without forcing
    callers to attach ``tzinfo`` everywhere.
    """

    if value in (None, "", 0):
        return ""

    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return ""

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    delta = reference - ts
    seconds = int(delta.total_seconds())

    if seconds < 0:
        # Future timestamps shouldn't happen for our data but render
        # politely instead of leaking a negative number.
        return "just now"
    if seconds < 45:
        return "just now"
    if seconds < 90:
        return "1m ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def is_test_pollution(value: Any) -> bool:
    """Heuristically detect pytest-origin entities.

    Templates use this to gate ``hide_tests`` behaviour:
    ``{% if not (entity.name|is_test_pollution) %} ... {% endif %}``.

    The predicate is intentionally permissive — surfacing five extra
    test rows on the Routes page is annoying; surfacing five hundred
    obliterates the page. Operators can always disable the gate with
    ``?hide_tests=0`` on the URL.
    """

    if value is None:
        return False
    name = str(value).strip()
    if not name:
        return False
    if _UUID_RE.match(name):
        return True
    lowered = name.lower()
    return any(marker in lowered for marker in _POLLUTION_SUBSTRINGS)


__all__ = ["is_test_pollution", "relative_time"]

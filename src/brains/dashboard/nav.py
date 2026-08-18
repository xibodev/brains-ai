"""Navigation constants for the brains.web-rendered dashboard pages.

Lives in ``brains.dashboard`` (not ``brains.web``) because the nav
shape is dashboard-specific — admin has its own nav. Kept tiny on
purpose so per-page templates can ``inject(dashboard_nav=DASHBOARD_NAV_WITH_ICONS)``
without pulling the rest of ``brains.dashboard.ui``.
"""

from __future__ import annotations

# (key, label, href, icon-name). ``key`` matches the ``active``
# context variable each page sets. Icons must exist in
# ``brains.web.icons._PATHS``; the template foundation test
# (``test_every_template_icon_reference_is_defined``) walks every
# template and would catch a typo here.
DASHBOARD_NAV_WITH_ICONS: list[tuple[str, str, str, str]] = [
    ("overview", "Overview", "/dashboard", "circle-dot"),
    ("sessions", "Sessions", "/dashboard/sessions", "bot"),
    ("exec", "Executor", "/dashboard/exec", "command"),
    ("tasks", "Tasks", "/dashboard/tasks", "list-checks"),
    ("squads", "Squads", "/dashboard/squads", "users"),
    ("decisions", "Decisions", "/dashboard/decisions", "sparkles"),
    ("handoffs", "Handoffs", "/dashboard/handoffs", "arrow-up-right"),
    ("routes", "Routes", "/dashboard/routes", "network"),
    ("graph", "Graph", "/dashboard/graph", "network"),
    ("events", "Events", "/dashboard/events", "activity"),
    ("knowledge", "Knowledge", "/dashboard/knowledge", "layers"),
    ("patterns", "Patterns", "/dashboard/patterns", "layers"),
    ("tools", "Tools", "/dashboard/tools", "wrench"),
    ("recurring", "Autopilots", "/dashboard/recurring", "refresh-cw"),
    ("workspaces", "Workspaces", "/dashboard/workspaces", "database"),
    ("operators", "Operators", "/dashboard/operators", "users"),
]


__all__ = ["DASHBOARD_NAV_WITH_ICONS"]

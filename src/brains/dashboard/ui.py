"""Inline HTML rendering for the user-facing dashboard.

Lives next to the admin UI's renderer but stays separate — clean
separation between "operator console" (admin) and "agent visibility"
(dashboard). Shares the CSS palette via the admin UI module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from brains.admin.ui import SHARED_CSS, _esc

# Module-level pill/status fragments. Defined as plain constants so the
# rendering code below can stay 3.11-compatible (PEP 701 -- mixed quote
# escapes inside an f-string expression -- is a 3.12+ feature).
_PILL_ACTIVE = '<span class="pill ok">active</span>'
_PILL_AVAILABLE = '<span class="pill ok">available</span>'
_PILL_MISSING = '<span class="pill bad">missing</span>'
_PILL_ENABLED = '<span class="pill ok">enabled</span>'
_PILL_DISABLED = '<span class="pill bad">disabled</span>'
_MUTED_EMDASH = '<span class="muted">\u2014</span>'

DASHBOARD_NAV = [
    ("overview", "Overview", "/dashboard"),
    ("sessions", "Sessions", "/dashboard/sessions"),
    ("tasks", "Tasks", "/dashboard/tasks"),
    ("decisions", "Decisions", "/dashboard/decisions"),
    ("handoffs", "Handoffs", "/dashboard/handoffs"),
    ("routes", "Routes", "/dashboard/routes"),
    ("graph", "Graph", "/dashboard/graph"),
    ("events", "Events", "/dashboard/events"),
    ("knowledge", "Knowledge", "/dashboard/knowledge"),
    ("patterns", "Patterns", "/dashboard/patterns"),
    ("tools", "Tools", "/dashboard/tools"),
    ("recurring", "Recurring", "/dashboard/recurring"),
    ("workspaces", "Workspaces", "/dashboard/workspaces"),
    ("operators", "Operators", "/dashboard/operators"),
]


def render_dashboard_layout(
    *,
    title: str,
    active: str,
    body: str,
    operator: str | None = None,
) -> str:
    nav = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">{_esc(label)}</a>'
        for key, label, href in DASHBOARD_NAV
    )
    # Render the current operator next to the brand so multi-operator
    # installs can tell at a glance whose key the cookie was signed
    # against. ``None`` hides the badge — single-operator installs
    # before Layer 1 ran still look the same.
    operator_badge = (
        f'<span class="muted" style="margin-left:.75rem;font-size:.85em;">'
        f"operator: {_esc(operator)}</span>"
        if operator
        else ""
    )
    return f"""<!doctype html>
<html><head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Brains · {_esc(title)}</title>
  <style>{SHARED_CSS}</style>
</head>
<body>
  <header class="topbar">
    <div class="brand">brains · dashboard{operator_badge}</div>
    <nav>
      {nav}
      <a href="/admin/overview">Admin ↗</a>
    </nav>
  </header>
  <main>
    {body}
  </main>
</body></html>
"""


def render_overview(*, counts: dict[str, Any], recent_events: list[dict[str, Any]]) -> str:
    event_rows = (
        "".join(
            f"<tr><td>{_esc(row['created_at'])}</td>"
            f"<td><span class='pill'>{_esc(row['kind'])}</span></td>"
            f"<td>{_esc(row['message'])}</td></tr>"
            for row in recent_events[:25]
        )
        or '<tr><td colspan=3 class="muted">No events yet.</td></tr>'
    )
    return f"""
<h1>Overview</h1>
<div class="cards">
  <div class="card"><div class="label">Workspaces</div><div class="value">{_esc(counts["workspaces"])}</div></div>
  <div class="card"><div class="label">Active sessions</div><div class="value">{_esc(counts["active_sessions"])}</div></div>
  <div class="card"><div class="label">Open decisions</div><div class="value">{_esc(counts["open_decisions"])}</div></div>
  <div class="card"><div class="label">Active tasks</div><div class="value">{_esc(counts["active_tasks"])}</div></div>
  <div class="card"><div class="label">Active handoffs</div><div class="value">{_esc(counts["active_handoffs"])}</div></div>
  <div class="card"><div class="label">Recent events</div><div class="value">{_esc(counts["recent_events"])}</div></div>
</div>
<div class="panel">
  <h2>Recent activity</h2>
  <table>
    <thead><tr><th>Time</th><th>Kind</th><th>Message</th></tr></thead>
    <tbody>{event_rows}</tbody>
  </table>
</div>
"""


def render_sessions(
    *,
    sessions: list[dict[str, Any]],
    total: int | None = None,
    page: int = 1,
    per_page: int = 50,
    search_value: str = "",
    tool_filter: str = "",
    state_filter: str = "",
    tool_options: list[str] | None = None,
) -> str:
    if total is None:
        total = len(sessions)
    tool_options = tool_options or []
    rows = (
        "".join(
            f"<tr>"
            f"<td><code>{_esc(row['id'])}</code></td>"
            f"<td>{_esc(row['workspace'])}</td>"
            f"<td><span class='pill'>{_esc(row['tool'])}</span></td>"
            f"<td>{_esc(row['started_at'])}</td>"
            f"<td>{_PILL_ACTIVE if row['ended_at'] is None else _esc(row['ended_at'])}</td>"
            f"<td>{_esc(row.get('summary') or '')}</td>"
            f"</tr>"
            for row in sessions
        )
        or '<tr><td colspan=6 class="muted">No sessions match the current filter.</td></tr>'
    )

    controls, pagination = render_listing_chrome(
        base_url="/dashboard/sessions",
        search_value=search_value,
        search_placeholder="Search id / workspace / tool / summary…",
        filters=[
            {
                "name": "tool",
                "label": "Tool",
                "value": tool_filter,
                "options": [("", "All tools")] + [(t, t) for t in tool_options],
            },
            {
                "name": "state",
                "label": "State",
                "value": state_filter,
                "options": [("", "All"), ("active", "active"), ("ended", "ended")],
            },
        ],
        page=page,
        per_page=per_page,
        total=total,
    )

    return f"""
<h1>Sessions</h1>
{controls}
<div class="panel">
  <table>
    <thead><tr><th>ID</th><th>Workspace</th><th>Tool</th><th>Started</th><th>Ended</th><th>Summary</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
{pagination}
"""


def _status_pill(status: str) -> str:
    mapping = {
        "available": "ok",
        "in_progress": "warn",
        "blocked": "bad",
        "done": "",
        "archived": "",
    }
    cls = mapping.get(status, "")
    return f'<span class="pill {cls}">{_esc(status)}</span>'


def render_tasks(
    *,
    tasks: list[dict[str, Any]],
    total: int | None = None,
    search_value: str = "",
    status_filter: str = "",
    priority_filter: str = "",
) -> str:
    if total is None:
        total = len(tasks)
    columns: dict[str, list[dict[str, Any]]] = {
        "available": [],
        "in_progress": [],
        "blocked": [],
        "done": [],
    }
    for task in tasks:
        columns.setdefault(task["status"], []).append(task)
    column_html = ""
    for status in ("available", "in_progress", "blocked", "done"):
        items = columns.get(status) or []
        item_html = (
            "".join(
                f"<li><div><strong>{_esc(t['code'])}</strong> "
                f"<span class='pill'>{_esc(t['priority'])}</span></div>"
                f"<div>{_esc(t['title'])}</div>"
                f"<div class='muted' style='font-size:11px'>{_esc(t['workspace'])}</div></li>"
                for t in items
            )
            or '<li class="muted">(empty)</li>'
        )
        column_html += f"""
<div class="panel">
  <h2>{_status_pill(status)} <span class='muted'>({len(items)})</span></h2>
  <ul style="list-style:none;padding:0;display:flex;flex-direction:column;gap:8px;">{item_html}</ul>
</div>
"""

    controls, _ = render_listing_chrome(
        base_url="/dashboard/tasks",
        search_value=search_value,
        search_placeholder="Search code / title / workspace…",
        filters=[
            {
                "name": "status",
                "label": "Status",
                "value": status_filter,
                "options": [
                    ("", "All"),
                    ("available", "available"),
                    ("in_progress", "in_progress"),
                    ("blocked", "blocked"),
                    ("done", "done"),
                ],
            },
            {
                "name": "priority",
                "label": "Priority",
                "value": priority_filter,
                "options": [("", "All"), ("p1", "p1"), ("p2", "p2"), ("p3", "p3")],
            },
        ],
        page=1,
        per_page=500,
        total=total,
    )
    return f"""
<h1>Tasks <span class="muted" style="font-size:14px;">({total} matching)</span></h1>
{controls}
<div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));">{column_html}</div>
"""


def render_decisions(
    *,
    open_decisions: list[dict[str, Any]],
    total: int | None = None,
    page: int = 1,
    per_page: int = 50,
    search_value: str = "",
) -> str:
    if total is None:
        total = len(open_decisions)
    rows = (
        "".join(
            f"<tr>"
            f"<td><a href='/dashboard/decisions/{_esc(row['code'])}'><code>{_esc(row['code'])}</code></a></td>"
            f"<td>{_esc(row['workspace'])}</td>"
            f"<td>{_esc(row['title'])}</td>"
            f"<td>{_esc(row.get('proposed_at', ''))}</td>"
            f"</tr>"
            for row in open_decisions
        )
        or '<tr><td colspan=4 class="muted">No decisions match the current filter.</td></tr>'
    )
    controls, pagination = render_listing_chrome(
        base_url="/dashboard/decisions",
        search_value=search_value,
        search_placeholder="Search code / title / workspace…",
        page=page,
        per_page=per_page,
        total=total,
    )
    return f"""
<h1>Decisions</h1>
{controls}
<div class="panel">
  <table>
    <thead><tr><th>Code</th><th>Workspace</th><th>Title</th><th>Proposed</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
{pagination}
"""


def render_decision_detail(row: dict[str, Any]) -> str:
    return f"""
<h1>{_esc(row["code"])} <span class="muted">· {_esc(row["workspace"])}</span></h1>
<div class="panel">
  <h2>{_esc(row["title"])}</h2>
  <p class="muted">{_esc(row.get("body") or "")}</p>
  <form method="post" action="/dashboard/decisions/{_esc(row["code"])}/resolve">
    <label>Chosen answer</label>
    <input type="text" name="chosen" value="approved" />
    <label>Status</label>
    <select name="status">
      <option value="resolved">resolved</option>
      <option value="rejected">rejected</option>
      <option value="deferred">deferred</option>
    </select>
    <label>Reasoning</label>
    <input type="text" name="reasoning" />
    <div style="margin-top:14px;">
      <button type="submit">Resolve</button>
      <a class="button secondary" href="/dashboard/decisions">Cancel</a>
    </div>
  </form>
</div>
"""


def render_handoffs(
    *,
    handoffs: list[dict[str, Any]],
    total: int | None = None,
    page: int = 1,
    per_page: int = 50,
    search_value: str = "",
    status_filter: str = "",
) -> str:
    if total is None:
        total = len(handoffs)
    rows = (
        "".join(
            f"<tr>"
            f"<td>{_esc(row.get('workspace', ''))}</td>"
            f"<td><strong>{_esc(row.get('title', ''))}</strong></td>"
            f"<td>{_esc(row.get('status', ''))}</td>"
            f"<td>{_esc(row.get('set_at', ''))}</td>"
            f"</tr>"
            for row in handoffs
        )
        or '<tr><td colspan=4 class="muted">No handoffs match the current filter.</td></tr>'
    )
    controls, pagination = render_listing_chrome(
        base_url="/dashboard/handoffs",
        search_value=search_value,
        search_placeholder="Search title / workspace / body…",
        filters=[
            {
                "name": "status",
                "label": "Status",
                "value": status_filter,
                "options": [
                    ("", "All"),
                    ("active", "active"),
                    ("picked", "picked"),
                    ("cleared", "cleared"),
                ],
            },
        ],
        page=page,
        per_page=per_page,
        total=total,
    )
    return f"""
<h1>Handoffs</h1>
{controls}
<div class="panel">
  <table>
    <thead><tr><th>Workspace</th><th>Title</th><th>Status</th><th>Set at</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
{pagination}
"""


def render_routes(*, routes: list[dict[str, Any]], by_tier: dict[str, int]) -> str:
    tier_cards = (
        "".join(
            f'<div class="card"><div class="label">{_esc(tier)}</div><div class="value">{count}</div></div>'
            for tier, count in by_tier.items()
        )
        or '<div class="card"><div class="label">No data</div><div class="value">—</div></div>'
    )
    rows = (
        "".join(
            f"<tr><td>{_esc(row['created_at'])}</td><td>{_esc(row['task_type'])}</td><td><code>{_esc(row['model_tier'])}</code></td></tr>"
            for row in routes
        )
        or '<tr><td colspan=3 class="muted">No route decisions yet.</td></tr>'
    )
    return f"""
<h1>Routes</h1>
<div class="cards">{tier_cards}</div>
<div class="panel">
  <h2>Recent decisions</h2>
  <table>
    <thead><tr><th>Time</th><th>Task type</th><th>Tier</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
"""


def render_events(
    *,
    events: list[dict[str, Any]],
    total: int | None = None,
    page: int = 1,
    per_page: int = 50,
    search_value: str = "",
    kind_filter: str = "",
    kind_options: list[str] | None = None,
) -> str:
    if total is None:
        total = len(events)
    kind_options = kind_options or sorted(
        {row.get("kind", "") for row in events if row.get("kind")}
    )
    rows = (
        "".join(
            f"<tr>"
            f"<td>{_esc(row['created_at'])}</td>"
            f"<td><span class='pill'>{_esc(row['kind'])}</span></td>"
            f"<td>{_esc(row['message'])}</td>"
            f"<td>{_esc(row.get('session_id') or '')}</td>"
            f"</tr>"
            for row in events
        )
        or '<tr><td colspan=4 class="muted">No events match the current filter.</td></tr>'
    )
    controls, pagination = render_listing_chrome(
        base_url="/dashboard/events",
        search_value=search_value,
        search_placeholder="Search message / kind / session…",
        filters=[
            {
                "name": "kind",
                "label": "Kind",
                "value": kind_filter,
                "options": [("", "All kinds")] + [(k, k) for k in kind_options],
            },
        ],
        page=page,
        per_page=per_page,
        total=total,
    )
    return f"""
<h1>Events</h1>
{controls}
<div class="panel">
  <table>
    <thead><tr><th>Time</th><th>Kind</th><th>Message</th><th>Session</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
{pagination}
"""


def render_patterns(
    *,
    patterns: list[dict[str, Any]],
    status: str,
    total: int | None = None,
    page: int = 1,
    per_page: int = 50,
    search_value: str = "",
    category_filter: str = "",
    category_options: list[str] | None = None,
) -> str:
    if total is None:
        total = len(patterns)
    category_options = category_options or []
    rows = (
        "".join(
            f"<tr>"
            f"<td><strong>{_esc(p['name'])}</strong></td>"
            f"<td><span class='pill'>{_esc(p['category'])}</span></td>"
            f"<td>{_esc(p['description'])}</td>"
            f"<td>{_esc(p['status'])}</td>"
            f"<td>{_esc(p['usage_count'])}</td>"
            f"</tr>"
            for p in patterns
        )
        or '<tr><td colspan=5 class="muted">No patterns match the current filter.</td></tr>'
    )
    tabs = "".join(
        f'<a class="{"active" if s == status else ""}" href="/dashboard/patterns?status={s}">{s}</a>'
        for s in ("proposed", "approved", "rejected", "all")
    )
    controls, pagination = render_listing_chrome(
        base_url="/dashboard/patterns",
        search_value=search_value,
        search_placeholder="Search name / description / category…",
        filters=[
            {
                "name": "category",
                "label": "Category",
                "value": category_filter,
                "options": [("", "All")] + [(c, c) for c in category_options],
            },
        ],
        page=page,
        per_page=per_page,
        total=total,
        extra_params={"status": status},
    )
    return f"""
<h1>Patterns</h1>
<div class="tabs">{tabs}</div>
{controls}
<div class="panel">
  <table>
    <thead><tr><th>Name</th><th>Category</th><th>Description</th><th>Status</th><th>Usage</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
{pagination}
"""


def render_tools(*, tools: list[dict[str, Any]]) -> str:
    rows = (
        "".join(
            f"<tr>"
            f"<td><strong>{_esc(t['name'])}</strong></td>"
            f"<td>{_esc(t['display_name'])}</td>"
            f"<td><code>{_esc(t['cli_command'])}</code></td>"
            f"<td>{_PILL_AVAILABLE if t['is_available'] else _PILL_MISSING}</td>"
            f"<td>{_esc(t.get('last_verified_at') or '')}</td>"
            f"</tr>"
            for t in tools
        )
        or '<tr><td colspan=5 class="muted">No tools registered.</td></tr>'
    )
    return f"""
<h1>Tools</h1>
<div class="panel">
  <form method="post" action="/dashboard/tools/verify" style="display:inline-block;margin-bottom:12px;">
    <button type="submit">Verify now</button>
  </form>
  <table>
    <thead><tr><th>Name</th><th>Display</th><th>CLI command</th><th>Status</th><th>Last verified</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
"""


def render_listing_chrome(
    *,
    base_url: str,
    search_value: str = "",
    search_placeholder: str = "Search…",
    filters: list[dict[str, Any]] | None = None,
    page: int = 1,
    per_page: int = 50,
    total: int = 0,
    extra_params: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(controls_html, pagination_html)`` for a dashboard list page.

    The controls are a GET form so reload-friendliness and bookmarks both
    work; pagination links preserve every filter via ``extra_params`` plus
    the active search/filter values. ``filters`` is a list of
    ``{"name": str, "label": str, "value": str, "options": [(value, label)]}``
    dicts; an option whose ``value`` matches the current ``value`` is
    pre-selected. ``total`` is the unfiltered-by-page total used to render
    pagination metadata.
    """
    filters = filters or []
    extra_params = extra_params or {}

    filter_html = "".join(
        f'<label style="margin-left:8px;">{_esc(flt["label"])}'
        f'<select name="{_esc(flt["name"])}" style="margin-left:6px;">'
        + "".join(
            f'<option value="{_esc(opt_value)}"'
            f"{' selected' if str(opt_value) == str(flt.get('value') or '') else ''}>"
            f"{_esc(opt_label)}</option>"
            for opt_value, opt_label in flt.get("options", [])
        )
        + "</select></label>"
        for flt in filters
    )

    # Per-page selector — fixed common buckets so the URL doesn't accept arbitrary ints
    per_page_options = [25, 50, 100, 200]
    per_page_select = (
        '<label style="margin-left:8px;">Per page'
        '<select name="per_page" style="margin-left:6px;">'
        + "".join(
            f'<option value="{n}"{" selected" if n == per_page else ""}>{n}</option>'
            for n in per_page_options
        )
        + "</select></label>"
    )

    controls_html = f"""
<form method="get" action="{_esc(base_url)}" class="panel" style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:12px;">
  <input type="search" name="q" value="{_esc(search_value)}" placeholder="{_esc(search_placeholder)}" style="flex:1 1 240px;min-width:200px;" />
  {filter_html}
  {per_page_select}
  <button type="submit">Apply</button>
  <a class="button secondary" href="{_esc(base_url)}">Reset</a>
</form>
"""

    # Pagination footer — only render when there's more than one page.
    pages = max(1, (max(total, 0) + per_page - 1) // per_page)
    start = ((page - 1) * per_page) + 1 if total else 0
    end = min(page * per_page, total)
    if pages <= 1:
        summary = (
            f'<p class="muted" style="margin-top:12px;">Showing {total} row(s).</p>'
            if total
            else ""
        )
        return controls_html, summary

    def _link(target_page: int, label: str, disabled: bool = False) -> str:
        if disabled:
            return f'<span class="muted" style="margin:0 4px;">{_esc(label)}</span>'
        qs_pairs = {
            "q": search_value,
            "per_page": str(per_page),
            "page": str(target_page),
        }
        for flt in filters:
            qs_pairs[flt["name"]] = str(flt.get("value") or "")
        for k, v in extra_params.items():
            qs_pairs[k] = v
        qs_pairs = {k: v for k, v in qs_pairs.items() if v not in ("", None)}
        qs = "&".join(f"{k}={_esc(str(v))}" for k, v in qs_pairs.items())
        return f'<a href="{_esc(base_url)}?{qs}" style="margin:0 4px;">{_esc(label)}</a>'

    prev_link = _link(page - 1, "← Prev", disabled=page <= 1)
    next_link = _link(page + 1, "Next →", disabled=page >= pages)
    pagination_html = (
        '<div class="panel" style="display:flex;align-items:center;justify-content:space-between;margin-top:12px;">'
        f'<span class="muted">Showing {start}–{end} of {total} row(s) · page {page} of {pages}</span>'
        f"<span>{prev_link}{next_link}</span>"
        "</div>"
    )
    return controls_html, pagination_html


def render_workspaces(
    *,
    workspaces: list[dict[str, Any]],
    total: int | None = None,
    page: int = 1,
    per_page: int = 50,
    search_value: str = "",
    status_filter: str = "",
    hide_test: bool = True,
) -> str:
    """Workspaces tab with server-side search, filter, and pagination.

    Backwards-compatible: when the new keyword args are omitted (older
    callers and tests), ``total`` defaults to ``len(workspaces)`` so a
    full unpaged render still works.
    """
    if total is None:
        total = len(workspaces)
    rows = (
        "".join(
            f"<tr>"
            f"<td><a href='/dashboard/workspaces/{_esc(w['slug'])}'><code>{_esc(w['slug'])}</code></a></td>"
            f"<td><code class='muted' style='font-size:11px;'>{_esc(w['path'])}</code></td>"
            f"<td>{_esc(w['status'])}</td>"
            f"<td class='muted'>{_esc(w.get('last_touched_at') or '')}</td>"
            f"</tr>"
            for w in workspaces
        )
        or '<tr><td colspan=4 class="muted">No workspaces match the current filter.</td></tr>'
    )

    controls, pagination = render_listing_chrome(
        base_url="/dashboard/workspaces",
        search_value=search_value,
        search_placeholder="Search slug or path…",
        filters=[
            {
                "name": "status",
                "label": "Status",
                "value": status_filter,
                "options": [
                    ("", "All statuses"),
                    ("active", "active"),
                    ("archived", "archived"),
                    ("retired", "retired"),
                ],
            },
            {
                "name": "hide_test",
                "label": "Hide tests",
                "value": "1" if hide_test else "0",
                "options": [("1", "Yes (hide pytest)"), ("0", "No (show all)")],
            },
        ],
        page=page,
        per_page=per_page,
        total=total,
    )

    cleanup_hint = (
        '<p class="muted" style="margin-top:8px;font-size:12px;">'
        "Polluted by old test runs? Run "
        "<code>brains workspaces prune --slug-prefix test- --slug-prefix adopt- "
        "--path-contains pytest --apply</code> "
        "to clean up. Existing tests now write to a tmp DB so this won't recur."
        "</p>"
    )

    return f"""
<h1>Workspaces</h1>
{controls}
<div class="panel">
  <table>
    <thead><tr><th>Slug</th><th>Path</th><th>Status</th><th>Last touched</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
{pagination}
{cleanup_hint}
"""


def render_workspace_detail(state: dict[str, Any]) -> str:
    workspace = state.get("workspace") or {}
    sessions = state.get("active_sessions") or []
    tasks = state.get("active_tasks") or []
    decisions = state.get("open_decisions") or []
    handoffs = state.get("active_handoffs") or []
    events = state.get("recent_events") or []
    return f"""
<h1>{_esc(workspace.get("slug") or "(workspace)")}</h1>
<p class="muted"><code>{_esc(workspace.get("path") or "")}</code></p>
<div class="cards">
  <div class="card"><div class="label">Active sessions</div><div class="value">{len(sessions)}</div></div>
  <div class="card"><div class="label">Tasks</div><div class="value">{len(tasks)}</div></div>
  <div class="card"><div class="label">Open decisions</div><div class="value">{len(decisions)}</div></div>
  <div class="card"><div class="label">Handoffs</div><div class="value">{len(handoffs)}</div></div>
</div>
<div class="panel">
  <h2>Recent events</h2>
  <pre>{_esc(json.dumps(events, indent=2))}</pre>
</div>
"""


def render_recurring(*, tasks: list[dict[str, Any]]) -> str:
    """Render the recurring-task overview page.

    Surfaces every recurring task definition with its schedule, last
    fire, enabled/disabled state, and any auto-spawn configuration so an
    operator can sanity-check the cohort at a glance.
    """
    rows = ""
    for t in tasks:
        spawn_tool = t.get("spawn_tool") or ""
        spawn_args = t.get("spawn_args") or ""
        spawn_prompt = t.get("spawn_prompt") or ""
        spawn_cell = ""
        if spawn_tool:
            spawn_cell = (
                f"<div><strong>{_esc(spawn_tool)}</strong></div>"
                f"<div class='muted'><code>{_esc(str(spawn_args))}</code></div>"
                f"<div class='muted'>{_esc(spawn_prompt)}</div>"
            )
        rows += (
            "<tr>"
            f"<td><code>{_esc(t['name'])}</code></td>"
            f"<td><code>{_esc(t.get('cron_expr') or '')}</code></td>"
            f"<td>{_esc(t['title_template'])}</td>"
            f"<td>{_esc(t.get('last_fired_at') or '')}</td>"
            f"<td>{_PILL_ENABLED if t.get('enabled') else _PILL_DISABLED}</td>"
            f"<td>{spawn_cell or _MUTED_EMDASH}</td>"
            "</tr>"
        )
    if not rows:
        rows = '<tr><td colspan=6 class="muted">No recurring tasks configured.</td></tr>'
    return f"""
<h1>Recurring tasks</h1>
<div class="panel">
  <table>
    <thead><tr>
      <th>Name</th><th>Schedule</th><th>Title template</th><th>Last fired</th><th>State</th><th>Auto-spawn</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
"""


def render_presence(*, presence: Sequence[Mapping[str, Any]], me: str | None) -> str:
    """Render the Layer-3 "other operators" panel.

    Shows every operator with at least one active session, excluding
    the caller. The projection is intentionally minimal — slug, display
    name, workspace count, active session count, last activity — so it
    matches the safety profile of the underlying MCP tool. No workspace
    names, no session ids, no summaries.
    """
    rows = ""
    for row in presence:
        slug = row.get("operator_slug") or ""
        name = row.get("display_name") or ""
        ws = row.get("workspace_count") or 0
        sess = row.get("active_session_count") or 0
        last = row.get("last_activity_at") or ""
        rows += (
            "<tr>"
            f"<td><code>{_esc(slug)}</code></td>"
            f"<td>{_esc(name)}</td>"
            f"<td>{ws}</td>"
            f"<td>{sess}</td>"
            f"<td>{_esc(last)}</td>"
            "</tr>"
        )
    if not rows:
        rows = (
            '<tr><td colspan=5 class="muted">'
            "No other operators have an active session right now."
            "</td></tr>"
        )
    me_blurb = (
        f'<p class="muted">You are signed in as <code>{_esc(me)}</code>. '
        "Your own sessions are excluded from this list.</p>"
        if me
        else '<p class="muted">Your own sessions are excluded from this list.</p>'
    )
    return f"""
<h1>Other operators</h1>
{me_blurb}
<div class="panel">
  <table>
    <thead><tr>
      <th>Operator</th><th>Display name</th><th>Workspaces</th>
      <th>Active sessions</th><th>Last activity</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
<p class="muted">
  Workspace names, session ids, and summaries are deliberately hidden
  here. Cross-workspace presence means "I know other work is happening,
  I can go ask about it", not "I can introspect every session running
  on the box."
</p>
"""

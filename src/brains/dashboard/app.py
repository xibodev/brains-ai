"""User-facing dashboard.

Rich, navigable view into every plane of the control system —
sessions, tasks, decisions, handoffs, routes, events, patterns,
tools, workspaces. Also mounts the admin console at ``/admin/*``.

HTML lives at ``/dashboard/*``. A minimal JSON surface for
programmatic access lives at ``/dashboard/api/*``. Org-scoped views are
gated by ``require_browser_auth`` so an operator can drive the
UI from the browser via cookie or pass an ``Authorization``
header from scripts; that gate refuses a Runtime credential and a
principal with no Org role, and every read it admits is filtered to the
Workspaces that principal can see.

The install-level surfaces here - the executor console (which launches a
process on the host), cross-Org operator presence, and the router-decision
views - are gated by ``require_install_admin`` instead. They are not
Org-attributed, so no Org role can confer them.
"""

from __future__ import annotations

from collections import Counter
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from brains.admin import router as admin_router
from brains.api.admin_key import ensure_admin_key
from brains.api.auth import (
    require_browser_auth,
    require_browser_auth_html,
    require_install_admin,
    require_install_admin_html,
)
from brains.api.errors import register_admin_redirect_handler
from brains.authz.deps import install_principal_context
from brains.control.decisions import list_open_decisions, resolve_decision
from brains.control.events import list_events
from brains.control.handoffs import get_handoff, list_handoffs
from brains.control.patterns import list_patterns
from brains.control.recurring import list_recurring_tasks
from brains.control.sessions import list_sessions
from brains.control.state import get_state
from brains.control.tasks import get_task, list_tasks
from brains.control.tool_registry import list_registered_tools
from brains.dashboard.nav import DASHBOARD_NAV_WITH_ICONS
from brains.dashboard.ui import (
    render_dashboard_layout,
    render_presence,
)
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import RouteDecision, Workspace
from brains.web import mount_static, render_response

app = FastAPI(title="Brains Dashboard")


# Self-contained operator console for gated agent sessions. Kept inline (not a
# Jinja template) so it doesn't couple to the template/icon foundation; it talks
# only to the /dashboard/api/exec* JSON surface + the existing decisions API.
_EXEC_CONSOLE_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>Brains — Executor Console</title>
<style>
 body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 header{padding:12px 18px;background:#171a21;border-bottom:1px solid #262b35}
 a{color:#7aa2f7} h1{font-size:16px;margin:0;display:inline-block}
 .wrap{display:grid;grid-template-columns:320px 1fr;gap:0;height:calc(100vh - 49px)}
 .side{border-right:1px solid #262b35;overflow:auto;padding:12px}
 .main{display:flex;flex-direction:column;overflow:hidden}
 form{padding:12px;border-bottom:1px solid #262b35;display:grid;gap:6px}
 input,select,textarea,button{background:#1c2029;color:#e6e6e6;border:1px solid #2d3340;border-radius:6px;padding:6px;font:inherit}
 textarea{min-height:60px;resize:vertical} button{cursor:pointer;background:#2545a0;border-color:#2545a0}
 button.deny{background:#7a2230;border-color:#7a2230}
 #log{flex:1;overflow:auto;white-space:pre-wrap;padding:12px;font-family:ui-monospace,monospace;font-size:12.5px}
 .sess{padding:7px 9px;border:1px solid #262b35;border-radius:6px;margin-bottom:6px;cursor:pointer}
 .sess:hover{background:#1c2029} .st-running{color:#9ece6a} .st-failed{color:#f7768e} .st-done{color:#7aa2f7}
 .ask{background:#2a2410;border:1px solid #6b5a16;border-radius:6px;padding:8px;margin:10px 12px}
 .row{display:flex;gap:8px;align-items:center} .muted{color:#8b93a7;font-size:12px}
</style></head><body>
<header><h1>⚡ Executor Console</h1> &nbsp;<span class=muted>gated agent sessions — outward actions need your approval</span>
 &nbsp; <a href="/dashboard">← dashboard</a></header>
<div class=wrap>
 <div class=side>
  <form id=start>
   <label>Tool<select name=tool><option>copilot</option><option>claude</option><option>codex</option></select></label>
   <label>Model<input name=model placeholder="claude-haiku-4.5 (optional)"></label>
   <label>Workspace<input name=workspace placeholder="/path/to/repo" required></label>
   <label>Orient query <span class=muted>(optional, for weak models)</span><input name=orient></label>
   <label>Prompt<textarea name=prompt required></textarea></label>
   <button type=submit>Start gated session</button>
  </form>
  <div id=list></div>
 </div>
 <div class=main>
  <div id=asks></div>
  <div id=log>select or start a session…</div>
 </div>
</div>
<script>
let cur=null, off=0, timer=null;
async function jget(u){const r=await fetch(u);return r.json()}
async function refreshList(){
 const s=await jget('/dashboard/api/exec');const el=document.getElementById('list');
 el.innerHTML=s.map(m=>`<div class=sess onclick="open_('${m.exec_id}')">
  <div><b>${m.tool}</b> <span class=st-${m.status}>${m.status}</span></div>
  <div class=muted>${m.exec_id} · ${(m.prompt||'').slice(0,46)}</div></div>`).join('')||'<span class=muted>no sessions yet</span>';
}
function open_(id){cur=id;off=0;document.getElementById('log').textContent='';poll()}
async function poll(){
 if(!cur)return;
 const d=await jget('/dashboard/api/exec/'+cur+'?offset='+off);
 if(d.output){document.getElementById('log').textContent+=d.output;off=d.offset;
  const lg=document.getElementById('log');lg.scrollTop=lg.scrollHeight}
 const a=document.getElementById('asks');
 a.innerHTML=(d.pending||[]).map(p=>`<div class=ask><div><b>${p.code}</b> ${p.title.replace('[gate] approve outward action:','')}</div>
  <div class=muted>${(p.body||'').replace(/\\n/g,' ').slice(0,120)}</div>
  <div class=row style=margin-top:6px>
   <button onclick="resolve_('${p.code}','approve','resolved')">Approve</button>
   <button class=deny onclick="resolve_('${p.code}','deny','rejected')">Deny</button></div></div>`).join('');
}
async function resolve_(code,chosen,status){
 const f=new FormData();f.append('chosen',chosen);f.append('status',status);f.append('reasoning','via console');
 await fetch('/dashboard/decisions/'+code+'/resolve',{method:'POST',body:f});poll();
}
document.getElementById('start').onsubmit=async e=>{
 e.preventDefault();const f=new FormData(e.target);
 const r=await fetch('/dashboard/api/exec/start',{method:'POST',body:f});const d=await r.json();
 if(d.exec_id){await refreshList();open_(d.exec_id)}else{alert(JSON.stringify(d))}
};
refreshList();setInterval(refreshList,3000);timer=setInterval(poll,1500);
</script></body></html>"""


@asynccontextmanager
async def _dashboard_lifespan(_: FastAPI):
    init_db()
    ensure_admin_key(print_banner=True, port=9876)
    # Auto-provision / sync the ``admin`` operator (Layer 1 of the
    # multi-operator model). Safe on every restart — idempotent.
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    yield


app.router.lifespan_context = _dashboard_lifespan
register_admin_redirect_handler(app)
# BL-P0-01 — the dashboard is its own ASGI app, so it needs its own principal
# slot. Without it, the cookie-resolved principal from ``require_browser_auth``
# would never reach the scoped control-layer reads and every request would fall
# back to the bootstrap admin, which sees every Org.
install_principal_context(app)
mount_static(app)
app.include_router(admin_router)


@app.middleware("http")
async def _bind_current_operator(request: Request, call_next):
    """Push the cookie-resolved operator slug into ``current_operator``.

    The coordination ``list_*`` functions filter by
    ``visible_workspace_ids_for_current()`` which reads the
    :data:`brains.control.operators.current_operator` ContextVar. SSE
    requests already get it set by ``MCPAuthMiddleware``; for dashboard
    HTTP requests we set it here from the signed browser cookie. Reset
    in a ``finally`` so the var never leaks across requests.
    """
    from brains.control.operators import current_operator

    slug = _current_operator_slug(request)
    token = current_operator.set(slug) if slug is not None else None
    try:
        return await call_next(request)
    finally:
        if token is not None:
            current_operator.reset(token)


def _counts() -> dict[str, Any]:
    init_db()
    # Layer 2 visibility filter — count only workspaces the current
    # operator can see so the overview tile reflects their universe.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    with SessionLocal() as session:
        query = session.query(Workspace)
        if visible is not None:
            query = query.filter(Workspace.id.in_(visible))
        workspace_count = query.count()
    state = get_state(limit=100)
    return {
        "workspaces": workspace_count,
        "active_sessions": len(state["active_sessions"]),
        "open_decisions": len(state["open_decisions"]),
        "active_tasks": len(state["active_tasks"]),
        "active_handoffs": len(state["active_handoffs"]),
        "recent_events": len(state["recent_events"]),
    }


def _nav_badges() -> dict[str, dict[str, Any]]:
    """Compute attention badges keyed by nav slot.

    Each value is ``{count, tone, title}``. The dashboard base template
    only renders the badge when ``count`` is truthy, so zero-count slots
    are naturally hidden. Tones map to the ``.nav-badge`` CSS variants.
    """
    try:
        c = _counts()
    except Exception:
        return {}
    badges: dict[str, dict[str, Any]] = {}
    if c["open_decisions"]:
        badges["decisions"] = {
            "count": c["open_decisions"],
            "tone": "is-info",
            "title": f"{c['open_decisions']} open decision(s) awaiting a human answer",
        }
    if c["active_handoffs"]:
        badges["handoffs"] = {
            "count": c["active_handoffs"],
            "tone": "",
            "title": f"{c['active_handoffs']} active handoff(s)",
        }
    if c["active_sessions"]:
        badges["sessions"] = {
            "count": c["active_sessions"],
            "tone": "is-success",
            "title": f"{c['active_sessions']} active session(s)",
        }
    return badges


def _dashboard_ctx(request: Request, active: str) -> dict[str, Any]:
    """Common render context for every dashboard page."""
    return {
        "request": request,
        "active": active,
        "operator": _current_operator_slug(request),
        "dashboard_nav": DASHBOARD_NAV_WITH_ICONS,
        "nav_badges": _nav_badges(),
    }


def _events_payload(limit: int = 50) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "message": row.message,
            "session_id": row.session_id,
            "created_at": row.created_at.isoformat(),
        }
        for row in list_events(limit=limit)
    ]


def _workspaces_payload(
    *,
    q: str = "",
    status: str = "",
    hide_test: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(rows, total)`` honouring search, filter, and pagination.

    Search is a case-insensitive substring match against slug + path.
    ``hide_test`` filters out the pytest-fixture workspaces that historically
    polluted the live DB (slug ``test-*``/``adopt-*`` or path containing
    ``pytest``). Layer-2 visibility still applies — operators only see
    workspaces they have access to.
    """
    init_db()
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    with SessionLocal() as session:
        query = session.query(Workspace)
        if visible is not None:
            query = query.filter(Workspace.id.in_(visible))
        if status:
            query = query.filter(Workspace.status == status)
        if hide_test:
            # Exclude rows whose slug or path screams "pytest fixture".
            from sqlalchemy import and_, not_, or_

            query = query.filter(
                and_(
                    not_(Workspace.slug.ilike("test-%")),
                    not_(Workspace.slug.ilike("adopt-%")),
                    not_(Workspace.slug.ilike("ws-a-%")),
                    not_(Workspace.slug.ilike("ws-b-%")),
                    not_(Workspace.path.ilike("%pytest%")),
                )
            )
        if q:
            from sqlalchemy import or_

            like = f"%{q}%"
            query = query.filter(or_(Workspace.slug.ilike(like), Workspace.path.ilike(like)))
        total = query.count()
        query = query.order_by(Workspace.slug.asc())
        if limit is not None:
            query = query.offset(max(0, offset)).limit(max(1, limit))
        rows = query.all()
        return (
            [
                {
                    "slug": row.slug,
                    "path": row.path,
                    "status": row.status,
                    "last_touched_at": row.last_touched_at.isoformat()
                    if row.last_touched_at
                    else None,
                    "last_summary": row.last_summary,
                }
                for row in rows
            ],
            total,
        )


def _apply_listing_filters(
    rows: list[dict[str, Any]],
    *,
    q: str = "",
    search_fields: tuple[str, ...] = (),
    filters: dict[str, str] | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Generic in-memory search + filter + pagination for a list of dicts.

    Returns ``(page_rows, total, page, per_page)``. Search is a
    case-insensitive substring match across ``search_fields``. ``filters``
    is a dict of ``{field_name: required_value}`` — empty values are
    ignored. Pagination params are clamped to sane ranges.
    """
    filters = filters or {}
    page = max(1, page)
    per_page = max(10, min(per_page, 500))

    needle = (q or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in rows:
        ok = True
        for field, required in filters.items():
            if required and str(row.get(field, "")).lower() != str(required).lower():
                ok = False
                break
        if ok and needle:
            ok = any(needle in str(row.get(field, "")).lower() for field in search_fields)
        if ok:
            out.append(row)

    total = len(out)
    start = (page - 1) * per_page
    end = start + per_page
    return out[start:end], total, page, per_page


def _current_operator_slug(request: Request) -> str | None:
    """Resolve the operator that owns the current dashboard session.

    Reads the signed cookie minted by ``mint_browser_token`` — its
    ``kid`` segment is the same 16-hex-char SHA-256 fingerprint stored
    in ``operators.key_fingerprint``. Looks the fingerprint up and
    returns the matching slug, or ``'admin'`` as a fallback so the
    badge always renders something sensible. Returns ``None`` only
    when the cookie is missing or malformed (e.g. unauthenticated
    early in startup).
    """
    from brains.api.auth import BROWSER_AUTH_COOKIE
    from brains.control.operators import ADMIN_SLUG
    from brains.storage.models import Operator

    cookie = request.cookies.get(BROWSER_AUTH_COOKIE)
    if not cookie:
        return ADMIN_SLUG
    parts = cookie.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return ADMIN_SLUG
    kid = parts[1]
    try:
        with SessionLocal() as session:
            row = session.query(Operator).filter(Operator.key_fingerprint == kid).one_or_none()
            return row.slug if row is not None else ADMIN_SLUG
    except Exception:
        return ADMIN_SLUG


# ---------- HTML pages ----------


@app.get(
    "/dashboard",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_overview_page(request: Request):
    from datetime import UTC, datetime, timedelta

    counts = _counts()
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)

    # 24h aggregates — done by scanning recent events (cheap; lists are
    # bounded by the existing list_events default limit).
    raw_events = list_events(limit=500)
    filtered_events: list[dict[str, Any]] = []
    decisions_resolved = 0
    handoffs_picked = 0
    sessions_started = 0
    events_24h = 0
    _ICON_BY_KIND = {
        "decision_filed": "sparkles",
        "decision_resolved": "sparkles",
        "handoff_set": "arrow-up-right",
        "handoff_picked_up": "arrow-up-right",
        "handoff_cleared": "arrow-up-right",
        "session_started": "bot",
        "session_ended": "bot",
        "route_decision": "network",
        "pattern_proposed": "layers",
        "tool_verified": "wrench",
        "task_added": "list-checks",
        "task_claimed": "list-checks",
    }
    for row in raw_events:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if created_at < cutoff:
            continue
        events_24h += 1
        if row.kind == "decision_resolved":
            decisions_resolved += 1
        elif row.kind == "handoff_picked_up":
            handoffs_picked += 1
        elif row.kind == "session_started":
            sessions_started += 1
        # Hide pytest pollution from the operator feed; the events page
        # still shows it for debugging.
        msg = (row.message or "").lower()
        if "pytest" in msg or "test_" in msg or "/tmp/" in msg.replace("\\", "/"):
            continue
        filtered_events.append(
            {
                "id": row.id,
                "kind": row.kind,
                "message": row.message,
                "session_id": row.session_id,
                "created_at": row.created_at.isoformat(),
                "_icon": _ICON_BY_KIND.get(row.kind, "activity"),
            }
        )

    attention_tiles = [
        {
            "label": "open decisions",
            "count": counts["open_decisions"],
            "href": "/dashboard/decisions",
            "tone_class": "is-info" if counts["open_decisions"] else "is-zero",
            "icon": "sparkles",
        },
        {
            "label": "active handoffs",
            "count": counts["active_handoffs"],
            "href": "/dashboard/handoffs",
            "tone_class": "is-warning" if counts["active_handoffs"] else "is-zero",
            "icon": "arrow-up-right",
        },
        {
            "label": "active sessions",
            "count": counts["active_sessions"],
            "href": "/dashboard/sessions?state=active",
            "tone_class": "is-success" if counts["active_sessions"] else "is-zero",
            "icon": "bot",
        },
        {
            "label": "active tasks",
            "count": counts["active_tasks"],
            "href": "/dashboard/tasks?status=available",
            "tone_class": "is-info" if counts["active_tasks"] else "is-zero",
            "icon": "list-checks",
        },
    ]

    quick_actions = [
        {"label": "Test a provider", "href": "/admin/test", "icon": "wrench"},
        {
            "label": "Verify tools now",
            "href": "/dashboard/tools/verify",
            "icon": "refresh-cw",
            "method": "post",
        },
        {"label": "Open admin config", "href": "/admin/config", "icon": "external-link"},
        {"label": "Browse workspaces", "href": "/dashboard/workspaces", "icon": "database"},
    ]

    hour = now.astimezone().hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    return render_response(
        "dashboard/today.html",
        request=request,
        page_title="Today",
        active="overview",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        greeting=greeting,
        greeting_eyebrow="TODAY",
        today_label=now.astimezone().strftime("%a, %b %d · %H:%M local"),
        summary_line=(
            f"{counts['active_sessions']} active session(s) · {events_24h} event(s) in the last 24h"
        ),
        counts=counts,
        attention_tiles=attention_tiles,
        quick_actions=quick_actions,
        stats_24h={
            "events": events_24h,
            "decisions_resolved": decisions_resolved,
            "handoffs_picked": handoffs_picked,
            "sessions_started": sessions_started,
        },
        recent_events=filtered_events[:30],
    )


@app.get(
    "/dashboard/sessions",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_sessions_page(
    request: Request,
    workspace: str | None = None,
    q: str = "",
    tool: str = "",
    state: str = "",
    page: int = 1,
    per_page: int = 50,
):
    sessions = list_sessions(workspace_path=workspace, limit=1000)
    # ``state`` filter is virtual (ended_at is None vs not None) — fold into a
    # synthetic field so the generic filter helper can match it.
    for row in sessions:
        row["_state"] = "active" if row.get("ended_at") is None else "ended"
    page_rows, total, page, per_page = _apply_listing_filters(
        sessions,
        q=q,
        search_fields=("id", "workspace", "tool", "summary"),
        filters={"tool": tool, "_state": state},
        page=page,
        per_page=per_page,
    )
    return render_response(
        "dashboard/sessions.html",
        request=request,
        page_title="Sessions",
        active="sessions",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        sessions=page_rows,
        total=total,
        page=page,
        per_page=per_page,
        search_value=q,
        tool_filter=tool,
        state_filter=state,
        tool_options=sorted({r.get("tool", "") for r in sessions if r.get("tool")}),
    )


@app.get(
    "/dashboard/tasks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_tasks_page(
    request: Request,
    workspace: str | None = None,
    q: str = "",
    status: str = "",
    priority: str = "",
):
    tasks = list_tasks(workspace_path=workspace, limit=500)
    page_rows, total, _page, _per_page = _apply_listing_filters(
        tasks,
        q=q,
        search_fields=("code", "title", "workspace"),
        filters={"status": status, "priority": priority},
        page=1,
        per_page=500,
    )
    return render_response(
        "dashboard/tasks.html",
        request=request,
        page_title="Tasks",
        active="tasks",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        tasks=page_rows,
        total=total,
        per_page=500,
        search_value=q,
        status_filter=status,
        priority_filter=priority,
    )


@app.get(
    "/dashboard/tasks/{code}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_task_detail_page(request: Request, code: str):
    task = get_task(code)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {code} not found")
    return render_response(
        "dashboard/task_detail.html",
        request=request,
        page_title=f"Task {code}",
        active="tasks",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        task=task,
    )


@app.get(
    "/dashboard/squads",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_squads_page(request: Request):
    """Board of squads across the operator's visible workspaces — each with its
    leader, members (roles), and the count of work currently routed to it."""
    from brains.control.memberships import visible_workspace_ids_for_current
    from brains.storage.db import SessionLocal
    from brains.storage.models import (
        AgentTask,
        Operator,
        Squad,
        SquadMember,
        Workspace,
    )

    visible = visible_workspace_ids_for_current()
    init_db()
    squads_view: list[dict] = []
    with SessionLocal() as session:
        query = session.query(Squad, Workspace).join(Workspace, Workspace.id == Squad.workspace_id)
        if visible is not None:
            query = query.filter(Squad.workspace_id.in_(visible))
        for squad, workspace in query.filter(Squad.status == "active").order_by(
            Workspace.slug, Squad.slug
        ):
            leader = session.get(Operator, squad.leader_operator_id)
            members = (
                session.query(SquadMember, Operator)
                .join(Operator, Operator.id == SquadMember.operator_id)
                .filter(SquadMember.squad_id == squad.id)
                .all()
            )
            open_work = (
                session.query(AgentTask)
                .filter(
                    AgentTask.workspace_id == squad.workspace_id,
                    AgentTask.tags.like(f"%squad:{squad.slug}%"),
                    AgentTask.status.in_(("available", "in_progress", "blocked")),
                )
                .count()
            )
            squads_view.append(
                {
                    "slug": squad.slug,
                    "name": squad.name,
                    "workspace": workspace.slug,
                    "description": squad.description,
                    "leader": leader.slug if leader else "?",
                    "members": [
                        {
                            "operator": op.slug,
                            "role": m.role,
                            "is_leader": op.id == squad.leader_operator_id,
                        }
                        for m, op in members
                    ],
                    "open_work": open_work,
                }
            )
    return render_response(
        "dashboard/squads.html",
        request=request,
        page_title="Squads",
        active="squads",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        squads=squads_view,
    )


@app.get(
    "/dashboard/decisions",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_decisions_page(
    request: Request,
    workspace: str | None = None,
    q: str = "",
    page: int = 1,
    per_page: int = 50,
):
    decisions = list_open_decisions(workspace)
    page_rows, total, page, per_page = _apply_listing_filters(
        decisions,
        q=q,
        search_fields=("code", "title", "workspace"),
        page=page,
        per_page=per_page,
    )
    return render_response(
        "dashboard/decisions.html",
        request=request,
        page_title="Decisions",
        active="decisions",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        open_decisions=page_rows,
        total=total,
        page=page,
        per_page=per_page,
        search_value=q,
    )


@app.get(
    "/dashboard/decisions/{code}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_decision_detail_page(code: str, request: Request):
    matches = [row for row in list_open_decisions() if row["code"] == code]
    if not matches:
        raise HTTPException(status_code=404, detail=f"decision not found or already closed: {code}")
    decision = matches[0]
    # Pull events touching this decision (best-effort substring match
    # on the event message — the metadata.code field is JSON-encoded
    # and we don't want a custom query path just for the detail view).
    related = [
        {
            "id": e.id,
            "kind": e.kind,
            "message": e.message,
            "created_at": e.created_at.isoformat(),
        }
        for e in list_events(limit=500)
        if code in (e.message or "")
    ]
    return render_response(
        "dashboard/decision_detail.html",
        request=request,
        page_title=code,
        active="decisions",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        decision=decision,
        history=related,
    )


@app.get(
    "/dashboard/sessions/{session_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_session_detail_page(session_id: str, request: Request):
    matches = [row for row in list_sessions(limit=1000) if row["id"] == session_id]
    if not matches:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    sess = matches[0]
    events_for_session = [
        {
            "id": e.id,
            "kind": e.kind,
            "message": e.message,
            "created_at": e.created_at.isoformat(),
        }
        for e in list_events(limit=500)
        if e.session_id == session_id
    ]
    return render_response(
        "dashboard/session_detail.html",
        request=request,
        page_title=session_id,
        active="sessions",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        session=sess,
        events=events_for_session,
    )


@app.post(
    "/dashboard/decisions/{code}/resolve",
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_resolve_decision(
    request: Request,
    code: str,
    chosen: str = Form(...),
    status: str = Form("resolved"),
    reasoning: str = Form(""),
):
    """Resolve one approval as the operator behind the console cookie.

    The resolver is the request's principal, not the install's admin: the
    approval's Org must be writable by it, its Workspace must be one the
    principal can see, and the separation-of-duty rule in
    ``brains.control.decisions`` still applies.
    """
    from brains.authz import policy
    from brains.authz.principal import CAP_ORG_WRITE
    from brains.authz.resolver import resolve_local_principal
    from brains.control.decisions import ApprovalAuthorizationError

    principal = getattr(request.state, "principal", None) or resolve_local_principal()
    workspace_id = policy.approval_workspace_id(code)
    if workspace_id is None:
        raise policy.not_found("approval", code)
    policy.require_workspace_capability(
        principal, CAP_ORG_WRITE, workspace_id, entity="approval", ref=code
    )
    try:
        resolve_decision(
            code, chosen=chosen, status=status, reasoning=reasoning, principal=principal
        )
    except ApprovalAuthorizationError as exc:
        raise policy.forbidden(str(exc)) from exc
    return RedirectResponse(url="/dashboard/decisions", status_code=303)


@app.get(
    "/dashboard/handoffs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_handoffs_page(
    request: Request,
    workspace: str | None = None,
    q: str = "",
    status: str = "",
    page: int = 1,
    per_page: int = 50,
):
    handoffs = list_handoffs(workspace_path=workspace, active_only=False)
    page_rows, total, page, per_page = _apply_listing_filters(
        handoffs,
        q=q,
        search_fields=("title", "workspace", "body"),
        filters={"status": status},
        page=page,
        per_page=per_page,
    )
    return render_response(
        "dashboard/handoffs.html",
        request=request,
        page_title="Handoffs",
        active="handoffs",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        handoffs=page_rows,
        total=total,
        page=page,
        per_page=per_page,
        search_value=q,
        status_filter=status,
    )


@app.get(
    "/dashboard/handoffs/{handoff_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_handoff_detail_page(request: Request, handoff_id: int):
    handoff = get_handoff(handoff_id)
    if handoff is None:
        raise HTTPException(status_code=404, detail=f"handoff {handoff_id} not found")
    return render_response(
        "dashboard/handoff_detail.html",
        request=request,
        page_title=f"Handoff #{handoff_id}",
        active="handoffs",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        handoff=handoff,
    )


@app.get(
    "/dashboard/routes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_install_admin_html)],
)
def dashboard_routes_page(request: Request, limit: int = 100):
    init_db()
    with SessionLocal() as session:
        rows = session.query(RouteDecision).order_by(RouteDecision.id.desc()).limit(limit).all()
    routes = [
        {
            "id": row.id,
            "task_type": row.task_type,
            "model_tier": row.model_tier,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
    by_tier = dict(Counter(r["model_tier"] for r in routes))
    by_task = dict(Counter(r["task_type"] for r in routes))
    by_tier_chart = [
        {"label": k, "value": v} for k, v in sorted(by_tier.items(), key=lambda kv: -kv[1])
    ]
    by_task_chart = [
        {"label": k, "value": v} for k, v in sorted(by_task.items(), key=lambda kv: -kv[1])[:10]
    ]
    return render_response(
        "dashboard/routes.html",
        request=request,
        page_title="Routes",
        active="routes",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        routes=routes,
        by_tier=by_tier,
        by_tier_chart=by_tier_chart,
        by_task_chart=by_task_chart,
    )


@app.get(
    "/dashboard/events",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_events_page(
    request: Request,
    limit: int = 500,
    kind: str = "",
    q: str = "",
    page: int = 1,
    per_page: int = 50,
):
    events = _events_payload(limit=limit)
    page_rows, total, page, per_page = _apply_listing_filters(
        events,
        q=q,
        search_fields=("message", "kind", "session_id"),
        filters={"kind": kind},
        page=page,
        per_page=per_page,
    )
    kinds = sorted({row.get("kind", "") for row in events if row.get("kind")})
    return render_response(
        "dashboard/events.html",
        request=request,
        page_title="Events",
        active="events",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        events=page_rows,
        total=total,
        page=page,
        per_page=per_page,
        search_value=q,
        kind_filter=kind,
        kind_options=kinds,
    )


@app.get(
    "/dashboard/knowledge",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_knowledge_page(request: Request):
    from brains.control.knowledge import search_knowledge
    from brains.control.signals import list_signals

    entries = search_knowledge(limit=100)
    signals = list_signals(limit=20)
    return render_response(
        "dashboard/knowledge.html",
        request=request,
        page_title="Knowledge",
        active="knowledge",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        entries=entries,
        signals=signals,
    )


@app.get(
    "/dashboard/graph",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_graph_page(request: Request, workspace: str | None = None, max_nodes: int = 200):
    from brains.context.graph_viz import graph_payload, render_graph_svg

    workspaces, _total = _workspaces_payload(hide_test=False, limit=500)
    selected_workspace = workspace or (workspaces[0]["path"] if workspaces else "")
    graph_svg = None
    graph_counts: dict[str, int] | None = None
    if selected_workspace:
        payload = graph_payload(selected_workspace, max_nodes=max_nodes)
        if payload is not None:
            graph_svg = render_graph_svg(selected_workspace, max_nodes=max_nodes)
            graph_counts = {"nodes": len(payload["nodes"]), "edges": len(payload["edges"])}

    return render_response(
        "dashboard/graph.html",
        request=request,
        page_title="Graph",
        active="graph",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        workspaces=workspaces,
        selected_workspace=selected_workspace,
        graph_svg=graph_svg,
        graph_counts=graph_counts,
    )


@app.get(
    "/dashboard/patterns",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_patterns_page(
    request: Request,
    status: str = "approved",
    q: str = "",
    category: str = "",
    page: int = 1,
    per_page: int = 50,
):
    try:
        patterns = list_patterns(status=status, limit=500)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    page_rows, total, page, per_page = _apply_listing_filters(
        patterns,
        q=q,
        search_fields=("name", "description", "category"),
        filters={"category": category},
        page=page,
        per_page=per_page,
    )
    categories = sorted({p.get("category", "") for p in patterns if p.get("category")})
    return render_response(
        "dashboard/patterns.html",
        request=request,
        page_title="Patterns",
        active="patterns",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        patterns=page_rows,
        status=status,
        total=total,
        page=page,
        per_page=per_page,
        search_value=q,
        category_filter=category,
        category_options=categories,
    )


@app.get(
    "/dashboard/tools",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_tools_page(request: Request):
    tools = list_registered_tools(verify_now=False)
    return render_response(
        "dashboard/tools.html",
        request=request,
        page_title="Tools",
        active="tools",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        tools=tools,
    )


@app.post(
    "/dashboard/tools/verify",
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_tools_verify():
    """Re-run tool verification then redirect back to the list."""
    list_registered_tools(verify_now=True)
    return RedirectResponse(url="/dashboard/tools", status_code=303)


@app.get(
    "/dashboard/recurring",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_recurring_page(request: Request, workspace: str | None = None):
    from brains.control.recurring import list_recurring_runs
    from brains.control.webhooks import list_webhook_triggers

    tasks = list_recurring_tasks(workspace_path=workspace)
    runs = list_recurring_runs(limit=25)
    webhooks = list_webhook_triggers()
    # Per-definition run counts give each schedule a lightweight activity badge.
    run_counts: dict[str, int] = {}
    for run in runs:
        run_counts[run["definition_name"]] = run_counts.get(run["definition_name"], 0) + 1
    return render_response(
        "dashboard/recurring.html",
        request=request,
        page_title="Autopilots",
        active="recurring",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        tasks=tasks,
        runs=runs,
        webhooks=webhooks,
        run_counts=run_counts,
    )


@app.get(
    "/dashboard/api/recurring",
    response_class=JSONResponse,
    dependencies=[Depends(require_browser_auth)],
)
def api_recurring(workspace: str | None = None):
    return list_recurring_tasks(workspace_path=workspace)


@app.get(
    "/dashboard/workspaces",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_workspaces_page(
    request: Request,
    q: str = "",
    status: str = "",
    hide_test: str = "1",
    page: int = 1,
    per_page: int = 50,
):
    page = max(1, page)
    per_page = max(10, min(per_page, 500))
    hide_test_flag = hide_test not in ("0", "false", "no", "")
    rows, total = _workspaces_payload(
        q=q.strip(),
        status=status.strip(),
        hide_test=hide_test_flag,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    return render_response(
        "dashboard/workspaces.html",
        request=request,
        page_title="Workspaces",
        active="workspaces",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        workspaces=rows,
        total=total,
        page=page,
        per_page=per_page,
        search_value=q,
        status_filter=status,
        hide_test=hide_test_flag,
    )


@app.get(
    "/dashboard/workspaces/{slug}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_browser_auth_html)],
)
def dashboard_workspace_detail_page(slug: str, request: Request):
    # Layer 2 visibility check — see ``brains.control.memberships``.
    from brains.control.memberships import operator_can_see_workspace
    from brains.control.operators import resolve_current_operator

    init_db()
    with SessionLocal() as session:
        workspace = session.query(Workspace).filter(Workspace.slug == slug).one_or_none()
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"unknown workspace: {slug}")
        op = resolve_current_operator()
        if not operator_can_see_workspace(op.get("id"), workspace.id):
            # 404 (not 403) so we don't reveal that the workspace
            # exists to operators who aren't members.
            raise HTTPException(status_code=404, detail=f"unknown workspace: {slug}")
        path = workspace.path
        ws_dict = {
            "id": workspace.id,
            "slug": workspace.slug,
            "path": workspace.path,
            "status": workspace.status,
            "last_touched_at": workspace.last_touched_at.isoformat()
            if workspace.last_touched_at
            else None,
            "last_summary": workspace.last_summary,
        }
    state = get_state(workspace_path=path, limit=100)
    return render_response(
        "dashboard/workspace_detail.html",
        request=request,
        page_title=slug,
        active="workspaces",
        operator=_current_operator_slug(request),
        dashboard_nav=DASHBOARD_NAV_WITH_ICONS,
        nav_badges=_nav_badges(),
        workspace=ws_dict,
        sessions=state.get("active_sessions") or [],
        tasks=state.get("active_tasks") or [],
        decisions=state.get("open_decisions") or [],
        handoffs=state.get("active_handoffs") or [],
        events=state.get("recent_events") or [],
    )


# ---------- JSON API (programmatic access) ----------


@app.get(
    "/dashboard/api/overview",
    response_class=JSONResponse,
    dependencies=[Depends(require_browser_auth)],
)
def api_overview():
    return {"counts": _counts(), "recent_events": _events_payload(limit=50)}


@app.get(
    "/dashboard/api/workspaces",
    response_class=JSONResponse,
    dependencies=[Depends(require_browser_auth)],
)
def api_workspaces(
    q: str = "",
    status: str = "",
    hide_test: str = "0",
    page: int = 1,
    per_page: int = 100,
):
    """JSON workspaces feed. Defaults to ``hide_test=0`` so programmatic
    consumers still see every row unless they explicitly opt in to the
    dashboard's "hide pytest" behaviour. Query params mirror the HTML page.
    """
    page = max(1, page)
    per_page = max(1, min(per_page, 1000))
    hide_test_flag = hide_test in ("1", "true", "yes")
    rows, total = _workspaces_payload(
        q=q.strip(),
        status=status.strip(),
        hide_test=hide_test_flag,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    return {"rows": rows, "total": total, "page": page, "per_page": per_page}


@app.get(
    "/dashboard/api/graph",
    response_class=JSONResponse,
    dependencies=[Depends(require_browser_auth)],
)
def api_graph(workspace: str, max_nodes: int = 200):
    from brains.context.graph_viz import graph_payload

    payload = graph_payload(workspace, max_nodes=max_nodes)
    if payload is None:
        raise HTTPException(status_code=404, detail="unknown workspace")
    return payload


@app.get(
    "/dashboard/api/workspaces/{slug}",
    response_class=JSONResponse,
    dependencies=[Depends(require_browser_auth)],
)
def api_workspace_detail(slug: str):
    # Layer 2 visibility check — see ``brains.control.memberships``.
    from brains.control.memberships import operator_can_see_workspace
    from brains.control.operators import resolve_current_operator

    init_db()
    with SessionLocal() as session:
        workspace = session.query(Workspace).filter(Workspace.slug == slug).one_or_none()
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"unknown workspace: {slug}")
        op = resolve_current_operator()
        if not operator_can_see_workspace(op.get("id"), workspace.id):
            raise HTTPException(status_code=404, detail=f"unknown workspace: {slug}")
        path = workspace.path
    state = get_state(workspace_path=path, limit=100)
    return {
        "workspace": state["workspace"],
        "active_sessions": state["active_sessions"],
        "open_decisions": state["open_decisions"],
        "active_handoffs": state["active_handoffs"],
        "active_tasks": state["active_tasks"],
        "recent_events": state["recent_events"],
    }


@app.get(
    "/dashboard/api/decisions",
    response_class=JSONResponse,
    dependencies=[Depends(require_browser_auth)],
)
def api_open_decisions(workspace: str | None = None):
    return list_open_decisions(workspace)


@app.get(
    "/dashboard/api/handoffs",
    response_class=JSONResponse,
    dependencies=[Depends(require_browser_auth)],
)
def api_handoffs(workspace: str | None = None):
    return list_handoffs(workspace_path=workspace, active_only=False)


@app.get(
    "/dashboard/api/routes",
    response_class=JSONResponse,
    dependencies=[Depends(require_install_admin)],
)
def api_routes(limit: int = 100):
    init_db()
    with SessionLocal() as session:
        rows = session.query(RouteDecision).order_by(RouteDecision.id.desc()).limit(limit).all()
    return [
        {
            "id": row.id,
            "task_type": row.task_type,
            "model_tier": row.model_tier,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@app.get(
    "/dashboard/api/events",
    response_class=JSONResponse,
    dependencies=[Depends(require_browser_auth)],
)
def api_events(limit: int = 200):
    return _events_payload(limit=limit)


# ---------- Executor console (gated agent sessions) ----------
#
# Install-level, not Org-scoped: ``api_exec_start`` launches a process on the
# host against an arbitrary workspace path, and the listing exposes every
# execution on the box. These are restricted to the install administrator for
# the same reason ``/admin/*`` is - an Org role cannot confer authority over
# the machine Brains runs on.


@app.get(
    "/dashboard/api/exec",
    response_class=JSONResponse,
    dependencies=[Depends(require_install_admin)],
)
def api_exec_list(limit: int = 50):
    from brains.exec import store

    return store.list_sessions(limit=limit)


@app.get(
    "/dashboard/api/exec/{exec_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_install_admin)],
)
def api_exec_get(exec_id: str, offset: int = 0):
    from dataclasses import asdict

    from brains.exec import store

    meta = store.load(exec_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown exec session")
    text, new_offset = store.read_output(exec_id, offset=offset)
    pending = [
        d
        for d in list_open_decisions(meta.workspace)
        if (d.get("title") or "").startswith("[gate]")
    ]
    return {"meta": asdict(meta), "output": text, "offset": new_offset, "pending": pending}


@app.post(
    "/dashboard/api/exec/start",
    response_class=JSONResponse,
    dependencies=[Depends(require_install_admin)],
)
def api_exec_start(
    prompt: str = Form(...),
    workspace: str = Form(...),
    tool: str = Form("copilot"),
    model: str = Form(""),
    orient: str = Form(""),
):
    if tool not in {"copilot", "claude", "codex"}:
        raise HTTPException(status_code=400, detail="tool must be copilot|claude|codex")
    from brains.exec.runner import start_streamed_session

    exec_id = start_streamed_session(
        tool=tool,
        prompt=prompt,
        workspace_path=workspace,
        model=model or None,
        orient_query=orient or None,
    )
    return {"exec_id": exec_id}


@app.get(
    "/dashboard/exec",
    response_class=HTMLResponse,
    dependencies=[Depends(require_install_admin_html)],
)
def dashboard_exec_console(request: Request):
    return HTMLResponse(_EXEC_CONSOLE_HTML)


# ---------- Layer 3: cross-workspace operator presence ----------


@app.get(
    "/dashboard/operators",
    response_class=HTMLResponse,
    dependencies=[Depends(require_install_admin_html)],
)
def dashboard_operators_page(request: Request):
    """Read-only "other operators" panel.

    Install-level: presence spans every Org, so it is restricted to the install
    administrator rather than filtered per Org. See
    :mod:`brains.control.presence` and decision record 0002.
    """
    from brains.control.presence import list_other_operators_active

    presence = list_other_operators_active()
    body = render_presence(
        presence=presence,
        me=_current_operator_slug(request),
    )
    return HTMLResponse(
        render_dashboard_layout(
            title="Operators",
            active="operators",
            body=body,
            operator=_current_operator_slug(request),
        )
    )


@app.get(
    "/dashboard/api/operators",
    response_class=JSONResponse,
    dependencies=[Depends(require_install_admin)],
)
def api_operators():
    from brains.control.presence import list_other_operators_active

    return list_other_operators_active()

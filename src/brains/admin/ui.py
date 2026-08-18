"""Legacy CSS + nav constants for the admin console.

The structured page renderers that used to live here (render_overview,
render_config_page, render_test_page, render_secrets_page, render_login_page,
render_admin_layout) have all been ported to Jinja templates under
src/brains/web/templates/admin/. What remains:

  * SHARED_CSS — still consumed by brains.dashboard.ui.render_dashboard_layout()
    for the legacy user dashboard pages that have not been ported yet.
  * ADMIN_NAV — the topbar nav tuple list (loaded by every admin route).
  * _esc()    — small html.escape wrapper used by brains.dashboard.ui as well.

When the remaining brains.dashboard.ui f-string renderers are ported to
templates, this whole file collapses to ADMIN_NAV and goes away.
"""

from __future__ import annotations

import html
from typing import Any

SHARED_CSS = """
:root {
  --bg: #0b1020;
  --panel: #131a30;
  --panel-2: #1a2342;
  --border: #25305a;
  --text: #e7ecf6;
  --muted: #9aa3c0;
  --accent: #6aa7ff;
  --accent-2: #7dd3fc;
  --ok: #34d399;
  --warn: #fbbf24;
  --bad: #f87171;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 24px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
header.topbar .brand { font-weight: 700; letter-spacing: 0.04em; }
header.topbar nav a {
  margin-right: 16px;
  color: var(--muted);
}
header.topbar nav a.active { color: var(--accent-2); font-weight: 600; }
main { padding: 24px; max-width: 1180px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 16px; }
h2 { font-size: 16px; margin: 24px 0 12px; color: var(--accent-2); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }
.card .value { font-size: 22px; font-weight: 700; margin-top: 4px; }
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 20px;
  margin-bottom: 16px;
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
table th, table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
}
table th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em; }
.pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  font-size: 11px;
  color: var(--muted);
}
.pill.ok { color: var(--ok); border-color: rgba(52, 211, 153, 0.4); }
.pill.warn { color: var(--warn); border-color: rgba(251, 191, 36, 0.4); }
.pill.bad { color: var(--bad); border-color: rgba(248, 113, 113, 0.4); }
form input[type=text], form input[type=password], form input[type=number], form textarea, form select {
  width: 100%;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 13px;
  font-family: inherit;
}
form textarea { min-height: 140px; font-family: ui-monospace, "JetBrains Mono", Menlo, monospace; }
form label { display: block; margin: 10px 0 4px; color: var(--muted); font-size: 12px; }
form button, .button {
  background: var(--accent);
  color: #0b1020;
  border: 0;
  border-radius: 6px;
  padding: 8px 14px;
  font-weight: 600;
  cursor: pointer;
  font-size: 13px;
}
form button.secondary, .button.secondary {
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
}
.notice { padding: 10px 12px; border-radius: 6px; margin-bottom: 12px; }
.notice.ok { background: rgba(52, 211, 153, 0.1); color: var(--ok); border: 1px solid rgba(52, 211, 153, 0.3); }
.notice.bad { background: rgba(248, 113, 113, 0.1); color: var(--bad); border: 1px solid rgba(248, 113, 113, 0.3); }
.tabs { display: flex; gap: 4px; margin-bottom: 16px; flex-wrap: wrap; }
.tabs a {
  padding: 6px 12px;
  border-radius: 6px;
  background: var(--panel);
  border: 1px solid var(--border);
  color: var(--muted);
}
.tabs a.active { color: var(--accent-2); border-color: var(--accent); }
code, pre {
  font-family: ui-monospace, "JetBrains Mono", Menlo, monospace;
  font-size: 12px;
}
pre {
  background: #07091a;
  padding: 12px;
  border-radius: 6px;
  overflow-x: auto;
  border: 1px solid var(--border);
}
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 900px) { .split { grid-template-columns: 1fr; } }
.muted { color: var(--muted); }
"""


ADMIN_NAV = [
    ("overview", "Overview", "/admin/overview", "circle-dot"),
    ("config", "Config", "/admin/config", "sliders"),
    ("test", "Test connection", "/admin/test", "plug-zap"),
    ("secrets", "Secrets", "/admin/secrets", "lock"),
]


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))

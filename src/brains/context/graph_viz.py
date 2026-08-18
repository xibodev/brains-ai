from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from html import escape
from pathlib import Path
from typing import Any

from brains.context.code_graph import _graph_rows, _visible_workspace
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import CodeGraphEdge, CodeGraphNode

# Node fill colour by AST kind — a code graph reads best when the node TYPE is
# obvious at a glance (a green function vs a purple class vs a blue module).
KIND_COLORS = {
    "file": "#0891b2",  # cyan
    "module": "#2563eb",  # blue
    "class": "#9333ea",  # purple
    "function": "#16a34a",  # green
}
KIND_FALLBACK = "#64748b"
KIND_ORDER = ("module", "file", "class", "function")

# Edge stroke colour by relation so containment / imports / calls are
# distinguishable without a separate overlay.
EDGE_COLORS = {
    "contains": "#cbd5e1",
    "imports": "#60a5fa",
    "calls": "#f59e0b",
}
EDGE_FALLBACK = "#cbd5e1"

PAN_ZOOM_JS = """(() => {
  document.querySelectorAll('[data-graph-panzoom]').forEach((shell) => {
    const svg = shell.querySelector('svg');
    if (!svg) return;
    let x = 0, y = 0, scale = 1, dragging = false, lastX = 0, lastY = 0;
    const apply = () => {
      svg.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
      svg.style.transformOrigin = '0 0';
    };
    shell.addEventListener('wheel', (event) => {
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.12 : 0.89;
      scale = Math.min(6, Math.max(0.2, scale * factor));
      apply();
    }, { passive: false });
    shell.addEventListener('pointerdown', (event) => {
      dragging = true; lastX = event.clientX; lastY = event.clientY;
      shell.classList.add('is-dragging'); shell.setPointerCapture(event.pointerId);
    });
    shell.addEventListener('pointermove', (event) => {
      if (!dragging) return;
      x += event.clientX - lastX; y += event.clientY - lastY;
      lastX = event.clientX; lastY = event.clientY; apply();
    });
    shell.addEventListener('pointerup', () => {
      dragging = false; shell.classList.remove('is-dragging');
    });
  });
})();"""


def _node_to_payload(row: CodeGraphNode, degree: int) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "path": row.path,
        "lineno": row.lineno,
        "subsystem_id": row.subsystem_id,
        "degree": degree,
    }


def _edge_to_payload(row: CodeGraphEdge) -> dict[str, Any]:
    return {
        "id": row.id,
        "src_id": row.src_id,
        "dst_id": row.dst_id,
        "relation": row.relation,
        "confidence": row.confidence,
    }


def _select_nodes(
    nodes: list[CodeGraphNode], edges: list[CodeGraphEdge], max_nodes: int
) -> tuple[list[CodeGraphNode], list[CodeGraphEdge], Counter[int]]:
    degree: Counter[int] = Counter()
    for edge in edges:
        degree[edge.src_id] += 1
        degree[edge.dst_id] += 1

    limit = max(0, int(max_nodes))
    ranked = sorted(
        nodes,
        key=lambda row: (-degree[row.id], row.path, row.kind, row.name, row.id),
    )
    selected = ranked[:limit] if len(ranked) > limit else ranked
    selected = sorted(
        selected,
        key=lambda row: (
            row.subsystem_id is None,
            row.subsystem_id if row.subsystem_id is not None else 0,
            row.path,
            row.kind,
            row.name,
            row.id,
        ),
    )
    selected_ids = {row.id for row in selected}
    selected_edges = [
        edge for edge in edges if edge.src_id in selected_ids and edge.dst_id in selected_ids
    ]
    return selected, selected_edges, degree


def graph_payload(workspace_path: str, *, max_nodes: int = 200) -> dict[str, Any] | None:
    workspace = _visible_workspace(workspace_path)
    if workspace is None:
        return None

    init_db()
    with _db_module.SessionLocal() as session:
        nodes, edges = _graph_rows(session, workspace.id)
        selected_nodes, selected_edges, degree = _select_nodes(nodes, edges, max_nodes)
        return {
            "workspace": workspace.slug,
            "workspace_path": workspace.path,
            "nodes": [_node_to_payload(row, degree[row.id]) for row in selected_nodes],
            "edges": [_edge_to_payload(row) for row in selected_edges],
        }


def _short_label(name: str, limit: int = 28) -> str:
    if len(name) <= limit:
        return name
    return f"{name[: limit - 1]}…"


def _kind_color(kind: str | None) -> str:
    return KIND_COLORS.get(str(kind or ""), KIND_FALLBACK)


# Canvas geometry. Wider than tall so labels (placed to the node's right) have
# room and the force layout has space to separate clusters.
_WIDTH = 1600
_HEIGHT = 1100


def _layout_nodes(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    width: int = _WIDTH,
    height: int = _HEIGHT,
) -> dict[int, tuple[float, float]]:
    """Force-directed (Fruchterman–Reingold) layout, pure-Python + deterministic.

    Connected nodes attract, all nodes repel, so densely-coupled clusters draw
    together and weakly-coupled ones drift apart — instead of the old single
    ring where every node sat on one circle and every edge crossed the middle.
    Repulsion is grid-bucketed (only nearby nodes repel) so it stays fast even
    at the 200-node cap. The RNG is seeded so the same graph always renders the
    same picture (stable screenshots / caching).
    """
    n = len(nodes)
    if n == 0:
        return {}

    ids = [int(node["id"]) for node in nodes]
    idx = {nid: i for i, nid in enumerate(ids)}
    rng = random.Random(20260620)

    # Seed on a sunflower disc so the initial state is spread, not a ring.
    px = [0.0] * n
    py = [0.0] * n
    r0 = 0.42 * min(width, height)
    for i in range(n):
        ang = 2.399963 * i  # golden-angle spiral
        rad = r0 * math.sqrt((i + 0.5) / n)
        px[i] = width / 2 + rad * math.cos(ang)
        py[i] = height / 2 + rad * math.sin(ang)

    epairs: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for edge in edges:
        a = idx.get(int(edge["src_id"]))
        b = idx.get(int(edge["dst_id"]))
        if a is None or b is None or a == b:
            continue
        key = (a, b) if a < b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        epairs.append((a, b))

    k = 0.85 * math.sqrt((width * height) / max(1, n))  # ideal edge length
    cutoff = 2.2 * k
    cutoff2 = cutoff * cutoff
    cell = cutoff if cutoff > 1 else 1.0
    temp = 0.12 * min(width, height)
    iterations = 220 if n <= 120 else (160 if n <= 200 else 110)

    for _ in range(iterations):
        dx_ = [0.0] * n
        dy_ = [0.0] * n

        # Repulsion — grid-bucketed: only nodes within `cutoff` push each other.
        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i in range(n):
            grid[(int(px[i] // cell), int(py[i] // cell))].append(i)
        for (gx, gy), members in grid.items():
            cand: list[int] = []
            for ax in (gx - 1, gx, gx + 1):
                for ay in (gy - 1, gy, gy + 1):
                    bucket = grid.get((ax, ay))
                    if bucket:
                        cand.extend(bucket)
            for i in members:
                xi, yi = px[i], py[i]
                fx = fy = 0.0
                for j in cand:
                    if j == i:
                        continue
                    ddx = xi - px[j]
                    ddy = yi - py[j]
                    d2 = ddx * ddx + ddy * ddy
                    if d2 > cutoff2:
                        continue
                    if d2 < 1e-9:
                        ddx = rng.random() - 0.5
                        ddy = rng.random() - 0.5
                        d2 = ddx * ddx + ddy * ddy + 1e-6
                    d = math.sqrt(d2)
                    force = (k * k) / d
                    fx += ddx / d * force
                    fy += ddy / d * force
                dx_[i] += fx
                dy_[i] += fy

        # Attraction along edges.
        for a, b in epairs:
            ddx = px[a] - px[b]
            ddy = py[a] - py[b]
            d = math.sqrt(ddx * ddx + ddy * ddy) or 1e-6
            force = (d * d) / k
            fx = ddx / d * force
            fy = ddy / d * force
            dx_[a] -= fx
            dy_[a] -= fy
            dx_[b] += fx
            dy_[b] += fy

        # Weak gravity to the centre keeps disconnected nodes from drifting off.
        cx, cy = width / 2, height / 2
        for i in range(n):
            dx_[i] += (cx - px[i]) * 0.012
            dy_[i] += (cy - py[i]) * 0.012

        # Apply, capped by the cooling temperature.
        for i in range(n):
            dl = math.sqrt(dx_[i] * dx_[i] + dy_[i] * dy_[i])
            if dl > 1e-9:
                step = min(dl, temp)
                px[i] += dx_[i] / dl * step
                py[i] += dy_[i] / dl * step
        temp *= 0.975

    # Normalise into the viewport with padding, preserving aspect ratio.
    minx, maxx = min(px), max(px)
    miny, maxy = min(py), max(py)
    pad = 90
    spanx = max(1e-6, maxx - minx)
    spany = max(1e-6, maxy - miny)
    scale = min((width - 2 * pad) / spanx, (height - 2 * pad) / spany)
    offx = (width - spanx * scale) / 2
    offy = (height - spany * scale) / 2
    return {
        nid: (offx + (px[i] - minx) * scale, offy + (py[i] - miny) * scale)
        for i, nid in enumerate(ids)
    }


def _labelled_ids(nodes: list[dict[str, Any]], *, cap: int = 44) -> set[int]:
    """Which nodes get a text label. Small graphs label everything; large ones
    label only the highest-degree hubs so the canvas stays readable."""
    if len(nodes) <= 60:
        return {int(node["id"]) for node in nodes}
    ranked = sorted(nodes, key=lambda node: -int(node.get("degree") or 0))
    return {int(node["id"]) for node in ranked[:cap]}


def _render_payload_svg(payload: dict[str, Any]) -> str:
    nodes = payload["nodes"]
    edges = payload["edges"]
    positions = _layout_nodes(nodes, edges)
    node_by_id = {int(node["id"]): node for node in nodes}
    labelled = _labelled_ids(nodes)

    width = _WIDTH
    height = _HEIGHT
    parts: list[str] = [
        (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Code graph for {escape(str(payload["workspace"]))}" '
            f'data-nodes="{len(nodes)}" data-edges="{len(edges)}">'
        ),
        f'<rect width="{width}" height="{height}" fill="#f8fafc"/>',
        (
            f'<text x="28" y="40" fill="#0f172a" font-family="Arial, sans-serif" '
            'font-size="18" font-weight="700">'
            f"Code graph · {escape(str(payload['workspace']))}</text>"
        ),
    ]

    # Edges first, behind the nodes, coloured + thickened by relation/confidence.
    for edge in edges:
        src = positions.get(int(edge["src_id"]))
        dst = positions.get(int(edge["dst_id"]))
        if src is None or dst is None:
            continue
        relation = str(edge["relation"])
        stroke = EDGE_COLORS.get(relation, EDGE_FALLBACK)
        try:
            confidence = float(edge.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        opacity = 0.30 + 0.35 * max(0.0, min(1.0, confidence))
        parts.append(
            f'<line x1="{src[0]:.1f}" y1="{src[1]:.1f}" '
            f'x2="{dst[0]:.1f}" y2="{dst[1]:.1f}" '
            f'stroke="{stroke}" stroke-width="1.1" stroke-opacity="{opacity:.2f}">'
            f"<title>{escape(relation)}</title></line>"
        )

    # Nodes, largest (highest-degree hubs) drawn last so they sit on top.
    ordered = sorted(node_by_id.items(), key=lambda item: int(item[1].get("degree") or 0))
    for node_id, node in ordered:
        x, y = positions[node_id]
        kind = node["kind"]
        color = _kind_color(kind)
        path = str(node["path"])
        lineno = node["lineno"] if node["lineno"] is not None else "-"
        title = escape(f"{node['name']} · {kind} · {path}:{lineno}")
        degree = int(node.get("degree") or 0)
        radius = 5.0 + min(11.0, degree * 0.7)
        parts.append(
            f'<g data-node-id="{node_id}" data-kind="{escape(str(kind))}">'
            f"<title>{title}</title>"
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="{color}" '
            'stroke="#ffffff" stroke-width="1.5"/>'
        )
        if node_id in labelled:
            label = escape(_short_label(str(node["name"])))
            parts.append(
                f'<text x="{x + radius + 4:.1f}" y="{y + 4:.1f}" '
                'font-family="Arial, sans-serif" font-size="11" fill="#1e293b" '
                'paint-order="stroke" stroke="#f8fafc" stroke-width="3" '
                'stroke-linejoin="round">'
                f"{label}</text>"
            )
        parts.append("</g>")

    # Legend (node kinds present in this graph), top-right.
    present = [k for k in KIND_ORDER if any(str(nd["kind"]) == k for nd in nodes)]
    if present:
        lx = width - 220
        ly = 64
        parts.append(
            f'<g font-family="Arial, sans-serif" font-size="12" fill="#334155">'
            f'<text x="{lx}" y="{ly - 14}" font-weight="700" fill="#0f172a">Node kind</text>'
        )
        for row, kind in enumerate(present):
            cy = ly + row * 22
            parts.append(
                f'<circle cx="{lx + 8}" cy="{cy - 4}" r="6" fill="{_kind_color(kind)}" '
                'stroke="#ffffff" stroke-width="1.5"/>'
                f'<text x="{lx + 24}" y="{cy}">{escape(kind)}</text>'
            )
        parts.append("</g>")

    if not nodes:
        parts.append(
            f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" fill="#64748b" '
            'font-family="Arial, sans-serif" font-size="18">'
            "No code graph nodes. Run graph_build first.</text>"
        )

    parts.append("</svg>")
    return "".join(parts)


def render_graph_svg(workspace_path: str, *, max_nodes: int = 200) -> str | None:
    payload = graph_payload(workspace_path, max_nodes=max_nodes)
    if payload is None:
        return None
    return _render_payload_svg(payload)


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug or "code-graph"


def _standalone_html(svg: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Brains code graph</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; }}
    header {{ padding: .75rem 1rem; border-bottom: 1px solid #334155; }}
    [data-graph-panzoom] {{ height: calc(100vh - 54px); overflow: hidden; background: #f8fafc; touch-action: none; }}
    [data-graph-panzoom] svg {{ display: block; width: 100%; height: 100%; cursor: grab; }}
    [data-graph-panzoom].is-dragging svg {{ cursor: grabbing; }}
  </style>
</head>
<body>
  <header>Scroll to zoom · drag to pan</header>
  <main data-graph-panzoom>
    {svg}
  </main>
  <script>{PAN_ZOOM_JS}</script>
</body>
</html>
"""


def graph_export(workspace_path: str, out_path: str) -> dict[str, Any]:
    payload = graph_payload(workspace_path)
    if payload is None:
        raise PermissionError("workspace is not visible to the current operator")

    svg = _render_payload_svg(payload)
    out_dir = Path(out_path)
    if out_dir.suffix.lower() in {".svg", ".html"}:
        out_dir = out_dir.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    base = _safe_slug(str(payload["workspace"]))
    svg_path = out_dir / f"{base}-code-graph.svg"
    html_path = out_dir / f"{base}-code-graph.html"
    svg_path.write_text(svg, encoding="utf-8")
    html_path.write_text(_standalone_html(svg), encoding="utf-8")

    return {
        "svg_path": str(svg_path),
        "html_path": str(html_path),
        "nodes": len(payload["nodes"]),
        "edges": len(payload["edges"]),
    }

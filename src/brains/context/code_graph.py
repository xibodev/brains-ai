from __future__ import annotations

import ast
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from brains.config import settings
from brains.control.common import normalize_path
from brains.control.sessions import register_workspace
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import CodeGraphEdge, CodeGraphNode

IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", "build", "dist"}
COMMUNITY_RELATIONS = {"calls", "contains", "imports"}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "where",
    "with",
}


@dataclass(frozen=True, slots=True)
class NodeKey:
    path: str
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    src: NodeKey
    dst: NodeKey
    relation: str
    confidence: str


@dataclass(slots=True)
class ParsedFile:
    rel_path: str
    module_name: str
    tree: ast.AST


class _AstExtractor(ast.NodeVisitor):
    """Extract a zero-dependency Python code graph from stdlib AST nodes.

    Multi-language parsing via tree-sitter is a future extension; this MVP keeps
    the always-on path near-stdlib and Python-only.
    """

    def __init__(
        self,
        parsed: ParsedFile,
        nodes: dict[NodeKey, int | None],
        edges: set[EdgeSpec],
        call_refs: list[tuple[NodeKey, str]],
        module_path_by_name: dict[str, str],
    ) -> None:
        self.parsed = parsed
        self.nodes = nodes
        self.edges = edges
        self.call_refs = call_refs
        self.module_path_by_name = module_path_by_name
        self.file_key = NodeKey(parsed.rel_path, "file", parsed.rel_path)
        self.module_key = NodeKey(parsed.rel_path, "module", parsed.module_name)
        self.scope: list[tuple[NodeKey, str, str]] = []

    def _add_node(self, key: NodeKey, lineno: int | None = None) -> None:
        existing = self.nodes.get(key)
        if key not in self.nodes or (existing is None and lineno is not None):
            self.nodes[key] = lineno

    def _parent_key(self) -> NodeKey:
        return self.scope[-1][0] if self.scope else self.file_key

    def _qualified_name(self, name: str) -> str:
        if not self.scope:
            return name
        return ".".join([item[1] for item in self.scope] + [name])

    def _current_function(self) -> NodeKey | None:
        for key, _name, kind in reversed(self.scope):
            if kind == "function":
                return key
        return None

    def _best_module_name(self, raw_name: str) -> str:
        parts = [part for part in raw_name.split(".") if part]
        for end in range(len(parts), 0, -1):
            candidate = ".".join(parts[:end])
            if candidate in self.module_path_by_name:
                return candidate
        return raw_name

    def _add_import(self, raw_name: str) -> None:
        target_name = self._best_module_name(raw_name)
        if not target_name:
            return
        target_path = self.module_path_by_name.get(target_name, target_name)
        target_key = NodeKey(target_path, "module", target_name)
        self._add_node(target_key)
        self.edges.add(EdgeSpec(self.module_key, target_key, "imports", "extracted"))

    def _import_from_target(self, node: ast.ImportFrom) -> str:
        package_parts = self.parsed.module_name.split(".")[:-1]
        if node.level:
            keep = max(0, len(package_parts) - (node.level - 1))
            prefix_parts = package_parts[:keep]
        else:
            prefix_parts = []
        module_parts = node.module.split(".") if node.module else []
        module_name = ".".join(prefix_parts + module_parts)
        if module_name:
            return module_name
        first_alias = node.names[0].name if node.names else ""
        return ".".join(prefix_parts + ([first_alias] if first_alias != "*" else []))

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self._add_import(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        self._add_import(self._import_from_target(node))
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        key = NodeKey(self.parsed.rel_path, "class", self._qualified_name(node.name))
        self._add_node(key, getattr(node, "lineno", None))
        self.edges.add(EdgeSpec(self._parent_key(), key, "contains", "extracted"))
        self.scope.append((key, node.name, "class"))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        key = NodeKey(self.parsed.rel_path, "function", self._qualified_name(node.name))
        self._add_node(key, getattr(node, "lineno", None))
        self.edges.add(EdgeSpec(self._parent_key(), key, "contains", "extracted"))
        self.scope.append((key, node.name, "function"))
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        source = self._current_function()
        call_name = _call_name(node.func)
        if source is not None and call_name:
            self.call_refs.append((source, call_name))
        self.generic_visit(node)


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _module_name_for_path(root: Path, file_path: Path) -> tuple[str, str]:
    rel = file_path.relative_to(root)
    rel_path = str(rel).replace("\\", "/")
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    module_name = ".".join(parts) if parts else "__init__"
    return rel_path, module_name


def _walk_python_files(root: Path, max_files: int) -> list[Path]:
    files: list[Path] = []
    if max_files <= 0:
        return files
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORE_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            file_path = Path(dirpath) / filename
            if not file_path.is_file():
                continue
            files.append(file_path)
            if len(files) >= max_files:
                return files
    return files


def _parse_files(root: Path, max_files: int) -> list[ParsedFile]:
    parsed: list[ParsedFile] = []
    for file_path in _walk_python_files(root, max_files):
        try:
            source = file_path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source, filename=str(file_path))
        except (OSError, SyntaxError, ValueError):
            continue
        rel_path, module_name = _module_name_for_path(root, file_path)
        parsed.append(ParsedFile(rel_path=rel_path, module_name=module_name, tree=tree))
    return parsed


def _can_see_workspace(workspace_id: int) -> bool:
    try:
        from brains.control.memberships import operator_can_see_workspace
        from brains.control.operators import resolve_current_operator

        operator = resolve_current_operator()
        return operator_can_see_workspace(operator.get("id"), workspace_id)
    except Exception:
        return True


def _visible_workspace(workspace_path: str):
    workspace = register_workspace(workspace_path)
    if not _can_see_workspace(workspace.id):
        return None
    return workspace


def _simple_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _build_specs(parsed_files: list[ParsedFile]) -> tuple[dict[NodeKey, int | None], set[EdgeSpec]]:
    nodes: dict[NodeKey, int | None] = {}
    edges: set[EdgeSpec] = set()
    call_refs: list[tuple[NodeKey, str]] = []
    module_path_by_name = {parsed.module_name: parsed.rel_path for parsed in parsed_files}

    for parsed in parsed_files:
        file_key = NodeKey(parsed.rel_path, "file", parsed.rel_path)
        module_key = NodeKey(parsed.rel_path, "module", parsed.module_name)
        nodes[file_key] = None
        nodes[module_key] = None
        edges.add(EdgeSpec(file_key, module_key, "contains", "extracted"))
        extractor = _AstExtractor(parsed, nodes, edges, call_refs, module_path_by_name)
        extractor.visit(parsed.tree)

    functions_by_simple_name: dict[str, list[NodeKey]] = defaultdict(list)
    for key in nodes:
        if key.kind == "function":
            functions_by_simple_name[_simple_name(key.name)].append(key)

    for source, call_name in call_refs:
        for target in functions_by_simple_name.get(call_name, []):
            edges.add(EdgeSpec(source, target, "calls", "inferred"))

    return nodes, edges


def _edge_sort_key(edge: EdgeSpec) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        edge.src.path,
        edge.src.kind,
        edge.src.name,
        edge.relation,
        edge.dst.path,
        edge.dst.kind,
        edge.dst.name,
        edge.confidence,
    )


def _assign_subsystems(
    nodes_by_key: dict[NodeKey, CodeGraphNode], edges: list[CodeGraphEdge]
) -> None:
    adjacency: dict[int, set[int]] = {row.id: set() for row in nodes_by_key.values()}
    for edge in edges:
        if edge.relation not in COMMUNITY_RELATIONS:
            continue
        adjacency.setdefault(edge.src_id, set()).add(edge.dst_id)
        adjacency.setdefault(edge.dst_id, set()).add(edge.src_id)

    component_by_id: dict[int, int] = {}
    subsystem_id = 0
    for node_id in sorted(adjacency):
        if node_id in component_by_id:
            continue
        subsystem_id += 1
        queue: deque[int] = deque([node_id])
        component_by_id[node_id] = subsystem_id
        while queue:
            current = queue.popleft()
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor in component_by_id:
                    continue
                component_by_id[neighbor] = subsystem_id
                queue.append(neighbor)

    for row in nodes_by_key.values():
        row.subsystem_id = component_by_id.get(row.id)


def build_code_graph(workspace_path: str, *, max_files: int = 2000) -> dict:
    workspace = register_workspace(workspace_path)
    if not _can_see_workspace(workspace.id):
        return {"workspace": workspace.slug, "nodes": 0, "edges": 0, "files": 0}

    root = Path(normalize_path(workspace_path)).expanduser().resolve()
    parsed_files = _parse_files(root, max_files=max_files)
    node_specs, edge_specs = _build_specs(parsed_files)

    init_db()
    with _db_module.SessionLocal() as session:
        session.query(CodeGraphEdge).filter(CodeGraphEdge.workspace_id == workspace.id).delete(
            synchronize_session=False
        )
        session.query(CodeGraphNode).filter(CodeGraphNode.workspace_id == workspace.id).delete(
            synchronize_session=False
        )
        session.flush()

        nodes_by_key: dict[NodeKey, CodeGraphNode] = {}
        for key, lineno in sorted(
            node_specs.items(), key=lambda item: (item[0].path, item[0].kind, item[0].name)
        ):
            row = CodeGraphNode(
                workspace_id=workspace.id,
                kind=key.kind,
                name=key.name,
                path=key.path,
                lineno=lineno,
            )
            session.add(row)
            nodes_by_key[key] = row
        session.flush()

        edge_rows: list[CodeGraphEdge] = []
        for spec in sorted(edge_specs, key=_edge_sort_key):
            src = nodes_by_key.get(spec.src)
            dst = nodes_by_key.get(spec.dst)
            if src is None or dst is None:
                continue
            edge_row = CodeGraphEdge(
                workspace_id=workspace.id,
                src_id=src.id,
                dst_id=dst.id,
                relation=spec.relation,
                confidence=spec.confidence,
            )
            session.add(edge_row)
            edge_rows.append(edge_row)
        session.flush()

        _assign_subsystems(nodes_by_key, edge_rows)
        session.commit()

    return {
        "workspace": workspace.slug,
        "nodes": len(node_specs),
        "edges": len(edge_rows),
        "files": len(parsed_files),
    }


def _ensure_graph_built(workspace_path: str) -> None:
    """Lazily build the code graph the first time it's queried.

    Graph construction is pure-LOCAL AST parsing (no network, no LLM tokens), so
    auto-building on a miss costs only local compute and removes the dead-by-default
    footgun where ``graph_*`` returns empty until someone runs ``graph-build`` by
    hand. Rebuilds only when the (visible) workspace has zero nodes; never raises.
    """
    if not settings.graph_auto_build:
        return
    try:
        workspace = register_workspace(workspace_path)
        if not _can_see_workspace(workspace.id):
            return
        init_db()
        with _db_module.SessionLocal() as session:
            has_nodes = (
                session.query(CodeGraphNode.id)
                .filter(CodeGraphNode.workspace_id == workspace.id)
                .first()
                is not None
            )
        if not has_nodes:
            build_code_graph(workspace_path)
    except Exception:
        return


def _node_to_dict(row: CodeGraphNode) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "path": row.path,
        "lineno": row.lineno,
        "subsystem_id": row.subsystem_id,
    }


def _keywords(text: str) -> list[str]:
    words = [word.lower() for word in re.findall(r"[A-Za-z_][A-Za-z0-9_./-]*", text or "")]
    keywords = [word for word in words if len(word) > 1 and word not in STOPWORDS]
    return keywords or ([text.lower()] if text else [])


def _score_node(row: CodeGraphNode, query: str) -> float:
    name = row.name.lower()
    path = row.path.lower()
    score = 0.0
    for keyword in _keywords(query):
        if keyword in name:
            score += 1.0
        if keyword in path:
            score += 0.5
    return score


def _matching_nodes(rows: list[CodeGraphNode], query: str, *, limit: int | None = None):
    scored = [(score, row) for row in rows if (score := _score_node(row, query)) > 0]
    scored.sort(key=lambda item: (-item[0], item[1].path, item[1].kind, item[1].name, item[1].id))
    matches = [row for _score, row in scored]
    return matches[:limit] if limit is not None else matches


def _graph_rows(session, workspace_id: int) -> tuple[list[CodeGraphNode], list[CodeGraphEdge]]:
    nodes = (
        session.query(CodeGraphNode)
        .filter(CodeGraphNode.workspace_id == workspace_id)
        .order_by(CodeGraphNode.id.asc())
        .all()
    )
    edges = (
        session.query(CodeGraphEdge)
        .filter(CodeGraphEdge.workspace_id == workspace_id)
        .order_by(CodeGraphEdge.id.asc())
        .all()
    )
    return nodes, edges


def _adjacency(
    edges: list[CodeGraphEdge],
    *,
    relation: str | None = None,
) -> dict[int, list[tuple[int, CodeGraphEdge, str]]]:
    adjacency: dict[int, list[tuple[int, CodeGraphEdge, str]]] = defaultdict(list)
    for edge in edges:
        if relation is not None and edge.relation != relation:
            continue
        adjacency[edge.src_id].append((edge.dst_id, edge, "out"))
        adjacency[edge.dst_id].append((edge.src_id, edge, "in"))
    return adjacency


def _incident_edges(
    session,
    workspace_id: int,
    node_ids: list[int],
    *,
    relation: str | None = None,
) -> list[CodeGraphEdge]:
    """Edges touching any of ``node_ids`` (as src OR dst), fetched from the DB.

    Pushes the neighbour filter into SQL (src_id / dst_id are indexed) so a
    ``graph_neighbors`` call on a large graph loads only the handful of incident
    edges instead of every edge in the workspace — the difference between a
    sub-second answer and loading ~1M rows into Python on a big repo.
    """
    if not node_ids:
        return []
    from sqlalchemy import or_

    query = session.query(CodeGraphEdge).filter(
        CodeGraphEdge.workspace_id == workspace_id,
        or_(CodeGraphEdge.src_id.in_(node_ids), CodeGraphEdge.dst_id.in_(node_ids)),
    )
    if relation is not None:
        query = query.filter(CodeGraphEdge.relation == relation)
    return query.order_by(CodeGraphEdge.id.asc()).all()


def _workspace_nodes(session, workspace_id: int) -> list[CodeGraphNode]:
    return (
        session.query(CodeGraphNode)
        .filter(CodeGraphNode.workspace_id == workspace_id)
        .order_by(CodeGraphNode.id.asc())
        .all()
    )


def graph_neighbors(
    workspace_path: str,
    node_query: str,
    *,
    relation: str | None = None,
    limit: int = 50,
) -> list[dict]:
    _ensure_graph_built(workspace_path)
    workspace = _visible_workspace(workspace_path)
    if workspace is None:
        return []
    init_db()
    with _db_module.SessionLocal() as session:
        # Match seeds against nodes only; then fetch just the seed-incident edges
        # from the DB (indexed src_id/dst_id) instead of loading the whole graph.
        nodes = _workspace_nodes(session, workspace.id)
        seeds = _matching_nodes(nodes, node_query)
        seed_ids = [seed.id for seed in seeds]
        edges = _incident_edges(session, workspace.id, seed_ids, relation=relation)
        adjacency = _adjacency(edges, relation=relation)
        node_by_id = {row.id: row for row in nodes}
        output: list[dict] = []
        seen: set[tuple[int, int, int, str]] = set()
        for seed in seeds:
            for neighbor_id, edge, direction in adjacency.get(seed.id, []):
                key = (seed.id, neighbor_id, edge.id, direction)
                if key in seen:
                    continue
                seen.add(key)
                neighbor = node_by_id.get(neighbor_id)
                if neighbor is None:
                    continue
                item = _node_to_dict(neighbor)
                item.update(
                    {
                        "relation": edge.relation,
                        "confidence": edge.confidence,
                        "direction": direction,
                        "seed": _node_to_dict(seed),
                    }
                )
                output.append(item)
                if len(output) >= limit:
                    return output
        return output


def graph_path(
    workspace_path: str,
    src_query: str,
    dst_query: str,
    *,
    max_depth: int = 6,
) -> list[dict] | None:
    _ensure_graph_built(workspace_path)
    workspace = _visible_workspace(workspace_path)
    if workspace is None:
        return None
    init_db()
    with _db_module.SessionLocal() as session:
        src_matches = _matching_nodes(_workspace_nodes(session, workspace.id), src_query, limit=1)
        dst_matches = _matching_nodes(_workspace_nodes(session, workspace.id), dst_query, limit=1)
        if not src_matches or not dst_matches:
            return None
        src_id = src_matches[0].id
        dst_id = dst_matches[0].id
        if src_id == dst_id:
            return [_node_to_dict(src_matches[0])]

        # Level-by-level BFS, fetching only the frontier's incident edges from the
        # DB each level (max_depth queries) instead of loading every edge. Same
        # shortest-path result.
        max_depth = max(0, max_depth)
        parent: dict[int, int | None] = {src_id: None}
        frontier: set[int] = {src_id}
        found = False
        for _level in range(max_depth):
            if not frontier:
                break
            edges = _incident_edges(session, workspace.id, list(frontier))
            adjacency = _adjacency(edges)
            next_frontier: set[int] = set()
            for current in frontier:
                for neighbor_id, _edge, _direction in adjacency.get(current, []):
                    if neighbor_id in parent:
                        continue
                    parent[neighbor_id] = current
                    if neighbor_id == dst_id:
                        found = True
                        break
                    next_frontier.add(neighbor_id)
                if found:
                    break
            if found:
                break
            frontier = next_frontier
        if not found:
            return None
        path_ids: list[int] = []
        cursor: int | None = dst_id
        while cursor is not None:
            path_ids.append(cursor)
            cursor = parent[cursor]
        path_ids.reverse()
        path_nodes = {
            row.id: row
            for row in session.query(CodeGraphNode).filter(CodeGraphNode.id.in_(path_ids)).all()
        }
        return [_node_to_dict(path_nodes[node_id]) for node_id in path_ids]


def graph_query(
    workspace_path: str,
    question: str,
    *,
    depth: int = 2,
    token_budget: int = 2000,
) -> str:
    max_chars = max(0, token_budget) * 4
    if max_chars == 0:
        return ""
    _ensure_graph_built(workspace_path)
    workspace = _visible_workspace(workspace_path)
    if workspace is None:
        return ""
    init_db()
    with _db_module.SessionLocal() as session:
        nodes = _workspace_nodes(session, workspace.id)
        node_by_id = {row.id: row for row in nodes}
        seeds = _matching_nodes(nodes, question, limit=10)
        if not seeds:
            return "No matching code graph nodes."[:max_chars]

        # Lazy level-by-level expansion: only the seed neighbourhood's incident
        # edges are fetched (depth queries), not the whole graph.
        selected_nodes = {seed.id for seed in seeds}
        selected_edges_by_id: dict[int, CodeGraphEdge] = {}
        frontier: set[int] = {seed.id for seed in seeds}
        depth = max(0, depth)
        for _level in range(depth):
            if not frontier or len(selected_nodes) >= 500:
                break
            edges = _incident_edges(session, workspace.id, list(frontier))
            adjacency = _adjacency(edges)
            next_frontier: set[int] = set()
            for current in frontier:
                for neighbor_id, edge, _direction in adjacency.get(current, []):
                    selected_edges_by_id[edge.id] = edge
                    if neighbor_id in selected_nodes:
                        continue
                    selected_nodes.add(neighbor_id)
                    next_frontier.add(neighbor_id)
                    if len(selected_nodes) >= 500:
                        break
            frontier = next_frontier

        # Hydrate any neighbour nodes discovered that weren't in the initial load.
        missing = [nid for nid in selected_nodes if nid not in node_by_id]
        if missing:
            for row in session.query(CodeGraphNode).filter(CodeGraphNode.id.in_(missing)).all():
                node_by_id[row.id] = row

        lines = [f"Code graph for {workspace.slug}: {question}", "Nodes:"]
        for node_id in sorted(selected_nodes, key=lambda value: (node_by_id[value].path, value)):
            row = node_by_id[node_id]
            lineno = row.lineno if row.lineno is not None else "-"
            lines.append(
                f"- N{row.id} {row.kind} {row.name} "
                f"({row.path}:{lineno}, subsystem={row.subsystem_id})"
            )
        lines.append("Edges:")
        selected_edges = [
            edge
            for edge in selected_edges_by_id.values()
            if edge.src_id in selected_nodes and edge.dst_id in selected_nodes
        ]
        selected_edges.sort(key=lambda edge: edge.id)
        for edge in selected_edges:
            lines.append(f"- N{edge.src_id} -[{edge.relation}/{edge.confidence}]-> N{edge.dst_id}")
        return "\n".join(lines)[:max_chars]


def list_subsystems(workspace_path: str) -> list[dict]:
    _ensure_graph_built(workspace_path)
    workspace = _visible_workspace(workspace_path)
    if workspace is None:
        return []
    init_db()
    with _db_module.SessionLocal() as session:
        rows = (
            session.query(CodeGraphNode)
            .filter(CodeGraphNode.workspace_id == workspace.id)
            .order_by(
                CodeGraphNode.subsystem_id.asc(), CodeGraphNode.path.asc(), CodeGraphNode.name.asc()
            )
            .all()
        )
        grouped: dict[int, list[CodeGraphNode]] = defaultdict(list)
        for row in rows:
            if row.subsystem_id is None:
                continue
            grouped[row.subsystem_id].append(row)
        return [
            {
                "subsystem_id": subsystem_id,
                "size": len(nodes),
                "sample_node_names": [node.name for node in nodes[:5]],
            }
            for subsystem_id, nodes in sorted(grouped.items())
        ]

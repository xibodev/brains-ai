"""Code graph v2: stable URNs, blocklist, 4-tier resolution, dead-code passes.

Blueprint #39. Pure-Python over stdlib ast; upsert-friendly (no destructive
deletes here — persistence layer uses ON CONFLICT DO UPDATE).
"""

from __future__ import annotations

import ast

CALL_BLOCKLIST = frozenset(
    {
        "print",
        "len",
        "range",
        "map",
        "filter",
        "sorted",
        "list",
        "dict",
        "set",
        "str",
        "int",
        "float",
        "bool",
        "type",
        "super",
        "isinstance",
        "issubclass",
        "hasattr",
        "getattr",
        "setattr",
        "open",
        "iter",
        "next",
        "zip",
        "enumerate",
        "any",
        "all",
        "min",
        "max",
        "sum",
        "abs",
        "round",
        "repr",
        "id",
        "hash",
        "dir",
        "vars",
        "input",
        "format",
        "tuple",
        "frozenset",
        "bytes",
        "object",
        "property",
        "classmethod",
        "staticmethod",
        "delattr",
        "callable",
        "compile",
        "eval",
        "exec",
        "globals",
        "locals",
        "append",
        "extend",
        "update",
        "pop",
        "get",
        "items",
        "keys",
        "values",
        "split",
        "join",
        "strip",
        "replace",
        "startswith",
        "endswith",
        "lower",
        "upper",
        "encode",
        "decode",
        "read",
        "write",
        "close",
        "console",
        "setTimeout",
        "setInterval",
        "clearTimeout",
        "clearInterval",
        "JSON",
        "Array",
        "Object",
        "Promise",
        "Math",
        "Date",
        "Error",
        "Symbol",
        "parseInt",
        "parseFloat",
        "isNaN",
        "fetch",
        "require",
        "exports",
        "module",
        "document",
        "window",
        "process",
        "Buffer",
        "URL",
        "log",
        "error",
        "warn",
        "info",
        "parse",
        "stringify",
        "assign",
        "freeze",
        "resolve",
        "reject",
        "useState",
        "useEffect",
        "useRef",
        "useCallback",
        "useMemo",
        "useContext",
        "useReducer",
    }
)

FRAMEWORK_DECORATORS = frozenset(
    {"route", "task", "validator", "field_validator", "fixture", "command", "endpoint"}
)


def urn(label: str, rel_path: str, symbol: str) -> str:
    rel = rel_path.replace("\\", "/").lstrip("/")
    return f"{label}:{rel}:{symbol}"


def _common_prefix_len(a: str, b: str) -> int:
    pa, pb = a.split("/"), b.split("/")
    n = 0
    for x, y in zip(pa, pb, strict=False):
        if x == y:
            n += 1
        else:
            break
    return n


def extract_symbols(tree: ast.AST) -> tuple[list[dict], list[dict]]:
    """Return (symbols, calls). symbols: {name, kind, lineno, class_name}."""
    symbols: list[dict] = []
    calls: list[dict] = []
    scope: list[str] = []

    class V(ast.NodeVisitor):
        def _q(self, name: str) -> str:
            return ".".join(scope + [name]) if scope else name

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            symbols.append(
                {
                    "name": self._q(node.name),
                    "kind": "class",
                    "lineno": node.lineno,
                    "class_name": node.name,
                    "bases": [ast.unparse(b) for b in node.bases]
                    if hasattr(ast, "unparse")
                    else [],
                }
            )
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            kind = "method" if scope else "function"
            symbols.append(
                {
                    "name": self._q(node.name),
                    "kind": kind,
                    "lineno": node.lineno,
                    "class_name": scope[-1] if scope else None,
                    "decorators": [
                        ast.unparse(d) if hasattr(ast, "unparse") else ""
                        for d in node.decorator_list
                    ],
                }
            )
            scope.append(node.name)
            self.generic_visit(node)
            scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            func = node.func
            if isinstance(func, ast.Name):
                calls.append({"name": func.id, "receiver": None, "line": node.lineno})
            elif isinstance(func, ast.Attribute):
                recv = func.value.id if isinstance(func.value, ast.Name) else ""
                calls.append({"name": func.attr, "receiver": recv or None, "line": node.lineno})
            self.generic_visit(node)

    V().visit(tree)
    return symbols, calls


def resolve_call(name: str, receiver: str | None, candidates: list[dict], caller_path: str):
    """4-tier resolution. candidates: [{urn, name, path, kind, class_name}]."""
    if name in CALL_BLOCKLIST and receiver not in ("self", "this"):
        return None, 0.0
    if receiver in ("self", "this"):
        for c in candidates:
            if c["name"].split(".")[-1] == name and c.get("kind") == "method":
                return c["urn"], 1.0
        return None, 0.0
    if receiver:
        for c in candidates:
            if c.get("class_name") == receiver and c["name"].split(".")[-1] == name:
                return c["urn"], 0.8
    same = [c for c in candidates if c["path"] == caller_path and c["name"].split(".")[-1] == name]
    if same:
        return same[0]["urn"], 1.0
    pool = [c for c in candidates if c["name"].split(".")[-1] == name]
    if not pool or len(pool) > 5:
        return None, 0.0
    best = max(pool, key=lambda c: (_common_prefix_len(caller_path, c["path"]), -len(c["path"])))
    return best["urn"], 0.5


def dead_code(symbols: list[dict], edges: list[dict]) -> list[str]:
    called = {e["dst"] for e in edges if e.get("relation") == "calls"}
    dead = []
    for s in symbols:
        u = s["urn"]
        nm = s["name"].split(".")[-1]
        if u in called:
            continue
        if nm.startswith("test_") or nm in ("__init__", "__new__"):
            continue
        if nm.startswith("__") and nm.endswith("__"):
            continue
        if s.get("path", "").endswith("__init__.py") and not nm.startswith("_"):
            continue
        if any(d in FRAMEWORK_DECORATORS for d in s.get("decorators", [])):
            continue
        dead.append(u)
    return dead


def build_upsert_rows(files: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """files: {rel_path: source}. Returns (nodes, edges) with stable URNs."""
    nodes: list[dict] = []
    node_by_simple: dict[str, list[dict]] = {}
    for rel_path, src in files.items():
        nodes.append(
            {
                "urn": urn("file", rel_path, rel_path),
                "kind": "file",
                "path": rel_path,
                "name": rel_path,
            }
        )
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        syms, _ = extract_symbols(tree)
        for s in syms:
            u = urn(
                s["kind"] if s["kind"] in ("class", "method") else "function", rel_path, s["name"]
            )
            row = {
                "urn": u,
                "kind": s["kind"],
                "path": rel_path,
                "name": s["name"],
                "class_name": s.get("class_name"),
                "lineno": s.get("lineno"),
                "decorators": s.get("decorators", []),
                "bases": s.get("bases", []),
            }
            nodes.append(row)
            node_by_simple.setdefault(s["name"].split(".")[-1], []).append(row)
    edges: list[dict] = []
    for rel_path, src in files.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        _, calls = extract_symbols(tree)
        for c in calls:
            cands: list[dict] = []
            for lst in node_by_simple.values():
                cands.extend(lst)
            target, conf = resolve_call(c["name"], c.get("receiver"), cands, rel_path)
            if target:
                edges.append(
                    {"src_path": rel_path, "dst": target, "relation": "calls", "confidence": conf}
                )
    return nodes, edges

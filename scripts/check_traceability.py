"""Generate Brains surface inventories from code and check them against the docs.

``check_docs.py`` validates the *shape* of the canonical documentation set:
presence, freshness headers, links, prohibited history, and the stable ID
vocabulary. It cannot tell whether the documented surfaces still exist.

Every inventory below is derived from
source at run time and compared against the canonical contract, so a new route,
client call, entity, migration, journey spec, or acceptance criterion that no
document knows about fails the gate, and a document that describes a surface
the code no longer has fails it too.

Checks
------

``spa``
    Routes declared in ``frontend/src/App.tsx`` against the route inventory in
    ``TRACEABILITY.md``, the required-route list in ``check_docs.py``, the
    component modules the routes name, and whether each route parameter is
    consumed by its component.

``client``
    ``frontend/src/api/client.ts`` calls against the methods and paths actually
    mounted on the FastAPI application. A call whose path is not a literal is
    an error rather than a silent omission.

``server``
    Every mounted route against the documented API family inventory, plus the
    Copilot bare-path aliases against their rewrite targets.

``entities``
    ``brains.storage.models`` tables against the frozen per-backend baseline DDL
    and the numbered migration deltas.

``migrations``
    The migration registry corpus against the files on disk and against the
    migration numbers the traceability document claims.

``markers``
    Journey specs and acceptance tests against the stable ``J*``/``F*`` IDs they
    encode, and every referenced ``AC-*`` against the acceptance criteria the
    feature contract declares.

Every intentional legacy, external, or dynamic exception is an explicit
allowlist entry below. Allowlists are checked in both directions: an entry that
no longer describes a real exception fails the gate rather than rotting.

Usage::

    python scripts/check_traceability.py [ROOT]
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


#: Documents whose ``AC-*`` references must resolve to a declared criterion.

#: The SPA route surface. A route added to ``frontend/src/App.tsx`` must be
#: added here too, so the browser surface cannot grow silently.
REQUIRED_SPA_ROUTES = (
    "/app",
    "/app/command-center",
    "/app/workspaces",
    "/app/workspaces/:slug",
    "/app/coordination",
    "/app/governance",
    "/app/operations",
    "/app/operations/config",
    "/app/operations/config/:section",
    "/app/act",
    "/app/inbox",
    "/app/config",
    "/app/*",
)

SPA_BASENAME = "/app"

#: Route parameters that are declared by the router but deliberately not read by
#: the screen yet (traceability mismatch UM-04). Each entry must also be stated
#: as a gap in the route's ``TRACEABILITY.md`` row. Removing the gap in code
#: without removing the entry here fails the gate.
UNCONSUMED_ROUTE_PARAMS: dict[str, tuple[str, ...]] = {}

#: Client calls whose literal path is intentionally not mounted by this server
#: (an externally served or legacy surface). Entries are ``(METHOD, path)``.
#: A call built from a non-literal path is always an error: the gate cannot
#: match it to a route, so it must stay a literal.
CLIENT_CALL_ALLOWLIST: dict[tuple[str, str], str] = {}

#: Families in the documented API inventory that are not FastAPI routes.
NON_ROUTE_FAMILIES = {
    # A cross-cutting dependency layer, not a mount point.
    "Identity/authorization",
}

#: Mounted-route prefix rules, in order. The first match wins. A mounted route
#: that matches no rule is an undocumented server surface.
SERVER_ROUTE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("/health", "Health"),
    ("/admin", "Admin"),
    ("/favicon.ico", "Modern browser"),
    ("/app", "Modern browser"),
    ("/static/brains", "Legacy static"),
    ("/docs", "Framework defaults"),
    ("/redoc", "Framework defaults"),
    ("/openapi.json", "Framework defaults"),
    ("/hooks/", "Trigger webhooks"),
    ("/relay/", "Relay"),
    ("/v1/ws", "Realtime"),
    ("/v1/events", "Realtime"),
    ("/v1/operator", "Operator console"),
    ("/v1/runtimes", "Runtimes"),
    ("/v1/integrations/github", "GitHub"),
    ("/v1/models", "Model gateway"),
    ("/v1/chat/completions", "Model gateway"),
    ("/v1/responses", "Model gateway"),
    ("/v1/messages", "Model gateway"),
    ("/v1/orgs/{}/pods", "Pods"),
    ("/v1/pods", "Pods"),
    ("/v1/onboarding", "Onboarding"),
    ("/v1/orgs/{}/autopilots", "Autopilots/Skills"),
    ("/v1/orgs/{}/skills", "Autopilots/Skills"),
    ("/v1/autopilots", "Autopilots/Skills"),
    ("/v1/orgs/{}/personas", "Personas"),
    ("/v1/personas", "Personas"),
    ("/v1/projects/{}/issues", "Issues"),
    ("/v1/issues", "Issues"),
    ("/v1/orgs/{}/projects", "Projects"),
    ("/v1/projects", "Projects"),
    ("/v1/orgs", "Orgs/members"),
    ("/v1/onboard", "Orgs/members"),
    ("/v1/approvals", "Inbox/coordination"),
    ("/v1/asks", "Inbox/coordination"),
    ("/v1/handoffs", "Inbox/coordination"),
    ("/v1/usage", "Inbox/coordination"),
    ("/v1/config", "Inbox/coordination"),
    ("/v1/admin", "Operational health"),
    ("/v1/sessions", "Inbox/coordination"),
)

#: Tables no migration creates because the ledger runner provisions them before
#: any migration can run.
LEDGER_MANAGED_TABLES = frozenset({"schema_versions"})

#: Migration IDs the traceability document is not required to name: the frozen
#: baseline and the historical no-op ledger markers that never had a delta.
MIGRATION_DOC_EXEMPT = frozenset({"0000_baseline", "0001_initial", "0002_schema_versions"})

#: Core features with no acceptance test in ``tests/test_acceptance_brains.py``.
#: Each entry is a declared evidence gap, not permission to skip coverage.
ACCEPTANCE_COVERAGE_GAPS: dict[str, str] = {}


_ROUTE_TAG_RE = re.compile(r"<Route\b")
_ATTR_PATH_RE = re.compile(r'\bpath="(?P<path>[^"]*)"')
_ATTR_INDEX_RE = re.compile(r"\bindex\b(?!=)")
_ELEMENT_ATTR_RE = re.compile(r"\belement=\{")
_ELEMENT_COMPONENT_RE = re.compile(r"<(?P<name>[A-Za-z0-9_]+)(?P<props>[^>]*)")
_NAVIGATE_TO_RE = re.compile(r'\bto="(?P<to>[^"]*)"')
_IMPORT_RE = re.compile(r'import\s*\{(?P<names>[^}]*)\}\s*from\s*"(?P<module>[^"]*)"')
_USE_PARAMS_RE = re.compile(r"\{(?P<names>[^{}]*)\}\s*=\s*useParams\s*[(<]")
_ROUTE_PARAM_RE = re.compile(r":([A-Za-z0-9_]+)")

_BACKTICK_RE = re.compile(r"`([^`]+)`")

#: Core features declare criteria as table rows; supporting capabilities declare
#: them as list items. Both shapes are declarations.
_AC_DECLARATION_RE = re.compile(
    r"(?m)^(?:\|\s*(?P<row>AC-(?:F\d{1,2}|B\d)-\d{2})\s*\||-\s*(?P<item>AC-(?:F\d{1,2}|B\d)-\d{2}):)"
)

_SPEC_FILE_RE = re.compile(r"\bj(?P<journey>\d{2})-[a-z0-9-]+\.spec\.ts\b")
_ACCEPTANCE_TEST_RE = re.compile(r"(?m)^def\s+test_(?P<feature>[fb])(?P<index>\d{1,2})_")

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'\[]?(?P<name>\w+)[\"'\]]?\s*\(",
    re.IGNORECASE,
)


class TraceabilityInputError(RuntimeError):
    """A source file the checker must parse is missing or unreadable."""


@dataclass(frozen=True)
class SpaRoute:
    """One route declared by the SPA router."""

    path: str
    component: str | None
    redirect_to: str | None

    @property
    def params(self) -> tuple[str, ...]:
        return tuple(_ROUTE_PARAM_RE.findall(self.path))


@dataclass(frozen=True)
class ClientCall:
    """One typed fetch the SPA API client performs."""

    method: str
    path: str


@dataclass(frozen=True)
class ClientCallInventory:
    """Every ``request(...)`` site in the API client, parsed or not.

    ``unmatchable`` holds the sites whose path is not a literal. They are
    reported rather than dropped: a call the gate cannot read is a call the
    gate cannot prove reaches a mounted route.
    """

    calls: tuple[ClientCall, ...]
    unmatchable: tuple[str, ...]


@dataclass(frozen=True)
class ServerRoute:
    """One route mounted on the FastAPI application."""

    method: str
    path: str


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unreadable tree
        raise TraceabilityInputError(f"cannot read {path}") from exc


def _require(root: Path, relative: str) -> str:
    path = root / relative
    if not path.is_file():
        raise TraceabilityInputError(f"missing required source: {relative}")
    return _read(path)


def normalize_path(path: str) -> str:
    """Collapse every path parameter to ``{}`` so both sides compare equal."""

    return re.sub(r"\{[^}]*\}", "{}", path)


# --------------------------------------------------------------------------
# frontend routes
# --------------------------------------------------------------------------


def _join_route(path: str) -> str:
    if path == "*":
        return f"{SPA_BASENAME}/*"
    if not path.startswith("/"):
        path = f"/{path}"
    if path == "/":
        return SPA_BASENAME
    return f"{SPA_BASENAME}{path}"


def _jsx_tag_body(text: str, start: int) -> tuple[str, int]:
    """Return the attribute source of the JSX tag opening at ``start``.

    The scan tracks brace, quote, and nested-tag depth so an ``element={<X />}``
    attribute does not terminate the tag early.
    """

    index = start
    depth = 0
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ">" and depth == 0:
            return text[start:index], index
        index += 1
    raise TraceabilityInputError("unterminated <Route> element in frontend/src/App.tsx")


def _element_expression(attrs: str) -> str | None:
    match = _ELEMENT_ATTR_RE.search(attrs)
    if match is None:
        return None
    open_index = match.end() - 1
    depth = 0
    index = open_index
    while index < len(attrs):
        char = attrs[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return attrs[open_index + 1 : index]
        index += 1
    return attrs[open_index + 1 :]


def collect_spa_routes(root: Path) -> tuple[SpaRoute, ...]:
    """Routes declared in ``frontend/src/App.tsx``, resolved to full paths."""

    text = _require(root, "frontend/src/App.tsx")
    routes: list[SpaRoute] = []
    position = 0
    while True:
        match = _ROUTE_TAG_RE.search(text, position)
        if match is None:
            break
        attrs, end = _jsx_tag_body(text, match.end())
        position = end + 1
        path_match = _ATTR_PATH_RE.search(attrs)
        is_index = _ATTR_INDEX_RE.search(attrs) is not None
        if path_match is None and not is_index:
            # A layout route (``<Route element={<AppShell />}>``) contributes no
            # path of its own.
            continue
        raw_path = "/" if is_index else path_match.group("path")  # type: ignore[union-attr]
        expression = _element_expression(attrs) or ""
        element = _ELEMENT_COMPONENT_RE.search(expression)
        component = element.group("name") if element else None
        redirect_to = None
        if component == "Navigate" and element is not None:
            navigate = _NAVIGATE_TO_RE.search(element.group("props"))
            redirect_to = _join_route(navigate.group("to")) if navigate else None
            component = None
        routes.append(
            SpaRoute(path=_join_route(raw_path), component=component, redirect_to=redirect_to)
        )
    return tuple(routes)


def collect_spa_imports(root: Path) -> dict[str, str]:
    """Component name -> module specifier imported by ``App.tsx``."""

    text = _require(root, "frontend/src/App.tsx")
    imports: dict[str, str] = {}
    for match in _IMPORT_RE.finditer(text):
        module = match.group("module")
        for raw_name in match.group("names").split(","):
            name = raw_name.strip()
            if name:
                imports[name] = module
    return imports


def collect_consumed_params(root: Path) -> dict[str, frozenset[str]]:
    """Component name -> route parameters that component reads via ``useParams``."""

    consumed: dict[str, set[str]] = {}
    source_root = root / "frontend/src"
    if not source_root.is_dir():
        raise TraceabilityInputError("missing required source: frontend/src")
    for path in sorted(source_root.rglob("*.tsx")):
        text = _read(path)
        names: set[str] = set()
        for match in _USE_PARAMS_RE.finditer(text):
            for raw in match.group("names").split(","):
                name = raw.split("=")[0].split(":")[0].strip()
                if name:
                    names.add(name)
        if names:
            consumed.setdefault(path.stem, set()).update(names)
    return {name: frozenset(values) for name, values in consumed.items()}


def _resolve_module(root: Path, importer: Path, module: str) -> Path | None:
    if not module.startswith("."):
        return None
    base = (importer.parent / module).resolve()
    for suffix in (".tsx", ".ts", "/index.tsx", "/index.ts"):
        candidate = Path(f"{base}{suffix}")
        if candidate.is_file():
            return candidate
    return None


def check_spa_routes(
    routes: tuple[SpaRoute, ...],
    required_routes: tuple[str, ...],
    imports: dict[str, str],
    consumed: dict[str, frozenset[str]],
    resolve: Callable[[str], Path | None] | None = None,
) -> list[str]:
    """Check declared SPA routes against the code that has to serve them."""

    errors: list[str] = []

    seen: set[str] = set()
    for route in routes:
        if route.path in seen:
            errors.append(f"spa: duplicate declared route {route.path}")
        seen.add(route.path)

    for path in sorted(seen - set(required_routes)):
        errors.append(f"spa: declared route {path} is missing from check_docs REQUIRED_SPA_ROUTES")
    for path in sorted(set(required_routes) - seen):
        errors.append(f"spa: check_docs REQUIRED_SPA_ROUTES lists {path}, which is not declared")

    for route in routes:
        if route.redirect_to is not None and not any(
            _matches(route.redirect_to, _ROUTE_PARAM_RE.sub("{}", declared)) for declared in seen
        ):
            errors.append(
                f"spa: route {route.path} redirects to {route.redirect_to}, which is not declared"
            )
        if route.component is None:
            continue
        if route.component not in imports:
            errors.append(f"spa: route {route.path} names unimported component {route.component}")
        elif resolve is not None and resolve(imports[route.component]) is None:
            errors.append(
                f"spa: component {route.component} for route {route.path} resolves to no module"
            )

    for route in routes:
        allowed = set(UNCONSUMED_ROUTE_PARAMS.get(route.path, ()))
        read_by_component = consumed.get(route.component or "", frozenset())
        for param in route.params:
            if param in read_by_component:
                if param in allowed:
                    errors.append(
                        f"spa: route {route.path} now consumes :{param}; "
                        "remove it from UNCONSUMED_ROUTE_PARAMS"
                    )
                continue
            if param not in allowed:
                errors.append(
                    f"spa: route {route.path} declares :{param}, which "
                    f"{route.component or 'its element'} never reads and no allowlist covers"
                )

    for path, params in sorted(UNCONSUMED_ROUTE_PARAMS.items()):
        if path not in seen:
            errors.append(f"spa: UNCONSUMED_ROUTE_PARAMS lists undeclared route {path}")
            continue
        declared = {param for route in routes if route.path == path for param in route.params}
        for param in sorted(set(params) - declared):
            errors.append(f"spa: UNCONSUMED_ROUTE_PARAMS lists :{param}, absent from {path}")

    return errors


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------

_CLOSERS = {"(": ")", "{": "}", "[": "]"}


def _argument_list(text: str, open_index: int) -> str:
    """Return the source between ``(`` at ``open_index`` and its match."""

    depth = 0
    index = open_index
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
        elif char in _CLOSERS:
            depth += 1
        elif char in ")}]":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
        index += 1
    raise TraceabilityInputError("unbalanced call expression in the API client")


def _split_top_level(arguments: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    index = 0
    while index < len(arguments):
        char = arguments[index]
        if quote is not None:
            current.append(char)
            if char == "\\":
                if index + 1 < len(arguments):
                    current.append(arguments[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
        elif char in _CLOSERS:
            depth += 1
        elif char in ")}]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _literal_path(expression: str) -> str | None:
    expression = expression.strip()
    if len(expression) < 2 or expression[0] != expression[-1]:
        return None
    if expression[0] not in "\"'`":
        return None
    body = expression[1:-1]
    # ``${qs(params)}`` is a query string, not a path segment.
    body = re.sub(r"\$\{qs\([^)]*\)\}", "", body)
    body = re.sub(r"\$\{[^}]*\}", "{}", body)
    if not body.startswith("/"):
        return None
    return body


def _declares_function(text: str, start: int) -> bool:
    """True when the identifier at ``start`` is a ``function`` declaration."""

    head = text[:start].rstrip()
    return head.endswith("function")


def collect_client_calls(root: Path) -> ClientCallInventory:
    """Every ``/v1`` call the SPA API client makes, with its HTTP method."""

    text = _require(root, "frontend/src/api/client.ts")
    calls: list[ClientCall] = []
    unmatchable: list[str] = []
    # Both shapes the client uses: an explicit generic (``request<Org>(...)``)
    # and a bare call. The ``request`` definition itself is skipped by name.
    for match in re.finditer(r"\brequest\s*(?:<[^(){}]*>)?\s*\(", text):
        if _declares_function(text, match.start()):
            continue
        open_index = match.end() - 1
        arguments = _argument_list(text, open_index)
        parts = _split_top_level(arguments)
        line = text.count("\n", 0, match.start()) + 1
        if not parts:
            unmatchable.append(f"line {line}: request() with no path argument")
            continue
        path = _literal_path(parts[0])
        if path is None:
            unmatchable.append(f"line {line}: request({parts[0].strip()[:48]}...)")
            continue
        method = "GET"
        if len(parts) > 1:
            method_match = re.search(r'\bmethod:\s*"(?P<method>[A-Z]+)"', parts[1])
            if method_match:
                method = method_match.group("method")
        calls.append(ClientCall(method=method, path=f"/v1{path}"))
    return ClientCallInventory(calls=tuple(calls), unmatchable=tuple(unmatchable))


def _matches(client_path: str, server_path: str) -> bool:
    client_parts = client_path.split("/")
    server_parts = server_path.split("/")
    if len(client_parts) != len(server_parts):
        return False
    for client_part, server_part in zip(client_parts, server_parts, strict=True):
        if server_part == client_part:
            continue
        if server_part == "{}" and client_part != "":
            continue
        return False
    return True


def check_client_server(
    inventory: ClientCallInventory,
    server_routes: tuple[ServerRoute, ...],
) -> list[str]:
    """Every client call must reach a mounted route with the same method."""

    errors: list[str] = []
    by_method: dict[str, list[str]] = {}
    for route in server_routes:
        by_method.setdefault(route.method, []).append(normalize_path(route.path))

    for site in inventory.unmatchable:
        errors.append(
            f"client: {site} does not use a literal path, so no server route can be matched"
        )

    used_allowlist: set[tuple[str, str]] = set()
    for call in inventory.calls:
        candidates = by_method.get(call.method, [])
        if call.path in candidates:
            continue
        if any(_matches(call.path, candidate) for candidate in candidates):
            continue
        key = (call.method, call.path)
        if key in CLIENT_CALL_ALLOWLIST:
            used_allowlist.add(key)
            continue
        errors.append(f"client: {call.method} {call.path} has no mounted server route")

    for key in sorted(set(CLIENT_CALL_ALLOWLIST) - used_allowlist):
        errors.append(f"client: CLIENT_CALL_ALLOWLIST entry {key[0]} {key[1]} matched no call")
    return errors


# --------------------------------------------------------------------------
# server routes and documented families
# --------------------------------------------------------------------------


def collect_server_routes(app: object) -> tuple[ServerRoute, ...]:
    """Mounted routes, one record per HTTP method (``WS`` for websockets)."""

    routes: list[ServerRoute] = []
    openapi = getattr(app, "openapi", None)
    if callable(openapi):
        for path, operations in openapi().get("paths", {}).items():
            for method in operations:
                method = method.upper()
                if method not in {"HEAD", "OPTIONS", "PARAMETERS"}:
                    routes.append(ServerRoute(method=method, path=path))
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", None)
        if not path or getattr(route, "methods", None):
            continue
        routes.append(ServerRoute(method="WS", path=path))
    return tuple(sorted(set(routes), key=lambda item: (item.path, item.method)))


def route_family(path: str) -> str | None:
    """The documented family a mounted path belongs to, or ``None``."""

    normalized = normalize_path(path)
    for prefix, family in SERVER_ROUTE_FAMILIES:
        if normalized == prefix or normalized.startswith(f"{prefix.rstrip('/')}/"):
            return family
    return None


def check_copilot_aliases(
    aliases: dict[str, str],
    server_routes: tuple[ServerRoute, ...],
) -> list[str]:
    """Each bare Copilot path must rewrite onto a route that exists."""

    mounted = {route.path for route in server_routes}
    errors: list[str] = []
    for source, target in sorted(aliases.items()):
        if target not in mounted:
            errors.append(f"server: Copilot alias {source} rewrites to unmounted {target}")
        if source in mounted:
            errors.append(f"server: Copilot alias {source} shadows a mounted route")
    return errors


# --------------------------------------------------------------------------
# entities and migrations
# --------------------------------------------------------------------------


def collect_sql_tables(text: str) -> frozenset[str]:
    return frozenset(match.group("name") for match in _CREATE_TABLE_RE.finditer(text))


def collect_baseline_tables(root: Path, backends: tuple[str, ...]) -> dict[str, frozenset[str]]:
    tables: dict[str, frozenset[str]] = {}
    for backend in backends:
        relative = f"src/brains/storage/baseline/{backend}.sql"
        tables[backend] = collect_sql_tables(_require(root, relative))
    return tables


def collect_delta_tables(root: Path) -> dict[str, frozenset[str]]:
    directory = root / "src/brains/storage/sql_migrations"
    if not directory.is_dir():
        raise TraceabilityInputError("missing required source: src/brains/storage/sql_migrations")
    tables: dict[str, frozenset[str]] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in (".sql", ".py") or path.name.startswith("_"):
            continue
        tables[path.name] = collect_sql_tables(_read(path))
    return tables


def check_entities(
    model_tables: frozenset[str],
    baseline_tables: dict[str, frozenset[str]],
    delta_tables: dict[str, frozenset[str]],
) -> list[str]:
    """Model tables must be provisioned; provisioned tables must have models."""

    errors: list[str] = []
    backends = sorted(baseline_tables)
    if len(backends) > 1:
        first = baseline_tables[backends[0]]
        for backend in backends[1:]:
            for table in sorted(first ^ baseline_tables[backend]):
                errors.append(
                    f"entities: baseline table {table} exists for one backend only "
                    f"({backends[0]} vs {backend})"
                )

    provisioned: set[str] = set()
    for tables in baseline_tables.values():
        provisioned |= set(tables)
    delta_created: set[str] = set()
    for tables in delta_tables.values():
        delta_created |= set(tables)
    provisioned |= delta_created

    for table in sorted(model_tables - provisioned - LEDGER_MANAGED_TABLES):
        errors.append(f"entities: model table {table} is created by no baseline or migration")
    for table in sorted(provisioned - model_tables):
        errors.append(f"entities: table {table} is provisioned but has no SQLAlchemy model")
    for table in sorted(LEDGER_MANAGED_TABLES - model_tables):
        errors.append(f"entities: LEDGER_MANAGED_TABLES names {table}, which has no model")
    for table in sorted(LEDGER_MANAGED_TABLES & provisioned):
        errors.append(
            f"entities: LEDGER_MANAGED_TABLES names {table}, which a migration now creates"
        )
    return errors


def check_migrations(
    corpus_ids: tuple[str, ...],
    disk_ids: tuple[str, ...],
    marker_ids: tuple[str, ...],
) -> list[str]:
    """The registry corpus and the files on disk must agree."""

    errors: list[str] = []
    seen: set[str] = set()
    for migration_id in corpus_ids:
        if migration_id in seen:
            errors.append(f"migrations: duplicate corpus ID {migration_id}")
        seen.add(migration_id)

    if list(corpus_ids) != sorted(corpus_ids):
        errors.append("migrations: corpus is not in stable lexical ID order")

    for migration_id in sorted(set(disk_ids) - seen):
        errors.append(f"migrations: {migration_id} exists on disk but is absent from the corpus")
    expected_on_disk = seen - set(marker_ids) - MIGRATION_DOC_EXEMPT
    for migration_id in sorted(expected_on_disk - set(disk_ids)):
        errors.append(f"migrations: corpus ID {migration_id} has no file on disk")

    return errors


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def check_repository(root: Path = ROOT) -> list[str]:
    """Run every generated traceability check against ``root``."""

    from brains.capabilities import WITHDRAWN_HTTP_EXACT_PATHS, WITHDRAWN_HTTP_PATH_PREFIXES
    from brains.main import app
    from brains.storage import migration_registry as registry
    from brains.storage.models import Base

    errors: list[str] = []

    routes = collect_spa_routes(root)
    errors.extend(
        check_spa_routes(
            routes,
            REQUIRED_SPA_ROUTES,
            collect_spa_imports(root),
            collect_consumed_params(root),
            resolve=lambda module: _resolve_module(root, root / "frontend/src/App.tsx", module),
        )
    )

    server_routes = collect_server_routes(app)
    withdrawn_exact = {normalize_path(path) for path in WITHDRAWN_HTTP_EXACT_PATHS}
    withdrawn_prefixes = tuple(normalize_path(path) for path in WITHDRAWN_HTTP_PATH_PREFIXES)
    collected_calls = collect_client_calls(root)
    client_calls = ClientCallInventory(
        calls=tuple(
            call
            for call in collected_calls.calls
            if normalize_path(call.path) not in withdrawn_exact
            and not normalize_path(call.path).startswith(withdrawn_prefixes)
        ),
        unmatchable=collected_calls.unmatchable,
    )
    errors.extend(check_client_server(client_calls, server_routes))
    # Compatibility gateway aliases are withdrawn from the core composition.
    # An empty inventory is still checked so reintroduction is explicit.
    errors.extend(check_copilot_aliases({}, server_routes))

    errors.extend(
        check_entities(
            frozenset(Base.metadata.tables),
            collect_baseline_tables(root, registry.SUPPORTED_BACKENDS),
            collect_delta_tables(root),
        )
    )

    errors.extend(
        check_migrations(
            tuple(spec.migration_id for spec in registry.corpus()),
            tuple(
                registry._split_backend_suffix(path.stem)[0]
                for path in registry.list_disk_migration_files()
            ),
            tuple(marker for marker, _ in registry.LEDGER_MARKERS),
        )
    )
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).resolve() if args else ROOT
    try:
        errors = check_repository(root)
    except TraceabilityInputError as exc:
        print("generated traceability contract failed:")
        print(f"- {exc}")
        return 1
    if errors:
        print("generated traceability contract failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("generated traceability contract passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

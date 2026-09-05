"""SQLite integrity diagnosis and repair (BL-P0-07).

Three concerns live here, in dependency order:

1. **Schema introspection.** :func:`schema_foreign_keys` reads
   ``PRAGMA foreign_key_list`` for every table in ``sqlite_master`` and
   :func:`descendant_delete_order` walks the reverse edges of that graph.
   Destructive cleanup therefore derives its dependency order from the
   database itself instead of a hand-maintained table list that silently
   rots whenever a model gains a foreign key.
2. **Diagnosis.** :func:`diagnose` runs ``PRAGMA integrity_check`` and
   ``PRAGMA foreign_key_check`` plus the product invariants named in
   a known limitation: contradictory terminal Session
   state, Org-less Workspaces, and orphaned/expired Session claims. Rows
   whose correct value cannot be derived from stored evidence are
   classified ``ambiguous_legacy`` and are never guessed at. A check that
   cannot run on the store's schema is listed in ``skipped_checks``, and an
   incomplete report is never ``ok``: missing coverage fails closed.
3. **Repair.** :func:`repair_database` is dry-run by default. Applying takes
   the SQLite write lock (``BEGIN IMMEDIATE``) *first* and holds it across
   diagnosis, backup capture, backup verification, every repair pass, and the
   commit, so no concurrent writer can slip between the state the archive
   captured and the state the repair mutates. The backup must be a manifest
   archive verified by an *isolated* restore *and* shown to still represent
   the live database (see :mod:`brains.backup`); the repair refuses to touch a
   database whose ``integrity_check`` is not ``ok``, re-plans within the
   transaction until nothing deterministic is left, and rolls back as a whole
   on any failure.

Engine scans and invariant replanning are deliberately separate. The
whole-database ``integrity_check``/``foreign_key_check`` pair runs once as
preflight (under the lock) and once as postflight (after the commit); the
convergence passes in between only evaluate the product and foreign-key state
they need in order to plan, re-checking foreign keys just over the tables the
previous pass could have broken. Holding the write lock is not a reason to
re-scan every page four times.

Two policy statements are made here rather than derived, because the schema
cannot express them:

* :data:`WORKSPACE_SCOPED_TABLES` names the direct children whose *optional*
  Workspace reference nevertheless means ownership.
* :data:`LEASE_TABLES` names the tables that hold ephemeral lock state rather
  than durable history. An orphaned lease row is not evidence of anything -
  the Session that held it is gone - so repair deletes it without
  ``--delete-orphans``. Every other row whose *required* parent is missing is
  durable, is reported as ``requires_operator``, and is removed only when the
  operator asks for it. Forgetting to name a lease table costs an operator
  decision; wrongly naming one would cost a record, so the list stays short
  and explicit.

Foreign-key *enforcement* is deliberately not turned on here. A store with
existing violations cannot have ``PRAGMA foreign_keys=ON`` applied without
breaking later writes in unpredictable places, so enforcement is opt-in
(``settings.sqlite_enforce_foreign_keys``) and gated by
:func:`assert_foreign_keys_clean`, which fails loudly when the data is not
ready for it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPORT_SCHEMA_VERSION = "1"

# Maximum number of offending rows echoed per finding. ``count`` always
# reports the true total so a truncated sample never hides scale.
SAMPLE_LIMIT = 50

# Session lifecycle vocabulary, mirrored from ``brains.control.sessions``.
# Duplicated as literals (not imported) so integrity tooling can run against
# a database without importing the control plane.
TERMINAL_SESSION_STATES = ("completed", "failed")

# Product policy, not a schema fact: leases and delivery cursors are ephemeral
# state rather than durable history. Repair may drop an orphaned row even when
# its parent column is required. Durable records (events, handoffs, tasks,
# checkpoints, audit entries) are never deleted by repair - their dangling
# references are nulled when the schema allows it, and reported for the
# operator when it does not.
LEASE_TABLES = (
    "approval_routing",
    "event_contexts",
    "help_request_executions",
    "session_leases",
    "topic_announcements",
    "topic_subscriptions",
    "workspace_claims",
)

# Direct children of ``workspaces`` whose *optional* Workspace reference means
# ownership: activity rows that have no meaning outside the Workspace they
# were recorded in. The schema cannot express that difference, so it is stated
# here. Everything else with a nullable Workspace foreign key (Projects,
# Issues, knowledge entries, recurring runs) is an independent record and
# keeps its row when a Workspace is deleted. A new table that is missing from
# this tuple is preserved, never silently destroyed.
#
# ``audit_log`` is deliberately absent even though it is activity-shaped. Its
# rows are links in a hash chain: deleting the newest one leaves a head that no
# longer matches the log, and `brains.audit` then refuses every subsequent
# append - a Workspace prune would silently stop all governed execution. The
# cascade clears the Workspace reference instead and keeps the entry.
WORKSPACE_SCOPED_TABLES = (
    "events",
    "help_requests",
    "mailboxes",
    "mailbox_messages",
    "session_checkpoints",
    "sources",
)

# Repair phases. One repaired invariant can be another invariant's input, so
# actions run in dependency order rather than in the order their findings were
# reported: Session state and end times are settled first, then Org scope, then
# the leases those Sessions hold, then foreign-key cleanup over whatever is
# left. Every foreign-key action shares one phase because the schema-derived
# cascade emits its own steps in a required order (clear references, then
# delete dependants deepest-first) that must not be re-interleaved.
_ACTION_PHASES = {
    "session.synchronize_terminal_state": 10,
    "session.stamp_ended_at": 11,
    "org.seed_default": 20,
    "workspace.assign_default_org": 21,
    "claim.delete_expired": 30,
    "claim.delete_ended_session": 31,
    "foreign_key.clear_dependant_reference": 40,
    "foreign_key.null_orphaned_reference": 40,
    "foreign_key.cascade_orphaned_dependant": 40,
    "foreign_key.delete_orphaned_row": 40,
}
_DEFAULT_PHASE = 50

# Ordering makes one pass enough for the dependencies we know about; the bound
# exists for the ones we do not. Exceeding it rolls the whole repair back.
MAX_REPAIR_PASSES = 4


class IntegrityError(RuntimeError):
    """Base class for integrity diagnosis/repair failures."""


class DatabaseCorruptError(IntegrityError):
    """``PRAGMA integrity_check`` reported structural corruption."""


class ForeignKeyViolationsError(IntegrityError):
    """``PRAGMA foreign_key_check`` reported violations."""


class BackupPrerequisiteError(IntegrityError):
    """Applying a repair requires a verified, matching backup."""


class RepairNotConvergedError(IntegrityError):
    """Repair kept producing new deterministic work; the transaction rolled back."""


class WriteLockUnavailableError(IntegrityError):
    """Another connection holds the SQLite write lock, so the store cannot be quiesced."""


class UnsupportedDatabaseError(IntegrityError):
    """The active storage backend is not a SQLite file."""


@dataclass(frozen=True)
class ForeignKeyEdge:
    """One ``PRAGMA foreign_key_list`` row, normalised."""

    table: str
    column: str
    parent_table: str
    parent_column: str
    nullable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Finding:
    """One deterministic integrity observation."""

    code: str
    category: str  # engine | foreign_key | invariant
    severity: str  # error | warning
    classification: str  # deterministic | ambiguous_legacy | requires_operator
    table: str
    detail: str
    count: int
    sample: tuple[dict[str, Any], ...] = ()
    repair: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "classification": self.classification,
            "table": self.table,
            "detail": self.detail,
            "count": self.count,
            "sample": [dict(row) for row in self.sample],
            "repair": self.repair,
        }


@dataclass(frozen=True)
class Report:
    """Machine-readable diagnosis output."""

    report_schema_version: str
    database: str
    evaluated_at: str
    integrity_check: tuple[str, ...]
    foreign_key_violations: int
    findings: tuple[Finding, ...]
    skipped_checks: tuple[dict[str, Any], ...] = ()

    @property
    def complete(self) -> bool:
        """True when every check actually ran on this schema.

        A skipped check is missing coverage, not a passed check. Diagnosis of
        a store that predates a migration therefore reports what it could see
        *and* says the picture is partial.
        """
        return not self.skipped_checks

    @property
    def ok(self) -> bool:
        """True only for a store that is proven clean *and* fully examined.

        ``ok`` is what the CLI exit code and the repair-readiness gate are
        derived from, so it fails closed: an incomplete diagnosis cannot
        report success, because the checks that did not run are exactly the
        ones whose result is unknown.
        """
        return self.complete and not self.findings and tuple(self.integrity_check) == ("ok",)

    @property
    def repairable(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.classification == "deterministic")

    @property
    def needs_operator(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.classification != "deterministic")

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_schema_version": self.report_schema_version,
            "database": self.database,
            "evaluated_at": self.evaluated_at,
            "ok": self.ok,
            "complete": self.complete,
            "integrity_check": list(self.integrity_check),
            "foreign_key_violations": self.foreign_key_violations,
            "counts": {
                "findings": len(self.findings),
                "deterministic": len(self.repairable),
                "ambiguous_legacy": len(
                    [f for f in self.findings if f.classification == "ambiguous_legacy"]
                ),
                "requires_operator": len(
                    [f for f in self.findings if f.classification == "requires_operator"]
                ),
                "skipped_checks": len(self.skipped_checks),
            },
            "findings": [f.to_dict() for f in self.findings],
            "skipped_checks": [dict(entry) for entry in self.skipped_checks],
        }


@dataclass
class RepairAction:
    """One planned mutation, with the rows it will touch."""

    code: str
    table: str
    description: str
    statement: str
    parameters: tuple[Any, ...] = ()
    expected_rows: int = 0
    applied_rows: int | None = None
    cascade: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "table": self.table,
            "description": self.description,
            "statement": " ".join(self.statement.split()),
            "expected_rows": self.expected_rows,
            "applied_rows": self.applied_rows,
            "cascade": list(self.cascade),
        }


# --------------------------------------------------------------- connections


def resolve_sqlite_path(db_path: str | Path | None = None) -> Path:
    """Return the SQLite file backing ``db_path`` or the active engine.

    Resolving through the live engine (rather than ``settings.db_url``)
    means tests that rebind ``brains.storage.db.engine`` to a temporary
    database are honoured, and no code path can silently reach for the
    operator's real store.
    """
    if db_path is not None:
        return Path(db_path).expanduser().resolve()

    from brains.storage.db import engine

    if engine.dialect.name != "sqlite":
        raise UnsupportedDatabaseError(
            f"integrity tooling requires a SQLite backend (got {engine.dialect.name!r})"
        )
    database = engine.url.database
    if not database or database == ":memory:":
        raise UnsupportedDatabaseError("integrity tooling requires an on-disk SQLite database")
    return Path(database).expanduser().resolve()


class MigrationsNotReadyError(IntegrityError):
    """The store's migration ledger is not healthy enough to repair."""


#: Ledger conditions that make a destructive repair unsafe. Schema *shape*
#: problems are deliberately not in this set: a dropped table is reported by
#: the diagnosis itself (``complete: false``), while these are all statements
#: that the store's migration history is unsettled.
_BLOCKING_MIGRATION_FINDINGS = frozenset(
    {
        "runner_refused",
        "interrupted_migration",
        "failed_migration",
        "ledger_gap",
        "migration_order_mismatch",
        "unknown_migration",
        "backend_mismatch",
    }
)


def assert_migrations_ready(database: Path) -> None:
    """Refuse a destructive repair over a store that is mid-migration.

    A repair rewrites rows against the schema the *running code* believes in.
    If a migration is pending, was interrupted, failed, was edited after it was
    applied, or was written by a build this one does not know, that belief is
    not established, and repairing first would write into a schema history
    nobody has settled.

    The check only runs when the target is the database the active engine is
    bound to; an explicitly supplied unrelated file is the caller's own.
    """
    from brains.storage.db import engine
    from brains.storage.migrations import migration_status

    bound = engine.url.database
    if engine.dialect.name != "sqlite" or not bound:
        return
    if Path(bound).expanduser().resolve() != database:
        return

    status = migration_status()
    pending = list(status.get("pending") or [])
    failed = list(status.get("failed") or [])
    blockers = [
        finding["code"] + (f" ({finding['migration_id']})" if finding.get("migration_id") else "")
        for finding in status.get("findings", [])
        if finding.get("code") in _BLOCKING_MIGRATION_FINDINGS
    ]
    if not pending and not failed and not blockers and not status.get("error"):
        return
    detail = "; ".join(blockers) or status.get("error") or "unsettled migration ledger"
    raise MigrationsNotReadyError(
        f"the migration ledger for {database} is not settled "
        f"(pending={pending}, failed={failed}): {detail}. Run `brains-ai db migrate` and "
        "re-check `brains-ai db migrations` before repairing."
    )


@contextmanager
def open_database(db_path: str | Path | None = None, *, read_only: bool = True):
    """Open a dedicated connection to the SQLite file.

    A dedicated connection keeps ``BEGIN``/``ROLLBACK`` under our control
    (``isolation_level=None``) instead of inheriting SQLAlchemy's implicit
    transaction handling, which matters for the all-or-nothing repair.
    """
    path = resolve_sqlite_path(db_path)
    if not path.exists():
        raise IntegrityError(f"SQLite database not found: {path}")
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


# -------------------------------------------------------- schema inspection


def list_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return tuple(row[0] for row in rows)


def table_columns(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    """``PRAGMA table_info`` keyed by column name.

    Rows are read positionally so the helper works with any connection,
    including a raw DB-API handle borrowed from SQLAlchemy that has no
    ``sqlite3.Row`` factory configured.
    """
    columns: dict[str, dict[str, Any]] = {}
    for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
        columns[row[1]] = {
            "name": row[1],
            "type": row[2],
            "notnull": bool(row[3]),
            "default": row[4],
            "pk": bool(row[5]),
        }
    return columns


def schema_foreign_keys(conn: sqlite3.Connection) -> dict[str, tuple[ForeignKeyEdge, ...]]:
    """Return every declared foreign key, keyed by child table.

    Composite foreign keys are returned as one edge per column; Brains
    declares only single-column keys today, and a composite key would still
    be reported (and therefore visible) rather than silently dropped.
    """
    graph: dict[str, tuple[ForeignKeyEdge, ...]] = {}
    for table in list_tables(conn):
        columns = table_columns(conn, table)
        edges: list[ForeignKeyEdge] = []
        # PRAGMA foreign_key_list columns: id, seq, table, from, to, ...
        for row in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
            parent_table = row[2]
            column = row[3]
            parent_column = row[4]
            if parent_column is None:
                primary = [
                    name for name, info in table_columns(conn, parent_table).items() if info["pk"]
                ]
                parent_column = primary[0] if primary else "rowid"
            info = columns.get(column)
            edges.append(
                ForeignKeyEdge(
                    table=table,
                    column=column,
                    parent_table=parent_table,
                    parent_column=parent_column,
                    nullable=bool(info is not None and not info["notnull"]),
                )
            )
        if edges:
            graph[table] = tuple(sorted(edges, key=lambda e: (e.column, e.parent_table)))
    return graph


def reverse_dependencies(
    graph: dict[str, tuple[ForeignKeyEdge, ...]],
) -> dict[str, tuple[ForeignKeyEdge, ...]]:
    """Invert the foreign-key graph: parent table -> edges pointing at it."""
    reverse: dict[str, list[ForeignKeyEdge]] = {}
    for edges in graph.values():
        for edge in edges:
            reverse.setdefault(edge.parent_table, []).append(edge)
    return {
        parent: tuple(sorted(edges, key=lambda e: (e.table, e.column)))
        for parent, edges in sorted(reverse.items())
    }


@dataclass(frozen=True)
class CascadeStep:
    """One statement in a schema-derived cascade.

    ``operation`` is ``delete`` when the row cannot outlive the root rows,
    and ``null`` when the row is a durable record that merely holds a
    reference into the doomed set (for example a Handoff in another
    Workspace that was picked up by a Session being removed).
    """

    table: str
    operation: str  # delete | null
    depth: int
    predicate: str  # SQL fragment; ``{root}`` marks the root-row predicate
    columns: tuple[str, ...]

    def sql(self, root_predicate: str) -> str:
        where = self.predicate.format(root=root_predicate)
        if self.operation == "null":
            assignments = ", ".join(f'"{column}" = NULL' for column in self.columns)
            return f'UPDATE "{self.table}" SET {assignments} WHERE {where}'
        return f'DELETE FROM "{self.table}" WHERE {where}'

    def count_sql(self, root_predicate: str) -> str:
        where = self.predicate.format(root=root_predicate)
        return f'SELECT COUNT(*) FROM "{self.table}" WHERE {where}'


def descendant_delete_order(
    conn: sqlite3.Connection,
    root_table: str,
    *,
    scoped_tables: Sequence[str] = (),
    max_depth: int = 12,
) -> tuple[CascadeStep, ...]:
    """Return the cascade for everything that depends on ``root_table``.

    The dependency graph, the delete order, and the predicates all come from
    the database's own foreign keys. Only one narrow judgement is a policy
    input: whether an *optional* direct reference to the root means ownership.

    * A ``NOT NULL`` foreign key means the row cannot exist without its
      parent, so the row is deleted. This is what makes transitive dependants
      (``approval_decisions`` -> ``approval_requests`` -> ``workspaces``)
      correct without maintaining a table list.
    * A nullable foreign key means the row is an independent record that
      merely points at the doomed set (a Persona created by a Session, an
      Issue recorded against a Workspace). Its reference is cleared and the
      record is kept, so cleanup can never destroy an entity that has its own
      identity.
    * ``scoped_tables`` names the direct children whose nullable reference to
      the root is nevertheless ownership - activity rows with no meaning
      outside the root they were recorded in. Anything not named is kept.

    Ordering is: reference-clearing updates first, while their targets still
    exist, then deletes by decreasing distance from the root, so a dependant
    is always removed before the row it depends on.
    """
    graph = schema_foreign_keys(conn)
    reverse = reverse_dependencies(graph)
    scoped = frozenset(scoped_tables)

    # Phase 1 - classify every reachable edge. Predicates are deliberately not
    # built here: a table can gain a second required parent later, and a
    # predicate captured too early would under-match its own dependants.
    delete_edges: dict[str, dict[str, ForeignKeyEdge]] = {}
    null_edges: dict[tuple[str, str], ForeignKeyEdge] = {}
    frontier: list[str] = [root_table]
    visited_edges: set[tuple[str, str, str]] = set()
    while frontier:
        parent_table = frontier.pop(0)
        for edge in reverse.get(parent_table, ()):
            if edge.table == root_table or edge.table == edge.parent_table:
                continue
            edge_key = (edge.parent_table, edge.table, edge.column)
            if edge_key in visited_edges:
                continue
            visited_edges.add(edge_key)
            owned = not edge.nullable or (edge.parent_table == root_table and edge.table in scoped)
            if not owned:
                null_edges[(edge.table, edge.column)] = edge
                continue
            columns = delete_edges.setdefault(edge.table, {})
            newly_owned = not columns
            columns[edge.column] = edge
            if newly_owned:
                frontier.append(edge.table)

    # Phase 2 - build predicates and distances from the final edge set.
    predicates: dict[str, str] = {root_table: "{root}"}
    distances: dict[str, int] = {root_table: 0}
    resolving: set[str] = set()

    def resolve(table: str) -> tuple[str, int]:
        if table in predicates:
            return predicates[table], distances[table]
        resolving.add(table)
        branches: list[str] = []
        distance = 0
        for column, edge in sorted(delete_edges.get(table, {}).items()):
            if edge.parent_table in resolving:
                # Self-referential chains cannot be expressed as a nested
                # predicate; skip that branch rather than emit invalid SQL.
                continue
            parent_predicate, parent_distance = resolve(edge.parent_table)
            branches.append(
                f'"{column}" IN (SELECT "{edge.parent_column}" '
                f'FROM "{edge.parent_table}" WHERE {parent_predicate})'
            )
            distance = max(distance, parent_distance + 1)
        resolving.discard(table)
        if not branches:
            raise IntegrityError(f"cannot derive a delete predicate for {table!r}")
        if distance > max_depth:
            raise IntegrityError(
                f"dependency chain to {table!r} exceeds max_depth={max_depth}; "
                "refusing to emit a partial cascade"
            )
        predicate = branches[0] if len(branches) == 1 else "(" + " OR ".join(branches) + ")"
        predicates[table] = predicate
        distances[table] = distance
        return predicate, distance

    deletes: list[CascadeStep] = []
    for table in sorted(delete_edges):
        predicate, distance = resolve(table)
        deletes.append(
            CascadeStep(
                table=table,
                operation="delete",
                depth=distance,
                predicate=predicate,
                columns=tuple(sorted(delete_edges[table])),
            )
        )

    nulls: list[CascadeStep] = []
    for (table, column), edge in sorted(null_edges.items()):
        parent_predicate, parent_distance = resolve(edge.parent_table)
        predicate = (
            f'"{column}" IN (SELECT "{edge.parent_column}" '
            f'FROM "{edge.parent_table}" WHERE {parent_predicate})'
        )
        if table in delete_edges:
            table_predicate, _table_distance = resolve(table)
            # A row already scheduled for deletion does not need its optional
            # references cleared first. Excluding it also avoids transiently
            # violating compound checks such as mailbox read attribution.
            predicate = f"({predicate}) AND COALESCE(({table_predicate}), 0) = 0"
        nulls.append(
            CascadeStep(
                table=table,
                operation="null",
                depth=parent_distance + 1,
                predicate=predicate,
                columns=(column,),
            )
        )

    ordered_nulls = tuple(sorted(nulls, key=lambda step: (step.table, step.columns)))
    ordered_deletes = tuple(sorted(deletes, key=lambda step: (-step.depth, step.table)))
    return ordered_nulls + ordered_deletes


# ------------------------------------------------------------------ helpers


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def _count(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row[0]) if row is not None else 0


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _has_columns(conn: sqlite3.Connection, table: str, *columns: str) -> bool:
    if not _has_table(conn, table):
        return False
    present = set(table_columns(conn, table))
    return all(column in present for column in columns)


def _missing_columns(conn: sqlite3.Connection, table: str, *columns: str) -> tuple[str, ...] | None:
    """Return the absent columns, or ``None`` when the table itself is absent."""
    if not _has_table(conn, table):
        return None
    present = set(table_columns(conn, table))
    return tuple(column for column in columns if column not in present)


def _requires(
    conn: sqlite3.Connection,
    skipped: list[dict[str, Any]],
    check: str,
    table: str,
    *columns: str,
) -> bool:
    """Guard a check against schemas that predate the columns it reads.

    An older store (or a partially migrated one) simply does not have the
    columns an invariant is expressed over. Querying them anyway raises
    ``sqlite3.OperationalError`` half way through a diagnosis, which is the
    one thing a diagnostic tool must not do. The check is skipped instead,
    and the skip is reported in the machine-readable output so the absence
    of a finding is never mistaken for a clean result.
    """
    missing = _missing_columns(conn, table, *columns)
    if missing is None:
        skipped.append({"check": check, "table": table, "reason": "table is absent"})
        return False
    if missing:
        skipped.append(
            {
                "check": check,
                "table": table,
                "reason": f"column(s) absent: {', '.join(missing)}",
            }
        )
        return False
    return True


def integrity_check(conn: sqlite3.Connection) -> tuple[str, ...]:
    """``PRAGMA integrity_check`` — a full scan of every page in the database.

    This is the most expensive statement in the module and its answer cannot
    change under a transaction that only runs DML, so callers that loop (the
    repair convergence passes) run it once as preflight and once as postflight
    rather than per pass. See :func:`_converge_within_transaction`.
    """
    return tuple(str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall())


def _repair_preflight_integrity_check(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Normalize only SQLite's structural-corruption failures for repair.

    SQLite builds differ on whether a damaged page is returned as an
    ``integrity_check`` row or aborts the pragma with ``SQLITE_CORRUPT``.  Both
    mean that repair must stop before creating a backup or mutating the store.
    Other database failures keep their original exception and semantics.
    """
    try:
        return integrity_check(conn)
    except sqlite3.DatabaseError as exc:
        error_code = getattr(exc, "sqlite_errorcode", None)
        corruption_codes = {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
        if isinstance(error_code, int) and not isinstance(error_code, bool) and error_code >= 0:
            is_corruption = error_code & 0xFF in corruption_codes
        else:
            message = str(exc).casefold()
            is_corruption = any(
                marker in message
                for marker in ("database disk image is malformed", "file is not a database")
            )
        if not is_corruption:
            raise
        raise DatabaseCorruptError(
            "refusing to repair a database because SQLite could not complete "
            f"PRAGMA integrity_check: {exc}"
        ) from exc


def foreign_key_violations(
    conn: sqlite3.Connection,
    *,
    tables: Sequence[str] | None = None,
) -> list[sqlite3.Row]:
    """Rows from ``PRAGMA foreign_key_check`` in a deterministic order.

    ``tables`` narrows the check to named tables. A whole-database check reads
    every row of every table with a foreign key, which is fine once but not
    once per repair pass. Repair therefore re-checks only the tables that its
    own previous pass could have broken - the ones it wrote to and their
    children - because a violation can only appear in a row that references a
    parent the pass removed. Anything outside that set was already checked by
    the preflight, and the full postflight check has the final word.
    """
    if tables is None:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    else:
        rows = []
        for table in sorted(set(tables)):
            if not _has_table(conn, table):
                continue
            rows.extend(conn.execute(f'PRAGMA foreign_key_check("{table}")').fetchall())
    return sorted(rows, key=lambda row: (str(row[0]), str(row[2]), int(row[1] or 0), int(row[3])))


def assert_foreign_keys_clean(conn: sqlite3.Connection) -> None:
    """Raise unless ``PRAGMA foreign_key_check`` is empty.

    This is the gate in front of foreign-key *enforcement*: enabling
    ``PRAGMA foreign_keys=ON`` over a store that already violates its own
    schema converts latent damage into unpredictable write failures later.
    """
    violations = foreign_key_violations(conn)
    if not violations:
        return
    summary: dict[str, int] = {}
    for row in violations:
        summary[str(row[0])] = summary.get(str(row[0]), 0) + 1
    breakdown = ", ".join(f"{table}={count}" for table, count in sorted(summary.items()))
    raise ForeignKeyViolationsError(
        f"{len(violations)} foreign-key violation(s) present ({breakdown}); "
        "run `brains-ai db diagnose` and `brains-ai db repair` before enabling "
        "foreign-key enforcement"
    )


# ---------------------------------------------------------------- diagnosis


def _finding_foreign_keys(
    conn: sqlite3.Connection, violations: Sequence[sqlite3.Row]
) -> list[Finding]:
    graph = schema_foreign_keys(conn)
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in violations:
        grouped.setdefault((str(row[0]), str(row[2])), []).append(row)

    findings: list[Finding] = []
    for (table, parent), rows in sorted(grouped.items()):
        edges = [e for e in graph.get(table, ()) if e.parent_table == parent]
        # ``fkid`` (row[3]) indexes into ``PRAGMA foreign_key_list`` order.
        nullable = bool(edges) and all(edge.nullable for edge in edges)
        columns = tuple(sorted({edge.column for edge in edges}))
        sample = tuple(
            {"rowid": row[1], "parent": parent, "fkid": row[3]} for row in rows[:SAMPLE_LIMIT]
        )
        if nullable:
            classification = "deterministic"
            repair = f"null {table}.{'/'.join(columns)} for orphaned references"
        elif table in LEASE_TABLES:
            classification = "deterministic"
            repair = f"delete orphaned {table} lease rows"
        else:
            classification = "requires_operator"
            repair = None
        findings.append(
            Finding(
                code="foreign_key.orphaned_reference",
                category="foreign_key",
                severity="error",
                classification=classification,
                table=table,
                detail=(
                    f"{len(rows)} row(s) in {table} reference a missing {parent} row "
                    f"via {'/'.join(columns) if columns else 'an undeclared column'}"
                ),
                count=len(rows),
                sample=sample,
                repair=repair,
            )
        )
    return findings


def _finding_sessions_ended_without_terminal_state(
    conn: sqlite3.Connection, skipped: list[dict[str, Any]]
) -> list[Finding]:
    if not _requires(
        conn,
        skipped,
        "session.ended_without_terminal_state",
        "agent_sessions",
        "id",
        "ended_at",
        "state",
    ):
        return []
    placeholders = ",".join("?" for _ in TERMINAL_SESSION_STATES)
    rows = _rows(
        conn,
        "SELECT id, state, ended_at FROM agent_sessions "
        f"WHERE ended_at IS NOT NULL AND (state IS NULL OR state NOT IN ({placeholders})) "
        "ORDER BY id",
        TERMINAL_SESSION_STATES,
    )
    if not rows:
        return []

    deterministic: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    has_events = _has_columns(conn, "events", "session_id", "kind")
    for row in rows:
        evidence = None
        if has_events:
            kinds = {
                str(r[0])
                for r in _rows(
                    conn,
                    "SELECT DISTINCT kind FROM events WHERE session_id = ? "
                    "AND kind IN ('session_reaped', 'session_end')",
                    (row["id"],),
                )
            }
            if "session_reaped" in kinds:
                evidence = "failed"
            elif "session_end" in kinds:
                evidence = "completed"
        entry = {"id": row["id"], "state": row["state"], "ended_at": row["ended_at"]}
        if evidence is None:
            ambiguous.append(entry)
        else:
            deterministic.append({**entry, "resolved_state": evidence})

    findings: list[Finding] = []
    if deterministic:
        findings.append(
            Finding(
                code="session.ended_without_terminal_state",
                category="invariant",
                severity="error",
                classification="deterministic",
                table="agent_sessions",
                detail=(
                    f"{len(deterministic)} ended session(s) hold a non-terminal state; "
                    "a session_reaped event resolves to 'failed' and a session_end "
                    "event resolves to 'completed'"
                ),
                count=len(deterministic),
                sample=tuple(deterministic[:SAMPLE_LIMIT]),
                repair="set state from the recorded session lifecycle event",
            )
        )
    if ambiguous:
        findings.append(
            Finding(
                code="session.ended_state_ambiguous",
                category="invariant",
                severity="warning",
                classification="ambiguous_legacy",
                table="agent_sessions",
                detail=(
                    f"{len(ambiguous)} ended session(s) hold a non-terminal state with no "
                    "session_end or session_reaped event; the correct terminal state cannot "
                    "be derived from stored evidence"
                ),
                count=len(ambiguous),
                sample=tuple(ambiguous[:SAMPLE_LIMIT]),
                repair=None,
            )
        )
    return findings


def _ended_at_source_expression(conn: sqlite3.Connection) -> str:
    """SQL for the latest defensible end time on this schema.

    ``last_activity_at`` only exists from migration 122 onwards, so a store
    that has not been migrated yet resolves the end time from ``started_at``
    alone rather than referencing a column that is not there.
    """
    if _has_columns(conn, "agent_sessions", "last_activity_at"):
        return "COALESCE(last_activity_at, started_at)"
    return "started_at"


def _finding_sessions_terminal_without_ended_at(
    conn: sqlite3.Connection, skipped: list[dict[str, Any]]
) -> list[Finding]:
    if not _requires(
        conn,
        skipped,
        "session.terminal_without_ended_at",
        "agent_sessions",
        "id",
        "ended_at",
        "state",
        "started_at",
    ):
        return []
    ended_at_source = _ended_at_source_expression(conn)
    placeholders = ",".join("?" for _ in TERMINAL_SESSION_STATES)
    rows = _rows(
        conn,
        f"SELECT id, state, {ended_at_source} AS resolved_ended_at FROM agent_sessions "
        f"WHERE ended_at IS NULL AND state IN ({placeholders}) ORDER BY id",
        TERMINAL_SESSION_STATES,
    )
    if not rows:
        return []
    deterministic = [
        {
            "id": row["id"],
            "state": row["state"],
            "resolved_ended_at": row["resolved_ended_at"],
        }
        for row in rows
        if row["resolved_ended_at"]
    ]
    ambiguous = [
        {"id": row["id"], "state": row["state"]} for row in rows if not row["resolved_ended_at"]
    ]

    findings: list[Finding] = []
    if deterministic:
        findings.append(
            Finding(
                code="session.terminal_without_ended_at",
                category="invariant",
                severity="error",
                classification="deterministic",
                table="agent_sessions",
                detail=(
                    f"{len(deterministic)} terminal session(s) have no ended_at; the last "
                    "recorded activity (or start) time is the latest defensible value"
                ),
                count=len(deterministic),
                sample=tuple(deterministic[:SAMPLE_LIMIT]),
                repair=f"stamp ended_at from {ended_at_source}",
            )
        )
    if ambiguous:
        findings.append(
            Finding(
                code="session.terminal_ended_at_ambiguous",
                category="invariant",
                severity="warning",
                classification="ambiguous_legacy",
                table="agent_sessions",
                detail=(
                    f"{len(ambiguous)} terminal session(s) have no ended_at and no "
                    f"{ended_at_source} value; no end time can be derived"
                ),
                count=len(ambiguous),
                sample=tuple(ambiguous[:SAMPLE_LIMIT]),
                repair=None,
            )
        )
    return findings


def _resolve_default_org(conn: sqlite3.Connection) -> tuple[int | None, str]:
    """Return ``(org_id, reason)`` for Workspace Org backfill.

    Deterministic when a ``default``-slugged Org exists or exactly one Org
    exists. Anything else is an operator decision, not a guess.
    """
    if not _has_columns(conn, "orgs", "id", "slug"):
        return None, "orgs table is absent"
    row = conn.execute("SELECT id FROM orgs WHERE slug = 'default'").fetchone()
    if row is not None:
        return int(row[0]), "org slug 'default'"
    rows = conn.execute("SELECT id FROM orgs ORDER BY id").fetchall()
    if len(rows) == 1:
        return int(rows[0][0]), "the only Org in the store"
    if not rows:
        return None, "no Org exists yet; repair seeds the default Org"
    return None, f"{len(rows)} Orgs exist and none is slugged 'default'"


def _finding_orgless_workspaces(
    conn: sqlite3.Connection, skipped: list[dict[str, Any]]
) -> list[Finding]:
    if not _requires(conn, skipped, "workspace.missing_org", "workspaces", "id", "slug", "org_id"):
        return []
    rows = _rows(
        conn,
        "SELECT id, slug FROM workspaces WHERE org_id IS NULL ORDER BY id",
    )
    if not rows:
        return []
    org_id, reason = _resolve_default_org(conn)
    ambiguous = org_id is None and not reason.startswith("no Org exists")
    sample = tuple({"id": row["id"], "slug": row["slug"]} for row in rows[:SAMPLE_LIMIT])
    return [
        Finding(
            code="workspace.missing_org",
            category="invariant",
            severity="error",
            classification="requires_operator" if ambiguous else "deterministic",
            table="workspaces",
            detail=f"{len(rows)} Workspace(s) have no Org scope ({reason})",
            count=len(rows),
            sample=sample,
            repair=None if ambiguous else f"assign the default Org ({reason})",
        )
    ]


def _finding_stale_claims(
    conn: sqlite3.Connection, now: datetime, skipped: list[dict[str, Any]]
) -> list[Finding]:
    if not _requires(
        conn,
        skipped,
        "claim.expired",
        "workspace_claims",
        "workspace_id",
        "session_id",
        "expires_at",
    ):
        return []
    findings: list[Finding] = []
    # ``expires_at`` is written by SQLAlchemy as ``YYYY-MM-DD HH:MM:SS.ffffff``
    # while the evaluation instant is ISO-8601 with a ``T`` and an offset. A
    # text comparison of those two encodings is wrong (and would report live
    # leases as expired), so both sides are normalised through ``julianday``.
    expired = _rows(
        conn,
        "SELECT workspace_id, session_id, expires_at FROM workspace_claims "
        "WHERE expires_at IS NOT NULL AND julianday(expires_at) < julianday(?) "
        "ORDER BY workspace_id",
        (_iso(now),),
    )
    if expired:
        findings.append(
            Finding(
                code="claim.expired",
                category="invariant",
                severity="warning",
                classification="deterministic",
                table="workspace_claims",
                detail=f"{len(expired)} Workspace claim(s) expired before {_iso(now)}",
                count=len(expired),
                sample=tuple(
                    {
                        "workspace_id": row["workspace_id"],
                        "session_id": row["session_id"],
                        "expires_at": row["expires_at"],
                    }
                    for row in expired[:SAMPLE_LIMIT]
                ),
                repair="delete expired claim leases",
            )
        )
    if _requires(conn, skipped, "claim.session_ended", "agent_sessions", "id", "ended_at"):
        ended = _rows(
            conn,
            "SELECT c.workspace_id, c.session_id FROM workspace_claims c "
            "JOIN agent_sessions s ON s.id = c.session_id "
            "WHERE s.ended_at IS NOT NULL ORDER BY c.workspace_id",
        )
        if ended:
            findings.append(
                Finding(
                    code="claim.session_ended",
                    category="invariant",
                    severity="warning",
                    classification="deterministic",
                    table="workspace_claims",
                    detail=(
                        f"{len(ended)} Workspace claim(s) are held by a Session that has ended"
                    ),
                    count=len(ended),
                    sample=tuple(
                        {"workspace_id": row["workspace_id"], "session_id": row["session_id"]}
                        for row in ended[:SAMPLE_LIMIT]
                    ),
                    repair="delete claim leases owned by ended Sessions",
                )
            )
    return findings


def diagnose(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    database: str | None = None,
    engine_integrity: Sequence[str] | None = None,
    foreign_key_rows: Sequence[sqlite3.Row] | None = None,
) -> Report:
    """Run every integrity check and return a deterministic report.

    Deterministic means: the same database and the same ``now`` produce the
    same report, including ordering. ``now`` only affects lease expiry.

    Checks whose table or columns do not exist on this schema are skipped and
    listed in ``skipped_checks``, so a store that predates a migration is
    diagnosed rather than crashed on, and the missing coverage is visible
    instead of looking like a clean result - ``ok`` is false while anything
    was skipped.

    ``engine_integrity`` and ``foreign_key_rows`` let a caller that has
    already paid for the engine-level scans supply them instead of repeating
    them. They exist for the repair convergence loop, which re-derives the
    *plan* after every pass but must not re-run a full-database
    ``integrity_check`` to do it. Both default to running the pragma.
    """
    evaluated_at = now or _utc_now()
    findings: list[Finding] = []
    skipped: list[dict[str, Any]] = []

    engine_result = (
        tuple(str(entry) for entry in engine_integrity)
        if engine_integrity is not None
        else integrity_check(conn)
    )
    if tuple(engine_result) != ("ok",):
        findings.append(
            Finding(
                code="sqlite.integrity_check",
                category="engine",
                severity="error",
                classification="requires_operator",
                table="(database)",
                detail="PRAGMA integrity_check did not return ok",
                count=len(engine_result),
                sample=tuple({"message": message} for message in engine_result[:SAMPLE_LIMIT]),
                repair=None,
            )
        )

    fk_rows = (
        list(foreign_key_rows) if foreign_key_rows is not None else foreign_key_violations(conn)
    )
    findings.extend(_finding_foreign_keys(conn, fk_rows))
    findings.extend(_finding_sessions_ended_without_terminal_state(conn, skipped))
    findings.extend(_finding_sessions_terminal_without_ended_at(conn, skipped))
    findings.extend(_finding_orgless_workspaces(conn, skipped))
    findings.extend(_finding_stale_claims(conn, evaluated_at, skipped))

    findings.sort(key=lambda f: (f.category, f.code, f.table))
    return Report(
        report_schema_version=REPORT_SCHEMA_VERSION,
        database=database or "",
        evaluated_at=_iso(evaluated_at),
        integrity_check=engine_result,
        foreign_key_violations=len(fk_rows),
        findings=tuple(findings),
        skipped_checks=tuple(sorted(skipped, key=lambda entry: (entry["check"], entry["table"]))),
    )


def diagnose_database(
    db_path: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> Report:
    """Open the active (or given) SQLite file read-only and diagnose it."""
    path = resolve_sqlite_path(db_path)
    with open_database(path, read_only=True) as conn:
        return diagnose(conn, now=now, database=str(path))


# ------------------------------------------------------------------- repair


def _plan_foreign_key_actions(
    conn: sqlite3.Connection,
    finding: Finding,
    *,
    delete_orphans: bool,
) -> list[RepairAction]:
    graph = schema_foreign_keys(conn)
    parents = {str(row["parent"]) for row in finding.sample}
    actions: list[RepairAction] = []
    for edge in graph.get(finding.table, ()):
        if edge.parent_table not in parents:
            continue
        orphan_predicate = (
            f'"{edge.column}" IS NOT NULL AND "{edge.column}" NOT IN '
            f'(SELECT "{edge.parent_column}" FROM "{edge.parent_table}")'
        )
        affected = _count(conn, f'SELECT COUNT(*) FROM "{finding.table}" WHERE {orphan_predicate}')
        if not affected:
            continue
        if edge.nullable:
            actions.append(
                RepairAction(
                    code="foreign_key.null_orphaned_reference",
                    table=finding.table,
                    description=(
                        f"null {finding.table}.{edge.column} where the referenced "
                        f"{edge.parent_table} row is gone (the record itself is kept)"
                    ),
                    statement=(
                        f'UPDATE "{finding.table}" SET "{edge.column}" = NULL '
                        f"WHERE {orphan_predicate}"
                    ),
                    expected_rows=affected,
                )
            )
        elif finding.table in LEASE_TABLES or delete_orphans:
            for step in descendant_delete_order(conn, finding.table):
                expected = _count(conn, step.count_sql(orphan_predicate))
                if not expected:
                    continue
                actions.append(
                    RepairAction(
                        code=(
                            "foreign_key.clear_dependant_reference"
                            if step.operation == "null"
                            else "foreign_key.cascade_orphaned_dependant"
                        ),
                        table=step.table,
                        description=(
                            f"{step.operation} {step.table} rows that depend on orphaned "
                            f"{finding.table} rows (schema-derived cascade)"
                        ),
                        statement=step.sql(orphan_predicate),
                        expected_rows=expected,
                        cascade=(finding.table,),
                    )
                )
            actions.append(
                RepairAction(
                    code="foreign_key.delete_orphaned_row",
                    table=finding.table,
                    description=(
                        f"delete {finding.table} rows whose required {edge.parent_table} "
                        "row is gone"
                    ),
                    statement=f'DELETE FROM "{finding.table}" WHERE {orphan_predicate}',
                    expected_rows=affected,
                )
            )
    return actions


def plan_repair(
    conn: sqlite3.Connection,
    report: Report,
    *,
    delete_orphans: bool = False,
) -> list[RepairAction]:
    """Translate deterministic findings into ordered, parameterised actions.

    Ambiguous and operator-owned findings never produce an action: repair
    reports them instead of inventing a value.

    Actions are returned in phase order (see :data:`_ACTION_PHASES`) so that a
    value another action depends on is written before that action reads it -
    a Session that gains ``ended_at`` releases its claim in the same pass
    rather than leaving a fresh finding behind.
    """
    actions: list[RepairAction] = []
    for finding in report.findings:
        if finding.code == "foreign_key.orphaned_reference":
            if finding.classification == "deterministic" or delete_orphans:
                actions.extend(
                    _plan_foreign_key_actions(conn, finding, delete_orphans=delete_orphans)
                )
        elif finding.code == "session.ended_without_terminal_state":
            for state in TERMINAL_SESSION_STATES:
                # ``sample`` is bounded, so derive the full row set from the
                # same evidence rule rather than trusting the echoed sample.
                event_kind = "session_reaped" if state == "failed" else "session_end"
                placeholders = ",".join("?" for _ in TERMINAL_SESSION_STATES)
                predicate = (
                    "ended_at IS NOT NULL "
                    f"AND (state IS NULL OR state NOT IN ({placeholders})) "
                    "AND EXISTS (SELECT 1 FROM events e WHERE e.session_id = agent_sessions.id "
                    "AND e.kind = ?)"
                )
                if state == "completed":
                    predicate += (
                        " AND NOT EXISTS (SELECT 1 FROM events e2 "
                        "WHERE e2.session_id = agent_sessions.id AND e2.kind = 'session_reaped')"
                    )
                affected = _count(
                    conn,
                    f"SELECT COUNT(*) FROM agent_sessions WHERE {predicate}",
                    (*TERMINAL_SESSION_STATES, event_kind),
                )
                if not affected:
                    continue
                actions.append(
                    RepairAction(
                        code="session.synchronize_terminal_state",
                        table="agent_sessions",
                        description=(
                            f"set state='{state}' for ended sessions carrying a {event_kind} event"
                        ),
                        statement=(
                            f"UPDATE agent_sessions SET state = '{state}' WHERE {predicate}"
                        ),
                        parameters=(*TERMINAL_SESSION_STATES, event_kind),
                        expected_rows=affected,
                    )
                )
        elif finding.code == "session.terminal_without_ended_at":
            ended_at_source = _ended_at_source_expression(conn)
            placeholders = ",".join("?" for _ in TERMINAL_SESSION_STATES)
            predicate = (
                f"ended_at IS NULL AND state IN ({placeholders}) AND {ended_at_source} IS NOT NULL"
            )
            affected = _count(
                conn,
                f"SELECT COUNT(*) FROM agent_sessions WHERE {predicate}",
                TERMINAL_SESSION_STATES,
            )
            if affected:
                actions.append(
                    RepairAction(
                        code="session.stamp_ended_at",
                        table="agent_sessions",
                        description=(
                            f"stamp ended_at from {ended_at_source} for terminal sessions"
                        ),
                        statement=(
                            f"UPDATE agent_sessions SET ended_at = {ended_at_source} "
                            f"WHERE {predicate}"
                        ),
                        parameters=TERMINAL_SESSION_STATES,
                        expected_rows=affected,
                    )
                )
        elif finding.code == "workspace.missing_org" and finding.classification == "deterministic":
            org_id, reason = _resolve_default_org(conn)
            if org_id is None:
                actions.append(
                    RepairAction(
                        code="org.seed_default",
                        table="orgs",
                        description="create the default Org so Workspaces can be scoped",
                        statement=(
                            "INSERT INTO orgs (slug, name, description, status, "
                            "created_at, updated_at) VALUES "
                            "('default', 'Default Org', NULL, 'active', ?, ?)"
                        ),
                        parameters=(_iso(_utc_now()), _iso(_utc_now())),
                        expected_rows=1,
                    )
                )
                actions.append(
                    RepairAction(
                        code="workspace.assign_default_org",
                        table="workspaces",
                        description="assign the seeded default Org to Org-less Workspaces",
                        statement=(
                            "UPDATE workspaces SET org_id = "
                            "(SELECT id FROM orgs WHERE slug = 'default') WHERE org_id IS NULL"
                        ),
                        expected_rows=finding.count,
                    )
                )
            else:
                actions.append(
                    RepairAction(
                        code="workspace.assign_default_org",
                        table="workspaces",
                        description=f"assign Org {org_id} ({reason}) to Org-less Workspaces",
                        statement="UPDATE workspaces SET org_id = ? WHERE org_id IS NULL",
                        parameters=(org_id,),
                        expected_rows=finding.count,
                    )
                )
        elif finding.code == "claim.expired":
            actions.append(
                RepairAction(
                    code="claim.delete_expired",
                    table="workspace_claims",
                    description="delete expired Workspace claim leases",
                    statement="DELETE FROM workspace_claims WHERE expires_at IS NOT NULL "
                    "AND julianday(expires_at) < julianday(?)",
                    parameters=(report.evaluated_at,),
                    expected_rows=finding.count,
                )
            )
        elif finding.code == "claim.session_ended":
            actions.append(
                RepairAction(
                    code="claim.delete_ended_session",
                    table="workspace_claims",
                    description="delete Workspace claim leases owned by ended Sessions",
                    statement=(
                        "DELETE FROM workspace_claims WHERE session_id IN "
                        "(SELECT id FROM agent_sessions WHERE ended_at IS NOT NULL)"
                    ),
                    expected_rows=finding.count,
                )
            )
    return sorted(actions, key=lambda action: _ACTION_PHASES.get(action.code, _DEFAULT_PHASE))


def apply_repair(conn: sqlite3.Connection, actions: Sequence[RepairAction]) -> list[RepairAction]:
    """Apply ``actions`` inside one transaction, rolling back on any failure."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _execute_actions(conn, actions)
        conn.execute("COMMIT")
    except Exception:
        _release_write_lock(conn)
        for action in actions:
            action.applied_rows = None
        raise
    return list(actions)


def _execute_actions(conn: sqlite3.Connection, actions: Sequence[RepairAction]) -> None:
    for action in actions:
        cursor = conn.execute(action.statement, tuple(action.parameters))
        action.applied_rows = cursor.rowcount if cursor.rowcount is not None else 0


def apply_repair_converged(
    conn: sqlite3.Connection,
    *,
    database: str,
    now: datetime,
    delete_orphans: bool = False,
    max_passes: int = MAX_REPAIR_PASSES,
) -> tuple[list[RepairAction], int]:
    """Repair until no deterministic finding remains, inside one transaction.

    This is the standalone entry point: it owns the ``BEGIN IMMEDIATE`` and
    the ``COMMIT``/``ROLLBACK``. :func:`repair_database` uses
    :func:`_converge_within_transaction` instead, because it must hold the
    same write lock across backup capture, verification, *and* mutation.
    """
    conn.execute("BEGIN IMMEDIATE")
    executed: list[RepairAction] = []
    try:
        executed, passes = _converge_within_transaction(
            conn,
            database=database,
            now=now,
            delete_orphans=delete_orphans,
            max_passes=max_passes,
            executed=executed,
        )
        conn.execute("COMMIT")
    except Exception:
        _release_write_lock(conn)
        for action in executed:
            action.applied_rows = None
        raise
    return executed, passes


def _replan_scope(
    conn: sqlite3.Connection, actions: Sequence[RepairAction]
) -> tuple[str, ...] | None:
    """Tables whose foreign keys the just-executed ``actions`` could have broken.

    A ``PRAGMA foreign_key_check`` violation only appears in a row whose
    parent is missing, so a pass can only create one in a table that
    references a table it wrote to. The scope is therefore the written tables
    plus their children in the schema's own reverse foreign-key graph.
    Returning ``None`` means "check everything", which is what an unknown
    write set has to mean.
    """
    written = {action.table for action in actions}
    if not written:
        return ()
    reverse = reverse_dependencies(schema_foreign_keys(conn))
    scope = set(written)
    for table in written:
        scope.update(edge.table for edge in reverse.get(table, ()))
    return tuple(sorted(scope))


def _converge_within_transaction(
    conn: sqlite3.Connection,
    *,
    database: str,
    now: datetime,
    delete_orphans: bool,
    max_passes: int = MAX_REPAIR_PASSES,
    executed: list[RepairAction] | None = None,
    engine_integrity: Sequence[str] | None = None,
    foreign_key_rows: Sequence[sqlite3.Row] | None = None,
) -> tuple[list[RepairAction], int]:
    """Re-plan and apply until nothing deterministic is left. Caller owns the txn.

    Repairing one invariant can legitimately expose another: stamping
    ``ended_at`` on a terminal Session turns its live claim into a lease held
    by an ended Session. Phase ordering resolves that within a single plan
    where the action already exists, but an action is only planned when its
    finding was observed, so the plan is re-derived from the database after
    each pass until it is empty.

    Re-planning is *invariant* work, not engine work. The engine-level scans
    are separated out accordingly:

    * ``PRAGMA integrity_check`` runs once, as the preflight this loop is
      handed. DML inside a transaction cannot make a structurally sound
      database unsound, and the full post-repair diagnosis re-runs it anyway,
      so repeating a whole-database page scan per pass would buy nothing while
      holding the write lock;
    * ``PRAGMA foreign_key_check`` runs whole-database once in that same
      preflight, then only over the tables the previous pass could have
      broken (see :func:`_replan_scope`).

    Failing to converge within ``max_passes`` is treated as a defect in the
    plan, not as something to keep grinding at: the caller rolls back.

    A caller that has already run the engine preflight passes it in through
    ``engine_integrity``/``foreign_key_rows`` so it is not paid for twice.
    """
    executed = executed if executed is not None else []
    passes = 0
    engine_integrity = (
        tuple(engine_integrity) if engine_integrity is not None else integrity_check(conn)
    )
    fk_rows: Sequence[sqlite3.Row] | None = (
        foreign_key_rows if foreign_key_rows is not None else foreign_key_violations(conn)
    )
    while True:
        report = diagnose(
            conn,
            now=now,
            database=database,
            engine_integrity=engine_integrity,
            foreign_key_rows=fk_rows,
        )
        actions = plan_repair(conn, report, delete_orphans=delete_orphans)
        if not actions:
            break
        passes += 1
        if passes > max_passes:
            raise RepairNotConvergedError(
                f"repair did not converge within {max_passes} pass(es); "
                "rolling back. Remaining planned actions: "
                + ", ".join(sorted({action.code for action in actions}))
            )
        _execute_actions(conn, actions)
        executed.extend(actions)
        scope = _replan_scope(conn, actions)
        fk_rows = foreign_key_violations(conn, tables=scope) if scope is not None else None
    return executed, passes


def _verified_backup(
    archive: str | Path | None,
    backup_to: str | Path | None,
    database: Path,
    source_lock: Any,
) -> dict[str, Any]:
    """Create and/or verify the backup that a destructive repair requires.

    ``source_lock`` is the repair's held SQLite write lock. Everything here
    runs under it: the archive is captured from a database no other connection
    can write, and it is verified against that same quiesced state, so the
    freshness verdict is still true when the caller's next statement mutates
    the store. The lock is re-proved by the backup layer around each step, so
    a lost or never-acquired lock is an error rather than an unnoticed race.

    Verification binds the archive to *this* database and to its *current*
    state: an archive taken before a later write no longer represents what is
    about to be mutated, so it is refused rather than accepted as a safety
    net. See :func:`brains.backup.verify_backup`.
    """
    from brains.backup import BackupError, create_backup, verify_backup

    if (archive is None) == (backup_to is None):
        raise BackupPrerequisiteError(
            "applying a repair requires exactly one of an existing verified backup "
            "(backup_archive) or a destination to create one (backup_to)"
        )
    try:
        if backup_to is not None:
            created = create_backup(backup_to, source_lock=source_lock)
            archive = created.archive_path
        verification = verify_backup(
            cast("str | Path", archive),
            expected_source_path=database,
            source_lock=source_lock,
        )
    except BackupError as exc:
        raise BackupPrerequisiteError(f"backup prerequisite failed: {exc}") from exc
    if not verification.ok:
        raise BackupPrerequisiteError(
            "backup verification failed: " + "; ".join(verification.failures)
        )
    return verification.to_dict()


def _acquire_write_lock(conn: sqlite3.Connection, database: Path) -> Any:
    """Take the SQLite write lock and return the token that proves it is held.

    ``BEGIN IMMEDIATE`` takes the write lock immediately instead of on first
    write, so from this point no other connection can commit until the repair
    commits or rolls back. That is the quiescence the backup prerequisite
    depends on: SQLite's online backup API cannot step against a connection
    that holds a write transaction, so the image cannot be taken *from* this
    transaction - but it can be taken from a reader while this transaction
    holds every writer off, which is the same guarantee by a different route.
    """
    from brains.backup import SourceWriteLock

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise WriteLockUnavailableError(
            f"could not acquire the SQLite write lock on {database} ({exc}); "
            "another writer is holding it. Quiesce writers and retry."
        ) from exc
    return SourceWriteLock(path=database, connection=conn)


def _release_write_lock(conn: sqlite3.Connection) -> None:
    """Roll back without letting the rollback replace the real failure.

    A ``ROLLBACK`` on a connection whose transaction SQLite has already
    unwound raises ``OperationalError``. Letting that escape from an
    ``except`` block would hide the error that caused the rollback, which is
    the one the operator needs.
    """
    with suppress(sqlite3.OperationalError):
        conn.execute("ROLLBACK")


def repair_database(
    db_path: str | Path | None = None,
    *,
    apply: bool = False,
    backup_archive: str | Path | None = None,
    backup_to: str | Path | None = None,
    delete_orphans: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Diagnose and (optionally) repair the SQLite store.

    Dry-run is the default and never opens a write transaction: it opens the
    file read-only, reports the first plan, and takes no lock.

    ``apply`` serializes the whole destructive sequence behind one write
    transaction. ``BEGIN IMMEDIATE`` is taken *first*, and only then is the
    store diagnosed, the backup captured and verified, and the repair applied
    - all against a database no other connection can write. The transaction is
    released only by the commit or rollback that ends the repair, so there is
    no instant at which a concurrent writer can commit between the state the
    archive captured and the state the repair mutates.
    """
    database = resolve_sqlite_path(db_path)
    evaluated_at = now or _utc_now()

    if apply:
        assert_migrations_ready(database)

    if not apply:
        with open_database(database, read_only=True) as conn:
            report = diagnose(conn, now=evaluated_at, database=str(database))
            planned = plan_repair(conn, report, delete_orphans=delete_orphans)
        return {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "database": str(database),
            "dry_run": True,
            "delete_orphans": delete_orphans,
            "diagnosis": report.to_dict(),
            "planned_actions": [action.to_dict() for action in planned],
            "applied": False,
            "passes": 0,
            "backup": None,
            "unrepaired": [f.to_dict() for f in report.needs_operator],
        }

    with open_database(database, read_only=False) as conn:
        lock = _acquire_write_lock(conn, database)
        executed: list[RepairAction] = []
        try:
            # Preflight, under the lock: this is the state the archive will
            # capture and the repair will mutate, and it is the only whole
            # database ``integrity_check``/``foreign_key_check`` the write
            # path runs. Convergence re-plans from invariants, not from a
            # repeated page scan.
            engine_integrity = _repair_preflight_integrity_check(conn)
            fk_rows = foreign_key_violations(conn)
            report = diagnose(
                conn,
                now=evaluated_at,
                database=str(database),
                engine_integrity=engine_integrity,
                foreign_key_rows=fk_rows,
            )
            if tuple(report.integrity_check) != ("ok",):
                raise DatabaseCorruptError(
                    "refusing to repair a database whose PRAGMA integrity_check is not ok: "
                    + "; ".join(report.integrity_check)
                )
            backup = _verified_backup(backup_archive, backup_to, database, lock)
            executed, passes = _converge_within_transaction(
                conn,
                database=str(database),
                now=evaluated_at,
                delete_orphans=delete_orphans,
                executed=executed,
                engine_integrity=engine_integrity,
                foreign_key_rows=fk_rows,
            )
            conn.execute("COMMIT")
        except Exception:
            _release_write_lock(conn)
            for action in executed:
                action.applied_rows = None
            raise

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "database": str(database),
        "dry_run": False,
        "delete_orphans": delete_orphans,
        "diagnosis": report.to_dict(),
        "planned_actions": [action.to_dict() for action in executed],
        "applied": True,
        "passes": passes,
        "backup": backup,
        "unrepaired": [f.to_dict() for f in report.needs_operator],
        "post_repair": _post_repair_summary(database, now=evaluated_at),
    }


def _post_repair_summary(database: Path, *, now: datetime | None) -> dict[str, Any]:
    """The final, whole-database verdict, taken after the transaction closed.

    This is the full check - ``integrity_check``, ``foreign_key_check``, and
    every invariant - so the cheaper scoped re-checks the convergence loop
    used to *plan* never become the last word on whether the store is clean.
    """
    with open_database(database, read_only=True) as conn:
        report = diagnose(conn, now=now, database=str(database))
    return {
        "ok": report.ok,
        "complete": report.complete,
        "foreign_key_violations": report.foreign_key_violations,
        "skipped_checks": [dict(entry) for entry in report.skipped_checks],
        "remaining_findings": [f.to_dict() for f in report.findings],
    }


def workspace_cascade_tables(conn: sqlite3.Connection) -> tuple[CascadeStep, ...]:
    """Schema-derived cascade for everything that depends on a Workspace."""
    return descendant_delete_order(conn, "workspaces", scoped_tables=WORKSPACE_SCOPED_TABLES)


def iter_cascade_statements(
    steps: Sequence[CascadeStep], root_predicate: str
) -> Iterator[tuple[str, str]]:
    """Yield ``(table, sql)`` for a cascade in application order."""
    for step in steps:
        yield step.table, step.sql(root_predicate)


__all__ = [
    "MAX_REPAIR_PASSES",
    "BackupPrerequisiteError",
    "CascadeStep",
    "DatabaseCorruptError",
    "Finding",
    "ForeignKeyEdge",
    "ForeignKeyViolationsError",
    "IntegrityError",
    "RepairAction",
    "RepairNotConvergedError",
    "Report",
    "UnsupportedDatabaseError",
    "WriteLockUnavailableError",
    "apply_repair",
    "apply_repair_converged",
    "assert_foreign_keys_clean",
    "descendant_delete_order",
    "diagnose",
    "diagnose_database",
    "foreign_key_violations",
    "integrity_check",
    "iter_cascade_statements",
    "open_database",
    "plan_repair",
    "repair_database",
    "resolve_sqlite_path",
    "schema_foreign_keys",
    "workspace_cascade_tables",
]

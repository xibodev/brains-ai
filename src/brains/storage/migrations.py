"""Reproducible schema evolution (BL-P0-08).

The contract
------------

A Brains store reaches the current schema through exactly one path: the
ordered, checksummed migration corpus in
:mod:`brains.storage.migration_registry`, recorded in the ``schema_versions``
ledger by :mod:`brains.storage.migration_ledger`.

* **Explicit.** ``Base.metadata.create_all`` is not part of the startup path.
  A fresh database is created by the frozen baseline DDL under
  ``brains/storage/baseline``, which is a checked-in artifact, so the meaning
  of the initial migration does not depend on the installed model code. The
  only table created from the ORM is the ledger itself, which the runner owns
  and which is deliberately excluded from the baseline.
* **Immutable.** Every migration has a stable ID, a lexical order, and a
  content checksum. An applied migration whose file later changes is a hard
  refusal, not a silent divergence.
* **Backend-honest.** A migration is recorded ``applied`` only after its
  implementation *for the active backend* ran and committed. A migration with
  no implementation for this backend is recorded ``skipped`` with a reason -
  and only when it is a historical SQLite catch-up patch whose target state
  the baseline already provisions. Anything else is refused. A row written by
  the pre-checksum ledger carries no backend, so it is adopted backend-aware:
  a migration this backend cannot run is kept ``skipped``/``legacy-unproven``
  instead of frozen as an immutable applied sentinel, and a row that cannot be
  evidence of execution here is labelled rather than re-executed.
* **Diagnosable.** Interrupted and failed runs stay in the ledger with their
  attempt count, timings, and error, and are retried on the next run rather
  than being papered over.
* **Verified.** After migrating, the live schema is checked against the
  declared models. Missing tables or columns raise instead of surfacing later
  as a query error.

New schema changes are therefore *always* a new numbered migration; adding a
model attribute alone is drift, and the verification says so.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from .db import SessionLocal, engine
from .migration_ledger import (
    CHECKSUM_ORIGIN_LEGACY,
    CHECKSUM_ORIGIN_LEGACY_UNPROVEN,
    LEGACY_EXECUTION_BACKEND,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    LedgerError,
    LedgerRow,
    adopt_legacy_row,
    begin_attempt,
    ensure_ledger,
    finish_attempt,
    read_ledger,
    record_skipped,
)
from .migration_registry import (
    BASELINE_BLOCK_MARKER,
    BASELINE_ID,
    CHECKSUM_ALGORITHM,
    KIND_BASELINE,
    KIND_MARKER,
    KIND_PYTHON,
    RUNNER_VERSION,
    SQL_MIGRATIONS_DIR,
    MigrationCorpusError,
    MigrationSpec,
    corpus,
    list_disk_migration_files,
)
from .models import Base

__all__ = [
    "SQL_MIGRATIONS_DIR",
    "MigrationBackendUnsupportedError",
    "MigrationChecksumError",
    "MigrationCorpusError",
    "MigrationError",
    "MigrationExecutionError",
    "MigrationFinding",
    "MigrationReport",
    "MigrationSchemaDriftError",
    "current_schema_versions",
    "init_db",
    "known_migration_ids",
    "migration_status",
    "reset_migration_cache",
    "run_migrations",
]

# ``SessionLocal`` stays bound because it is part of this module's historical
# surface: tests rebind it alongside ``engine`` to isolate a temp database.
_SESSION_FACTORY = SessionLocal

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# One development-only migration body reached this machine's live store before
# 139 was committed and released. The draft added ``help_requests.required_tool``
# directly; the immutable release moved that state into the additive
# ``help_request_constraints`` table so the frozen baseline did not drift.
#
# This allowlist is intentionally exact and backend-specific. It does NOT turn
# checksum mismatches into warnings generally: every other migration/checksum
# pair remains a hard refusal. Migration 140 converges the leaked draft schema
# to the released shape and preserves any populated constraint values.
_PRE_RELEASE_CHECKSUM_COMPAT: dict[tuple[str, str], frozenset[str]] = {
    ("139_agent_comms", "sqlite"): frozenset(
        {"af734f5b5ba05f3ff9a6439e6f6e825b7b65bcecf795bd5e94eaefdc48cfb05e"}
    ),
}


class MigrationError(RuntimeError):
    """Base class for every refusal raised by the migration runner."""


class MigrationChecksumError(MigrationError):
    """A migration recorded in the ledger no longer matches its file."""


class MigrationBackendUnsupportedError(MigrationError):
    """No implementation exists for this backend and none may be assumed."""


class MigrationExecutionError(MigrationError):
    """A migration's delta raised; the transaction was rolled back."""


class MigrationSchemaDriftError(MigrationError):
    """The migrated schema does not contain everything the models declare."""


@dataclass(frozen=True)
class MigrationFinding:
    code: str
    severity: str
    detail: str
    migration_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "migration_id": self.migration_id,
            "detail": self.detail,
        }


@dataclass
class MigrationReport:
    """What the runner saw and did on one backend."""

    backend: str
    database: str
    runner_version: str = RUNNER_VERSION
    checksum_algorithm: str = CHECKSUM_ALGORITHM
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    findings: list[MigrationFinding] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    schema_verified: bool = False

    @property
    def healthy(self) -> bool:
        return (
            not self.pending
            and not self.failed
            and self.schema_verified
            and not any(finding.severity == SEVERITY_ERROR for finding in self.findings)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "database": self.database,
            "runner_version": self.runner_version,
            "checksum_algorithm": self.checksum_algorithm,
            "healthy": self.healthy,
            "schema_verified": self.schema_verified,
            "counts": {
                "applied": len(self.applied),
                "skipped": len(self.skipped),
                "pending": len(self.pending),
                "failed": len(self.failed),
                "executed_this_run": len(self.executed),
            },
            "applied": list(self.applied),
            "skipped": list(self.skipped),
            "pending": list(self.pending),
            "failed": list(self.failed),
            "executed_this_run": list(self.executed),
            "findings": [finding.to_dict() for finding in self.findings],
            "migrations": list(self.ledger),
        }


# --------------------------------------------------------------------------- #
# Per-engine caches. Keyed weakly so a test that rebinds the engine gets a
# fresh verdict instead of inheriting another database's.
# --------------------------------------------------------------------------- #

_LEDGER_READY: WeakKeyDictionary = WeakKeyDictionary()
_SCHEMA_VERIFIED: WeakKeyDictionary = WeakKeyDictionary()
_RUN_LOCK = threading.RLock()


def reset_migration_cache() -> None:
    """Forget ledger-bootstrap and schema-verification results (tests)."""
    _LEDGER_READY.clear()
    _SCHEMA_VERIFIED.clear()


def _active_engine() -> Engine:
    # Read through the module global so tests that rebind ``migrations.engine``
    # are honoured.
    return engine


def _ensure_sqlite_parent_directory(active: Engine) -> None:
    if active.dialect.name != "sqlite":
        return
    database = active.url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return
    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def database_identity(active: Engine | None = None) -> str:
    """A human-readable database identity with no credentials in it."""
    target = _active_engine() if active is None else active
    url = target.url
    if url.get_backend_name() == "sqlite":
        return f"sqlite:///{url.database or ':memory:'}"
    host = url.host or "localhost"
    port = f":{url.port}" if url.port else ""
    return f"{url.get_backend_name()}://{host}{port}/{url.database or ''}"


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _sqlite3_connection(raw: Any) -> sqlite3.Connection:
    candidate = getattr(raw, "driver_connection", None) or getattr(raw, "connection", raw)
    if not isinstance(candidate, sqlite3.Connection):
        raise MigrationExecutionError(
            f"the sqlite migration path needs a sqlite3 connection, got {type(candidate).__name__}"
        )
    return candidate


def split_sqlite_statements(script: str) -> list[str]:
    """Split a SQLite script into complete statements.

    ``executescript`` is deliberately not used: it issues an implicit
    ``COMMIT`` first, which would break the per-migration transaction and let a
    half-applied delta survive a crash.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if buffer.strip() and sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    remainder = "\n".join(
        line for line in buffer.splitlines() if line.strip() and not line.strip().startswith("--")
    ).strip()
    if remainder:
        raise MigrationCorpusError(f"incomplete trailing SQL statement: {remainder[:120]!r}")
    return statements


def _load_python_upgrade(path: Path) -> Callable[[sqlite3.Connection], None]:
    spec = importlib.util.spec_from_file_location(f"_brains_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise MigrationExecutionError(f"cannot load python migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    upgrade = getattr(module, "upgrade", None)
    if not callable(upgrade):
        raise MigrationExecutionError(f"python migration {path.name} has no upgrade(conn)")
    return upgrade


def _run_sqlite_transaction(active: Engine, work: Callable[[sqlite3.Connection], Any]) -> None:
    """Run ``work`` against a raw SQLite connection in one write transaction."""
    raw = active.raw_connection()
    try:
        conn = _sqlite3_connection(raw)
        previous_isolation = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                work(conn)
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.isolation_level = previous_isolation
    finally:
        raw.close()


def _execute_sqlite(active: Engine, path: Path) -> None:
    """Run one SQLite delta inside a single write transaction."""

    def _work(conn: sqlite3.Connection) -> None:
        if path.suffix.lower() == ".py":
            _load_python_upgrade(path)(conn)
        else:
            for statement in split_sqlite_statements(path.read_text(encoding="utf-8")):
                conn.execute(statement)

    _run_sqlite_transaction(active, _work)


def _execute_sql_script(active: Engine, path: Path) -> None:
    """Run one non-SQLite delta inside a single transaction."""
    script = path.read_text(encoding="utf-8")
    with active.begin() as conn:
        conn.exec_driver_sql(script)


@dataclass(frozen=True)
class _BaselineBlock:
    table: str | None
    body: str


def _strip_sql_comments(text: str) -> str:
    """Remove ``--`` and ``/* */`` SQL comments, respecting quoted strings.

    A naive strip would delete a comment marker that happens to appear inside
    a single-quoted string literal (e.g. a default value containing ``--``),
    so this walks the text and only treats ``--``/``/*`` as a comment start
    outside of a quoted string. Doubled single quotes (SQL's escaped quote)
    are handled so an escaped quote does not end the string early.
    """
    result: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    while i < length:
        char = text[i]
        if in_string:
            result.append(char)
            if char == "'":
                if text[i : i + 2] == "''":
                    result.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if char == "'":
            in_string = True
            result.append(char)
            i += 1
            continue
        if text[i : i + 2] == "--":
            newline = text.find("\n", i)
            i = length if newline == -1 else newline
            continue
        if text[i : i + 2] == "/*":
            end = text.find("*/", i + 2)
            i = length if end == -1 else end + 2
            continue
        result.append(char)
        i += 1
    return "".join(result)


def _is_executable_sql(text: str) -> bool:
    """Whether anything is left of ``text`` once comments/whitespace are gone.

    A block whose only content is comments - most commonly the file header
    that precedes the first ``@baseline-block`` marker - carries no schema
    delta. Executing it anyway sends a comment-only statement to the driver;
    psycopg2 refuses that with "query is empty" once its own comment-strip
    yields nothing to run, so such a block must never be scheduled.
    """
    return bool(_strip_sql_comments(text).strip())


def parse_baseline_blocks(script: str) -> list[_BaselineBlock]:
    """Split a baseline file on its ``-- @baseline-block:`` markers.

    A file with no markers is one unconditional block, so a hand-written or
    future baseline still runs. A block - including the preamble before the
    first marker - that is only comments or whitespace carries no schema delta
    and is dropped rather than scheduled as an unconditional no-op statement.
    """
    blocks: list[_BaselineBlock] = []
    current_table: str | None = None
    buffer: list[str] = []

    def _flush() -> None:
        body = "\n".join(buffer).strip()
        if body and _is_executable_sql(body):
            blocks.append(_BaselineBlock(table=current_table, body=body))

    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith(BASELINE_BLOCK_MARKER):
            _flush()
            buffer = []
            selector = stripped[len(BASELINE_BLOCK_MARKER) :].strip()
            current_table = selector[len("table=") :] if selector.startswith("table=") else None
            continue
        buffer.append(line)
    _flush()
    return blocks


def _execute_baseline(active: Engine, path: Path, backend: str) -> str:
    """Provision every table the store does not have yet, with its indexes.

    A table an older store already owns is left entirely alone - the numbered
    deltas are what evolve it - so the baseline is safe on a legacy database
    whose tables predate columns this build declares.
    """
    blocks = parse_baseline_blocks(path.read_text(encoding="utf-8"))
    present = set(inspect(active).get_table_names())
    pending = [block for block in blocks if block.table is None or block.table not in present]
    created = sorted(block.table for block in pending if block.table is not None)

    if pending:
        if backend == "sqlite":
            _run_sqlite_transaction(
                active,
                lambda conn: [
                    conn.execute(statement)
                    for block in pending
                    for statement in split_sqlite_statements(block.body)
                ],
            )
        else:
            with active.begin() as conn:
                for block in pending:
                    conn.exec_driver_sql(block.body)

    return (
        f"executed {backend} baseline schema DDL from {path.name}: "
        f"provisioned {len(created)} of {sum(1 for b in blocks if b.table)} tables"
        + (f" ({', '.join(created)})" if created else "")
    )


def _execute(active: Engine, spec: MigrationSpec, backend: str) -> str:
    if spec.kind == KIND_MARKER:
        return "historical ledger marker; no schema delta to execute"
    path = spec.implementation_for(backend)
    if path is None:  # pragma: no cover - guarded by the caller
        raise MigrationBackendUnsupportedError(
            f"migration {spec.migration_id} has no {backend} implementation"
        )
    if spec.kind == KIND_BASELINE:
        return _execute_baseline(active, path, backend)
    if backend == "sqlite":
        _execute_sqlite(active, path)
    else:
        _execute_sql_script(active, path)
    language = "python" if spec.kind == KIND_PYTHON else "sql"
    return f"executed {backend} {language} delta from {path.name}"


# --------------------------------------------------------------------------- #
# Ledger analysis
# --------------------------------------------------------------------------- #


def _analyze_ledger(
    specs: tuple[MigrationSpec, ...],
    ledger: dict[str, LedgerRow],
    backend: str,
) -> list[MigrationFinding]:
    findings: list[MigrationFinding] = []
    known = {spec.migration_id for spec in specs}

    for migration_id in sorted(set(ledger) - known):
        findings.append(
            MigrationFinding(
                code="unknown_migration",
                severity=SEVERITY_WARNING,
                migration_id=migration_id,
                detail=(
                    "the ledger records a migration this build does not ship; the store "
                    "was written by a different Brains build"
                ),
            )
        )

    highest_settled = -1
    for spec in specs:
        row = ledger.get(spec.migration_id)
        if row is None:
            continue
        status = row.effective_status
        if status in (STATUS_APPLIED, STATUS_SKIPPED):
            highest_settled = max(highest_settled, spec.order)
        if row.backend and row.backend != backend:
            findings.append(
                MigrationFinding(
                    code="backend_mismatch",
                    severity=SEVERITY_WARNING,
                    migration_id=spec.migration_id,
                    detail=(
                        f"recorded against the {row.backend} backend but this store is {backend}"
                    ),
                )
            )
        if status == STATUS_RUNNING:
            findings.append(
                MigrationFinding(
                    code="interrupted_migration",
                    severity=SEVERITY_WARNING,
                    migration_id=spec.migration_id,
                    detail=(
                        f"a previous run started this migration and never finished "
                        f"(attempt {row.attempts or 1}); its transaction rolled back, so "
                        "it is retried"
                    ),
                )
            )
        elif status == STATUS_FAILED:
            recorded = row.error or "no error recorded"
            findings.append(
                MigrationFinding(
                    code="failed_migration",
                    severity=SEVERITY_WARNING,
                    migration_id=spec.migration_id,
                    detail=f"a previous attempt failed and rolled back: {recorded}",
                )
            )

    for spec in specs:
        if spec.migration_id in ledger:
            continue
        if spec.order < highest_settled:
            findings.append(
                MigrationFinding(
                    code="ledger_gap",
                    severity=SEVERITY_WARNING,
                    migration_id=spec.migration_id,
                    detail=(
                        "a later migration is already settled while this one is missing "
                        "from the ledger; it is applied out of its original order"
                    ),
                )
            )
    return findings


def _legacy_adoption(
    active: Engine,
    spec: MigrationSpec,
    backend: str,
    findings: list[MigrationFinding],
) -> str:
    """Resolve one pre-checksum ledger row against the *active* backend.

    The pre-checksum ledger recorded neither a checksum nor a backend, and the
    runner that wrote it only executed deltas on
    :data:`~brains.storage.migration_ledger.LEGACY_EXECUTION_BACKEND`; on every
    other backend it inserted the version as a sentinel without running
    anything. Adoption is therefore backend-aware:

    * No implementation for this backend - the row becomes ``skipped`` with the
      unimplemented identity checksum, so it is not frozen as an immutable
      ``applied`` sentinel and a backend implementation that ships later is
      applicable again rather than a checksum mismatch.
    * An implementation exists and the legacy row could have run it (SQLite, or
      a marker that has no delta at all) - the checksum is adopted and labelled
      ``legacy-adopted``: recorded, not verified.
    * An implementation exists but the legacy row cannot be evidence it ran on
      this backend - the row stays ``applied`` and is labelled
      ``legacy-unproven``. Re-running a delta a store may already carry is not
      safe, so it is reported instead of executed, and the post-migration schema
      verification is what proves the resulting schema.

    Returns the status the row now holds.
    """
    if not spec.supports(backend):
        checksum = spec.checksum_for(backend)
        covered = (
            f"the target state is provisioned by {BASELINE_ID}"
            if spec.baseline_covered
            else "no shipped implementation provisions its target state on this backend"
        )
        adopt_legacy_row(
            active,
            migration_id=spec.migration_id,
            checksum=checksum,
            order=spec.order,
            backend=backend,
            status=STATUS_SKIPPED,
            checksum_origin=CHECKSUM_ORIGIN_LEGACY_UNPROVEN,
            detail=(
                f"pre-checksum ledger row adopted as skipped: this build ships no {backend} "
                f"implementation, so nothing ran here; {covered}"
            ),
        )
        findings.append(
            MigrationFinding(
                code="legacy_backend_unimplemented",
                severity=SEVERITY_INFO if spec.baseline_covered else SEVERITY_WARNING,
                migration_id=spec.migration_id,
                detail=(
                    "the pre-checksum ledger recorded this migration without a backend and "
                    f"this build has no {backend} implementation for it; the row is kept as "
                    f"{STATUS_SKIPPED}/{CHECKSUM_ORIGIN_LEGACY_UNPROVEN} rather than as an "
                    f"applied sentinel, so a {backend} implementation that ships later still "
                    "runs"
                ),
            )
        )
        return STATUS_SKIPPED

    checksum = spec.checksum_for(backend)
    if spec.kind == KIND_MARKER or backend == LEGACY_EXECUTION_BACKEND:
        adopt_legacy_row(
            active,
            migration_id=spec.migration_id,
            checksum=checksum,
            order=spec.order,
            backend=backend,
            status=STATUS_APPLIED,
            checksum_origin=CHECKSUM_ORIGIN_LEGACY,
            detail=(
                "checksum adopted from the current corpus; the pre-checksum ledger "
                "recorded no hash to verify against"
            ),
        )
        findings.append(
            MigrationFinding(
                code="legacy_checksum_adopted",
                severity=SEVERITY_INFO,
                migration_id=spec.migration_id,
                detail=(
                    "the pre-checksum ledger recorded no hash for this migration; the "
                    f"current one was adopted and marked {CHECKSUM_ORIGIN_LEGACY}, which "
                    "is not retroactive proof that this file is what ran"
                ),
            )
        )
        return STATUS_APPLIED

    adopt_legacy_row(
        active,
        migration_id=spec.migration_id,
        checksum=checksum,
        order=spec.order,
        backend=backend,
        status=STATUS_APPLIED,
        checksum_origin=CHECKSUM_ORIGIN_LEGACY_UNPROVEN,
        detail=(
            f"pre-checksum ledger row kept applied on {backend} without proof it ran there; "
            "not re-executed, because re-running a delta a store may already carry is not "
            "safe. The schema verification is the check"
        ),
    )
    findings.append(
        MigrationFinding(
            code="legacy_backend_unverified",
            severity=SEVERITY_WARNING,
            migration_id=spec.migration_id,
            detail=(
                f"the pre-checksum ledger recorded this migration without a backend; only "
                f"{LEGACY_EXECUTION_BACKEND} deltas were executed by that runner, so the row "
                f"is not evidence that the {backend} implementation ran. It is left applied "
                f"and marked {CHECKSUM_ORIGIN_LEGACY_UNPROVEN} rather than re-executed; "
                "the post-migration schema verification is what proves the schema"
            ),
        )
    )
    return STATUS_APPLIED


def _settle_recorded(
    active: Engine,
    spec: MigrationSpec,
    row: LedgerRow,
    backend: str,
    findings: list[MigrationFinding],
) -> tuple[str, bool]:
    """Verify or adopt a settled ledger row. Returns its status and whether it was rewritten."""
    if row.checksum is None:
        return _legacy_adoption(active, spec, backend, findings), True
    expected = spec.checksum_for(row.backend or backend)
    if row.checksum != expected:
        accepted = _PRE_RELEASE_CHECKSUM_COMPAT.get(
            (spec.migration_id, row.backend or backend), frozenset()
        )
        if row.checksum in accepted:
            findings.append(
                MigrationFinding(
                    code="pre_release_checksum_accepted",
                    severity=SEVERITY_WARNING,
                    migration_id=spec.migration_id,
                    detail=(
                        "accepted one exact pre-release SQLite checksum leaked before "
                        "the immutable migration was published; migration "
                        "140_agent_comms_repair converges that draft schema without "
                        "rewriting the ledger"
                    ),
                )
            )
            return row.effective_status, False
        raise MigrationChecksumError(
            f"migration {spec.migration_id} was applied with checksum {row.checksum} but the "
            f"shipped implementation hashes to {expected}. A migration a database has already "
            "applied must never be edited; restore the file and add a new numbered migration "
            "instead."
        )
    return row.effective_status, False


# --------------------------------------------------------------------------- #
# Schema verification
# --------------------------------------------------------------------------- #


def _verify_schema(active: Engine) -> list[str]:
    """Return the model-declared tables/columns the live schema is missing."""
    inspector = inspect(active)
    present = set(inspector.get_table_names())
    missing: list[str] = []
    for table in Base.metadata.tables.values():
        if table.name not in present:
            missing.append(table.name)
            continue
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing.extend(
            f"{table.name}.{column.name}" for column in table.columns if column.name not in columns
        )
    return sorted(missing)


def _drift_error(backend: str, missing: list[str]) -> MigrationSchemaDriftError:
    shown = ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else "")
    return MigrationSchemaDriftError(
        f"the {backend} schema is missing {len(missing)} model-declared object(s) after "
        f"migrating: {shown}. Add a numbered migration for the change; the runner does not "
        "create tables from the installed models."
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def _ensure_ledger_once(active: Engine) -> None:
    if _LEDGER_READY.get(active):
        return
    ensure_ledger(active)
    _LEDGER_READY[active] = True


def run_migrations(*, apply: bool = True, include_ledger: bool = True) -> MigrationReport:
    """Bring the active database to the current schema, or report what is left.

    With ``apply=False`` no delta runs; the ledger is still self-upgraded (that
    is what makes it readable) and a pre-checksum row still has its checksum
    adopted, so a read-only caller sees the same truth the applying caller
    would.

    ``include_ledger=False`` skips materialising the per-migration listing.
    ``init_db`` runs on every entry point and does not need it.
    """
    active = _active_engine()
    _ensure_sqlite_parent_directory(active)
    backend = active.dialect.name
    report = MigrationReport(backend=backend, database=database_identity(active))
    specs = corpus()

    with _RUN_LOCK:
        _ensure_ledger_once(active)
        ledger = read_ledger(active)
        report.findings.extend(_analyze_ledger(specs, ledger, backend))
        adopted = False

        for spec in specs:
            row = ledger.get(spec.migration_id)
            status = row.effective_status if row is not None else None
            settled = status in (STATUS_APPLIED, STATUS_SKIPPED)
            # A migration skipped for lack of a backend implementation becomes
            # applicable the moment that implementation ships.
            reapplicable = status == STATUS_SKIPPED and spec.supports(backend)

            if row is not None and settled and not reapplicable:
                resolved, rewritten = _settle_recorded(active, spec, row, backend, report.findings)
                adopted = adopted or rewritten
                target = report.applied if resolved == STATUS_APPLIED else report.skipped
                target.append(spec.migration_id)
                continue

            if not spec.supports(backend):
                if not spec.baseline_covered:
                    remedy = (
                        f"Ship a {backend} baseline at brains/storage/baseline/{backend}.sql"
                        if spec.kind == KIND_BASELINE
                        else f"Ship {spec.migration_id}.{backend}.sql with an equivalent delta"
                    )
                    raise MigrationBackendUnsupportedError(
                        f"migration {spec.migration_id} has no implementation for the "
                        f"{backend} backend and is not covered by the frozen baseline "
                        f"schema. {remedy} before running Brains on this backend; the "
                        "runner will not record an unexecuted migration as applied."
                    )
                if not apply:
                    report.pending.append(spec.migration_id)
                    continue
                record_skipped(
                    active,
                    migration_id=spec.migration_id,
                    description=spec.description,
                    order=spec.order,
                    checksum=spec.checksum_for(backend),
                    backend=backend,
                    reason=(
                        f"no {backend} implementation; the target state is provisioned by "
                        f"{BASELINE_ID}"
                    ),
                )
                report.skipped.append(spec.migration_id)
                report.executed.append(spec.migration_id)
                continue

            if not apply:
                report.pending.append(spec.migration_id)
                continue

            attempts = ((row.attempts or 0) + 1) if row is not None else 1
            started = begin_attempt(
                active,
                migration_id=spec.migration_id,
                description=spec.description,
                order=spec.order,
                checksum=spec.checksum_for(backend),
                backend=backend,
                attempts=attempts,
            )
            try:
                detail = _execute(active, spec, backend)
            except Exception as exc:
                finish_attempt(
                    active,
                    migration_id=spec.migration_id,
                    status=STATUS_FAILED,
                    started=started,
                    outcome_detail=f"attempt {attempts} rolled back",
                    error=f"{type(exc).__name__}: {exc}",
                )
                report.failed.append(spec.migration_id)
                raise MigrationExecutionError(
                    f"migration {spec.migration_id} failed on the {backend} backend and its "
                    f"transaction was rolled back: {type(exc).__name__}: {exc}"
                ) from exc
            except BaseException as exc:
                finish_attempt(
                    active,
                    migration_id=spec.migration_id,
                    status=STATUS_FAILED,
                    started=started,
                    outcome_detail=f"attempt {attempts} interrupted",
                    error=f"{type(exc).__name__}: interrupted",
                )
                report.failed.append(spec.migration_id)
                raise
            finish_attempt(
                active,
                migration_id=spec.migration_id,
                status=STATUS_APPLIED,
                started=started,
                outcome_detail=detail,
            )
            report.applied.append(spec.migration_id)
            report.executed.append(spec.migration_id)

        if include_ledger:
            final = read_ledger(active) if (report.executed or adopted) else ledger
            report.ledger = [row.to_dict() for row in _ordered_rows(final, specs)]

    if not apply:
        report.schema_verified = not _verify_schema(active)
        return report

    if report.executed or not _SCHEMA_VERIFIED.get(active, False):
        missing = _verify_schema(active)
        if missing:
            _SCHEMA_VERIFIED[active] = False
            raise _drift_error(backend, missing)
    _SCHEMA_VERIFIED[active] = True
    report.schema_verified = True
    return report


def _ordered_rows(
    ledger: dict[str, LedgerRow], specs: tuple[MigrationSpec, ...]
) -> list[LedgerRow]:
    order = {spec.migration_id: spec.order for spec in specs}
    return sorted(ledger.values(), key=lambda row: (order.get(row.version, 10_000), row.version))


def init_db() -> None:
    """Bring the database to the current schema. Safe to call on every entry.

    This is the only startup path. Nothing is created from the installed
    models except the runner's own ledger table.
    """
    run_migrations(apply=True, include_ledger=False)


def migration_status() -> dict[str, Any]:
    """Read-only migration readiness for diagnostics. Never applies a delta."""
    active = _active_engine()
    backend = active.dialect.name
    try:
        report = run_migrations(apply=False)
    except (MigrationError, MigrationCorpusError, LedgerError) as exc:
        return {
            "backend": backend,
            "database": database_identity(active),
            "runner_version": RUNNER_VERSION,
            "checksum_algorithm": CHECKSUM_ALGORITHM,
            "healthy": False,
            "schema_verified": False,
            "error": f"{type(exc).__name__}: {exc}",
            "counts": {},
            "applied": [],
            "skipped": [],
            "pending": [],
            "failed": [],
            "executed_this_run": [],
            "migrations": [],
            "findings": [
                {
                    "code": "runner_refused",
                    "severity": SEVERITY_ERROR,
                    "migration_id": None,
                    "detail": str(exc),
                }
            ],
        }
    payload = report.to_dict()
    payload["error"] = None
    return payload


def current_schema_versions() -> list[str]:
    """Applied migration IDs, oldest first.

    Only ``applied`` rows are returned: a ``skipped`` migration did not run on
    this backend, and a ``failed`` or ``running`` one did not complete, so
    neither is a claim about the schema.
    """
    active = _active_engine()
    _ensure_ledger_once(active)
    ledger = read_ledger(active)
    rows = [row for row in ledger.values() if row.effective_status == STATUS_APPLIED]
    return [row.version for row in sorted(rows, key=lambda row: (str(row.applied_at), row.version))]


def known_migration_ids() -> frozenset[str]:
    """Every migration ID this build ships, for compatibility checks."""
    return frozenset(spec.migration_id for spec in corpus())


def _list_disk_migrations() -> list[Path]:
    """Primary numbered disk migrations in lexical order (historical helper)."""
    return list_disk_migration_files()

"""The migration ledger: ``schema_versions`` reads, writes, and self-upgrade.

The ledger is the runner's own bookkeeping table, so it is created and
upgraded here - before any migration runs and independently of the product
schema. Every write goes through its own short transaction on its own
connection so a row that says ``running`` is durable *before* the delta starts
and a row that says ``applied`` is only written *after* the delta committed.

A store written by the pre-checksum ledger has rows with only
``version``/``description``/``applied_at``. :func:`ensure_ledger` adds the new
columns and marks those rows ``applied`` with ``checksum_origin`` left unset;
the runner then adopts each row once, backend-aware, and records what that
adoption is worth, so the upgrade is explicit rather than a silent claim of
verification.

The pre-checksum ledger recorded no backend, and the runner that wrote it only
ever executed a delta on SQLite: on every other backend it inserted the version
as a sentinel without running anything. A legacy row is therefore evidence of
execution only on :data:`LEGACY_EXECUTION_BACKEND`, which is why adoption
records either :data:`CHECKSUM_ORIGIN_LEGACY` or
:data:`CHECKSUM_ORIGIN_LEGACY_UNPROVEN` instead of one undifferentiated
"adopted".
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Table, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, IntegrityError

from .migration_registry import RUNNER_VERSION
from .models import SchemaVersion

LEDGER_TABLE = "schema_versions"

STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"
STATUS_SKIPPED = "skipped"

CHECKSUM_ORIGIN_RUNNER = "runner"
CHECKSUM_ORIGIN_LEGACY = "legacy-adopted"
#: A legacy row that cannot be evidence that this backend ran this migration.
#: The value is deliberately short: ``checksum_origin`` is a 16-character
#: column in every store this build can meet, and the ledger never truncates a
#: vocabulary word into a different one.
CHECKSUM_ORIGIN_LEGACY_UNPROVEN = "legacy-unproven"

#: The only backend the pre-checksum runner executed deltas on. A legacy row on
#: any other backend was written without running anything.
LEGACY_EXECUTION_BACKEND = "sqlite"

#: Columns added on top of the historical ``(id, version, description,
#: applied_at)`` ledger. Order matters only for readability.
_ADDED_COLUMNS: tuple[str, ...] = (
    "migration_order",
    "checksum",
    "checksum_origin",
    "backend",
    "status",
    "outcome_detail",
    "started_at",
    "completed_at",
    "duration_ms",
    "attempts",
    "error",
    "runner_version",
)

_MAX_ERROR_CHARS = 2000
_MAX_DETAIL_CHARS = 480


class LedgerError(RuntimeError):
    """The ledger table could not be created, upgraded, or written."""


@dataclass(frozen=True)
class LedgerRow:
    version: str
    description: str | None
    applied_at: Any
    migration_order: int | None
    checksum: str | None
    checksum_origin: str | None
    backend: str | None
    status: str | None
    outcome_detail: str | None
    started_at: Any
    completed_at: Any
    duration_ms: int | None
    attempts: int | None
    error: str | None
    runner_version: str | None

    @property
    def effective_status(self) -> str:
        """Legacy rows predate ``status`` and mean 'applied'."""
        return self.status or STATUS_APPLIED

    def to_dict(self) -> dict[str, Any]:
        def _iso(value: Any) -> str | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return value.isoformat()
            return str(value)

        return {
            "migration_id": self.version,
            "description": self.description,
            "status": self.effective_status,
            "backend": self.backend,
            "checksum": self.checksum,
            "checksum_origin": self.checksum_origin,
            "order": self.migration_order,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "applied_at": _iso(self.applied_at),
            "outcome_detail": self.outcome_detail,
            "error": self.error,
            "runner_version": self.runner_version,
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def ensure_ledger(engine: Engine) -> None:
    """Create the ledger table if missing and add any columns it lacks.

    The table object comes from the ORM model rather than hand-written
    per-dialect DDL, because it is the one table the runner owns outright. It
    is excluded from the frozen baseline DDL precisely so there is exactly one
    creator.
    """
    table = cast("Table", SchemaVersion.__table__)
    try:
        table.create(bind=engine, checkfirst=True)
    except DatabaseError as exc:
        # A concurrent process can win the create race; only a genuinely
        # missing table is an error.
        if not inspect(engine).has_table(LEDGER_TABLE):
            raise LedgerError(f"cannot create the {LEDGER_TABLE} ledger: {exc}") from exc

    inspector = inspect(engine)
    existing = {column["name"] for column in inspector.get_columns(LEDGER_TABLE)}
    missing = [name for name in _ADDED_COLUMNS if name not in existing]
    for name in missing:
        column = table.columns[name]
        column_type = column.type.compile(engine.dialect)
        statement = f"ALTER TABLE {LEDGER_TABLE} ADD COLUMN {name} {column_type}"
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(statement)
        except DatabaseError as exc:
            refreshed = {c["name"] for c in inspect(engine).get_columns(LEDGER_TABLE)}
            if name not in refreshed:
                raise LedgerError(
                    f"cannot upgrade the {LEDGER_TABLE} ledger with column {name!r}: {exc}"
                ) from exc

    # A ledger table created before this contract can also be missing its
    # index; an upgraded store has to end up with the same shape as a fresh one.
    for index in table.indexes:
        with contextlib.suppress(DatabaseError):
            index.create(bind=engine, checkfirst=True)

    if missing:
        _adopt_legacy_rows(engine)


def _adopt_legacy_rows(engine: Engine) -> None:
    """Mark pre-checksum rows as applied without claiming they were verified."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {LEDGER_TABLE} SET status = :status, runner_version = :runner "
                "WHERE status IS NULL"
            ),
            {"status": STATUS_APPLIED, "runner": RUNNER_VERSION},
        )


def read_ledger(engine: Engine) -> dict[str, LedgerRow]:
    columns = (
        "version, description, applied_at, migration_order, checksum, checksum_origin, "
        "backend, status, outcome_detail, started_at, completed_at, duration_ms, "
        "attempts, error, runner_version"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT {columns} FROM {LEDGER_TABLE}")).mappings().all()
    return {
        str(row["version"]): LedgerRow(
            version=str(row["version"]),
            description=row["description"],
            applied_at=row["applied_at"],
            migration_order=row["migration_order"],
            checksum=row["checksum"],
            checksum_origin=row["checksum_origin"],
            backend=row["backend"],
            status=row["status"],
            outcome_detail=row["outcome_detail"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            duration_ms=row["duration_ms"],
            attempts=row["attempts"],
            error=row["error"],
            runner_version=row["runner_version"],
        )
        for row in rows
    }


def _insert(engine: Engine, values: dict[str, Any]) -> bool:
    names = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO {LEDGER_TABLE} ({names}) VALUES ({placeholders})"),
                values,
            )
        return True
    except IntegrityError:
        # Another process inserted the same version first.
        return False


def _update(engine: Engine, version: str, values: dict[str, Any]) -> None:
    assignments = ", ".join(f"{name} = :{name}" for name in values)
    payload = dict(values)
    payload["_version"] = version
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE {LEDGER_TABLE} SET {assignments} WHERE version = :_version"),
            payload,
        )


def begin_attempt(
    engine: Engine,
    *,
    migration_id: str,
    description: str,
    order: int,
    checksum: str,
    backend: str,
    attempts: int,
) -> datetime:
    """Durably record that this migration is about to run. Returns its start."""
    started = _now()
    values = {
        "version": migration_id,
        "description": _truncate(description, _MAX_DETAIL_CHARS),
        "applied_at": started,
        "migration_order": order,
        "checksum": checksum,
        "checksum_origin": CHECKSUM_ORIGIN_RUNNER,
        "backend": backend,
        "status": STATUS_RUNNING,
        "outcome_detail": None,
        "started_at": started,
        "completed_at": None,
        "duration_ms": None,
        "attempts": attempts,
        "error": None,
        "runner_version": RUNNER_VERSION,
    }
    if not _insert(engine, values):
        payload = dict(values)
        payload.pop("version")
        _update(engine, migration_id, payload)
    return started


def finish_attempt(
    engine: Engine,
    *,
    migration_id: str,
    status: str,
    started: datetime,
    outcome_detail: str | None = None,
    error: str | None = None,
) -> None:
    completed = _now()
    _update(
        engine,
        migration_id,
        {
            "status": status,
            "applied_at": completed,
            "completed_at": completed,
            "duration_ms": max(0, int((completed - started).total_seconds() * 1000)),
            "outcome_detail": _truncate(outcome_detail, _MAX_DETAIL_CHARS),
            "error": _truncate(error, _MAX_ERROR_CHARS),
        },
    )


def record_skipped(
    engine: Engine,
    *,
    migration_id: str,
    description: str,
    order: int,
    checksum: str,
    backend: str,
    reason: str,
) -> None:
    """Record a migration that has no implementation for this backend.

    ``skipped`` is deliberately not ``applied``: nothing ran, and the ledger
    says so.
    """
    now = _now()
    values = {
        "version": migration_id,
        "description": _truncate(description, _MAX_DETAIL_CHARS),
        "applied_at": now,
        "migration_order": order,
        "checksum": checksum,
        "checksum_origin": CHECKSUM_ORIGIN_RUNNER,
        "backend": backend,
        "status": STATUS_SKIPPED,
        "outcome_detail": _truncate(reason, _MAX_DETAIL_CHARS),
        "started_at": now,
        "completed_at": now,
        "duration_ms": 0,
        "attempts": 0,
        "error": None,
        "runner_version": RUNNER_VERSION,
    }
    if not _insert(engine, values):
        payload = dict(values)
        payload.pop("version")
        _update(engine, migration_id, payload)


def adopt_legacy_row(
    engine: Engine,
    *,
    migration_id: str,
    checksum: str,
    order: int,
    backend: str,
    status: str,
    checksum_origin: str,
    detail: str,
) -> None:
    """Resolve a row the pre-checksum ledger wrote without checksum or backend.

    The caller decides what the legacy evidence is worth for the *active*
    backend: a row for a migration this backend has no implementation for
    becomes :data:`STATUS_SKIPPED` (so a backend implementation that ships later
    is applicable again), and a row that stays :data:`STATUS_APPLIED` records
    whether its checksum is adopted or merely unverified. Nothing is executed
    here; the row is only made to say what is actually known.
    """
    _update(
        engine,
        migration_id,
        {
            "checksum": checksum,
            "checksum_origin": checksum_origin,
            "migration_order": order,
            "backend": backend,
            "status": status,
            "runner_version": RUNNER_VERSION,
            "outcome_detail": _truncate(detail, _MAX_DETAIL_CHARS),
        },
    )

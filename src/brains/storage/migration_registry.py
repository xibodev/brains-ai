"""The migration corpus: stable IDs, immutable checksums, backend applicability.

This module answers one question deterministically: *given a backend, what is
the ordered list of migrations that define the current schema, and what is the
immutable identity of each one?* It never touches a database.

Corpus shape
------------

``0000_baseline``
    The frozen, checked-in DDL that creates the whole product schema, one file
    per backend under ``brains/storage/baseline``. It is generated once from
    the SQLAlchemy models and then never edited: editing it would change the
    checksum recorded in every existing ledger. New schema changes are added
    as new numbered deltas instead.

Ledger markers
    ``0001_initial``, ``0002_schema_versions``, ``104_squads``,
    ``111_recurring_runs`` and ``112_webhook_triggers`` are historical IDs that
    existing ledgers already contain and that never had a delta of their own.
    They are retained as no-op markers so an upgraded store keeps a continuous
    ledger; their checksum is derived from the ID, so it is stable forever.

Numbered disk deltas
    ``NNN_name.py`` (a ``def upgrade(conn)`` taking a raw ``sqlite3``
    connection) or ``NNN_name.sql`` under ``brains/storage/sql_migrations``.
    A backend-specific sibling ``NNN_name.<backend>.sql`` supplies the
    equivalent delta for another backend.

Order is the lexical order of the migration ID, which is stable across
platforms and independent of filesystem iteration order.

Backend applicability
---------------------

Every migration declares the backends it can actually execute on. A migration
with no implementation for the active backend is only allowed to be recorded
as ``skipped`` when its ID is in :data:`BASELINE_COVERED_MIGRATIONS` - the
frozen set of historical SQLite catch-up patches whose target state the
baseline DDL already provisions. Anything else is a hard refusal, so a new
migration cannot be silently recorded as applied on a backend that never ran
it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# Bumped when the ledger contract itself changes, not when a migration is
# added. Persisted per row so a ledger written by an older runner is
# identifiable.
RUNNER_VERSION = "2"

# ``schema_versions.version`` is VARCHAR(32); refusing longer IDs at plan time
# beats truncating them into a backend error later.
MAX_MIGRATION_ID_LENGTH = 32

# Content hashes are taken over newline-normalised UTF-8 so a CRLF checkout
# cannot invalidate a ledger written on a LF checkout.
CHECKSUM_ALGORITHM = "sha256/utf8-lf/1"

BASELINE_ID = "0000_baseline"
BASELINE_DIR = Path(__file__).resolve().parent / "baseline"
SQL_MIGRATIONS_DIR = Path(__file__).resolve().parent / "sql_migrations"

#: The baseline file is organised into blocks. ``table=<name>`` blocks provision
#: one table together with its indexes and are executed only when that table
#: does not exist yet - the frozen-artifact equivalent of ``create_all``'s
#: check-first behaviour. ``always`` blocks carry their own existence guard.
BASELINE_BLOCK_MARKER = "-- @baseline-block:"

#: Backends this build knows how to migrate. Keys are SQLAlchemy dialect names.
SUPPORTED_BACKENDS: tuple[str, ...] = ("sqlite", "postgresql")

#: ``NNN_name.<suffix>.sql`` supplies a backend-specific implementation.
_BACKEND_SUFFIXES: dict[str, str] = {"sqlite": "sqlite", "postgresql": "postgresql"}

_ID_RE = re.compile(r"^[0-9]+_[a-z0-9_]+$")

MIGRATION_ID_RE = _ID_RE


class MigrationCorpusError(RuntimeError):
    """The shipped migration corpus is not a valid, ordered, unique set."""


#: Historical IDs that exist in ledgers but never had a delta of their own.
LEDGER_MARKERS: tuple[tuple[str, str], ...] = (
    ("0001_initial", "initial schema (superseded by 0000_baseline; retained ledger marker)"),
    ("0002_schema_versions", "schema_versions tracking + provider config keys"),
    ("104_squads", "squads + squad_members (leader-routed team assignment)"),
    ("111_recurring_runs", "recurring_runs audit trail (per-fire run records)"),
    ("112_webhook_triggers", "webhook_triggers + webhook_deliveries (inbound trigger front door)"),
)

#: Migrations that ship without a non-SQLite implementation *and* whose target
#: state the frozen baseline DDL provisions on every backend. Frozen: adding an
#: ID here is a deliberate statement that the baseline already expresses the
#: delta, and the post-migration schema verification proves it or fails.
BASELINE_COVERED_MIGRATIONS: frozenset[str] = frozenset(
    {
        "010_hivemind_consolidation",
        "020_rag_chunks_meta",
        "030_help_requests",
        "040_session_resume",
        "050_operators",
        "060_workspace_membership",
        "070_audit_log",
        "080_chunk_embeddings",
        "090_usage_ledger",
        "091_usage_ledger_is_stub",
        "092_usage_ledger_holdout",
        "100_session_machine_id",
        "101_tenant_indexes",
        "102_knowledge_v2",
        "103_code_graph",
        "110_recurring_squad",
        "120_org_workspace",
        "121_session_links",
        "122_enrolment_tokens",
        "123_session_state",
        "124_issue_comments",
        "125_skills",
    }
)

KIND_BASELINE = "baseline"
KIND_MARKER = "marker"
KIND_SQL = "sql"
KIND_PYTHON = "python"


def checksum_text(text: str) -> str:
    """Content hash of ``text`` under :data:`CHECKSUM_ALGORITHM`."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _checksum_file(path: Path) -> str:
    return checksum_text(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class MigrationSpec:
    """One migration, identified by a stable ID and an immutable checksum."""

    migration_id: str
    order: int
    description: str
    kind: str
    #: backend -> implementation file. Empty for :data:`KIND_MARKER`.
    implementations: tuple[tuple[str, str], ...]
    #: backend -> checksum of that implementation.
    checksums: tuple[tuple[str, str], ...]
    baseline_covered: bool

    def implementation_for(self, backend: str) -> Path | None:
        for name, path in self.implementations:
            if name == backend:
                return Path(path)
        return None

    def supports(self, backend: str) -> bool:
        return self.kind == KIND_MARKER or self.implementation_for(backend) is not None

    def checksum_for(self, backend: str) -> str:
        for name, value in self.checksums:
            if name == backend:
                return value
        if self.kind == KIND_MARKER:
            return checksum_text(f"brains-migration-marker/1:{self.migration_id}")
        # A migration with no implementation for this backend is identified by
        # what it *is*, not by a body it never ran, so the recorded checksum
        # stays stable if another backend's file is later added.
        return checksum_text(f"brains-migration-unimplemented/1:{self.migration_id}")

    @property
    def backends(self) -> tuple[str, ...]:
        if self.kind == KIND_MARKER:
            return SUPPORTED_BACKENDS
        return tuple(name for name, _ in self.implementations)


def _split_backend_suffix(stem: str) -> tuple[str, str | None]:
    """``('120_org_workspace', 'postgresql')`` for ``120_org_workspace.postgresql``."""
    head, _, tail = stem.rpartition(".")
    if head and tail in _BACKEND_SUFFIXES:
        return head, _BACKEND_SUFFIXES[tail]
    return stem, None


def _numeric_prefix(name: str) -> str:
    return name.split("_", 1)[0]


def list_disk_migration_files(directory: Path | None = None) -> list[Path]:
    """Primary (default-backend) numbered migration files, in lexical order.

    Backend-specific siblings (``*.postgresql.sql``) are deliberately excluded:
    they are implementations of an existing migration ID, not migrations of
    their own.
    """
    root = SQL_MIGRATIONS_DIR if directory is None else directory
    if not root.exists():
        return []
    out: list[Path] = []
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in (".sql", ".py"):
            continue
        if path.name.startswith("_"):
            continue
        if not _numeric_prefix(path.name).isdigit():
            continue
        if _split_backend_suffix(path.stem)[1] is not None:
            continue
        out.append(path)
    return out


def _discover_disk_specs(directory: Path) -> dict[str, dict[str, Path]]:
    """migration ID -> {backend: implementation path}."""
    found: dict[str, dict[str, Path]] = {}
    seen_primary: dict[str, Path] = {}
    if not directory.exists():
        return found
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in (".sql", ".py"):
            continue
        if path.name.startswith("_"):
            continue
        if not _numeric_prefix(path.name).isdigit():
            continue
        migration_id, backend = _split_backend_suffix(path.stem)
        target = backend or "sqlite"
        bucket = found.setdefault(migration_id, {})
        if target in bucket:
            raise MigrationCorpusError(
                f"duplicate {target} implementation for migration {migration_id!r}: "
                f"{bucket[target].name} and {path.name}"
            )
        bucket[target] = path
        if backend is None:
            previous = seen_primary.get(migration_id)
            if previous is not None:
                raise MigrationCorpusError(
                    f"duplicate migration ID {migration_id!r}: {previous.name} and {path.name}"
                )
            seen_primary[migration_id] = path
    return found


def baseline_path(backend: str) -> Path:
    return BASELINE_DIR / f"{backend}.sql"


def _baseline_spec() -> MigrationSpec:
    implementations: list[tuple[str, str]] = []
    checksums: list[tuple[str, str]] = []
    for backend in SUPPORTED_BACKENDS:
        path = baseline_path(backend)
        if not path.is_file():
            continue
        implementations.append((backend, str(path)))
        checksums.append((backend, _checksum_file(path)))
    if not implementations:
        raise MigrationCorpusError(
            f"no baseline schema DDL found under {BASELINE_DIR}; the migration "
            "contract cannot create a fresh database"
        )
    return MigrationSpec(
        migration_id=BASELINE_ID,
        order=0,
        description="frozen baseline schema DDL",
        kind=KIND_BASELINE,
        implementations=tuple(implementations),
        checksums=tuple(checksums),
        baseline_covered=False,
    )


def build_corpus(directory: Path | None = None) -> tuple[MigrationSpec, ...]:
    """The full ordered corpus. Raises on duplicate or malformed IDs."""
    root = SQL_MIGRATIONS_DIR if directory is None else directory
    specs: dict[str, MigrationSpec] = {}

    baseline = _baseline_spec()
    specs[baseline.migration_id] = baseline

    for migration_id, description in LEDGER_MARKERS:
        if migration_id in specs:
            raise MigrationCorpusError(f"duplicate migration ID {migration_id!r}")
        specs[migration_id] = MigrationSpec(
            migration_id=migration_id,
            order=0,
            description=description,
            kind=KIND_MARKER,
            implementations=(),
            checksums=(),
            baseline_covered=False,
        )

    for migration_id, implementations in _discover_disk_specs(root).items():
        if migration_id in specs:
            raise MigrationCorpusError(
                f"disk migration {migration_id!r} collides with a reserved migration ID"
            )
        primary = implementations.get("sqlite")
        kind = KIND_PYTHON if primary is not None and primary.suffix == ".py" else KIND_SQL
        specs[migration_id] = MigrationSpec(
            migration_id=migration_id,
            order=0,
            description=f"disk migration {migration_id}",
            kind=kind,
            implementations=tuple(
                (backend, str(path)) for backend, path in sorted(implementations.items())
            ),
            checksums=tuple(
                (backend, _checksum_file(path)) for backend, path in sorted(implementations.items())
            ),
            baseline_covered=migration_id in BASELINE_COVERED_MIGRATIONS,
        )

    ordered: list[MigrationSpec] = []
    for index, migration_id in enumerate(sorted(specs)):
        spec = specs[migration_id]
        if len(migration_id) > MAX_MIGRATION_ID_LENGTH:
            raise MigrationCorpusError(
                f"migration ID {migration_id!r} is longer than the "
                f"{MAX_MIGRATION_ID_LENGTH}-character ledger column"
            )
        if not _ID_RE.match(migration_id):
            raise MigrationCorpusError(
                f"migration ID {migration_id!r} must be <digits>_<lower_snake_case>"
            )
        ordered.append(
            MigrationSpec(
                migration_id=spec.migration_id,
                order=index,
                description=spec.description,
                kind=spec.kind,
                implementations=spec.implementations,
                checksums=spec.checksums,
                baseline_covered=spec.baseline_covered,
            )
        )
    if not ordered or ordered[0].migration_id != BASELINE_ID:
        raise MigrationCorpusError(
            f"{BASELINE_ID} must order first; got {ordered[0].migration_id if ordered else 'none'}"
        )
    return tuple(ordered)


_CORPUS_CACHE: dict[tuple[str, tuple[tuple[str, int, int], ...]], tuple[MigrationSpec, ...]] = {}


def _fingerprint(directory: Path) -> tuple[tuple[str, int, int], ...]:
    entries: list[tuple[str, int, int]] = []
    for root in (BASELINE_DIR, directory):
        if not root.exists():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_file():
                continue
            stat = path.stat()
            entries.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


def corpus(directory: Path | None = None) -> tuple[MigrationSpec, ...]:
    """Cached :func:`build_corpus`, invalidated when a migration file changes."""
    root = SQL_MIGRATIONS_DIR if directory is None else directory
    key = (str(root), _fingerprint(root))
    cached = _CORPUS_CACHE.get(key)
    if cached is None:
        cached = build_corpus(root)
        _CORPUS_CACHE.clear()
        _CORPUS_CACHE[key] = cached
    return cached

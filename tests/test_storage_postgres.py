"""Tests for the Postgres storage backend.

Two tiers:

1. Pure-unit tests for ``resolve_db_url`` URL coercion — no live DB needed.
2. Integration tests that hit a real Postgres if ``BRAINS_TEST_PG_URL`` is
   set in the environment, otherwise skipped. The integration suite
   provisions a temporary schema, runs ``init_db`` against it, verifies
   every model table exists, and confirms the ledger records the SQLite
   catch-up patches as ``skipped`` with a reason rather than as applied.
"""

from __future__ import annotations

import contextlib
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from brains.extras import ExtraNotInstalledError
from brains.storage.backends import _coerce_postgres_url, resolve_db_url
from brains.storage.migration_registry import LEDGER_MARKERS

# --- Pure URL coercion -----------------------------------------------------


@pytest.mark.parametrize(
    "input_url, expected",
    [
        (
            "postgresql://u:p@h:5432/db",
            "postgresql+psycopg://u:p@h:5432/db",
        ),
        (
            "postgres://u:p@h/db",
            "postgresql+psycopg://u:p@h/db",
        ),
        (
            "postgresql+psycopg://u:p@h/db",
            "postgresql+psycopg://u:p@h/db",
        ),
        (
            "postgresql+asyncpg://u:p@h/db",
            "postgresql+psycopg://u:p@h/db",
        ),
        (
            "sqlite:///brains.db",
            "sqlite:///brains.db",
        ),
    ],
)
def test_coerce_postgres_url(input_url: str, expected: str) -> None:
    assert _coerce_postgres_url(input_url) == expected


def _settings_stub(backend: str, db_url: str = "sqlite:///brains.db") -> SimpleNamespace:
    return SimpleNamespace(
        subsystems=SimpleNamespace(
            storage=SimpleNamespace(backend=backend),
        ),
        db_url=db_url,
    )


def test_resolve_db_url_sqlite_passthrough() -> None:
    settings_obj = _settings_stub("sqlite", "sqlite:///foo.db")
    assert resolve_db_url(settings_obj) == "sqlite:///foo.db"


def test_resolve_db_url_unknown_backend_rejected() -> None:
    settings_obj = _settings_stub("mysql")
    with pytest.raises(ValueError) as excinfo:
        resolve_db_url(settings_obj)
    assert "Unsupported storage backend" in str(excinfo.value)


def test_resolve_db_url_postgres_without_extra_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    monkeypatch.setitem(sys.modules, "psycopg", None)
    settings_obj = _settings_stub("postgres", "postgresql://u:p@h/db")
    with pytest.raises(ExtraNotInstalledError) as excinfo:
        resolve_db_url(settings_obj)
    assert "pip install 'brains-ai[postgres]'" in str(excinfo.value)


# --- Integration (live Postgres) ------------------------------------------


_PG_URL_ENV = "BRAINS_TEST_PG_URL"
_pg_url = os.environ.get(_PG_URL_ENV)
_pg_skip = pytest.mark.skipif(
    not _pg_url,
    reason=(
        f"set {_PG_URL_ENV} to a Postgres URL to run integration tests "
        "(e.g. postgresql://brains:brains@127.0.0.1:5433/brains_test)"
    ),
)


@pytest.fixture
def pg_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Repoint Brains at a fresh tmp schema inside the test Postgres."""
    pytest.importorskip("psycopg")
    pytest.importorskip("sqlalchemy")
    import yaml

    overlay = tmp_path / "brains.runtime.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "subsystems": {"storage": {"backend": "postgres"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay))
    monkeypatch.setenv("BRAINS_DB_URL", _pg_url or "")

    # Force every storage-touching module to use the new engine.
    from brains import config as config_module
    from brains.storage import db as db_module

    config_module.reload_settings()
    from sqlalchemy import create_engine

    new_url = resolve_db_url(config_module.settings)
    new_engine = create_engine(new_url, future=True)
    monkeypatch.setattr(db_module, "engine", new_engine)
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=new_engine, expire_on_commit=False)
    )

    # Patch the migrations module's references too (they were bound at import).
    from brains.storage import migrations as mig_module

    monkeypatch.setattr(mig_module, "engine", new_engine)
    monkeypatch.setattr(mig_module, "SessionLocal", db_module.SessionLocal)
    # Any module that captured ``SessionLocal`` at import time - repositories,
    # control planes, the CLI - keeps writing to the default database unless it
    # is rebound as well.
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if name.startswith("brains.") and getattr(module, "SessionLocal", None) is not None:
            monkeypatch.setattr(module, "SessionLocal", db_module.SessionLocal, raising=False)
    mig_module.reset_migration_cache()

    # Isolate test in its own schema so it doesn't collide with parallel
    # runs or leftover state.
    schema_name = f"brains_test_{uuid.uuid4().hex[:8]}"
    with new_engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema_name}"')

    # Force all subsequent connections to use this schema by overriding
    # search_path at the connection-pool level.
    from sqlalchemy import event

    @event.listens_for(new_engine, "connect")
    def _set_search_path(dbapi_conn, _connection_record):  # type: ignore[no-redef]
        with dbapi_conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema_name}"')

    try:
        yield
    finally:
        with new_engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{schema_name}" CASCADE')
        new_engine.dispose()
        # Drop the Postgres environment *before* reloading, or the process-wide
        # settings keep pointing at Postgres for every later test in the run.
        monkeypatch.delenv("BRAINS_DB_URL", raising=False)
        monkeypatch.delenv("BRAINS_RUNTIME_OVERLAY", raising=False)
        config_module.reload_settings()


@contextlib.contextmanager
def _pristine_model_metadata() -> Iterator[None]:
    """Run ``create_all`` without leaving a mark on the shared model metadata.

    ``create_all`` breaks the model's foreign-key cycles with
    ``ALTER TABLE ... ADD CONSTRAINT``, and doing so marks those constraints as
    "not to be inlined in a CREATE TABLE" on the process-wide
    ``Base.metadata``. Left in place, every later render or comparison in the
    same test session would silently lose those foreign keys.
    """
    from brains.storage.models import Base

    saved = [
        (constraint, constraint.name, getattr(constraint, "_create_rule", None))
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
    ]
    try:
        yield
    finally:
        for constraint, name, create_rule in saved:
            constraint.name = name
            constraint._create_rule = create_rule


@_pg_skip
def test_init_db_on_postgres_provisions_every_table(pg_settings: None) -> None:
    from sqlalchemy import inspect

    from brains.storage import db as db_module
    from brains.storage.migrations import current_schema_versions, init_db, run_migrations
    from brains.storage.models import Base

    init_db()
    inspector = inspect(db_module.engine)
    declared_tables = set(Base.metadata.tables.keys())
    actual_tables = set(inspector.get_table_names())
    missing = declared_tables - actual_tables
    assert not missing, f"the postgres baseline did not provision: {missing}"

    versions = current_schema_versions()
    # The baseline is the only migration with a Postgres implementation today,
    # and it is the only executed one; the historical markers carry no delta.
    assert set(versions) == {
        "0000_baseline",
        *(migration_id for migration_id, _ in LEDGER_MARKERS),
    }

    report = run_migrations(apply=False)
    assert report.backend == "postgresql"
    assert report.schema_verified is True
    # Every SQLite catch-up patch is recorded as skipped, never as applied:
    # nothing ran for it on this backend.
    assert "010_hivemind_consolidation" in report.skipped
    assert "020_rag_chunks_meta" in report.skipped
    assert "010_hivemind_consolidation" not in versions
    assert "020_rag_chunks_meta" not in versions


@_pg_skip
def test_postgres_ledger_records_backend_and_reason(pg_settings: None) -> None:
    from sqlalchemy import text

    from brains.storage import db as db_module
    from brains.storage.migrations import init_db

    init_db()
    with db_module.engine.connect() as conn:
        rows = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                text("SELECT version, status, backend, outcome_detail FROM schema_versions")
            )
        }
    assert rows["0000_baseline"][0] == "applied"
    assert rows["0000_baseline"][1] == "postgresql"
    status, backend, detail = rows["010_hivemind_consolidation"]
    assert status == "skipped"
    assert backend == "postgresql"
    assert "0000_baseline" in detail


@_pg_skip
def test_init_db_on_postgres_is_idempotent(pg_settings: None) -> None:
    from brains.storage.migrations import current_schema_versions, init_db

    init_db()
    first = current_schema_versions()
    init_db()
    second = current_schema_versions()
    assert first == second, "init_db must be idempotent across runs"


@_pg_skip
def test_repositories_roundtrip_on_postgres(pg_settings: None) -> None:
    """A minimal end-to-end: insert a trace row, read it back."""
    from brains.storage.migrations import init_db
    from brains.storage.repositories import list_traces, write_trace

    init_db()
    write_trace("echo:general", '{"hello": "world"}')
    rows = list_traces(limit=5)
    assert rows, "expected at least one trace row after insert"
    assert any(row.route == "echo:general" for row in rows)


# --- Legacy Postgres stores ------------------------------------------------

#: The four-column ledger the pre-checksum runner wrote, in Postgres syntax.
_LEGACY_PG_LEDGER_DDL = """
CREATE TABLE schema_versions (
    id SERIAL NOT NULL PRIMARY KEY,
    version VARCHAR(32) NOT NULL,
    description VARCHAR(512),
    applied_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    UNIQUE (version)
)
"""

_DUPLICATE_FOREIGN_KEYS = """
SELECT c.conrelid::regclass::text AS relation,
       c.conkey::text AS columns,
       c.confrelid::regclass::text AS target,
       c.confkey::text AS target_columns,
       count(*) AS copies
FROM pg_constraint c
JOIN pg_class rel ON rel.oid = c.conrelid
WHERE c.contype = 'f'
  AND rel.relnamespace = current_schema()::regnamespace
GROUP BY 1, 2, 3, 4
HAVING count(*) > 1
"""

_FOREIGN_KEY_COUNT = """
SELECT count(*)
FROM pg_constraint c
JOIN pg_class rel ON rel.oid = c.conrelid
WHERE c.contype = 'f' AND rel.relnamespace = current_schema()::regnamespace
"""

_INDEX_COUNT = """
SELECT count(*)
FROM pg_index i
JOIN pg_class rel ON rel.oid = i.indrelid
WHERE rel.relnamespace = current_schema()::regnamespace
  AND rel.relname <> 'schema_versions'
"""


def _duplicate_foreign_keys() -> list[tuple]:
    from sqlalchemy import text

    from brains.storage import db as db_module

    with db_module.engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text(_DUPLICATE_FOREIGN_KEYS))]


def _foreign_key_count() -> int:
    from sqlalchemy import text

    from brains.storage import db as db_module

    with db_module.engine.connect() as conn:
        return int(conn.execute(text(_FOREIGN_KEY_COUNT)).scalar_one())


def _index_count() -> int:
    from sqlalchemy import text

    from brains.storage import db as db_module

    with db_module.engine.connect() as conn:
        return int(conn.execute(text(_INDEX_COUNT)).scalar_one())


def _build_legacy_create_all_store() -> None:
    """Reproduce a pre-BL-P0-08 Postgres store.

    The old startup path was ``Base.metadata.create_all`` plus a four-column
    ``schema_versions`` table into which *every* numbered disk migration was
    recorded without running it, because the deltas are SQLite SQL.
    """
    from sqlalchemy import text

    from brains.storage import db as db_module
    from brains.storage.migrations import known_migration_ids
    from brains.storage.models import Base

    with _pristine_model_metadata():
        Base.metadata.create_all(db_module.engine)
    recorded = sorted(known_migration_ids() - {"0000_baseline"})
    with db_module.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE schema_versions")
        conn.exec_driver_sql(_LEGACY_PG_LEDGER_DDL)
        for index, version in enumerate(recorded):
            conn.execute(
                text(
                    "INSERT INTO schema_versions (version, description, applied_at) "
                    "VALUES (:version, :description, :applied_at)"
                ),
                {
                    "version": version,
                    "description": f"disk migration {version}.py skipped on postgresql backend",
                    "applied_at": f"2024-01-01 00:{index:02d}:00",
                },
            )


@_pg_skip
def test_fresh_postgres_baseline_creates_no_duplicate_foreign_keys(pg_settings: None) -> None:
    from brains.storage.migrations import init_db

    init_db()
    assert _duplicate_foreign_keys() == []

    # The always-blocks are re-evaluated on every run; a second pass must not
    # add a second copy of any constraint either.
    from brains.storage import migrations as mig_module

    mig_module.reset_migration_cache()
    init_db()
    assert _duplicate_foreign_keys() == []


@_pg_skip
def test_legacy_create_all_store_gains_no_duplicate_foreign_keys(pg_settings: None) -> None:
    """The baseline's guards must recognise create_all's own ``*_fkey`` names."""
    from sqlalchemy import text

    from brains.storage import db as db_module
    from brains.storage.migrations import init_db

    _build_legacy_create_all_store()
    before = _foreign_key_count()
    before_indexes = _index_count()
    assert before > 0, "the legacy store has no foreign keys to duplicate"

    init_db()

    assert _duplicate_foreign_keys() == []
    assert _foreign_key_count() == before, "the baseline added a redundant foreign key"
    # The same name-independence question applies to every other named object
    # the baseline could re-create over a legacy store. The runner-owned ledger
    # table is excluded: it is rebuilt from its four-column legacy shape here,
    # so it legitimately regains its own index.
    assert _index_count() == before_indexes, "the baseline added a redundant index"

    # And the legacy names are still the ones enforcing those references.
    with db_module.engine.connect() as conn:
        legacy_named = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint c JOIN pg_class rel ON rel.oid = c.conrelid "
                "WHERE c.contype = 'f' AND rel.relnamespace = current_schema()::regnamespace "
                "AND c.conname LIKE '%\\_fkey'"
            )
        ).scalar_one()
    assert legacy_named > 0


@_pg_skip
def test_legacy_postgres_ledger_is_adopted_backend_aware(pg_settings: None) -> None:
    """Rows the old runner recorded without running must not become applied."""
    from sqlalchemy import text

    from brains.storage import db as db_module
    from brains.storage.migration_ledger import (
        CHECKSUM_ORIGIN_LEGACY,
        CHECKSUM_ORIGIN_LEGACY_UNPROVEN,
        CHECKSUM_ORIGIN_RUNNER,
    )
    from brains.storage.migrations import current_schema_versions, init_db, run_migrations

    _build_legacy_create_all_store()
    init_db()

    with db_module.engine.connect() as conn:
        rows = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                text("SELECT version, status, checksum_origin, backend FROM schema_versions")
            )
        }

    markers = {migration_id for migration_id, _ in LEDGER_MARKERS}
    for version, (status, origin, backend) in rows.items():
        assert backend == "postgresql", version
        if version == "0000_baseline":
            assert (status, origin) == ("applied", CHECKSUM_ORIGIN_RUNNER)
        elif version in markers:
            # A marker has no delta on any backend, so adopting it is honest.
            assert (status, origin) == ("applied", CHECKSUM_ORIGIN_LEGACY), version
        else:
            # Nothing ran for these on Postgres, and this build ships no
            # Postgres implementation, so they stay reapplicable.
            assert (status, origin) == ("skipped", CHECKSUM_ORIGIN_LEGACY_UNPROVEN), version

    assert set(current_schema_versions()) == {"0000_baseline", *markers}

    report = run_migrations(apply=False)
    assert report.schema_verified is True
    assert report.healthy is True
    assert "010_hivemind_consolidation" in report.skipped
    assert _duplicate_foreign_keys() == []

    # Stable across runs: no re-execution, and no false checksum mismatch.
    from brains.storage import migrations as mig_module

    mig_module.reset_migration_cache()
    second = run_migrations()
    assert second.executed == []
    assert second.healthy is True

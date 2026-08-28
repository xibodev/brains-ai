"""The reproducible schema-evolution contract (BL-P0-08).

These tests are the evidence for AC-B5-01 and AC-B5-03: a fresh database and
every supported legacy store reach the *same* schema through the migration
contract, a migration is only ever recorded as applied when its backend's
delta ran and committed, and every way the ledger can be wrong - an edited
migration, a gap, an interrupted run, a failed run, a backend with no
implementation - is detected rather than absorbed.

Nothing here touches the operator's database: every test binds the storage
modules to a per-test SQLite file, and the synthetic-corpus tests additionally
bind the migration registry to per-test baseline/migration directories.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from typer.testing import CliRunner

import brains.storage.db as db_module
import brains.storage.migration_registry as registry
import brains.storage.migrations as migrations_module

# Imported at collection time, before any fixture rebinds the engine: these
# modules capture ``SessionLocal`` at import, so importing them later from
# inside a test would permanently bind them to that test's temporary database.
import brains.cli.app  # noqa: F401  isort:skip
import brains.backup  # noqa: F401  isort:skip
import brains.storage.repositories  # noqa: F401  isort:skip
from brains.storage.migration_ledger import (
    CHECKSUM_ORIGIN_LEGACY,
    CHECKSUM_ORIGIN_LEGACY_UNPROVEN,
    CHECKSUM_ORIGIN_RUNNER,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
)
from brains.storage.migrations import (
    MigrationBackendUnsupportedError,
    MigrationChecksumError,
    MigrationCorpusError,
    MigrationExecutionError,
    MigrationSchemaDriftError,
    current_schema_versions,
    init_db,
    known_migration_ids,
    migration_status,
    parse_baseline_blocks,
    run_migrations,
)
from brains.storage.models import Base

# ``040_session_resume`` creates this unique index explicitly while the model
# expresses the same rule as an inline table constraint. It is the one object a
# migrated database has that ``create_all`` alone does not produce, and it
# predates this work.
KNOWN_MIGRATION_ONLY_INDEXES = {("tool_session_links", "ux_tool_session_links_triple")}

HISTORICAL_LEDGER_IDS = (
    "0001_initial",
    "0002_schema_versions",
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
    "104_squads",
    "110_recurring_squad",
    "111_recurring_runs",
    "112_webhook_triggers",
    "120_org_workspace",
    "121_session_links",
    "122_enrolment_tokens",
    "123_session_state",
    "124_issue_comments",
    "125_skills",
    "126_governed_actions",
    "127_signed_head_lease",
    "128_execution_heartbeat",
    "129_api_credentials",
    "130_org_member_backfill",
    "131_api_credential_source",
    "132_realtime_events",
    "133_session_commands",
    "134_pod_persona_roster",
    "135_onboarding_attempts",
    "136_usage_attribution",
    "137_integration_deliveries",
    "138_skill_attachments",
)

#: Deltas shipped in THIS generation that postdate the checksum regime: they
#: must run for real everywhere, never adopted as legacy evidence.
POST_CHECKSUM_DELTAS = (
    "139_agent_comms",
    "140_agent_comms_repair",
    "141_secure_settings",
    "142_session_successor",
    "143_session_leases",
    "144_topic_subscriptions",
    "145_approval_routing",
    "146_feedback_inbox",
)

_LEGACY_LEDGER_DDL = """
CREATE TABLE schema_versions (
    id INTEGER NOT NULL PRIMARY KEY,
    version VARCHAR(32) NOT NULL,
    description VARCHAR(512),
    applied_at DATETIME NOT NULL,
    UNIQUE (version)
)
"""


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _bind(monkeypatch, db_path: Path):
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    session_factory = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", session_factory)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", session_factory)
    # Control and storage modules capture ``SessionLocal`` at import time;
    # rebind those references too, or they keep writing to the shared
    # per-process test database instead of this test's file.
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if name.startswith("brains.") and getattr(module, "SessionLocal", None) is not None:
            monkeypatch.setattr(module, "SessionLocal", session_factory, raising=False)
    migrations_module.reset_migration_cache()
    return engine


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """A per-test SQLite file the storage modules are bound to."""
    db_path = tmp_path / "brains.sqlite"
    _bind(monkeypatch, db_path)
    yield db_path
    migrations_module.reset_migration_cache()


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path))


def _schema_shape(db_path: Path) -> dict[str, tuple]:
    """Tables, columns, foreign keys, and indexes, comparably."""
    conn = _connect(db_path)
    try:
        shape: dict[str, tuple] = {}
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in rows:
            columns = tuple(
                sorted(
                    (row[1], (row[2] or "").upper(), row[3], row[5])
                    for row in conn.execute(f"PRAGMA table_info({name})")
                )
            )
            foreign_keys = tuple(
                sorted(
                    (row[2], row[3], row[4])
                    for row in conn.execute(f"PRAGMA foreign_key_list({name})")
                )
            )
            indexes = tuple(
                sorted(
                    (row[1], row[2])
                    for row in conn.execute(f"PRAGMA index_list({name})")
                    if not row[1].startswith("sqlite_autoindex")
                )
            )
            shape[name] = (columns, foreign_keys, indexes)
        return shape
    finally:
        conn.close()


def _create_all_shape(tmp_path: Path, name: str) -> dict[str, tuple]:
    """The schema ``Base.metadata.create_all`` alone would produce.

    ``create_all`` is used here as comparison scaffolding only - never as a
    provisioning path - so the test can assert that the migration contract
    reaches the schema the models declare.
    """
    reference = tmp_path / name
    engine = create_engine(f"sqlite:///{reference.as_posix()}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        Base.metadata.create_all(engine)
    engine.dispose()
    return _schema_shape(reference)


def _ledger_rows(db_path: Path) -> dict[str, dict]:
    conn = _connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return {
            row["version"]: dict(row)
            for row in conn.execute("SELECT * FROM schema_versions").fetchall()
        }
    finally:
        conn.close()


def _build_legacy_store(db_path: Path, *, recorded: tuple[str, ...], drop: tuple[str, ...] = ()):
    """Reproduce a pre-BL-P0-08 store faithfully.

    The old startup path was ``Base.metadata.create_all`` followed by the
    numbered disk migrations, recorded in a four-column ``schema_versions``
    table with no checksum, backend, or outcome. This rebuilds exactly that,
    without going through the new runner, so the upgrade tests are run against
    a store the previous code would actually have produced.
    """
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        Base.metadata.create_all(engine)
    engine.dispose()

    conn = _connect(db_path)
    try:
        for path in registry.list_disk_migration_files():
            if path.stem not in recorded:
                continue
            spec = importlib.util.spec_from_file_location(f"_legacy_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.upgrade(conn)
        conn.commit()

        conn.execute("DROP TABLE schema_versions")
        conn.execute(_LEGACY_LEDGER_DDL)
        for index, version in enumerate(recorded):
            conn.execute(
                "INSERT INTO schema_versions (version, description, applied_at) VALUES (?, ?, ?)",
                (version, f"legacy row for {version}", f"2024-01-01T00:{index:02d}:00+00:00"),
            )
        for target in drop:
            if "." in target:
                table, column = target.split(".", 1)
                for (index_name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ?",
                    (table,),
                ).fetchall():
                    if index_name and not index_name.startswith("sqlite_autoindex"):
                        info = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
                        if any(row[2] == column for row in info):
                            conn.execute(f"DROP INDEX {index_name}")
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            else:
                conn.execute(f"DROP TABLE {target}")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Fresh SQLite
# --------------------------------------------------------------------------- #


def test_fresh_sqlite_reaches_the_declared_schema(isolated_db, tmp_path):
    report = run_migrations()

    assert report.healthy is True
    assert report.backend == "sqlite"
    assert report.executed[0] == "0000_baseline"
    assert report.skipped == []
    assert report.pending == []
    assert report.failed == []
    assert report.findings == []
    assert set(report.applied) == {"0000_baseline", *HISTORICAL_LEDGER_IDS, *POST_CHECKSUM_DELTAS}

    migrated = _schema_shape(isolated_db)
    declared = _create_all_shape(tmp_path, "declared.sqlite")
    assert set(declared) - set(migrated) == set(), "migrations did not provision a declared table"

    extras = set()
    for table, (columns, foreign_keys, indexes) in migrated.items():
        assert table in declared
        assert columns == declared[table][0]
        assert foreign_keys == declared[table][1]
        extras |= {(table, name) for name, _ in set(indexes) - set(declared[table][2])}
        assert set(declared[table][2]) - set(indexes) == set()
    assert extras == KNOWN_MIGRATION_ONLY_INDEXES


def test_fresh_ledger_records_checksum_backend_and_outcome(isolated_db):
    run_migrations()
    rows = _ledger_rows(isolated_db)

    assert set(rows) == {"0000_baseline", *HISTORICAL_LEDGER_IDS, *POST_CHECKSUM_DELTAS}
    for version, row in rows.items():
        assert row["status"] == STATUS_APPLIED, version
        assert row["backend"] == "sqlite", version
        assert row["checksum"] and len(row["checksum"]) == 64, version
        assert row["checksum_origin"] == CHECKSUM_ORIGIN_RUNNER, version
        assert row["runner_version"] == registry.RUNNER_VERSION, version
        assert row["attempts"] == 1, version
        assert row["started_at"] and row["completed_at"], version
        assert row["duration_ms"] is not None, version
        assert row["error"] is None, version

    order = [rows[version]["migration_order"] for version in sorted(rows)]
    assert order == sorted(order), "ledger order is not the corpus order"
    assert rows["0000_baseline"]["migration_order"] == 0
    assert "baseline schema DDL" in rows["0000_baseline"]["outcome_detail"]
    assert "no schema delta" in rows["0001_initial"]["outcome_detail"]


def test_init_db_is_idempotent_and_does_not_use_create_all(isolated_db, monkeypatch):
    init_db()
    first = current_schema_versions()

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("startup must not provision schema from the installed models")

    monkeypatch.setattr(Base.metadata, "create_all", _forbidden)
    migrations_module.reset_migration_cache()
    init_db()

    assert current_schema_versions() == first
    assert run_migrations().executed == []


def test_concurrent_first_boot_converges(tmp_path, monkeypatch):
    """The supervisor starts gateway, dashboard and MCP against one file."""
    db_path = tmp_path / "concurrent.sqlite"
    _bind(monkeypatch, db_path)
    errors: list[BaseException] = []

    def _boot() -> None:
        try:
            init_db()
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_boot) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    rows = _ledger_rows(db_path)
    assert set(rows) == {"0000_baseline", *HISTORICAL_LEDGER_IDS, *POST_CHECKSUM_DELTAS}
    assert {row["status"] for row in rows.values()} == {STATUS_APPLIED}


# --------------------------------------------------------------------------- #
# Legacy stores
# --------------------------------------------------------------------------- #


def test_legacy_full_ledger_upgrades_without_re_running_deltas(isolated_db, tmp_path):
    """A store at the previous generation: every historical ID, no checksums."""
    _build_legacy_store(isolated_db, recorded=HISTORICAL_LEDGER_IDS)

    report = run_migrations()

    assert report.healthy is True
    # Only the baseline had no row; every historical delta is left alone.
    # Post-checksum deltas of this generation run for real on top.
    assert report.executed == ["0000_baseline", *POST_CHECKSUM_DELTAS]
    assert {finding.code for finding in report.findings} == {
        "ledger_gap",
        "legacy_checksum_adopted",
    }

    rows = _ledger_rows(isolated_db)
    for version in HISTORICAL_LEDGER_IDS:
        assert rows[version]["status"] == STATUS_APPLIED
        assert rows[version]["checksum_origin"] == CHECKSUM_ORIGIN_LEGACY
        assert rows[version]["checksum"]
    assert rows["0000_baseline"]["checksum_origin"] == CHECKSUM_ORIGIN_RUNNER

    fresh = tmp_path / "fresh.sqlite"
    engine = create_engine(f"sqlite:///{fresh.as_posix()}")
    try:
        _run_against(engine)
    finally:
        engine.dispose()
    assert _schema_shape(isolated_db) == _schema_shape(fresh)


def test_legacy_partial_ledger_converges_on_the_same_schema(isolated_db, tmp_path):
    """A store from before the recent deltas, missing their tables and columns."""
    _build_legacy_store(
        isolated_db,
        recorded=HISTORICAL_LEDGER_IDS[: HISTORICAL_LEDGER_IDS.index("100_session_machine_id")],
        drop=(
            "skills",
            "issue_comments",
            "enrolment_tokens",
            "agent_sessions.machine_id",
        ),
    )

    report = run_migrations()

    assert report.healthy is True
    assert "0000_baseline" in report.executed
    assert "125_skills" in report.executed
    assert "100_session_machine_id" in report.executed
    assert "090_usage_ledger" not in report.executed, "an already-recorded delta re-ran"

    conn = _connect(isolated_db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_sessions)")}
    finally:
        conn.close()
    assert "machine_id" in columns, "the delta that adds the column did not run"

    fresh = tmp_path / "fresh-partial.sqlite"
    engine = create_engine(f"sqlite:///{fresh.as_posix()}")
    try:
        _run_against(engine)
    finally:
        engine.dispose()
    assert _schema_shape(isolated_db) == _schema_shape(fresh)


def test_legacy_gap_is_reported_not_absorbed(isolated_db):
    recorded = tuple(v for v in HISTORICAL_LEDGER_IDS if v != "070_audit_log")
    _build_legacy_store(isolated_db, recorded=recorded)

    report = run_migrations()

    gaps = {f.migration_id for f in report.findings if f.code == "ledger_gap"}
    assert "070_audit_log" in gaps
    assert "070_audit_log" in report.executed
    assert report.healthy is True


def _run_against(engine) -> None:  # noqa: ANN001 - SQLAlchemy engine
    """Run the corpus against ``engine`` without disturbing the bound module."""
    previous = migrations_module.engine
    migrations_module.engine = engine
    try:
        migrations_module.reset_migration_cache()
        run_migrations()
    finally:
        migrations_module.engine = previous
        migrations_module.reset_migration_cache()


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_edited_historical_migration_is_refused(isolated_db):
    init_db()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE schema_versions SET checksum = ? WHERE version = ?",
            ("0" * 64, "101_tenant_indexes"),
        )
        conn.commit()
    finally:
        conn.close()

    migrations_module.reset_migration_cache()
    with pytest.raises(MigrationChecksumError) as excinfo:
        init_db()
    assert "101_tenant_indexes" in str(excinfo.value)
    assert "never be edited" in str(excinfo.value)

    status = migration_status()
    assert status["healthy"] is False
    assert status["findings"][0]["code"] == "runner_refused"


def test_only_exact_leaked_139_checksum_is_repaired(isolated_db):
    """The development leak is accepted narrowly and converged by migration 140."""
    init_db()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "INSERT INTO help_requests "
            "(code, subject, question, status, ask_depth, created_at, expires_at) "
            "VALUES ('HR-DRAFT', 'draft', 'draft?', 'open', 1, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00"),
        )
        # Reproduce the pre-release draft shape and ledger state: direct column,
        # no side table/index, and the exact checksum observed on the live store.
        conn.execute("DELETE FROM schema_versions WHERE version = '140_agent_comms_repair'")
        conn.execute("DROP TABLE help_request_constraints")
        conn.execute("DROP INDEX ix_topic_posts_from_workspace_id")
        conn.execute("ALTER TABLE help_requests ADD COLUMN required_tool VARCHAR(64)")
        conn.execute(
            "UPDATE help_requests SET required_tool = 'not:copilot' WHERE code = 'HR-DRAFT'"
        )
        conn.execute(
            "UPDATE schema_versions SET checksum = ? WHERE version = '139_agent_comms'",
            ("af734f5b5ba05f3ff9a6439e6f6e825b7b65bcecf795bd5e94eaefdc48cfb05e",),
        )
        conn.commit()
    finally:
        conn.close()

    migrations_module.reset_migration_cache()
    report = run_migrations()
    assert report.healthy is True
    assert "140_agent_comms_repair" in report.executed
    assert any(f.code == "pre_release_checksum_accepted" for f in report.findings)

    conn = _connect(isolated_db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(help_requests)")}
        assert "required_tool" not in columns
        assert conn.execute(
            "SELECT required_tool FROM help_request_constraints WHERE request_code = 'HR-DRAFT'"
        ).fetchone() == ("not:copilot",)
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(topic_posts)").fetchall()}
        assert "ix_topic_posts_from_workspace_id" in indexes
    finally:
        conn.close()


def test_unknown_139_checksum_is_still_refused(isolated_db):
    init_db()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE schema_versions SET checksum = ? WHERE version = '139_agent_comms'",
            ("f" * 64,),
        )
        conn.commit()
    finally:
        conn.close()

    migrations_module.reset_migration_cache()
    with pytest.raises(MigrationChecksumError, match="139_agent_comms"):
        init_db()


def test_interrupted_migration_is_reported_and_retried(isolated_db):
    init_db()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE schema_versions SET status = ?, completed_at = NULL, attempts = 1 "
            "WHERE version = ?",
            (STATUS_RUNNING, "125_skills"),
        )
        conn.commit()
    finally:
        conn.close()

    migrations_module.reset_migration_cache()
    report = run_migrations()

    assert {f.code for f in report.findings} == {"interrupted_migration"}
    assert "125_skills" in report.executed
    rows = _ledger_rows(isolated_db)
    assert rows["125_skills"]["status"] == STATUS_APPLIED
    assert rows["125_skills"]["attempts"] == 2


def test_failed_migration_is_reported_and_retried(isolated_db):
    init_db()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE schema_versions SET status = ?, error = ?, attempts = 3 WHERE version = ?",
            (STATUS_FAILED, "OperationalError: disk I/O error", "124_issue_comments"),
        )
        conn.commit()
    finally:
        conn.close()

    migrations_module.reset_migration_cache()
    report = run_migrations()

    failed = [f for f in report.findings if f.code == "failed_migration"]
    assert [f.migration_id for f in failed] == ["124_issue_comments"]
    assert "disk I/O error" in failed[0].detail
    rows = _ledger_rows(isolated_db)
    assert rows["124_issue_comments"]["status"] == STATUS_APPLIED
    assert rows["124_issue_comments"]["attempts"] == 4
    assert rows["124_issue_comments"]["error"] is None


def test_unknown_migration_in_the_ledger_is_reported(isolated_db):
    init_db()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "INSERT INTO schema_versions (version, description, applied_at, status, backend) "
            "VALUES ('900_from_the_future', 'newer build', '2030-01-01T00:00:00+00:00', "
            "'applied', 'sqlite')"
        )
        conn.commit()
    finally:
        conn.close()

    migrations_module.reset_migration_cache()
    report = run_migrations()
    unknown = [f for f in report.findings if f.code == "unknown_migration"]
    assert [f.migration_id for f in unknown] == ["900_from_the_future"]


def test_schema_drift_from_an_unmigrated_model_is_refused(isolated_db, monkeypatch):
    from sqlalchemy import Column, Integer, MetaData, Table

    init_db()
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    Table("model_without_a_migration", metadata, Column("id", Integer, primary_key=True))
    monkeypatch.setattr(migrations_module, "Base", SimpleNamespace(metadata=metadata))
    migrations_module.reset_migration_cache()

    with pytest.raises(MigrationSchemaDriftError) as excinfo:
        init_db()
    assert "model_without_a_migration" in str(excinfo.value)
    assert "Add a numbered migration" in str(excinfo.value)


def test_current_schema_versions_reports_only_applied_rows(isolated_db):
    init_db()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE schema_versions SET status = ? WHERE version = ?",
            (STATUS_SKIPPED, "103_code_graph"),
        )
        conn.execute(
            "UPDATE schema_versions SET status = ? WHERE version = ?",
            (STATUS_FAILED, "102_knowledge_v2"),
        )
        conn.commit()
    finally:
        conn.close()

    versions = current_schema_versions()
    assert "103_code_graph" not in versions
    assert "102_knowledge_v2" not in versions
    assert "125_skills" in versions


# --------------------------------------------------------------------------- #
# Corpus validation
# --------------------------------------------------------------------------- #


def test_shipped_corpus_is_ordered_unique_and_checksummed():
    specs = registry.build_corpus()
    ids = [spec.migration_id for spec in specs]

    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    assert ids[0] == "0000_baseline"
    assert set(ids) == {"0000_baseline", *HISTORICAL_LEDGER_IDS, *POST_CHECKSUM_DELTAS}
    for spec in specs:
        assert len(spec.checksum_for("sqlite")) == 64
        assert len(spec.migration_id) <= registry.MAX_MIGRATION_ID_LENGTH


def test_baseline_ships_for_every_supported_backend_and_excludes_the_ledger():
    for backend in registry.SUPPORTED_BACKENDS:
        path = registry.baseline_path(backend)
        assert path.is_file(), backend
        ddl = path.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS schema_versions" not in ddl, backend
        assert "IF NOT EXISTS" in ddl, backend


def test_checksum_is_newline_normalised():
    assert registry.checksum_text("a\r\nb") == registry.checksum_text("a\nb")


def test_ledger_vocabulary_fits_the_columns_it_is_written_to():
    """Every status/origin word must fit its column on a strict backend.

    SQLite ignores ``VARCHAR`` lengths, so a word that is one character too
    long only fails on Postgres - which is exactly the store this contract has
    to be honest about.
    """
    from brains.storage.migration_ledger import (
        CHECKSUM_ORIGIN_LEGACY,
        CHECKSUM_ORIGIN_LEGACY_UNPROVEN,
        CHECKSUM_ORIGIN_RUNNER,
    )
    from brains.storage.models import SchemaVersion

    columns = SchemaVersion.__table__.columns
    vocabulary = {
        "status": (STATUS_APPLIED, STATUS_SKIPPED, STATUS_FAILED, STATUS_RUNNING),
        "checksum_origin": (
            CHECKSUM_ORIGIN_RUNNER,
            CHECKSUM_ORIGIN_LEGACY,
            CHECKSUM_ORIGIN_LEGACY_UNPROVEN,
        ),
        "backend": registry.SUPPORTED_BACKENDS,
        "runner_version": (registry.RUNNER_VERSION,),
    }
    for column_name, words in vocabulary.items():
        limit = columns[column_name].type.length
        for word in words:
            assert len(word) <= limit, f"{word!r} does not fit {column_name} ({limit})"


def test_duplicate_and_malformed_ids_are_refused(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "010_dup.py").write_text("def upgrade(conn):\n    pass\n", encoding="utf-8")
    (directory / "010_dup.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(registry.MigrationCorpusError):
        registry.build_corpus(directory)

    directory2 = tmp_path / "migrations2"
    directory2.mkdir()
    long_id = "010_" + "x" * registry.MAX_MIGRATION_ID_LENGTH
    (directory2 / f"{long_id}.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(registry.MigrationCorpusError):
        registry.build_corpus(directory2)


def test_incomplete_sql_script_is_refused():
    with pytest.raises(MigrationCorpusError):
        migrations_module.split_sqlite_statements("CREATE TABLE t (a INTEGER)")
    assert migrations_module.split_sqlite_statements(
        "-- header\nCREATE TABLE t (a INTEGER);\nCREATE INDEX i ON t (a);\n-- trailer\n"
    ) == ["-- header\nCREATE TABLE t (a INTEGER);", "CREATE INDEX i ON t (a);"]


# --------------------------------------------------------------------------- #
# Synthetic corpus: backend applicability and transactional execution
# --------------------------------------------------------------------------- #


@pytest.fixture
def synthetic_corpus(tmp_path, monkeypatch):
    """A tiny corpus with its own baseline, models, and migration directory.

    Both baselines are written in SQL that SQLite accepts, so the non-SQLite
    decision path can be exercised without a live Postgres: only the dialect
    name the runner reads is changed.
    """
    from sqlalchemy import Column, Integer, MetaData, String, Table

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    ddl = (
        "-- synthetic baseline\n"
        "CREATE TABLE IF NOT EXISTS widgets (\n"
        "    id INTEGER NOT NULL PRIMARY KEY,\n"
        "    name VARCHAR(64),\n"
        "    tint VARCHAR(16)\n"
        ");\n"
    )
    (baseline_dir / "sqlite.sql").write_text(ddl, encoding="utf-8")
    (baseline_dir / "postgresql.sql").write_text(ddl, encoding="utf-8")

    migrations_dir = tmp_path / "sql_migrations"
    migrations_dir.mkdir()

    metadata = MetaData()
    Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(64)),
        Column("tint", String(16)),
    )

    monkeypatch.setattr(registry, "BASELINE_DIR", baseline_dir)
    monkeypatch.setattr(registry, "SQL_MIGRATIONS_DIR", migrations_dir)
    monkeypatch.setattr(registry, "LEDGER_MARKERS", ())
    monkeypatch.setattr(registry, "BASELINE_COVERED_MIGRATIONS", frozenset({"010_covered"}))
    monkeypatch.setattr(migrations_module, "Base", SimpleNamespace(metadata=metadata))
    registry._CORPUS_CACHE.clear()

    db_path = tmp_path / "synthetic.sqlite"
    engine = _bind(monkeypatch, db_path)
    yield SimpleNamespace(
        db_path=db_path,
        engine=engine,
        migrations_dir=migrations_dir,
        baseline_dir=baseline_dir,
    )
    registry._CORPUS_CACHE.clear()
    migrations_module.reset_migration_cache()


def test_non_sqlite_backend_records_skipped_not_applied(synthetic_corpus, monkeypatch):
    (synthetic_corpus.migrations_dir / "010_covered.py").write_text(
        "import sqlite3\n\n\ndef upgrade(conn: sqlite3.Connection) -> None:\n"
        "    conn.execute('CREATE INDEX IF NOT EXISTS ix_widgets_name ON widgets (name)')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")

    report = run_migrations()

    assert report.backend == "postgresql"
    assert report.applied == ["0000_baseline"]
    assert report.skipped == ["010_covered"]
    rows = _ledger_rows(synthetic_corpus.db_path)
    assert rows["010_covered"]["status"] == STATUS_SKIPPED
    assert rows["010_covered"]["backend"] == "postgresql"
    assert "0000_baseline" in rows["010_covered"]["outcome_detail"]
    assert rows["0000_baseline"]["status"] == STATUS_APPLIED
    assert current_schema_versions() == ["0000_baseline"]

    indexes = {
        row[1] for row in _connect(synthetic_corpus.db_path).execute("PRAGMA index_list(widgets)")
    }
    assert "ix_widgets_name" not in indexes, "a skipped migration must not have run"


def test_backend_without_an_implementation_is_refused(synthetic_corpus, monkeypatch):
    (synthetic_corpus.migrations_dir / "020_sqlite_only.py").write_text(
        "import sqlite3\n\n\ndef upgrade(conn: sqlite3.Connection) -> None:\n"
        "    conn.execute('CREATE INDEX IF NOT EXISTS ix_widgets_tint ON widgets (tint)')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")

    with pytest.raises(MigrationBackendUnsupportedError) as excinfo:
        run_migrations()
    message = str(excinfo.value)
    assert "020_sqlite_only" in message
    assert "020_sqlite_only.postgresql.sql" in message

    rows = _ledger_rows(synthetic_corpus.db_path)
    assert "020_sqlite_only" not in rows, "a refused migration must not be recorded"


def test_backend_specific_delta_runs_and_is_recorded_applied(synthetic_corpus, monkeypatch):
    (synthetic_corpus.migrations_dir / "020_sqlite_only.py").write_text(
        "import sqlite3\n\n\ndef upgrade(conn: sqlite3.Connection) -> None:\n"
        "    conn.execute('CREATE INDEX IF NOT EXISTS ix_widgets_tint ON widgets (tint)')\n",
        encoding="utf-8",
    )
    (synthetic_corpus.migrations_dir / "020_sqlite_only.postgresql.sql").write_text(
        "CREATE INDEX IF NOT EXISTS ix_widgets_tint ON widgets (tint);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")

    report = run_migrations()

    assert report.applied == ["0000_baseline", "020_sqlite_only"]
    rows = _ledger_rows(synthetic_corpus.db_path)
    assert rows["020_sqlite_only"]["status"] == STATUS_APPLIED
    assert rows["020_sqlite_only"]["backend"] == "postgresql"
    assert "020_sqlite_only.postgresql.sql" in rows["020_sqlite_only"]["outcome_detail"]
    indexes = {
        row[1] for row in _connect(synthetic_corpus.db_path).execute("PRAGMA index_list(widgets)")
    }
    assert "ix_widgets_tint" in indexes


def test_a_previously_skipped_migration_runs_once_its_backend_ships(synthetic_corpus, monkeypatch):
    (synthetic_corpus.migrations_dir / "010_covered.py").write_text(
        "import sqlite3\n\n\ndef upgrade(conn: sqlite3.Connection) -> None:\n"
        "    conn.execute('CREATE INDEX IF NOT EXISTS ix_widgets_name ON widgets (name)')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")
    assert run_migrations().skipped == ["010_covered"]

    (synthetic_corpus.migrations_dir / "010_covered.postgresql.sql").write_text(
        "CREATE INDEX IF NOT EXISTS ix_widgets_name ON widgets (name);\n",
        encoding="utf-8",
    )
    registry._CORPUS_CACHE.clear()
    migrations_module.reset_migration_cache()

    report = run_migrations()
    assert report.applied == ["0000_baseline", "010_covered"]
    rows = _ledger_rows(synthetic_corpus.db_path)
    assert rows["010_covered"]["status"] == STATUS_APPLIED


def test_a_failing_delta_rolls_back_and_records_the_error(synthetic_corpus):
    (synthetic_corpus.migrations_dir / "030_broken.sql").write_text(
        "CREATE TABLE IF NOT EXISTS gadgets (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO no_such_table (id) VALUES (1);\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationExecutionError) as excinfo:
        run_migrations()
    assert "030_broken" in str(excinfo.value)
    assert "rolled back" in str(excinfo.value)

    conn = _connect(synthetic_corpus.db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "gadgets" not in tables, "the first statement was not rolled back"

    rows = _ledger_rows(synthetic_corpus.db_path)
    assert rows["030_broken"]["status"] == STATUS_FAILED
    assert rows["030_broken"]["attempts"] == 1
    assert "no_such_table" in rows["030_broken"]["error"]
    assert rows["030_broken"]["completed_at"]

    status = migration_status()
    assert status["healthy"] is False
    assert "030_broken" in status["pending"]

    # Fixing the delta lets the retry converge, and the ledger keeps the history.
    (synthetic_corpus.migrations_dir / "030_broken.sql").write_text(
        "CREATE TABLE IF NOT EXISTS gadgets (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    registry._CORPUS_CACHE.clear()
    migrations_module.reset_migration_cache()
    report = run_migrations()
    assert "030_broken" in report.executed
    rows = _ledger_rows(synthetic_corpus.db_path)
    assert rows["030_broken"]["status"] == STATUS_APPLIED
    assert rows["030_broken"]["attempts"] == 2


# --------------------------------------------------------------------------- #
# Legacy Postgres ledgers
# --------------------------------------------------------------------------- #

_SYNTHETIC_WIDGETS_DDL = (
    "CREATE TABLE IF NOT EXISTS widgets ("
    "id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(64), tint VARCHAR(16))"
)


def _build_legacy_backend_store(db_path: Path, *, recorded: tuple[str, ...], extra=()) -> None:
    """A store the pre-checksum runner would have left on a non-SQLite backend.

    The old runner provisioned the schema with ``create_all`` and then inserted
    every numbered disk migration into a four-column ``schema_versions`` table
    *without running it*, because the deltas are SQLite SQL. This reproduces
    exactly that shape: the tables exist, the ledger claims the deltas, no
    checksum or backend is recorded, and nothing ever executed.
    """
    conn = _connect(db_path)
    try:
        conn.execute(_SYNTHETIC_WIDGETS_DDL)
        for statement in extra:
            conn.execute(statement)
        conn.execute("DROP TABLE IF EXISTS schema_versions")
        conn.execute(_LEGACY_LEDGER_DDL)
        for index, version in enumerate(recorded):
            conn.execute(
                "INSERT INTO schema_versions (version, description, applied_at) VALUES (?, ?, ?)",
                (
                    version,
                    f"disk migration {version} skipped on postgresql backend "
                    "(create_all already provisioned the target schema)",
                    f"2024-01-01T00:{index:02d}:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _sqlite_only_delta(directory: Path, migration_id: str, index_name: str) -> None:
    (directory / f"{migration_id}.py").write_text(
        "import sqlite3\n\n\ndef upgrade(conn: sqlite3.Connection) -> None:\n"
        f"    conn.execute('CREATE INDEX IF NOT EXISTS {index_name} ON widgets (name)')\n",
        encoding="utf-8",
    )


def _indexes(db_path: Path) -> set[str]:
    conn = _connect(db_path)
    try:
        return {row[1] for row in conn.execute("PRAGMA index_list(widgets)")}
    finally:
        conn.close()


def _tables(db_path: Path) -> set[str]:
    conn = _connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _spec(migration_id: str):  # noqa: ANN202 - registry MigrationSpec
    return next(spec for spec in registry.corpus() if spec.migration_id == migration_id)


def test_legacy_postgres_row_without_an_implementation_becomes_skipped(
    synthetic_corpus, monkeypatch
):
    """The core of the legacy-Postgres bug: an unexecuted row is not applied.

    The old runner recorded every SQLite delta as applied on Postgres without
    running it. Adopting that row as ``applied`` would freeze the
    "no implementation" identity checksum as an immutable claim, so the delta
    could never run when its Postgres implementation shipped.
    """
    _sqlite_only_delta(synthetic_corpus.migrations_dir, "010_covered", "ix_widgets_name")
    _build_legacy_backend_store(synthetic_corpus.db_path, recorded=("010_covered",))
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")

    report = run_migrations()

    assert report.healthy is True
    assert report.backend == "postgresql"
    assert report.applied == ["0000_baseline"]
    assert report.skipped == ["010_covered"]
    assert "010_covered" not in report.executed, "a legacy row must not re-run a delta"
    assert current_schema_versions() == ["0000_baseline"]

    row = _ledger_rows(synthetic_corpus.db_path)["010_covered"]
    assert row["status"] == STATUS_SKIPPED
    assert row["backend"] == "postgresql"
    assert row["checksum_origin"] == CHECKSUM_ORIGIN_LEGACY_UNPROVEN
    assert row["checksum"] == _spec("010_covered").checksum_for("postgresql")

    finding = next(f for f in report.findings if f.code == "legacy_backend_unimplemented")
    assert finding.migration_id == "010_covered"
    assert "postgresql" in finding.detail
    assert "ix_widgets_name" not in _indexes(synthetic_corpus.db_path)

    # The adopted row is stable: a second run neither rewrites nor refuses it.
    migrations_module.reset_migration_cache()
    second = run_migrations()
    assert second.executed == []
    assert second.skipped == ["010_covered"]
    assert second.healthy is True


def test_legacy_postgres_row_runs_once_when_its_backend_delta_ships(synthetic_corpus, monkeypatch):
    """The whole point of keeping the row skipped: it stays reapplicable."""
    _sqlite_only_delta(synthetic_corpus.migrations_dir, "010_covered", "ix_widgets_name")
    _build_legacy_backend_store(synthetic_corpus.db_path, recorded=("010_covered",))
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")
    assert run_migrations().skipped == ["010_covered"]

    # A delta that is not idempotent: running it twice raises, so a single
    # successful pass is proof it executed exactly once.
    (synthetic_corpus.migrations_dir / "010_covered.postgresql.sql").write_text(
        "CREATE TABLE widget_notes (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )
    registry._CORPUS_CACHE.clear()
    migrations_module.reset_migration_cache()

    report = run_migrations()

    assert report.executed == ["010_covered"]
    assert report.applied == ["0000_baseline", "010_covered"]
    assert "widget_notes" in _tables(synthetic_corpus.db_path)
    row = _ledger_rows(synthetic_corpus.db_path)["010_covered"]
    assert row["status"] == STATUS_APPLIED
    assert row["checksum_origin"] == CHECKSUM_ORIGIN_RUNNER
    assert row["checksum"] == _spec("010_covered").checksum_for("postgresql")
    assert row["attempts"] == 1

    # And it never runs again, and never reports a false checksum mismatch.
    migrations_module.reset_migration_cache()
    third = run_migrations()
    assert third.executed == []
    assert third.applied == ["0000_baseline", "010_covered"]
    assert third.healthy is True


def test_legacy_and_fresh_postgres_stores_converge(synthetic_corpus, monkeypatch, tmp_path):
    """A legacy Postgres store and a fresh one end on the same schema and ledger."""
    _sqlite_only_delta(synthetic_corpus.migrations_dir, "010_covered", "ix_widgets_name")
    _build_legacy_backend_store(synthetic_corpus.db_path, recorded=("010_covered",))
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")
    assert run_migrations().skipped == ["010_covered"]

    # The Postgres implementation ships later, exactly as it would in a release.
    (synthetic_corpus.migrations_dir / "010_covered.postgresql.sql").write_text(
        "CREATE TABLE IF NOT EXISTS widget_notes (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )
    registry._CORPUS_CACHE.clear()
    migrations_module.reset_migration_cache()
    legacy = run_migrations()
    assert legacy.executed == ["010_covered"]
    assert legacy.healthy is True

    fresh_path = tmp_path / "fresh-postgres.sqlite"
    fresh_engine = create_engine(f"sqlite:///{fresh_path.as_posix()}")
    monkeypatch.setattr(fresh_engine.dialect, "name", "postgresql")
    try:
        _run_against(fresh_engine)
    finally:
        fresh_engine.dispose()

    legacy_rows = _ledger_rows(synthetic_corpus.db_path)
    fresh_rows = _ledger_rows(fresh_path)
    assert {v: row["status"] for v, row in legacy_rows.items()} == {
        v: row["status"] for v, row in fresh_rows.items()
    }
    assert {v: row["checksum"] for v, row in legacy_rows.items()} == {
        v: row["checksum"] for v, row in fresh_rows.items()
    }
    assert {v: row["checksum_origin"] for v, row in legacy_rows.items()} == {
        v: row["checksum_origin"] for v, row in fresh_rows.items()
    }
    assert legacy_rows["010_covered"]["checksum_origin"] == CHECKSUM_ORIGIN_RUNNER
    assert _schema_shape(synthetic_corpus.db_path) == _schema_shape(fresh_path)


# --------------------------------------------------------------------------- #
# Baseline block parsing: comment/whitespace preambles must not become an
# unconditional block, while genuinely executable content still runs.
# --------------------------------------------------------------------------- #


def test_comment_only_preamble_before_the_first_marker_is_dropped():
    """A file header of pure comments must not become a ``table=None`` block.

    ``table=None`` blocks run unconditionally on every migration, so a header
    surviving as one would send a comment-only statement to the driver on
    every store, every time.
    """
    script = (
        "-- Brains frozen baseline schema.\n"
        "--\n"
        "-- Migration ID: 0000_baseline\n"
        "\n"
        "-- @baseline-block: table=widgets\n"
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);\n"
    )

    blocks = parse_baseline_blocks(script)

    assert len(blocks) == 1
    assert blocks[0].table == "widgets"


def test_whitespace_only_preamble_before_the_first_marker_is_dropped():
    script = (
        "\n   \n\t\n"
        "-- @baseline-block: table=widgets\n"
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);\n"
    )

    blocks = parse_baseline_blocks(script)

    assert len(blocks) == 1
    assert blocks[0].table == "widgets"


def test_block_comment_only_preamble_before_the_first_marker_is_dropped():
    script = (
        "/* Brains frozen baseline schema.\n"
        "   Migration ID: 0000_baseline */\n"
        "-- @baseline-block: table=widgets\n"
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);\n"
    )

    blocks = parse_baseline_blocks(script)

    assert len(blocks) == 1
    assert blocks[0].table == "widgets"


def test_executable_preamble_before_the_first_marker_still_runs():
    """A preamble that carries real SQL - not just comments - must be kept."""
    script = (
        "-- session-scoped pragmas\n"
        "PRAGMA foreign_keys = ON;\n"
        "\n"
        "-- @baseline-block: table=widgets\n"
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);\n"
    )

    blocks = parse_baseline_blocks(script)

    assert len(blocks) == 2
    assert blocks[0].table is None
    assert "PRAGMA foreign_keys" in blocks[0].body
    assert blocks[1].table == "widgets"


def test_unmarked_executable_baseline_file_is_still_one_unconditional_block():
    """A hand-written baseline with no ``@baseline-block`` markers still runs."""
    script = "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);\n"

    blocks = parse_baseline_blocks(script)

    assert len(blocks) == 1
    assert blocks[0].table is None
    assert "CREATE TABLE" in blocks[0].body


def test_unmarked_comment_only_baseline_file_yields_no_blocks():
    """A baseline that is nothing but comments schedules nothing to execute."""
    script = "-- nothing here yet\n-- placeholder\n"

    assert parse_baseline_blocks(script) == []


def test_always_blocks_survive_parsing_alongside_a_dropped_header():
    """``always`` blocks (their own selector, not ``table=``) are unaffected."""
    script = (
        "-- Brains frozen baseline schema.\n"
        "-- Migration ID: 0000_baseline\n"
        "\n"
        "-- @baseline-block: table=widgets\n"
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);\n"
        "\n"
        "-- @baseline-block: always\n"
        "DO $$\nBEGIN\n    NULL;\nEND\n$$;\n"
    )

    blocks = parse_baseline_blocks(script)

    assert len(blocks) == 2
    assert blocks[0].table == "widgets"
    assert blocks[1].table is None
    assert "DO $$" in blocks[1].body


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_real_baselines_parse_without_a_header_block(backend):
    """The shipped baselines' file header must not survive parsing as a block."""
    ddl = registry.baseline_path(backend).read_text(encoding="utf-8")

    blocks = parse_baseline_blocks(ddl)

    assert blocks, f"the {backend} baseline produced no blocks at all"
    assert blocks[0].table is not None, (
        f"the {backend} baseline's file header survived parsing as an unconditional block"
    )
    table_blocks = [block for block in blocks if block.table is not None]
    assert table_blocks, f"the {backend} baseline has no table= blocks"


def test_real_postgres_baseline_keeps_its_always_guarded_blocks():
    """The Postgres baseline's guarded foreign-key blocks still parse as ``always``."""
    ddl = registry.baseline_path("postgresql").read_text(encoding="utf-8")

    blocks = parse_baseline_blocks(ddl)
    always_blocks = [
        block for block in blocks if block.table is None and "ADD CONSTRAINT" in block.body
    ]

    assert always_blocks, "the postgres baseline has no surviving always/foreign-key blocks"
    for block in always_blocks:
        assert "DO $$" in block.body
        assert "IF NOT EXISTS" in block.body


class _FakeEmptyQueryError(Exception):
    """Stands in for ``psycopg2.ProgrammingError: can't execute an empty query``."""


def test_baseline_header_is_never_sent_to_a_comment_refusing_driver(synthetic_corpus, monkeypatch):
    """A psycopg2-like refusal of comment-only queries never fires for the header.

    Real psycopg2 raises ``ProgrammingError: can't execute an empty query`` for
    any statement that becomes empty once comments are stripped. A baseline
    header wrongly scheduled as an unconditional ``table=None`` block would
    trip that refusal on the very first Postgres migration run, for every
    store. This monkeypatches ``Connection.exec_driver_sql`` to reproduce that
    refusal and proves the header is never handed to it.
    """
    header = (
        "-- Brains frozen baseline schema for the postgresql backend.\n"
        "--\n"
        "-- Migration ID: 0000_baseline\n"
        "--\n"
        "-- Generated once from the SQLAlchemy models; do not edit by hand.\n"
        "\n"
    )
    ddl = (
        header + "-- @baseline-block: table=widgets\n" + "CREATE TABLE IF NOT EXISTS widgets (\n"
        "    id INTEGER NOT NULL PRIMARY KEY,\n"
        "    name VARCHAR(64),\n"
        "    tint VARCHAR(16)\n"
        ");\n"
    )
    (synthetic_corpus.baseline_dir / "postgresql.sql").write_text(ddl, encoding="utf-8")
    registry._CORPUS_CACHE.clear()
    migrations_module.reset_migration_cache()
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")

    from sqlalchemy.engine import Connection

    original_exec_driver_sql = Connection.exec_driver_sql

    def _refuse_comment_only_statements(self, statement, *args, **kwargs):  # noqa: ANN001
        stripped = "\n".join(
            line
            for line in statement.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ).strip()
        if not stripped:
            raise _FakeEmptyQueryError("can't execute an empty query")
        return original_exec_driver_sql(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", _refuse_comment_only_statements)

    report = run_migrations()

    assert report.healthy is True
    assert report.applied == ["0000_baseline"]
    assert current_schema_versions() == ["0000_baseline"]
    assert _tables(synthetic_corpus.db_path) == {"widgets", "schema_versions"}


def test_legacy_postgres_row_with_an_implementation_is_not_re_executed(
    synthetic_corpus, monkeypatch
):
    """The ambiguous case: recorded before backends were tracked, delta exists.

    Re-running a delta a store may already carry is not safe, and claiming the
    row is verified would be a lie, so the row stays applied and is labelled
    ``legacy-unproven`` with a warning.
    """
    _sqlite_only_delta(synthetic_corpus.migrations_dir, "020_dual", "ix_widgets_name")
    (synthetic_corpus.migrations_dir / "020_dual.postgresql.sql").write_text(
        "CREATE TABLE widget_notes (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )
    _build_legacy_backend_store(synthetic_corpus.db_path, recorded=("020_dual",))
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")
    registry._CORPUS_CACHE.clear()

    report = run_migrations()

    assert report.applied == ["0000_baseline", "020_dual"]
    assert "020_dual" not in report.executed, "an adopted row must never be executed twice"
    assert "widget_notes" not in _tables(synthetic_corpus.db_path)

    row = _ledger_rows(synthetic_corpus.db_path)["020_dual"]
    assert row["status"] == STATUS_APPLIED
    assert row["checksum_origin"] == CHECKSUM_ORIGIN_LEGACY_UNPROVEN
    assert row["checksum"] == _spec("020_dual").checksum_for("postgresql")

    finding = next(f for f in report.findings if f.code == "legacy_backend_unverified")
    assert finding.severity == "warning"
    assert "not evidence" in finding.detail

    migrations_module.reset_migration_cache()
    second = run_migrations()
    assert second.executed == []
    assert [f.code for f in second.findings if f.code.startswith("legacy_")] == []


def test_legacy_sqlite_row_with_an_implementation_is_adopted_not_downgraded(synthetic_corpus):
    """The same shape on SQLite is evidence: the old runner did execute there."""
    _sqlite_only_delta(synthetic_corpus.migrations_dir, "010_covered", "ix_widgets_name")
    _build_legacy_backend_store(
        synthetic_corpus.db_path,
        recorded=("010_covered",),
        extra=("CREATE INDEX IF NOT EXISTS ix_widgets_name ON widgets (name)",),
    )

    report = run_migrations()

    assert report.applied == ["0000_baseline", "010_covered"]
    assert "010_covered" not in report.executed
    row = _ledger_rows(synthetic_corpus.db_path)["010_covered"]
    assert row["status"] == STATUS_APPLIED
    assert row["checksum_origin"] == CHECKSUM_ORIGIN_LEGACY
    assert row["checksum"] == _spec("010_covered").checksum_for("sqlite")
    assert {f.code for f in report.findings} >= {"legacy_checksum_adopted"}


def test_legacy_postgres_ledger_reports_unknown_rows_clearly(synthetic_corpus, monkeypatch):
    """A row from a build this one does not ship is surfaced, never adopted."""
    _sqlite_only_delta(synthetic_corpus.migrations_dir, "010_covered", "ix_widgets_name")
    _build_legacy_backend_store(
        synthetic_corpus.db_path, recorded=("010_covered", "900_from_the_future")
    )
    monkeypatch.setattr(synthetic_corpus.engine.dialect, "name", "postgresql")

    report = run_migrations()

    unknown = [f for f in report.findings if f.code == "unknown_migration"]
    assert [f.migration_id for f in unknown] == ["900_from_the_future"]
    assert "different Brains build" in unknown[0].detail
    assert "900_from_the_future" not in known_migration_ids()

    row = _ledger_rows(synthetic_corpus.db_path)["900_from_the_future"]
    assert row["checksum"] is None, "an unknown row must never be given a checksum"
    assert row["checksum_origin"] is None
    assert row["backend"] is None
    # It still counts as applied schema, so the backup gate refuses an archive
    # from that build instead of restoring it silently.
    assert "900_from_the_future" in current_schema_versions()

    status = migration_status()
    listed = {entry["migration_id"]: entry for entry in status["migrations"]}
    assert listed["900_from_the_future"]["checksum"] is None
    assert any(f["code"] == "unknown_migration" for f in status["findings"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_db_migrations_cli_reports_readiness(isolated_db):
    from brains.cli.app import app as cli_app

    runner = CliRunner()
    before = runner.invoke(cli_app, ["db", "migrations"])
    assert before.exit_code == 1, before.output
    pending = json.loads(before.stdout)
    assert pending["healthy"] is False
    assert "0000_baseline" in pending["pending"]
    assert pending["database"].startswith("sqlite:///")

    applied = runner.invoke(cli_app, ["db", "migrate"])
    assert applied.exit_code == 0, applied.output
    expected_count = 1 + len(HISTORICAL_LEDGER_IDS) + len(POST_CHECKSUM_DELTAS)
    assert json.loads(applied.stdout)["counts"]["executed_this_run"] == expected_count

    after = runner.invoke(cli_app, ["db", "migrations"])
    assert after.exit_code == 0, after.output
    payload = json.loads(after.stdout)
    assert payload["healthy"] is True
    assert payload["schema_verified"] is True
    assert payload["counts"]["applied"] == expected_count
    assert payload["runner_version"] == registry.RUNNER_VERSION
    assert all(row["checksum"] for row in payload["migrations"])


def test_database_identity_never_exposes_credentials(monkeypatch):
    from sqlalchemy.engine import make_url

    postgres = SimpleNamespace(
        url=make_url("postgresql+psycopg://brains:sup3rsecret@db.internal:5432/brains"),
        dialect=SimpleNamespace(name="postgresql"),
    )
    identity = migrations_module.database_identity(postgres)

    assert identity == "postgresql://db.internal:5432/brains"
    assert "sup3rsecret" not in identity
    assert "brains:" not in identity


def test_db_migrations_cli_output_carries_no_credentials(isolated_db):
    from brains.cli.app import app as cli_app

    init_db()
    result = CliRunner().invoke(cli_app, ["db", "migrations"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["database"] == f"sqlite:///{isolated_db.as_posix()}"
    assert "password" not in result.output.lower()


def test_db_repair_refuses_an_unsettled_ledger(isolated_db):
    from brains.cli.app import app as cli_app

    init_db()
    conn = _connect(isolated_db)
    try:
        conn.execute("DELETE FROM schema_versions WHERE version = '125_skills'")
        conn.commit()
    finally:
        conn.close()
    migrations_module.reset_migration_cache()

    result = CliRunner().invoke(
        cli_app,
        ["db", "repair", "--apply", "--backup-to", str(isolated_db.parent / "gate.tar.gz")],
    )
    assert result.exit_code == 2
    assert "not settled" in result.output


# --------------------------------------------------------------------------- #
# Backup compatibility
# --------------------------------------------------------------------------- #


def test_backup_restore_refuses_a_newer_store(isolated_db, tmp_path, monkeypatch):
    from brains import backup as backup_module

    init_db()
    monkeypatch.setattr(
        backup_module, "_current_db_url", lambda: f"sqlite:///{isolated_db.as_posix()}"
    )
    archive = tmp_path / "newer.tar.gz"
    monkeypatch.setattr(
        backup_module,
        "_current_schema_versions",
        lambda: [*current_schema_versions(), "900_from_the_future"],
    )
    backup_module.create_backup(archive)

    with pytest.raises(backup_module.SchemaIncompatible) as excinfo:
        backup_module.restore_backup(
            archive, target_url=f"sqlite:///{(tmp_path / 'restored.db').as_posix()}"
        )
    assert "900_from_the_future" in str(excinfo.value)

    verification = backup_module.verify_backup(str(archive))
    assert verification.ok is False
    assert verification.checks["schema_compatibility"]["unknown_migrations"] == [
        "900_from_the_future"
    ]


def test_backup_restore_accepts_a_matching_store(isolated_db, tmp_path, monkeypatch):
    from brains import backup as backup_module
    from brains.storage.repositories import write_trace

    init_db()
    monkeypatch.setattr(
        backup_module, "_current_db_url", lambda: f"sqlite:///{isolated_db.as_posix()}"
    )
    write_trace("echo:general", '{"hello": "world"}')

    archive = tmp_path / "matching.tar.gz"
    result = backup_module.create_backup(archive)
    assert "0000_baseline" in result.schema_versions

    restored = tmp_path / "restored.sqlite"
    backup_module.restore_backup(archive, target_url=f"sqlite:///{restored.as_posix()}")
    assert _schema_shape(restored) == _schema_shape(isolated_db)

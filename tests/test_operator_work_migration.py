"""Real 155 -> 156 upgrades preserve history with SQLite FK enforcement enabled."""

from __future__ import annotations

import importlib
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.orm import sessionmaker

from brains.storage import migration_registry, migrations
from brains.storage.models import (
    AgentSession,
    Base,
    CoordinationProposal,
    Operator,
    Workspace,
)

MIGRATION = "156_operator_work_authorship"
delta = importlib.import_module(f"brains.storage.sql_migrations.{MIGRATION}")
PARENTS = ("work_assignments", "coordination_proposals")
CHILDREN = ("work_assignment_attempts", "coordination_contributions")
STAMP = "2030-01-01 00:00:00"


def _insert(conn, table, row):
    names = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(row.values()))


def _rows(conn, table):
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _schema(conn):
    return conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()


@pytest.fixture(scope="module")
def pre_operator_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("operator-migration") / "through155.sqlite"
    engine = create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(engine, "connect")
    def enable_fk(conn, record):
        conn.execute("PRAGMA foreign_keys = ON")

    factory = sessionmaker(bind=engine)
    previous = MetaData()
    for table in Base.metadata.sorted_tables:
        historical = table.to_metadata(previous)
        if table.name in PARENTS:
            historical._columns.remove(historical.c.creator_kind)
            historical.c.creator_session_id.nullable = False
            historical.constraints = {
                constraint
                for constraint in historical.constraints
                if constraint.name not in {"ck_work_assignments_creator", "ck_coordination_creator"}
            }
    corpus = tuple(
        spec for spec in migration_registry.build_corpus() if spec.migration_id < MIGRATION
    )
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(migrations, "engine", engine)
            patch.setattr(migrations, "SessionLocal", factory)
            patch.setattr(migrations, "Base", SimpleNamespace(metadata=previous))
            patch.setattr(migrations, "corpus", lambda: corpus)
            migrations.reset_migration_cache()
            report = migrations.run_migrations()
            assert report.healthy
            assert report.executed[-3:] == [
                "153_mailbox_identity_lifecycle",
                "154_work_assignments",
                "155_peer_coordination",
            ]
        with factory() as session:
            session.add(Operator(id=901, slug="synthetic-author"))
            session.flush()
            session.add(Workspace(id=901, slug="synthetic", path="/synthetic/authorship"))
            session.flush()
            session.add_all(
                AgentSession(id=name, workspace_id=901, tool="codex", created_by_operator_id=901)
                for name in ("requester", "peer")
            )
            session.commit()
        with sqlite3.connect(path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            _insert(
                conn,
                "work_assignments",
                dict(
                    code="WA-preserved",
                    workspace_id=901,
                    creator_operator_id=901,
                    creator_session_id="requester",
                    idempotency_key="original-work-key",
                    request_hash="a" * 64,
                    title="Preserve this work",
                    spec_version=1,
                    specification_json='{"context": ["opaque"], "unicode": "é"}',
                    specification_hash="b" * 64,
                    status="uncertain",
                    revision=7,
                    generation=2,
                    created_at=STAMP,
                    updated_at=STAMP,
                    cancel_requested_at=STAMP,
                    cancel_requested_by_session_id="requester",
                ),
            )
            for generation, status in ((1, "completed"), (2, "uncertain")):
                _insert(
                    conn,
                    "work_assignment_attempts",
                    dict(
                        attempt_id=f"attempt-{generation}",
                        assignment_code="WA-preserved",
                        generation=generation,
                        source_session_id="peer",
                        tool="codex",
                        status=status,
                        accepted_at=STAMP,
                        deadline_at=STAMP,
                        max_runtime_seconds=60,
                        settled_at=STAMP if generation == 1 else None,
                        evidence="original evidence",
                        result="original result",
                        usage_json='{"tokens": 17}',
                    ),
                )
            for version in (1, 2):
                _insert(
                    conn,
                    "coordination_proposals",
                    dict(
                        code="CO-preserved",
                        version=version,
                        workspace_id=901,
                        creator_operator_id=901,
                        creator_session_id="requester",
                        result_owner_session_id="peer",
                        idempotency_key=f"proposal-key-{version}",
                        request_hash="c" * 64,
                        title="Preserve proposal history",
                        specification_json='{"participants": ["requester", "peer"]}',
                        specification_hash="d" * 64,
                        status="cancelled" if version == 1 else "collecting",
                        revision=version * 4,
                        round=0,
                        initial_closed=0,
                        deadline_at=STAMP,
                        created_at=STAMP,
                        updated_at=STAMP,
                        cancellation_reason="replaced" if version == 1 else None,
                    ),
                )
                _insert(
                    conn,
                    "coordination_contributions",
                    dict(
                        contribution_id=f"contribution-{version}",
                        proposal_code="CO-preserved",
                        version=version,
                        author_session_id="peer",
                        kind="initial",
                        round=0,
                        slot="initial:0:peer",
                        idempotency_key=f"contribution-key-{version}",
                        payload_json='{"body": "original", "dissent": ["keep"]}',
                        request_hash="e" * 64,
                        created_at=STAMP,
                    ),
                )
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        engine.dispose()
        migrations.reset_migration_cache()
    return path


@pytest.fixture
def old_db(pre_operator_template, tmp_path):
    path = tmp_path / "upgrade.sqlite"
    with sqlite3.connect(pre_operator_template) as source, sqlite3.connect(path) as target:
        source.backup(target)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("enforced", [False, True])
def test_upgrade_preserves_history_constraints_and_indexes(old_db, enforced):
    conn = old_db
    conn.execute(f"PRAGMA foreign_keys = {int(enforced)}")
    # An additional installed index must survive along with the shipped ones.
    conn.execute("CREATE INDEX synthetic_work_title ON work_assignments(title)")
    tables = (*PARENTS, *CHILDREN, "agent_sessions", "operators", "schema_versions")
    before = {table: _rows(conn, table) for table in tables}
    foreign_keys = {
        table: conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        for table in (*PARENTS, *CHILDREN)
    }
    indexes = [row for row in _schema(conn) if row[0] == "index"]
    conn.execute("BEGIN IMMEDIATE")
    delta.upgrade(conn)
    assert conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (int(enforced),)
    conn.commit()  # Catch deferred-FK counters as well as immediate violations.
    for table in tables:
        rows = _rows(conn, table)
        if table in PARENTS:
            assert all(row.pop("creator_kind") == "session" for row in rows)
        assert rows == before[table]
    for table, keys in foreign_keys.items():
        assert conn.execute(f"PRAGMA foreign_key_list({table})").fetchall() == keys
    assert [row for row in _schema(conn) if row[0] == "index"] == indexes
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reference = create_engine("sqlite://")
    try:
        Base.metadata.create_all(reference)
        with reference.connect() as expected:
            for table in PARENTS:
                assert conn.execute(f"PRAGMA table_info({table})").fetchall() == [
                    tuple(row) for row in expected.exec_driver_sql(f"PRAGMA table_info({table})")
                ]
    finally:
        reference.dispose()
    for table in PARENTS:
        columns = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        assert columns["creator_session_id"][3] == 0
        assert columns["creator_kind"][3:5] == (1, "'session'")
    snapshot = _schema(conn)
    delta.upgrade(conn)
    assert _schema(conn) == snapshot


@pytest.mark.parametrize("table", PARENTS)
def test_operator_authors_and_default_session_authors(old_db, table):
    delta.upgrade(old_db)
    row = _rows(old_db, table)[0]
    row.update(code="new-operator", idempotency_key="new-operator", creator_kind="operator")
    row["creator_session_id"] = None
    _insert(old_db, table, row)
    row.update(code="new-session", idempotency_key="new-session", creator_session_id="requester")
    del row["creator_kind"]
    _insert(old_db, table, row)
    assert old_db.execute(
        f"SELECT creator_kind FROM {table} WHERE code='new-session'"
    ).fetchone() == ("session",)
    old_db.commit()
    assert old_db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("table", PARENTS)
@pytest.mark.parametrize(
    ("kind", "session_id"),
    [
        ("session", None),
        ("operator", "requester"),
        ("unknown", None),
        ("unknown", "requester"),
        (None, None),
        (None, "requester"),
    ],
)
def test_mixed_or_missing_authorship_is_rejected(old_db, table, kind, session_id):
    delta.upgrade(old_db)
    with pytest.raises(sqlite3.IntegrityError):
        old_db.execute(
            f"UPDATE {table} SET creator_kind=?, creator_session_id=?", (kind, session_id)
        )


def test_original_identity_active_attempt_and_contribution_rules_survive(old_db):
    delta.upgrade(old_db)
    for table in (*PARENTS, *CHILDREN):
        row = _rows(old_db, table)[0]
        # Change only the PK: the original idempotency/generation/slot still conflicts.
        row[next(iter(row))] = "duplicate"
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            _insert(old_db, table, row)
    live = _rows(old_db, "work_assignment_attempts")[1]
    live.update(attempt_id="second-live", generation=3)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert(old_db, "work_assignment_attempts", live)
    for statement in (
        "UPDATE work_assignments SET revision=0",
        "UPDATE coordination_proposals SET round=5",
        "UPDATE coordination_contributions SET slot='invalid'",
        "UPDATE work_assignment_attempts SET max_runtime_seconds=0",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            old_db.execute(statement)
    for statement in (
        "UPDATE work_assignments SET creator_session_id='missing'",
        "UPDATE coordination_proposals SET creator_session_id='missing'",
        "UPDATE work_assignment_attempts SET assignment_code='missing'",
        "UPDATE coordination_contributions SET version=99 WHERE contribution_id='contribution-1'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            old_db.execute(statement)
    for statement in (
        "UPDATE work_assignment_attempts SET source_session_id=NULL",
        "UPDATE coordination_contributions SET author_session_id=NULL",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            old_db.execute(statement)


@pytest.mark.parametrize("after_table", PARENTS)
@pytest.mark.parametrize("enforced", [False, True])
def test_interruption_rolls_back_both_rebuilds_and_preserves_caller_transaction(
    old_db, monkeypatch, after_table, enforced
):
    old_db.execute(f"PRAGMA foreign_keys = {int(enforced)}")
    before_schema = _schema(old_db)
    before = {table: _rows(old_db, table) for table in (*PARENTS, *CHILDREN)}
    rebuild = delta._rebuild

    def interrupted(conn, table, constraint):
        rebuild(conn, table, constraint)
        if table == after_table:
            raise RuntimeError("synthetic rebuild interruption")

    old_db.execute("BEGIN IMMEDIATE")
    old_db.execute("UPDATE operators SET slug='caller-write' WHERE id=901")
    with monkeypatch.context() as patch:
        patch.setattr(delta, "_rebuild", interrupted)
        with pytest.raises(RuntimeError, match="synthetic rebuild"):
            delta.upgrade(old_db)
    assert old_db.in_transaction
    assert _schema(old_db) == before_schema
    assert {table: _rows(old_db, table) for table in before} == before
    assert old_db.execute("SELECT slug FROM operators WHERE id=901").fetchone() == ("caller-write",)
    assert old_db.execute("PRAGMA foreign_keys").fetchone() == (int(enforced),)
    assert old_db.execute("PRAGMA defer_foreign_keys").fetchone() == (0,)
    assert old_db.execute("PRAGMA foreign_key_check").fetchall() == []
    delta.upgrade(old_db)
    old_db.commit()


@pytest.mark.parametrize(
    "definition",
    [
        '"creator_session_id" VARCHAR(32) NOT NULL',
        "`creator_session_id` VARCHAR( 32 ) NOT\nNULL",
        "[creator_session_id] varchar (32) not null",
    ],
)
def test_rebuild_accepts_quoted_indented_historical_author_columns(definition):
    # Render the real historical DDL with equivalent SQLite quoting/whitespace.
    # This connection is deliberately FK-off; the populated full-corpus tests
    # above separately exercise enforcement and history preservation.
    conn = sqlite3.connect(":memory:")

    class QuotedDDL:
        def execute(self, sql):
            return conn.execute(sql.replace("creator_session_id VARCHAR(32) NOT NULL", definition))

    try:
        for version in ("154_work_assignments", "155_peer_coordination"):
            importlib.import_module(f"brains.storage.sql_migrations.{version}").upgrade(QuotedDDL())
        delta.upgrade(conn)
        for table in PARENTS:
            columns = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            assert columns["creator_session_id"][3] == 0
            assert columns["creator_kind"][3:5] == (1, "'session'")
        before = _schema(conn)
        delta.upgrade(conn)
        assert _schema(conn) == before
    finally:
        conn.close()


def test_success_still_rolls_back_with_callers_transaction(old_db):
    before = _schema(old_db)
    old_db.execute("BEGIN IMMEDIATE")
    delta.upgrade(old_db)
    old_db.rollback()
    assert _schema(old_db) == before
    assert old_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_requester_alias_preserves_stored_creator_column():
    proposal = CoordinationProposal(requester_session_id="requester", creator_kind="session")
    assert proposal.creator_session_id == "requester"
    proposal.requester_session_id = None
    assert proposal.creator_session_id is None
    assert "requester_session_id" not in CoordinationProposal.__table__.columns


def test_existing_deferral_setting_is_restored(old_db):
    old_db.execute("BEGIN IMMEDIATE")
    old_db.execute("PRAGMA defer_foreign_keys = ON")
    delta.upgrade(old_db)
    assert old_db.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert old_db.execute("PRAGMA defer_foreign_keys").fetchone() == (1,)
    old_db.commit()


def test_runner_failed_upgrade_retry_and_ledger_replay(old_db, monkeypatch):
    path = old_db.execute("PRAGMA database_list").fetchone()[2]
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def enable_fk(conn, record):
        conn.execute("PRAGMA foreign_keys = ON")

    monkeypatch.setattr(migrations, "engine", engine)
    monkeypatch.setattr(migrations, "SessionLocal", sessionmaker(bind=engine))
    before = {table: _rows(old_db, table) for table in (*PARENTS, *CHILDREN)}
    ledger = old_db.execute("SELECT * FROM schema_versions ORDER BY id").fetchall()
    load = migrations._load_python_upgrade

    def interrupted(path):
        upgrade = load(path)
        if path.stem != MIGRATION:
            return upgrade

        def fail(conn):
            upgrade(conn)
            raise RuntimeError("synthetic post-rebuild interruption")

        return fail

    try:
        migrations.reset_migration_cache()
        with monkeypatch.context() as patch:
            patch.setattr(migrations, "_load_python_upgrade", interrupted)
            with pytest.raises(migrations.MigrationExecutionError, match="post-rebuild"):
                migrations.run_migrations()
        assert {table: _rows(old_db, table) for table in before} == before
        assert old_db.execute(
            "SELECT status, attempts FROM schema_versions WHERE version=?", (MIGRATION,)
        ).fetchone() == ("failed", 1)
        migrations.reset_migration_cache()
        assert migrations.run_migrations().executed == [MIGRATION]
        assert (
            old_db.execute(
                "SELECT * FROM schema_versions WHERE version != ? ORDER BY id", (MIGRATION,)
            ).fetchall()
            == ledger
        )  # Includes immutable 154/155 checksums.
        assert old_db.execute(
            "SELECT status, attempts FROM schema_versions WHERE version=?", (MIGRATION,)
        ).fetchone() == ("applied", 2)
        # Interrupted ledger replay against already-upgraded tables is a no-op.
        old_db.execute("UPDATE schema_versions SET status='running' WHERE version=?", (MIGRATION,))
        old_db.commit()
        migrations.reset_migration_cache()
        assert migrations.run_migrations().executed == [MIGRATION]
        assert migrations.run_migrations().executed == []
        for table, rows in before.items():
            upgraded = _rows(old_db, table)
            if table in PARENTS:
                assert all(row.pop("creator_kind") == "session" for row in upgraded)
            assert upgraded == rows
        assert old_db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        engine.dispose()
        migrations.reset_migration_cache()

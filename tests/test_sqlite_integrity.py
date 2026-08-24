"""Tests for SQLite integrity diagnosis, repair, and verified backups (BL-P0-07).

The repair workflow is destructive by definition, so every test here runs
against a per-test temporary SQLite file. Nothing in this module reads or
writes the operator's real ``~/.brains`` state.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from typer.testing import CliRunner

import brains.audit as audit_module
import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.audit import _reset_key_cache
from brains.backup import BackupError, create_backup, inspect_archive, verify_backup
from brains.config import settings
from brains.storage import integrity
from brains.storage.integrity import (
    BackupPrerequisiteError,
    ForeignKeyViolationsError,
    RepairAction,
    apply_repair,
    assert_foreign_keys_clean,
    diagnose,
    diagnose_database,
    open_database,
    plan_repair,
    repair_database,
)
from brains.storage.migrations import init_db

FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Per-test SQLite database wired into the storage/audit modules."""
    # Modules that capture ``SessionLocal`` at import time must be imported
    # *before* the rebind below, or the first test that happens to import one
    # of them binds it to this temporary database for the whole process - and
    # a later test that deliberately uses the shared per-process database
    # (``brains.control.sessions`` writer paths) then reads a store that no
    # longer exists. Importing here makes this module's order independent.
    import brains.cli.app  # noqa: F401
    import brains.control.sessions  # noqa: F401

    db_path = tmp_path / "brains.sqlite"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    monkeypatch.setenv("BRAINS_AUDIT_KEY_FILE", str(tmp_path / "audit-key"))
    monkeypatch.delenv("BRAINS_AUDIT_KEY", raising=False)
    monkeypatch.setattr(settings, "db_url", f"sqlite:///{db_path}", raising=False)

    engine = create_engine(f"sqlite:///{db_path}")
    session_factory = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", session_factory)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", session_factory)
    monkeypatch.setattr(audit_module, "SessionLocal", session_factory)

    _reset_key_cache()
    init_db()
    engine.dispose()
    yield db_path
    _reset_key_cache()
    engine.dispose()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_workspace(conn: sqlite3.Connection, slug: str, *, org_id: int | None = None) -> int:
    conn.execute(
        "INSERT INTO workspaces (slug, path, name, status, visibility, org_id, "
        "created_at, updated_at) VALUES (?, ?, ?, 'active', 'shared', ?, ?, ?)",
        (
            slug,
            f"/repos/{slug}",
            slug,
            org_id,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    return int(conn.execute("SELECT id FROM workspaces WHERE slug = ?", (slug,)).fetchone()[0])


def _seed_session(
    conn: sqlite3.Connection,
    session_id: str,
    workspace_id: int,
    *,
    state: str = "running",
    ended_at: str | None = None,
    started_at: str = "2026-01-01T00:00:00+00:00",
    last_activity_at: str | None = None,
) -> str:
    conn.execute(
        "INSERT INTO agent_sessions (id, workspace_id, tool, state, started_at, "
        "ended_at, last_activity_at) VALUES (?, ?, 'pytest', ?, ?, ?, ?)",
        (session_id, workspace_id, state, started_at, ended_at, last_activity_at),
    )
    return session_id


def _seed_event(conn: sqlite3.Connection, workspace_id: int | None, session_id: str, kind: str):
    conn.execute(
        "INSERT INTO events (workspace_id, session_id, kind, message, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (workspace_id, session_id, kind, kind, "2026-01-01T00:00:00+00:00"),
    )


def _snapshot(db_path: Path) -> dict[str, list[tuple]]:
    conn = _connect(db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {
            table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()]
            for table in tables
        }
    finally:
        conn.close()


def _is_audit_table(name: str) -> bool:
    return name.startswith("audit_")


def _snapshot_without_audit(db_path: Path) -> dict[str, list[tuple]]:
    """Everything the repair could mutate, excluding the append-only log.

    The audit tables are expected to grow around a repair - the attempt is
    recorded before the write lock is taken, so even a refused repair leaves
    evidence - and "did the repair change any data" is a question about the
    rest of the store.
    """
    return {table: rows for table, rows in _snapshot(db_path).items() if not _is_audit_table(table)}


def _audit_tables(db_path: Path) -> dict[str, list[tuple]]:
    return {table: rows for table, rows in _snapshot(db_path).items() if _is_audit_table(table)}


# ----------------------------------------------------------------------
# Backup: capture, WAL consistency, isolated restore verification
# ----------------------------------------------------------------------


def test_backup_manifest_captures_source_identity(isolated_db):
    conn = _connect(isolated_db)
    try:
        _seed_workspace(conn, "ws-identity")
    finally:
        conn.close()

    result = create_backup(isolated_db.parent / "backup.tar.gz")

    with tarfile.open(result.archive_path, "r:gz") as tar:
        member = tar.extractfile("manifest.json")
        assert member is not None
        manifest = json.loads(member.read().decode("utf-8"))

    assert manifest["manifest_version"] == "2"
    assert manifest["source_path"] == str(isolated_db.resolve())
    assert manifest["schema_fingerprint"] == result.schema_fingerprint
    assert len(manifest["schema_fingerprint"]) == 64
    assert manifest["source_identity"]["page_size"] > 0
    assert manifest["table_row_counts"]["workspaces"] == 1
    assert manifest["foreign_key_violations"] == 0
    assert "workspaces" in " ".join(manifest["schema_objects"])


def test_backup_captures_rows_committed_to_an_open_wal_database(isolated_db):
    """A live WAL database must be copied through the online backup API.

    The rows below are committed into the WAL while a writer connection is
    still open; a naive copy of the ``.sqlite`` file alone would miss them.
    """
    writer = _connect(isolated_db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        workspace_id = _seed_workspace(writer, "ws-wal")
        _seed_session(writer, "ses-wal", workspace_id)
        assert (isolated_db.parent / f"{isolated_db.name}-wal").exists()

        result = create_backup(isolated_db.parent / "wal-backup.tar.gz")

        extracted = isolated_db.parent / "extracted"
        extracted.mkdir()
        with tarfile.open(result.archive_path, "r:gz") as tar:
            tar.extract("brains.sqlite", path=str(extracted), filter="data")
        copy = _connect(extracted / "brains.sqlite")
        try:
            slugs = [row[0] for row in copy.execute("SELECT slug FROM workspaces").fetchall()]
            sessions = [row[0] for row in copy.execute("SELECT id FROM agent_sessions").fetchall()]
        finally:
            copy.close()
    finally:
        writer.close()

    assert "ws-wal" in slugs
    assert "ses-wal" in sessions
    assert result.table_row_counts["agent_sessions"] == 1


def test_verify_backup_passes_and_leaves_the_live_database_untouched(isolated_db):
    conn = _connect(isolated_db)
    try:
        _seed_workspace(conn, "ws-verify")
    finally:
        conn.close()
    archive = isolated_db.parent / "verify.tar.gz"
    create_backup(archive)

    before = _snapshot(isolated_db)
    verification = verify_backup(archive, expected_source_path=isolated_db)

    assert verification.ok, verification.failures
    assert verification.checks["integrity_check"] == ["ok"]
    assert verification.checks["foreign_key_violations"] == 0
    assert verification.checks["live_schema_matches"] is True
    assert verification.checks["live_source_matches"] is True
    assert verification.checks["row_count_differences"] == {}
    assert _snapshot(isolated_db) == before


def test_verify_backup_rejects_a_corrupt_archive(isolated_db):
    bad = isolated_db.parent / "corrupt.tar.gz"
    bad.write_bytes(b"this is not a gzip stream")
    verification = verify_backup(bad)
    assert verification.ok is False
    assert any("unreadable or corrupt" in failure for failure in verification.failures)


def test_verify_backup_rejects_a_tampered_payload(isolated_db):
    archive = isolated_db.parent / "tamper.tar.gz"
    create_backup(archive)

    extracted = isolated_db.parent / "ext"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(str(extracted), filter="data")
    blob = extracted / "brains.sqlite"
    blob.write_bytes(blob.read_bytes() + b"tampered")
    tampered = isolated_db.parent / "tampered.tar.gz"
    with tarfile.open(tampered, "w:gz") as tar:
        tar.add(extracted / "manifest.json", arcname="manifest.json")
        tar.add(blob, arcname="brains.sqlite")

    verification = verify_backup(tampered)
    assert verification.ok is False
    assert any("sha256" in failure for failure in verification.failures)


def test_verify_backup_rejects_row_counts_that_do_not_match_the_manifest(isolated_db):
    archive = isolated_db.parent / "counts.tar.gz"
    create_backup(archive)

    extracted = isolated_db.parent / "ext"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(str(extracted), filter="data")
    blob = extracted / "brains.sqlite"
    conn = _connect(blob)
    try:
        _seed_workspace(conn, "ws-smuggled")
    finally:
        conn.close()

    import hashlib

    payload = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
    payload["data_sha256"] = hashlib.sha256(blob.read_bytes()).hexdigest()
    payload["data_size_bytes"] = blob.stat().st_size
    (extracted / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    rebuilt = isolated_db.parent / "rebuilt.tar.gz"
    with tarfile.open(rebuilt, "w:gz") as tar:
        tar.add(extracted / "manifest.json", arcname="manifest.json")
        tar.add(blob, arcname="brains.sqlite")

    verification = verify_backup(rebuilt)
    assert verification.ok is False
    assert any("row counts differ" in failure for failure in verification.failures)


def test_verify_backup_rejects_an_archive_from_another_database(isolated_db):
    archive = isolated_db.parent / "foreign.tar.gz"
    create_backup(archive)
    other = isolated_db.parent / "other.sqlite"
    other.write_bytes(isolated_db.read_bytes())

    verification = verify_backup(archive, expected_source_path=other)
    assert verification.ok is False
    assert any("was taken from" in failure for failure in verification.failures)


def test_verify_backup_rejects_a_legacy_manifest_without_source_identity(isolated_db):
    archive = isolated_db.parent / "legacy.tar.gz"
    create_backup(archive)

    extracted = isolated_db.parent / "ext"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(str(extracted), filter="data")
    payload = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
    legacy = {
        key: value
        for key, value in payload.items()
        if key
        in {
            "schema_version",
            "brains_version",
            "created_at",
            "backend",
            "data_file",
            "data_sha256",
            "data_size_bytes",
            "sanitized_db_url",
            "schema_versions",
        }
    }
    legacy["manifest_version"] = "1"
    (extracted / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")
    rebuilt = isolated_db.parent / "legacy-rebuilt.tar.gz"
    with tarfile.open(rebuilt, "w:gz") as tar:
        tar.add(extracted / "manifest.json", arcname="manifest.json")
        tar.add(extracted / "brains.sqlite", arcname="brains.sqlite")

    verification = verify_backup(rebuilt)
    assert verification.ok is False
    assert any("predates source-identity capture" in failure for failure in verification.failures)


def test_verify_backup_missing_archive_raises(isolated_db):
    with pytest.raises(BackupError):
        verify_backup(isolated_db.parent / "nope.tar.gz")


def _rebuild_archive_with_manifest(
    archive: Path, workdir: Path, mutate, *, name: str = "rebuilt.tar.gz"
) -> Path:
    """Unpack ``archive``, let ``mutate`` edit the manifest, and repack it."""
    workdir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(str(workdir), filter="data")
    manifest_path = workdir / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(payload)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    rebuilt = archive.parent / name
    with tarfile.open(rebuilt, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        tar.add(workdir / "brains.sqlite", arcname="brains.sqlite")
    return rebuilt


def test_backup_manifest_records_a_live_source_fingerprint(isolated_db):
    result = create_backup(isolated_db.parent / "fingerprint.tar.gz")
    manifest = inspect_archive(result.archive_path)

    assert manifest["source_fingerprint"] == result.source_fingerprint
    assert manifest["source_fingerprint"] == manifest["data_sha256"]
    assert manifest["source_fingerprint_algorithm"] == "sqlite-backup-image-sha256/1"


def test_verify_backup_refuses_an_archive_the_database_has_outgrown(isolated_db):
    """A backup only protects the state it captured.

    Anything committed after the archive was written is not in it, so the
    archive is no longer a safety net for a destructive repair of *this*
    database, even though the archive itself is still perfectly valid.
    """
    archive = isolated_db.parent / "stale.tar.gz"
    create_backup(archive)
    assert verify_backup(archive, expected_source_path=isolated_db).ok

    conn = _connect(isolated_db)
    try:
        _seed_workspace(conn, "ws-written-after-the-backup")
    finally:
        conn.close()

    verification = verify_backup(archive, expected_source_path=isolated_db)
    assert verification.ok is False
    assert verification.checks["live_source_matches"] is False
    assert verification.checks["live_schema_matches"] is True
    assert any("has changed since this archive was written" in f for f in verification.failures)
    # Unbound verification still passes: the archive is intact, just not current.
    assert verify_backup(archive).ok is True


def test_verify_backup_sees_a_wal_write_that_is_not_yet_checkpointed(isolated_db):
    """WAL frames are content. A write parked in the ``-wal`` file counts."""
    writer = _connect(isolated_db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        archive = isolated_db.parent / "wal-fresh.tar.gz"
        create_backup(archive)
        fresh = verify_backup(archive, expected_source_path=isolated_db)
        assert fresh.ok, fresh.failures

        _seed_workspace(writer, "ws-wal-after-backup")
        assert (isolated_db.parent / f"{isolated_db.name}-wal").exists()
        stale = verify_backup(archive, expected_source_path=isolated_db)
    finally:
        writer.close()

    assert stale.ok is False
    assert any("has changed since this archive was written" in f for f in stale.failures)


def test_verify_backup_refuses_a_manifest_without_a_source_fingerprint(isolated_db):
    archive = isolated_db.parent / "no-fingerprint.tar.gz"
    create_backup(archive)

    def drop_fingerprint(payload: dict) -> None:
        payload["source_fingerprint"] = ""
        payload["source_fingerprint_algorithm"] = ""

    rebuilt = _rebuild_archive_with_manifest(
        archive, isolated_db.parent / "nofp", drop_fingerprint, name="no-fp.tar.gz"
    )

    verification = verify_backup(rebuilt, expected_source_path=isolated_db)
    assert verification.ok is False
    assert any("no source fingerprint" in failure for failure in verification.failures)
    assert verify_backup(rebuilt).ok is True


def test_verify_backup_refuses_an_unknown_fingerprint_algorithm(isolated_db):
    archive = isolated_db.parent / "future-algo.tar.gz"
    create_backup(archive)

    def change_algorithm(payload: dict) -> None:
        payload["source_fingerprint_algorithm"] = "some-future-scheme/9"

    rebuilt = _rebuild_archive_with_manifest(
        archive, isolated_db.parent / "algo", change_algorithm, name="algo.tar.gz"
    )

    verification = verify_backup(rebuilt, expected_source_path=isolated_db)
    assert verification.ok is False
    assert any("cannot be compared" in failure for failure in verification.failures)


def test_verify_backup_refuses_a_source_path_that_no_longer_exists(isolated_db):
    archive = isolated_db.parent / "gone.tar.gz"
    create_backup(archive)
    missing = (isolated_db.parent / "deleted.sqlite").resolve()

    def repoint(payload: dict) -> None:
        payload["source_path"] = str(missing)

    rebuilt = _rebuild_archive_with_manifest(
        archive, isolated_db.parent / "gone-ext", repoint, name="gone-rebuilt.tar.gz"
    )

    verification = verify_backup(rebuilt, expected_source_path=missing)
    assert verification.ok is False
    assert any("does not exist" in failure for failure in verification.failures)


def test_verify_backup_does_not_scan_the_live_database(isolated_db, monkeypatch):
    """The live store gets a fingerprint, not an audit.

    ``integrity_check``, ``foreign_key_check`` and row counting belong on the
    isolated restored copy. Running them against the operator's live database
    would be a long read for an answer the archive already carries.
    """
    import brains.backup as backup_module

    archive = isolated_db.parent / "lightweight.tar.gz"
    create_backup(archive)

    scanned: list[str] = []
    real_snapshot = backup_module._sqlite_snapshot

    def recording_snapshot(path):
        scanned.append(str(Path(path).resolve()))
        return real_snapshot(path)

    monkeypatch.setattr(backup_module, "_sqlite_snapshot", recording_snapshot)
    verification = verify_backup(archive, expected_source_path=isolated_db)

    assert verification.ok, verification.failures
    assert len(scanned) == 1, scanned
    assert str(isolated_db.resolve()) not in scanned


# ----------------------------------------------------------------------
# Backup: manifest compatibility and malformed input
# ----------------------------------------------------------------------


def _archive_with_raw_manifest(archive: Path, workdir: Path, raw: str, *, name: str) -> Path:
    """Repack ``archive`` with ``raw`` bytes standing in for the manifest."""
    workdir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(str(workdir), filter="data")
    manifest_path = workdir / "manifest.json"
    manifest_path.write_text(raw, encoding="utf-8")
    rebuilt = archive.parent / name
    with tarfile.open(rebuilt, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        tar.add(workdir / "brains.sqlite", arcname="brains.sqlite")
    return rebuilt


def test_manifest_reader_ignores_unknown_future_fields(isolated_db):
    """Forward compatibility: a later build may add fields to a version we read.

    Adding a field must not turn every existing archive into an error, so
    unknown keys inside a readable ``manifest_version`` are dropped rather
    than rejected. The archive still verifies on its known claims.
    """
    archive = isolated_db.parent / "future-fields.tar.gz"
    create_backup(archive)

    def add_future_fields(payload: dict) -> None:
        payload["encryption"] = {"scheme": "age", "recipients": ["operator"]}
        payload["retention_days"] = 30

    rebuilt = _rebuild_archive_with_manifest(
        archive, isolated_db.parent / "future", add_future_fields, name="future.tar.gz"
    )

    manifest = inspect_archive(rebuilt)
    assert manifest["manifest_version"] == "2"
    assert "encryption" not in manifest
    assert "retention_days" not in manifest
    verification = verify_backup(rebuilt, expected_source_path=isolated_db)
    assert verification.ok, verification.failures


def test_an_unreadable_manifest_version_is_refused_by_name(isolated_db):
    """Backward compatibility runs one way, and the error says so.

    This build writes ``manifest_version`` 2 and reads 1 and 2. A future
    version has no reader contract here, so it is named and refused - never
    silently read with this version's assumptions - and the CLI turns that
    into one line and exit 1 rather than a traceback.
    """
    from brains.cli.app import app as cli_app

    archive = isolated_db.parent / "future-version.tar.gz"
    create_backup(archive)

    def bump_version(payload: dict) -> None:
        payload["manifest_version"] = "3"

    rebuilt = _rebuild_archive_with_manifest(
        archive, isolated_db.parent / "v3", bump_version, name="v3.tar.gz"
    )

    with pytest.raises(BackupError) as exc:
        inspect_archive(rebuilt)
    assert "unsupported manifest_version '3'" in str(exc.value)
    assert "this build reads 1, 2" in str(exc.value)

    verification = verify_backup(rebuilt)
    assert verification.ok is False
    assert any("manifest_version" in failure for failure in verification.failures)

    runner = CliRunner()
    inspected = runner.invoke(cli_app, ["backup-inspect", str(rebuilt)])
    assert inspected.exit_code == 1
    assert "cannot inspect archive" in inspected.output
    assert "Traceback" not in inspected.output
    assert inspected.exception is None or isinstance(inspected.exception, SystemExit)

    verified = runner.invoke(cli_app, ["db", "verify-backup", str(rebuilt)])
    assert verified.exit_code == 1
    assert json.loads(verified.stdout)["ok"] is False


def test_a_v1_archive_is_still_readable_by_this_build(isolated_db):
    """The new reader stays compatible with archives older builds wrote."""
    archive = isolated_db.parent / "v1-compat.tar.gz"
    create_backup(archive)

    def downgrade(payload: dict) -> None:
        for key in (
            "source_path",
            "source_identity",
            "schema_fingerprint",
            "schema_objects",
            "table_row_counts",
            "foreign_key_violations",
            "source_fingerprint",
            "source_fingerprint_algorithm",
        ):
            payload.pop(key, None)
        payload["manifest_version"] = "1"

    rebuilt = _rebuild_archive_with_manifest(
        archive, isolated_db.parent / "v1", downgrade, name="v1.tar.gz"
    )

    manifest = inspect_archive(rebuilt)
    assert manifest["manifest_version"] == "1"
    assert manifest["backend"] == "sqlite"
    assert manifest["source_path"] == ""
    # Readable, but not verifiable: the claims it would be checked against
    # were never recorded, and verification says that instead of passing it.
    verification = verify_backup(rebuilt)
    assert verification.ok is False
    assert any("predates source-identity capture" in f for f in verification.failures)


@pytest.mark.parametrize(
    ("label", "mutate", "expected"),
    [
        (
            "missing-required-field",
            lambda payload: payload.pop("brains_version"),
            "manifest missing required field(s): ['brains_version']",
        ),
        (
            "wrong-scalar-type",
            lambda payload: payload.update(data_size_bytes="lots"),
            "manifest field 'data_size_bytes' must be int, got str",
        ),
        (
            "wrong-container-type",
            lambda payload: payload.update(schema_versions="120"),
            "manifest field 'schema_versions' must be list, got str",
        ),
        (
            "wrong-mapping-type",
            lambda payload: payload.update(source_identity=[]),
            "manifest field 'source_identity' must be dict, got list",
        ),
        (
            "boolean-for-a-count",
            lambda payload: payload.update(foreign_key_violations=True),
            "manifest field 'foreign_key_violations' must be int, got bool",
        ),
        (
            "unsafe-data-file",
            lambda payload: payload.update(data_file="../escape.sqlite"),
            "unsafe data_file in manifest",
        ),
    ],
)
def test_malformed_manifest_fields_raise_backup_error(isolated_db, label, mutate, expected):
    """A hand-edited or foreign manifest is an error message, not a traceback.

    ``brains_version`` is the case that used to escape: it is required by the
    dataclass but was not in the checked set, so a manifest without it raised
    ``TypeError`` from the constructor instead of :class:`BackupError`.
    """
    archive = isolated_db.parent / f"{label}.tar.gz"
    create_backup(archive)

    rebuilt = _rebuild_archive_with_manifest(
        archive, isolated_db.parent / label, mutate, name=f"{label}-rebuilt.tar.gz"
    )

    with pytest.raises(BackupError) as exc:
        inspect_archive(rebuilt)
    assert expected in str(exc.value)


@pytest.mark.parametrize(
    ("label", "raw", "expected"),
    [
        ("not-json", "{not json", "manifest.json is not valid JSON"),
        ("json-array", "[1, 2, 3]", "manifest.json must be a JSON object, got list"),
        ("json-scalar", '"manifest"', "manifest.json must be a JSON object, got str"),
    ],
)
def test_a_manifest_that_is_not_an_object_is_a_backup_error(isolated_db, label, raw, expected):
    from brains.cli.app import app as cli_app

    archive = isolated_db.parent / f"{label}.tar.gz"
    create_backup(archive)
    rebuilt = _archive_with_raw_manifest(
        archive, isolated_db.parent / label, raw, name=f"{label}-rebuilt.tar.gz"
    )

    with pytest.raises(BackupError) as exc:
        inspect_archive(rebuilt)
    assert expected in str(exc.value)

    result = CliRunner().invoke(cli_app, ["backup-inspect", str(rebuilt)])
    assert result.exit_code == 1
    assert "cannot inspect archive" in result.output


def test_inspecting_something_that_is_not_an_archive_is_a_backup_error(isolated_db):
    from brains.cli.app import app as cli_app

    junk = isolated_db.parent / "not-a-tarball.tar.gz"
    junk.write_bytes(b"absolutely not a gzip stream")

    with pytest.raises(BackupError) as exc:
        inspect_archive(junk)
    assert "archive unreadable or corrupt" in str(exc.value)

    result = CliRunner().invoke(cli_app, ["backup-inspect", str(junk)])
    assert result.exit_code == 1
    assert "cannot inspect archive" in result.output


# ----------------------------------------------------------------------
# Backup: the enforced source write lock
# ----------------------------------------------------------------------


def test_a_source_lock_that_is_not_actually_held_is_refused(isolated_db):
    """The quiescence claim is checked, not trusted.

    ``SourceWriteLock`` is only meaningful if holding it is verifiable, so
    both the backup capture and the verification re-prove it: the caller's
    connection must be inside a transaction *and* no other connection may be
    able to take the write lock. A deferred ``BEGIN`` satisfies the first and
    fails the second, which is exactly the mistake worth catching.
    """
    from brains.backup import SourceLockLost, SourceWriteLock

    archive = isolated_db.parent / "lock-check.tar.gz"
    create_backup(archive)

    conn = sqlite3.connect(str(isolated_db), isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=2000")
        lock = SourceWriteLock(path=isolated_db.resolve(), connection=conn)

        with pytest.raises(SourceLockLost) as exc:
            verify_backup(archive, source_lock=lock)
        assert "is not open" in str(exc.value)
        with pytest.raises(SourceLockLost):
            create_backup(isolated_db.parent / "never-written.tar.gz", source_lock=lock)
        assert not (isolated_db.parent / "never-written.tar.gz").exists()

        conn.execute("BEGIN")  # deferred: a transaction, but no write lock
        with pytest.raises(SourceLockLost) as exc:
            verify_backup(archive, source_lock=lock)
        assert "can still acquire the write lock" in str(exc.value)
        conn.execute("ROLLBACK")

        conn.execute("BEGIN IMMEDIATE")
        verification = verify_backup(archive, source_lock=lock)
        assert verification.ok, verification.failures
        assert verification.checks["bound_under_source_write_lock"] is True
        assert verification.checks["source_write_lock_held"] is True
        assert verification.checks["expected_source_path"] == str(isolated_db.resolve())
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_a_source_lock_on_another_database_cannot_bind_this_archive(isolated_db):
    from brains.backup import SourceWriteLock

    archive = isolated_db.parent / "wrong-lock.tar.gz"
    create_backup(archive)
    other = isolated_db.parent / "other.sqlite"
    other.write_bytes(isolated_db.read_bytes())

    conn = sqlite3.connect(str(other), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        lock = SourceWriteLock(path=other.resolve(), connection=conn)
        verification = verify_backup(archive, expected_source_path=isolated_db, source_lock=lock)
        conn.execute("ROLLBACK")
    finally:
        conn.close()

    assert verification.ok is False
    assert any("the held write lock is on" in failure for failure in verification.failures)


# ----------------------------------------------------------------------
# Schema-derived cascade
# ----------------------------------------------------------------------


def test_workspace_cascade_is_derived_from_declared_foreign_keys(isolated_db):
    with open_database(isolated_db, read_only=True) as conn:
        steps = integrity.workspace_cascade_tables(conn)

    by_table = {step.table: step for step in steps}
    # Direct Workspace scope is deleted.
    assert by_table["agent_sessions"].operation == "delete"
    assert by_table["events"].operation == "delete"
    # Transitive required dependants the hand-maintained list never covered.
    assert by_table["approval_decisions"].operation == "delete"
    assert by_table["chunks"].operation == "delete"
    # Independent records keep their row and lose the stale reference.
    assert by_table["personas"].operation == "null"
    assert by_table["knowledge_patterns"].operation == "null"
    # ``help_requests`` has no ``workspace_id`` column at all; the schema-derived
    # plan uses the column that actually exists.
    assert "from_workspace_id" in by_table["help_requests"].columns
    # Deletes run deepest-dependant first, updates before any delete.
    operations = [step.operation for step in steps]
    assert operations.index("delete") > max(
        index for index, op in enumerate(operations) if op == "null"
    )


def test_workspace_cascade_preserves_independent_records(isolated_db):
    """Deleting a Workspace must not destroy entities with their own identity."""
    conn = _connect(isolated_db)
    try:
        doomed = _seed_workspace(conn, "ws-doomed", org_id=_default_org_id(conn))
        keeper = _seed_workspace(conn, "ws-keeper", org_id=_default_org_id(conn))
        conn.execute(
            "INSERT INTO projects (code, org_id, slug, workspace_id, name, status, created_at, "
            "updated_at) VALUES ('P-1', ?, 'p-1', ?, 'Project', 'active', ?, ?)",
            (
                _default_org_id(conn),
                doomed,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        project_id = int(conn.execute("SELECT id FROM projects").fetchone()[0])
        conn.execute(
            "INSERT INTO issues (code, project_id, workspace_id, title, status, priority, "
            "created_at, updated_at) VALUES ('I-1', ?, ?, 'Issue', 'open', 'p2', ?, ?)",
            (project_id, keeper, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO knowledge_entries (code, type, title, body, status, scope, "
            "workspace_id, tags, confidence, provenance, importance, severity, evidence, "
            "metadata_json, created_at, updated_at) VALUES ('K-1', 'caveat', 'k', 'b', "
            "'active', 'workspace', ?, '', 'medium', 'inferred', 0.5, 'info', '', '{}', ?, ?)",
            (doomed, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )

        for step in integrity.workspace_cascade_tables(conn):
            conn.execute(step.sql(f"id = {doomed}"))
        conn.execute(f"DELETE FROM workspaces WHERE id = {doomed}")

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        project = conn.execute("SELECT workspace_id FROM projects").fetchone()
        assert project is not None and project["workspace_id"] is None
        issue = conn.execute("SELECT workspace_id FROM issues").fetchone()
        assert issue is not None and issue["workspace_id"] == keeper
        knowledge = conn.execute("SELECT workspace_id FROM knowledge_entries").fetchone()
        assert knowledge is not None and knowledge["workspace_id"] is None
    finally:
        conn.close()


def test_workspace_cascade_removes_cross_workspace_session_dependants(isolated_db):
    """A claim held on a surviving Workspace by a doomed Session must not survive."""
    conn = _connect(isolated_db)
    try:
        doomed = _seed_workspace(conn, "ws-doomed", org_id=_default_org_id(conn))
        keeper = _seed_workspace(conn, "ws-keeper", org_id=_default_org_id(conn))
        _seed_session(conn, "ses-doomed", doomed)
        conn.execute(
            "INSERT INTO workspace_claims (workspace_id, session_id, scope, claimed_at, "
            "expires_at) VALUES (?, 'ses-doomed', 'code', ?, ?)",
            (keeper, "2026-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO session_checkpoints (workspace_id, session_id, summary, created_at) "
            "VALUES (?, 'ses-doomed', 'checkpoint', ?)",
            (keeper, "2026-01-01T00:00:00+00:00"),
        )

        for step in integrity.workspace_cascade_tables(conn):
            conn.execute(step.sql(f"id = {doomed}"))
        conn.execute(f"DELETE FROM workspaces WHERE id = {doomed}")

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM workspace_claims").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM session_checkpoints").fetchone()[0] == 0
    finally:
        conn.close()


def test_workspace_cascade_orders_merged_dependants_before_their_parents(isolated_db):
    with open_database(isolated_db, read_only=True) as conn:
        steps = integrity.workspace_cascade_tables(conn)

    deletes = [step for step in steps if step.operation == "delete"]
    order = {step.table: index for index, step in enumerate(deletes)}
    # ``workspace_claims`` and ``session_checkpoints`` are reachable both
    # directly and through ``agent_sessions``; they must be removed first.
    assert order["workspace_claims"] < order["agent_sessions"]
    assert order["session_checkpoints"] < order["agent_sessions"]
    assert order["approval_decisions"] < order["approval_requests"]
    assert order["chunks"] < order["artifacts"] < order["sources"]
    conn = _connect(isolated_db)
    try:
        doomed = _seed_workspace(conn, "ws-doomed")
        keeper = _seed_workspace(conn, "ws-keeper")
        _seed_session(conn, "ses-doomed", doomed)
        _seed_session(conn, "ses-keeper", keeper)
        conn.execute(
            "INSERT INTO approval_requests (code, workspace_id, session_id, title, status, "
            "created_at) VALUES ('ap-1', ?, 'ses-doomed', 'x', 'open', ?)",
            (doomed, "2026-01-01T00:00:00+00:00"),
        )
        request_id = int(conn.execute("SELECT id FROM approval_requests").fetchone()[0])
        conn.execute(
            "INSERT INTO approval_decisions (code, approval_request_id, chosen, decided_at) "
            "VALUES ('ad-1', ?, 'yes', ?)",
            (request_id, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO personas (org_id, slug, name, status, created_by_session_id, "
            "created_at, updated_at) VALUES (?, 'p-1', 'P', 'active', 'ses-doomed', ?, ?)",
            (_default_org_id(conn), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )

        for step in integrity.workspace_cascade_tables(conn):
            conn.execute(step.sql(f"id = {doomed}"))
        conn.execute(f"DELETE FROM workspaces WHERE id = {doomed}")

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0] == 0
        persona = conn.execute("SELECT created_by_session_id FROM personas").fetchone()
        assert persona["created_by_session_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0] == 1
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Diagnosis
# ----------------------------------------------------------------------


def _default_org_id(conn: sqlite3.Connection) -> int:
    """The default Org that migration 120 seeds on every SQLite database."""
    row = conn.execute("SELECT id FROM orgs WHERE slug = 'default'").fetchone()
    assert row is not None, "migration 120 should have seeded the default org"
    return int(row[0])


def _seed_anomalies(db_path: Path) -> dict[str, int]:
    """Seed one instance of every product invariant BL-P0-07 names."""
    conn = _connect(db_path)
    try:
        org_id = _default_org_id(conn)
        scoped = _seed_workspace(conn, "ws-scoped", org_id=org_id)
        orgless = _seed_workspace(conn, "ws-orgless")

        # Terminal-state contradictions.
        _seed_session(
            conn, "ses-reaped", scoped, state="running", ended_at="2026-02-01T00:00:00+00:00"
        )
        _seed_event(conn, scoped, "ses-reaped", "session_reaped")
        _seed_session(
            conn, "ses-ended", scoped, state="running", ended_at="2026-02-01T00:00:00+00:00"
        )
        _seed_event(conn, scoped, "ses-ended", "session_end")
        _seed_session(
            conn, "ses-ambiguous", scoped, state="blocked", ended_at="2026-02-01T00:00:00+00:00"
        )
        _seed_session(
            conn,
            "ses-completed-open",
            scoped,
            state="completed",
            last_activity_at="2026-03-01T00:00:00+00:00",
        )

        # Orphaned Session references the schema declares.
        _seed_event(conn, scoped, "ses-gone", "agent_stdout")
        conn.execute(
            "INSERT INTO handoffs (workspace_id, title, set_by_session_id, set_at, status) "
            "VALUES (?, 'handoff', 'ses-gone', ?, 'active')",
            (scoped, "2026-01-01T00:00:00+00:00"),
        )

        # Expired and session-ended claims.
        conn.execute(
            "INSERT INTO workspace_claims (workspace_id, session_id, scope, claimed_at, "
            "expires_at) VALUES (?, 'ses-ended', 'code', ?, ?)",
            (scoped, "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO workspace_claims (workspace_id, session_id, scope, claimed_at, "
            "expires_at) VALUES (?, 'ses-gone', 'code', ?, ?)",
            (orgless, "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        )
        return {"org_id": org_id, "scoped": scoped, "orgless": orgless}
    finally:
        conn.close()


def test_diagnose_reports_every_documented_invariant(isolated_db):
    _seed_anomalies(isolated_db)
    report = diagnose_database(isolated_db, now=FIXED_NOW)

    codes = {finding.code for finding in report.findings}
    assert "session.ended_without_terminal_state" in codes
    assert "session.ended_state_ambiguous" in codes
    assert "session.terminal_without_ended_at" in codes
    assert "workspace.missing_org" in codes
    assert "claim.expired" in codes
    assert "claim.session_ended" in codes
    assert "foreign_key.orphaned_reference" in codes
    assert report.integrity_check == ("ok",)
    assert report.foreign_key_violations > 0
    assert report.ok is False


def test_diagnosis_is_deterministic(isolated_db):
    _seed_anomalies(isolated_db)
    first = diagnose_database(isolated_db, now=FIXED_NOW).to_dict()
    second = diagnose_database(isolated_db, now=FIXED_NOW).to_dict()
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_ambiguous_legacy_rows_are_classified_not_guessed(isolated_db):
    _seed_anomalies(isolated_db)
    report = diagnose_database(isolated_db, now=FIXED_NOW)
    ambiguous = [f for f in report.findings if f.code == "session.ended_state_ambiguous"]
    assert len(ambiguous) == 1
    assert ambiguous[0].classification == "ambiguous_legacy"
    assert ambiguous[0].repair is None
    assert [row["id"] for row in ambiguous[0].sample] == ["ses-ambiguous"]


def test_orgless_workspaces_are_ambiguous_without_a_default_org(isolated_db):
    conn = _connect(isolated_db)
    try:
        conn.execute("UPDATE orgs SET slug = 'alpha' WHERE slug = 'default'")
        conn.execute(
            "INSERT INTO orgs (slug, name, status, created_at, updated_at) "
            "VALUES ('beta', 'beta', 'active', ?, ?)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        _seed_workspace(conn, "ws-unscoped")
    finally:
        conn.close()

    report = diagnose_database(isolated_db, now=FIXED_NOW)
    finding = next(f for f in report.findings if f.code == "workspace.missing_org")
    assert finding.classification == "requires_operator"
    assert finding.repair is None


# ----------------------------------------------------------------------
# Repair
# ----------------------------------------------------------------------


def test_dry_run_plans_actions_and_mutates_nothing(isolated_db):
    _seed_anomalies(isolated_db)
    before = _snapshot(isolated_db)

    payload = repair_database(isolated_db, apply=False, now=FIXED_NOW)

    assert payload["dry_run"] is True
    assert payload["applied"] is False
    assert payload["backup"] is None
    assert payload["planned_actions"]
    assert all(action["applied_rows"] is None for action in payload["planned_actions"])
    assert _snapshot(isolated_db) == before


def test_dry_run_is_deterministic(isolated_db):
    _seed_anomalies(isolated_db)
    first = repair_database(isolated_db, apply=False, now=FIXED_NOW)
    second = repair_database(isolated_db, apply=False, now=FIXED_NOW)
    assert first == second


def test_apply_requires_a_backup(isolated_db):
    _seed_anomalies(isolated_db)
    before = _snapshot(isolated_db)
    with pytest.raises(BackupPrerequisiteError):
        repair_database(isolated_db, apply=True, now=FIXED_NOW)
    assert _snapshot(isolated_db) == before


def test_apply_refuses_an_unverifiable_backup(isolated_db):
    _seed_anomalies(isolated_db)
    bogus = isolated_db.parent / "bogus.tar.gz"
    bogus.write_bytes(b"not an archive")
    before = _snapshot(isolated_db)

    with pytest.raises(BackupPrerequisiteError) as exc:
        repair_database(isolated_db, apply=True, backup_archive=bogus, now=FIXED_NOW)

    assert "verification failed" in str(exc.value)
    assert _snapshot(isolated_db) == before


def test_apply_refuses_a_backup_taken_from_another_database(isolated_db):
    _seed_anomalies(isolated_db)
    archive = isolated_db.parent / "elsewhere.tar.gz"
    create_backup(archive)
    other = isolated_db.parent / "other.sqlite"
    other.write_bytes(isolated_db.read_bytes())
    before = _snapshot(isolated_db)

    with pytest.raises(BackupPrerequisiteError):
        repair_database(other, apply=True, backup_archive=archive, now=FIXED_NOW)
    assert _snapshot(isolated_db) == before


def test_apply_refuses_a_backup_taken_before_a_later_write(isolated_db):
    """The known dirty state is fine; a *newer* dirty state is not.

    The archive still describes this database, and the anomalies it captured
    are exactly the ones repair is about to fix. What it does not contain is
    the row written after it, so restoring it would lose that row: the repair
    refuses instead of mutating behind an out-of-date safety net.
    """
    _seed_anomalies(isolated_db)
    archive = isolated_db.parent / "before-the-write.tar.gz"
    create_backup(archive)

    conn = _connect(isolated_db)
    try:
        _seed_workspace(conn, "ws-written-after-the-backup", org_id=_default_org_id(conn))
    finally:
        conn.close()
    before = _snapshot(isolated_db)

    with pytest.raises(BackupPrerequisiteError) as exc:
        repair_database(isolated_db, apply=True, backup_archive=archive, now=FIXED_NOW)

    assert "has changed since this archive was written" in str(exc.value)
    assert _snapshot(isolated_db) == before


def test_apply_accepts_a_backup_that_still_represents_the_database(isolated_db):
    _seed_anomalies(isolated_db)
    archive = isolated_db.parent / "current.tar.gz"
    create_backup(archive)

    payload = repair_database(isolated_db, apply=True, backup_archive=archive, now=FIXED_NOW)

    assert payload["applied"] is True
    assert payload["backup"]["ok"] is True
    assert payload["backup"]["checks"]["live_source_matches"] is True


def test_apply_repairs_deterministically_and_clears_foreign_key_violations(isolated_db):
    ids = _seed_anomalies(isolated_db)

    payload = repair_database(
        isolated_db,
        apply=True,
        backup_to=isolated_db.parent / "pre-repair.tar.gz",
        now=FIXED_NOW,
    )

    assert payload["applied"] is True
    assert payload["backup"]["ok"] is True
    assert payload["post_repair"]["foreign_key_violations"] == 0

    conn = _connect(isolated_db)
    try:
        states = dict(conn.execute("SELECT id, state FROM agent_sessions").fetchall())
        assert states["ses-reaped"] == "failed"
        assert states["ses-ended"] == "completed"
        # Ambiguous rows are reported, never guessed.
        assert states["ses-ambiguous"] == "blocked"

        ended = conn.execute(
            "SELECT ended_at FROM agent_sessions WHERE id = 'ses-completed-open'"
        ).fetchone()[0]
        assert ended == "2026-03-01T00:00:00+00:00"

        org_ids = [row[0] for row in conn.execute("SELECT org_id FROM workspaces").fetchall()]
        assert org_ids == [ids["org_id"], ids["org_id"]]

        # Durable records survive; only the dangling reference is cleared.
        orphan_event = conn.execute(
            "SELECT session_id FROM events WHERE kind = 'agent_stdout'"
        ).fetchone()
        assert orphan_event["session_id"] is None
        handoff = conn.execute("SELECT set_by_session_id FROM handoffs").fetchone()
        assert handoff["set_by_session_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3

        # Stale leases are gone.
        assert conn.execute("SELECT COUNT(*) FROM workspace_claims").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    remaining = {f["code"] for f in payload["post_repair"]["remaining_findings"]}
    assert remaining == {"session.ended_state_ambiguous"}


def test_apply_is_idempotent(isolated_db):
    _seed_anomalies(isolated_db)
    repair_database(
        isolated_db, apply=True, backup_to=isolated_db.parent / "first.tar.gz", now=FIXED_NOW
    )
    after_first = _snapshot(isolated_db)
    second = repair_database(
        isolated_db, apply=True, backup_to=isolated_db.parent / "second.tar.gz", now=FIXED_NOW
    )
    assert second["planned_actions"] == []
    assert _snapshot(isolated_db) == after_first


def test_repair_rolls_back_every_action_when_one_fails(isolated_db):
    _seed_anomalies(isolated_db)
    before = _snapshot(isolated_db)

    with open_database(isolated_db, read_only=True) as conn:
        report = diagnose(conn, now=FIXED_NOW, database=str(isolated_db))
        actions = plan_repair(conn, report)
    assert actions
    actions.append(
        RepairAction(
            code="test.explode",
            table="agent_sessions",
            description="deliberate failure",
            statement="UPDATE agent_sessions SET no_such_column = 1",
        )
    )

    with (
        open_database(isolated_db, read_only=False) as conn,
        pytest.raises(sqlite3.OperationalError),
    ):
        apply_repair(conn, actions)

    assert _snapshot(isolated_db) == before
    assert all(action.applied_rows is None for action in actions)


def test_repair_refuses_a_structurally_corrupt_database(isolated_db, monkeypatch):
    _seed_anomalies(isolated_db)
    monkeypatch.setattr(
        integrity,
        "integrity_check",
        lambda conn: ("*** in database main ***", "page 3 is never used"),
    )
    before = _snapshot(isolated_db)

    with pytest.raises(integrity.DatabaseCorruptError):
        repair_database(
            isolated_db,
            apply=True,
            backup_to=isolated_db.parent / "corrupt.tar.gz",
            now=FIXED_NOW,
        )
    assert _snapshot(isolated_db) == before


def test_required_orphans_need_an_operator_unless_delete_orphans_is_requested(isolated_db):
    conn = _connect(isolated_db)
    try:
        workspace_id = _seed_workspace(conn, "ws-tasks")
        conn.execute(
            "INSERT INTO agent_tasks (code, workspace_id, title, priority, status, created_at) "
            "VALUES ('T-1', ?, 'orphan', 'p2', 'available', ?)",
            (workspace_id + 500, "2026-01-01T00:00:00+00:00"),
        )
    finally:
        conn.close()

    report = diagnose_database(isolated_db, now=FIXED_NOW)
    finding = next(f for f in report.findings if f.table == "agent_tasks")
    assert finding.classification == "requires_operator"

    payload = repair_database(
        isolated_db,
        apply=True,
        backup_to=isolated_db.parent / "tasks.tar.gz",
        now=FIXED_NOW,
    )
    assert payload["post_repair"]["foreign_key_violations"] == 1
    conn = _connect(isolated_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0] == 1
    finally:
        conn.close()

    payload = repair_database(
        isolated_db,
        apply=True,
        backup_to=isolated_db.parent / "tasks-2.tar.gz",
        delete_orphans=True,
        now=FIXED_NOW,
    )
    assert payload["post_repair"]["foreign_key_violations"] == 0
    conn = _connect(isolated_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_orphaned_lease_rows_are_cleaned_without_delete_orphans(isolated_db):
    """The documented lease exception (``docs/OPERATIONS.md``).

    A claim whose owning Session no longer exists is expired lock state, not
    history, so repair removes it even though the reference sits on a
    required column. A durable row in the same position (``agent_tasks``)
    still waits for an operator decision - see the test above.
    """
    conn = _connect(isolated_db)
    try:
        workspace_id = _seed_workspace(conn, "ws-lease", org_id=_default_org_id(conn))
        conn.execute(
            "INSERT INTO workspace_claims (workspace_id, session_id, scope, claimed_at, "
            "expires_at) VALUES (?, 'ses-vanished', 'code', ?, ?)",
            (workspace_id, "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        )
    finally:
        conn.close()

    report = diagnose_database(isolated_db, now=FIXED_NOW)
    finding = next(f for f in report.findings if f.table == "workspace_claims")
    assert finding.classification == "deterministic"

    payload = repair_database(
        isolated_db,
        apply=True,
        backup_to=isolated_db.parent / "lease.tar.gz",
        now=FIXED_NOW,
    )
    assert payload["delete_orphans"] is False
    assert payload["post_repair"]["ok"] is True
    conn = _connect(isolated_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM workspace_claims").fetchone()[0] == 0
    finally:
        conn.close()


def test_repair_converges_when_stamping_ended_at_releases_a_live_claim(isolated_db):
    """Regression: one repair must not leave the next one behind.

    A completed Session with no ``ended_at`` still holds an unexpired claim,
    so the claim is *live* at diagnosis time. Stamping ``ended_at`` is what
    makes that lease stale, and a single-pass repair would have committed the
    new anomaly and reported success.
    """
    conn = _connect(isolated_db)
    try:
        org_id = _default_org_id(conn)
        workspace_id = _seed_workspace(conn, "ws-converge", org_id=org_id)
        _seed_session(
            conn,
            "ses-completed-holding-a-claim",
            workspace_id,
            state="completed",
            last_activity_at="2026-03-01T00:00:00+00:00",
        )
        conn.execute(
            "INSERT INTO workspace_claims (workspace_id, session_id, scope, claimed_at, "
            "expires_at) VALUES (?, 'ses-completed-holding-a-claim', 'code', ?, ?)",
            (workspace_id, "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        )
    finally:
        conn.close()

    report = diagnose_database(isolated_db, now=FIXED_NOW)
    assert {f.code for f in report.findings} == {"session.terminal_without_ended_at"}

    payload = repair_database(
        isolated_db,
        apply=True,
        backup_to=isolated_db.parent / "converge.tar.gz",
        now=FIXED_NOW,
    )

    assert payload["passes"] == 2
    assert [action["code"] for action in payload["planned_actions"]] == [
        "session.stamp_ended_at",
        "claim.delete_ended_session",
    ]
    assert payload["post_repair"]["ok"] is True
    assert payload["post_repair"]["remaining_findings"] == []

    conn = _connect(isolated_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM workspace_claims").fetchone()[0] == 0
        ended = conn.execute(
            "SELECT ended_at FROM agent_sessions WHERE id = 'ses-completed-holding-a-claim'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert ended == "2026-03-01T00:00:00+00:00"


def _extract_archive_database(archive: Path, dest_dir: Path) -> Path:
    """Unpack the SQLite blob an archive carries, without restoring anything."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(str(dest_dir), filter="data")
    return dest_dir / "brains.sqlite"


def _concurrent_write_attempt(db_path: Path, slug: str) -> str:
    """Try to commit a row from another connection, in another thread.

    Returns ``"committed"`` or ``"blocked: ..."``. The busy timeout is short
    on purpose: the question is whether SQLite lets the write through *now*,
    not whether it eventually can.
    """
    outcome: list[str] = []

    def run() -> None:
        conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=0)
        try:
            conn.execute("PRAGMA busy_timeout=250")
            _seed_workspace(conn, slug)
            outcome.append("committed")
        except sqlite3.OperationalError as exc:
            outcome.append(f"blocked: {exc}")
        finally:
            conn.close()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "the concurrent writer never finished"
    return outcome[0]


def test_a_concurrent_writer_cannot_slip_between_backup_and_repair(isolated_db, monkeypatch):
    """Regression: the archive is the exact state the repair mutates.

    Capture, verification, and mutation used to be three independent
    connections with no lock between them, so a write committed after the
    fingerprint matched was outside the safety net the repair then relied on.
    Applying now takes ``BEGIN IMMEDIATE`` first and holds it through all
    three, so this test can hammer the two windows that mattered - just after
    the archive is written, and just after the freshness verdict is taken -
    and neither writer gets in.
    """
    import brains.backup as backup_module

    _seed_anomalies(isolated_db)
    archive = isolated_db.parent / "serialized.tar.gz"

    attempts: list[str] = []
    real_create = backup_module.create_backup
    real_verify = backup_module.verify_backup

    def create_and_race(out_path, **kwargs):
        result = real_create(out_path, **kwargs)
        attempts.append(_concurrent_write_attempt(isolated_db, "ws-during-backup"))
        return result

    def verify_and_race(archive_path, **kwargs):
        verification = real_verify(archive_path, **kwargs)
        attempts.append(_concurrent_write_attempt(isolated_db, "ws-during-verify"))
        return verification

    monkeypatch.setattr(backup_module, "create_backup", create_and_race)
    monkeypatch.setattr(backup_module, "verify_backup", verify_and_race)

    payload = repair_database(isolated_db, apply=True, backup_to=archive, now=FIXED_NOW)

    assert payload["applied"] is True
    assert attempts == [
        f"blocked: {sqlite3.OperationalError('database is locked')}",
        f"blocked: {sqlite3.OperationalError('database is locked')}",
    ], attempts
    assert payload["backup"]["checks"]["bound_under_source_write_lock"] is True
    assert payload["backup"]["checks"]["source_write_lock_held"] is True

    # ``--backup-to`` is truthful: the archive holds the pre-repair state that
    # the repair then mutated, and neither blocked writer is in either one.
    captured = _extract_archive_database(archive, isolated_db.parent / "captured")
    conn = sqlite3.connect(str(captured))
    try:
        assert (
            conn.execute("SELECT state FROM agent_sessions WHERE id='ses-reaped'").fetchone()[0]
            == "running"
        )
        assert conn.execute("SELECT COUNT(*) FROM workspace_claims").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workspaces WHERE slug LIKE 'ws-during-%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()

    conn = _connect(isolated_db)
    try:
        assert (
            conn.execute("SELECT state FROM agent_sessions WHERE id='ses-reaped'").fetchone()[0]
            == "failed"
        )
        assert conn.execute("SELECT COUNT(*) FROM workspace_claims").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM workspaces WHERE slug LIKE 'ws-during-%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()

    # The lock is a window, not a permanent state.
    assert _concurrent_write_attempt(isolated_db, "ws-after-repair") == "committed"


def test_apply_refuses_to_start_when_another_writer_holds_the_lock(isolated_db):
    """No lock, no repair: the prerequisite is quiescence, not optimism."""
    _seed_anomalies(isolated_db)
    before = _snapshot(isolated_db)

    blocker = sqlite3.connect(str(isolated_db), isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(integrity.WriteLockUnavailableError) as exc:
            repair_database(
                isolated_db,
                apply=True,
                backup_to=isolated_db.parent / "unreachable.tar.gz",
                now=FIXED_NOW,
            )
        blocker.execute("ROLLBACK")
    finally:
        blocker.close()

    assert "could not acquire the SQLite write lock" in str(exc.value)
    assert not (isolated_db.parent / "unreachable.tar.gz").exists()
    assert _snapshot(isolated_db) == before


def test_dry_run_takes_no_write_lock(isolated_db):
    """Read-only behaviour is preserved: a dry run runs beside a live writer."""
    _seed_anomalies(isolated_db)

    blocker = sqlite3.connect(str(isolated_db), isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        payload = repair_database(isolated_db, apply=False, now=FIXED_NOW)
        blocker.execute("ROLLBACK")
    finally:
        blocker.close()

    assert payload["dry_run"] is True
    assert payload["applied"] is False
    assert payload["planned_actions"]


def test_engine_integrity_check_is_not_repeated_per_convergence_pass(isolated_db, monkeypatch):
    """Instrumentation: engine scans are preflight/postflight, not per pass.

    ``PRAGMA integrity_check`` reads every page in the database and its answer
    cannot change under a transaction that only runs DML, so re-running it for
    each convergence pass would multiply the most expensive statement in the
    module by the pass count - while holding the write lock. The count here is
    the same for a one-pass and a two-pass repair: one preflight under the
    lock, one full post-repair verdict.

    ``PRAGMA foreign_key_check`` is scoped rather than dropped: after a pass it
    only re-checks the tables that pass could have broken, and the full check
    still runs at both ends.
    """
    conn = _connect(isolated_db)
    try:
        org_id = _default_org_id(conn)
        workspace_id = _seed_workspace(conn, "ws-instrumented", org_id=org_id)
        _seed_session(
            conn,
            "ses-instrumented",
            workspace_id,
            state="completed",
            last_activity_at="2026-03-01T00:00:00+00:00",
        )
        conn.execute(
            "INSERT INTO workspace_claims (workspace_id, session_id, scope, claimed_at, "
            "expires_at) VALUES (?, 'ses-instrumented', 'code', ?, ?)",
            (workspace_id, "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
        )
    finally:
        conn.close()

    integrity_calls: list[str] = []
    fk_scopes: list[tuple[str, ...] | None] = []
    real_integrity_check = integrity.integrity_check
    real_fk_violations = integrity.foreign_key_violations

    def counting_integrity_check(connection):
        integrity_calls.append("integrity_check")
        return real_integrity_check(connection)

    def recording_fk_violations(connection, *, tables=None):
        fk_scopes.append(None if tables is None else tuple(tables))
        return real_fk_violations(connection, tables=tables)

    monkeypatch.setattr(integrity, "integrity_check", counting_integrity_check)
    monkeypatch.setattr(integrity, "foreign_key_violations", recording_fk_violations)

    two_pass = repair_database(
        isolated_db,
        apply=True,
        backup_to=isolated_db.parent / "instrumented-a.tar.gz",
        now=FIXED_NOW,
    )
    assert two_pass["passes"] == 2
    assert len(integrity_calls) == 2, integrity_calls
    # Whole-database foreign-key checks: preflight and postflight only.
    assert fk_scopes.count(None) == 2, fk_scopes
    scoped = [scope for scope in fk_scopes if scope is not None]
    assert len(scoped) == 2, fk_scopes
    assert all("workspace_claims" in scope for scope in scoped), scoped

    # A one-pass repair pays exactly the same engine cost, which is what
    # "not per pass" means.
    integrity_calls.clear()
    fk_scopes.clear()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "INSERT INTO workspace_claims (workspace_id, session_id, scope, claimed_at, "
            "expires_at) VALUES (?, 'ses-instrumented', 'code', ?, ?)",
            (
                _seed_workspace(conn, "ws-instrumented-2", org_id=_default_org_id(conn)),
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()

    one_pass = repair_database(
        isolated_db,
        apply=True,
        backup_to=isolated_db.parent / "instrumented-b.tar.gz",
        now=FIXED_NOW,
    )
    assert one_pass["passes"] == 1
    assert len(integrity_calls) == 2, integrity_calls
    assert fk_scopes.count(None) == 2, fk_scopes


def test_repair_rolls_back_everything_when_it_cannot_converge(isolated_db, monkeypatch):
    _seed_anomalies(isolated_db)
    before = _snapshot(isolated_db)

    real_plan = integrity.plan_repair

    def never_finishing_plan(conn, report, **kwargs):
        planned = real_plan(conn, report, **kwargs)
        # A plan that keeps proposing work no matter what the database says.
        return planned or [
            RepairAction(
                code="test.no_op",
                table="agent_sessions",
                description="always proposes itself",
                statement="UPDATE agent_sessions SET state = state",
            )
        ]

    monkeypatch.setattr(integrity, "plan_repair", never_finishing_plan)

    with pytest.raises(integrity.RepairNotConvergedError):
        repair_database(
            isolated_db,
            apply=True,
            backup_to=isolated_db.parent / "no-converge.tar.gz",
            now=FIXED_NOW,
        )
    assert _snapshot(isolated_db) == before


def test_diagnosis_survives_a_pre_migration_agent_sessions_schema(tmp_path):
    """Regression: a store from before migration 122 has no last_activity_at.

    Diagnosis must report what this schema *can* answer and say which checks
    it could not run, rather than raising OperationalError half way through.
    """
    legacy = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(legacy))
    try:
        conn.executescript(
            """
            CREATE TABLE agent_sessions (
                id TEXT PRIMARY KEY,
                workspace_id INTEGER,
                tool TEXT,
                state TEXT,
                started_at TEXT,
                ended_at TEXT
            );
            INSERT INTO agent_sessions (id, state, started_at, ended_at)
            VALUES ('ses-legacy', 'completed', '2026-01-01T00:00:00+00:00', NULL);
            INSERT INTO agent_sessions (id, state, started_at, ended_at)
            VALUES ('ses-legacy-unknown', 'failed', NULL, NULL);
            """
        )
        conn.commit()
    finally:
        conn.close()

    report = diagnose_database(legacy, now=FIXED_NOW)

    codes = {f.code for f in report.findings}
    assert codes == {"session.terminal_without_ended_at", "session.terminal_ended_at_ambiguous"}
    stamped = next(f for f in report.findings if f.code == "session.terminal_without_ended_at")
    assert stamped.repair == "stamp ended_at from started_at"
    assert stamped.sample[0]["resolved_ended_at"] == "2026-01-01T00:00:00+00:00"

    skipped = {entry["check"]: entry for entry in report.skipped_checks}
    assert skipped["workspace.missing_org"]["reason"] == "table is absent"
    assert skipped["claim.expired"]["reason"] == "table is absent"
    assert report.to_dict()["counts"]["skipped_checks"] == len(report.skipped_checks)
    # Skipped is not passed: an unexamined invariant is unknown, not clean.
    assert report.complete is False
    assert report.ok is False
    assert report.to_dict()["complete"] is False

    payload = repair_database(legacy, apply=False, now=FIXED_NOW)
    statements = [action["statement"] for action in payload["planned_actions"]]
    assert statements == [
        "UPDATE agent_sessions SET ended_at = started_at WHERE ended_at IS NULL "
        "AND state IN (?,?) AND started_at IS NOT NULL"
    ]
    assert not any("last_activity_at" in statement for statement in statements)


# ----------------------------------------------------------------------
# Foreign-key enforcement gate
# ----------------------------------------------------------------------


def test_assert_foreign_keys_clean_passes_on_a_clean_store(isolated_db):
    with open_database(isolated_db, read_only=True) as conn:
        assert_foreign_keys_clean(conn)


def test_assert_foreign_keys_clean_names_the_violating_tables(isolated_db):
    conn = _connect(isolated_db)
    try:
        _seed_event(conn, None, "ses-missing", "agent_stdout")
    finally:
        conn.close()

    with (
        open_database(isolated_db, read_only=True) as conn,
        pytest.raises(ForeignKeyViolationsError) as exc,
    ):
        assert_foreign_keys_clean(conn)
    assert "events=1" in str(exc.value)
    assert "db repair" in str(exc.value)


def test_enforcement_gate_refuses_to_enable_over_a_dirty_store(isolated_db):
    conn = _connect(isolated_db)
    try:
        _seed_event(conn, None, "ses-missing", "agent_stdout")
        cursor = conn.cursor()
        with pytest.raises(ForeignKeyViolationsError):
            db_module._enforce_sqlite_foreign_keys(cursor)
        assert cursor.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        conn.close()


def test_enforcement_gate_enables_over_a_clean_store(isolated_db, monkeypatch):
    monkeypatch.setattr(db_module, "_fk_verified_databases", set())
    conn = _connect(isolated_db)
    try:
        cursor = conn.cursor()
        db_module._enforce_sqlite_foreign_keys(cursor)
        assert cursor.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Writer paths
# ----------------------------------------------------------------------


def test_reaping_a_zombie_session_synchronizes_state_with_ended_at(tmp_path):
    """Writer regression: ``ended_at`` and ``state`` must move together.

    This exercises the real control plane, so it runs against the shared
    per-process test database provided by ``tests/conftest.py`` rather than
    the monkeypatched fixture above: ``brains.control.sessions`` binds its
    session factory at import time.
    """
    from sqlalchemy import text

    from brains.control.sessions import (
        current_machine_id,
        reap_zombie_sessions,
        register_workspace,
    )
    from brains.storage.db import SessionLocal

    workspace = register_workspace(str(tmp_path / "zombie-repo"))
    session_id = "ses-zombie-integrity"
    with SessionLocal() as session:
        session.execute(
            text(
                "INSERT INTO agent_sessions (id, workspace_id, tool, pid, machine_id, state, "
                # Reaper contract: dead pid AND stale heartbeat.
                "started_at, last_activity_at) VALUES (:id, :ws, 'pytest', 999999, :machine, "
                "'running', :ts, :ts)"
            ),
            {
                "id": session_id,
                "ws": workspace.id,
                "machine": current_machine_id(),
                "ts": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            },
        )
        session.commit()

    assert session_id in reap_zombie_sessions()

    with SessionLocal() as session:
        row = session.execute(
            text("SELECT state, ended_at FROM agent_sessions WHERE id = :id"),
            {"id": session_id},
        ).one()
    assert row.ended_at is not None
    assert row.state == "failed"


def test_registering_a_workspace_assigns_org_scope(tmp_path):
    from brains.control.sessions import register_workspace

    workspace = register_workspace(str(tmp_path / "scoped-repo"))
    assert workspace.org_id is not None


def test_registering_a_workspace_rejects_an_unknown_org(tmp_path):
    from brains.control.sessions import register_workspace

    with pytest.raises(ValueError, match="unknown org"):
        register_workspace(str(tmp_path / "bad-org"), org_id=9999)


# ----------------------------------------------------------------------
# CLI surface
# ----------------------------------------------------------------------


def test_db_diagnose_cli_exits_non_zero_with_findings(isolated_db):
    from brains.cli.app import app as cli_app

    _seed_anomalies(isolated_db)
    result = CliRunner().invoke(cli_app, ["db", "diagnose"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["counts"]["findings"] > 0


def test_db_repair_cli_dry_run_then_apply(isolated_db):
    from brains.audit import list_entries
    from brains.cli.app import app as cli_app

    _seed_anomalies(isolated_db)
    runner = CliRunner()
    before = _snapshot_without_audit(isolated_db)

    dry = runner.invoke(cli_app, ["db", "repair"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout)["dry_run"] is True
    assert _snapshot(isolated_db) == before | _audit_tables(isolated_db)

    refused = runner.invoke(cli_app, ["db", "repair", "--apply"])
    assert refused.exit_code == 2
    # Nothing was repaired - and the log says an operator tried to. The
    # attempt is recorded before the repair can take the write lock, so a
    # refusal leaves attempted+failed evidence rather than no trace at all.
    assert _snapshot_without_audit(isolated_db) == before
    assert [entry["action"] for entry in list_entries(action_prefix="admin.db_repaired")] == [
        "admin.db_repaired.failed",
        "admin.db_repaired.attempted",
    ]

    applied = runner.invoke(
        cli_app,
        ["db", "repair", "--apply", "--backup-to", str(isolated_db.parent / "cli.tar.gz")],
    )
    # This fixture keeps one row whose terminal state cannot be derived, so the
    # store is not clean afterwards and the exit code has to say so.
    assert applied.exit_code == 1, applied.output
    payload = json.loads(applied.stdout)
    assert payload["applied"] is True
    assert payload["post_repair"]["foreign_key_violations"] == 0
    assert payload["post_repair"]["ok"] is False
    assert {f["classification"] for f in payload["post_repair"]["remaining_findings"]} == {
        "ambiguous_legacy"
    }


def test_db_repair_cli_is_refused_when_its_attempt_cannot_be_recorded(isolated_db, monkeypatch):
    """A repair that cannot be recorded must not take the write lock at all.

    The repair holds the SQLite write lock across its whole transaction, so
    its record cannot join that transaction. The attempt is therefore
    committed first, and a store that cannot accept it refuses the repair
    instead of mutating and then discovering it has nothing to say about it.
    """
    import brains.audit as audit_module_local
    from brains.cli.app import app as cli_app

    class _BrokenSession:
        def __enter__(self):
            raise RuntimeError("audit store is down")

        def __exit__(self, *args):
            return False

    _seed_anomalies(isolated_db)
    before = _snapshot(isolated_db)
    archive = isolated_db.parent / "unrecorded.tar.gz"
    monkeypatch.setattr(audit_module_local, "SessionLocal", lambda: _BrokenSession())

    refused = CliRunner().invoke(cli_app, ["db", "repair", "--apply", "--backup-to", str(archive)])

    assert refused.exit_code == 3, refused.output
    assert _snapshot(isolated_db) == before
    assert not archive.exists()


def test_db_repair_cli_records_the_attempt_before_it_repairs(isolated_db):
    """The applied repair carries attempted + completed evidence, in order."""
    from brains.audit import list_entries
    from brains.cli.app import app as cli_app

    _seed_anomalies(isolated_db)
    applied = CliRunner().invoke(
        cli_app,
        ["db", "repair", "--apply", "--backup-to", str(isolated_db.parent / "recorded.tar.gz")],
    )
    assert applied.exit_code in (0, 1), applied.output

    entries = list_entries(action_prefix="admin.db_repaired", limit=10)
    assert [entry["action"] for entry in entries] == [
        "admin.db_repaired",
        "admin.db_repaired.attempted",
    ]
    assert entries[0]["payload"]["attempt_audit_id"] == entries[-1]["id"]
    assert entries[-1]["id"] < entries[0]["id"]


def test_db_repair_cli_exits_zero_only_when_nothing_remains(isolated_db):
    """Regression: a converged repair reports success; a partial one does not.

    The Session below is ``completed`` without an ``ended_at`` and holds an
    unexpired claim. Repair has to settle both in one transaction, otherwise
    it commits a store that its own diagnosis rejects while exiting 0.
    """
    from brains.cli.app import app as cli_app

    conn = _connect(isolated_db)
    try:
        workspace_id = _seed_workspace(conn, "ws-cli-converge", org_id=_default_org_id(conn))
        _seed_session(
            conn,
            "ses-cli-completed",
            workspace_id,
            state="completed",
            last_activity_at="2026-03-01T00:00:00+00:00",
        )
        conn.execute(
            "INSERT INTO workspace_claims (workspace_id, session_id, scope, claimed_at, "
            "expires_at) VALUES (?, 'ses-cli-completed', 'code', ?, ?)",
            (workspace_id, "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
        )
    finally:
        conn.close()

    applied = CliRunner().invoke(
        cli_app,
        ["db", "repair", "--apply", "--backup-to", str(isolated_db.parent / "cli-converge.tar.gz")],
    )
    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.stdout)
    assert payload["passes"] == 2
    assert payload["post_repair"]["ok"] is True

    conn = _connect(isolated_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM workspace_claims").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_skipped_check_prevents_a_clean_diagnosis_and_a_clean_exit(isolated_db):
    """Regression: missing coverage must not read as a clean store.

    Dropping a table this build's invariants are expressed over is the same
    situation as a store that predates the migration which created it: the
    check cannot run, so its result is unknown. ``ok`` used to ignore that and
    report success from an empty finding list; now the report carries an
    explicit ``complete`` field, ``ok`` requires it, and both the diagnosis
    and the repair readiness verdict exit non-zero.
    """
    from brains.cli.app import app as cli_app

    conn = _connect(isolated_db)
    try:
        conn.execute("DROP TABLE workspace_claims")
    finally:
        conn.close()

    report = diagnose_database(isolated_db, now=FIXED_NOW)
    assert report.findings == ()
    assert report.integrity_check == ("ok",)
    assert [entry["check"] for entry in report.skipped_checks] == ["claim.expired"]
    assert report.complete is False
    assert report.ok is False

    runner = CliRunner()
    diagnosed = runner.invoke(cli_app, ["db", "diagnose"])
    assert diagnosed.exit_code == 1, diagnosed.output
    diagnosis = json.loads(diagnosed.stdout)
    assert diagnosis["ok"] is False
    assert diagnosis["complete"] is False
    assert diagnosis["counts"]["findings"] == 0
    assert diagnosis["counts"]["skipped_checks"] == 1

    applied = runner.invoke(
        cli_app,
        [
            "db",
            "repair",
            "--apply",
            "--backup-to",
            str(isolated_db.parent / "incomplete.tar.gz"),
        ],
    )
    assert applied.exit_code == 1, applied.output
    payload = json.loads(applied.stdout)
    assert payload["applied"] is True
    assert payload["planned_actions"] == []
    assert payload["post_repair"]["remaining_findings"] == []
    assert payload["post_repair"]["complete"] is False
    assert payload["post_repair"]["ok"] is False
    assert [entry["check"] for entry in payload["post_repair"]["skipped_checks"]] == [
        "claim.expired"
    ]


def test_db_verify_backup_cli(isolated_db):
    from brains.cli.app import app as cli_app

    archive = isolated_db.parent / "cli-verify.tar.gz"
    create_backup(archive)
    runner = CliRunner()

    ok = runner.invoke(
        cli_app, ["db", "verify-backup", str(archive), "--expect-source", str(isolated_db)]
    )
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["ok"] is True

    bad = isolated_db.parent / "cli-bad.tar.gz"
    bad.write_bytes(b"nope")
    failed = runner.invoke(cli_app, ["db", "verify-backup", str(bad)])
    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["ok"] is False


def test_db_fk_check_cli(isolated_db):
    from brains.cli.app import app as cli_app

    runner = CliRunner()
    clean = runner.invoke(cli_app, ["db", "fk-check"])
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.stdout)["clean"] is True

    conn = _connect(isolated_db)
    try:
        _seed_event(conn, None, "ses-missing", "agent_stdout")
    finally:
        conn.close()

    dirty = runner.invoke(cli_app, ["db", "fk-check"])
    assert dirty.exit_code == 1
    assert json.loads(dirty.stdout)["clean"] is False


def test_expired_claim_uses_the_supplied_evaluation_time(isolated_db):
    conn = _connect(isolated_db)
    try:
        workspace_id = _seed_workspace(conn, "ws-claim")
        _seed_session(conn, "ses-claim", workspace_id)
        conn.execute(
            "INSERT INTO workspace_claims (workspace_id, session_id, scope, claimed_at, "
            "expires_at) VALUES (?, 'ses-claim', 'code', ?, ?)",
            (
                workspace_id,
                FIXED_NOW.isoformat(),
                (FIXED_NOW + timedelta(hours=1)).isoformat(),
            ),
        )
    finally:
        conn.close()

    fresh = diagnose_database(isolated_db, now=FIXED_NOW)
    assert not [f for f in fresh.findings if f.code == "claim.expired"]

    later = diagnose_database(isolated_db, now=FIXED_NOW + timedelta(hours=2))
    assert [f for f in later.findings if f.code == "claim.expired"]


def test_live_claim_written_through_the_orm_is_not_reported_as_expired(isolated_db):
    """Regression: the stored encoding is not ISO-8601, so compare as time.

    SQLAlchemy writes ``YYYY-MM-DD HH:MM:SS.ffffff`` while the evaluation
    instant is ISO-8601 with a ``T`` separator and an offset. A plain text
    comparison of the two would report every lease expiring later on the same
    day as expired and delete live lock state.
    """
    from brains.storage.models import WorkspaceClaim

    conn = _connect(isolated_db)
    try:
        workspace_id = _seed_workspace(conn, "ws-orm-claim")
        _seed_session(conn, "ses-orm-claim", workspace_id)
    finally:
        conn.close()

    with db_module.SessionLocal() as session:
        session.add(
            WorkspaceClaim(
                workspace_id=workspace_id,
                session_id="ses-orm-claim",
                scope="code",
                claimed_at=FIXED_NOW,
                expires_at=FIXED_NOW + timedelta(hours=11),
            )
        )
        session.commit()

    conn = _connect(isolated_db)
    try:
        stored = conn.execute("SELECT expires_at FROM workspace_claims").fetchone()[0]
    finally:
        conn.close()
    assert "T" not in stored, f"unexpected storage encoding: {stored!r}"

    fresh = diagnose_database(isolated_db, now=FIXED_NOW)
    assert not [f for f in fresh.findings if f.code == "claim.expired"]

    later = diagnose_database(isolated_db, now=FIXED_NOW + timedelta(hours=12))
    expired = [f for f in later.findings if f.code == "claim.expired"]
    assert len(expired) == 1
    assert expired[0].count == 1

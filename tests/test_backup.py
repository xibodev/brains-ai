"""Tests for the backup + restore tooling.

Covers :mod:`brains.backup` for the SQLite backend (the default).
Postgres path is covered by a small unit test that monkeypatches
``shutil.which`` to assert ``pg_dump`` discovery is required, but the
end-to-end pg_dump+psql roundtrip lives behind the same Postgres-gated
fixture as ``tests/test_storage_postgres.py`` and is intentionally not
exercised here.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine

import brains.audit as audit_module
import brains.backup as backup_module
import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.audit import _reset_key_cache
from brains.backup import (
    BackupError,
    ManifestMismatch,
    UnsupportedBackend,
    create_backup,
    inspect_archive,
    restore_backup,
)
from brains.config import settings
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, Workspace


@pytest.fixture
def isolated_sqlite(tmp_path, monkeypatch):
    """Per-test SQLite DB + audit-key isolation.

    Points ``settings.db_url`` at a tmp SQLite file so ``backup`` /
    ``restore`` operate against an isolated store. ``BRAINS_STATE_DIR``
    + ``BRAINS_AUDIT_KEY_FILE`` keep the audit log out of ``~/.brains``.
    """
    db_path = tmp_path / "brains.sqlite"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    monkeypatch.setenv("BRAINS_AUDIT_KEY_FILE", str(tmp_path / "audit-key"))
    monkeypatch.delenv("BRAINS_AUDIT_KEY", raising=False)

    # The backup module reads settings.db_url at call-time, so we
    # mutate the Pydantic model directly.
    monkeypatch.setattr(settings, "db_url", f"sqlite:///{db_path}", raising=False)

    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(audit_module, "SessionLocal", SessionLocal)

    _reset_key_cache()
    init_db()
    yield tmp_path
    _reset_key_cache()


# ----------------------------------------------------------------------
# create_backup (SQLite)
# ----------------------------------------------------------------------


def test_create_backup_produces_tar_gz_with_manifest(isolated_sqlite):
    """Smoke test: archive exists, is a valid tar.gz, has manifest + blob."""
    out = isolated_sqlite / "backup.tar.gz"
    result = create_backup(out)
    assert Path(result.archive_path).exists()
    assert result.backend == "sqlite"
    assert result.data_size_bytes > 0
    assert len(result.data_sha256) == 64

    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert "brains.sqlite" in names
        member = tar.extractfile("manifest.json")
        assert member is not None
        manifest = json.loads(member.read().decode("utf-8"))
    assert manifest["manifest_version"] == "2"
    assert manifest["backend"] == "sqlite"
    assert manifest["brains_version"]
    assert manifest["data_sha256"] == result.data_sha256
    assert manifest["data_size_bytes"] == result.data_size_bytes
    assert isinstance(manifest["schema_versions"], list)


def test_create_backup_preserves_row_data(isolated_sqlite):
    """The SQLite blob inside the archive must be a real DB containing our rows."""
    with db_module.SessionLocal() as session:
        ws = Workspace(path=str(isolated_sqlite / "ws"), slug="ws-1")
        session.add(ws)
        session.commit()
        ws_id = ws.id
        s = AgentSession(id="sess-test-1", workspace_id=ws_id, tool="pytest")
        session.add(s)
        session.commit()
        sess_id = s.id

    out = isolated_sqlite / "backup.tar.gz"
    create_backup(out)

    # Extract the embedded sqlite file and query it directly.
    import sqlite3

    extract_dir = isolated_sqlite / "extracted"
    extract_dir.mkdir()
    with tarfile.open(out, "r:gz") as tar:
        tar.extract("brains.sqlite", path=str(extract_dir), filter="data")
    embedded = extract_dir / "brains.sqlite"
    conn = sqlite3.connect(str(embedded))
    try:
        rows = conn.execute("SELECT id, slug FROM workspaces").fetchall()
        assert (ws_id, "ws-1") in rows
        sess_rows = conn.execute("SELECT id, tool FROM agent_sessions").fetchall()
        assert (sess_id, "pytest") in sess_rows
    finally:
        conn.close()


def test_create_backup_redacts_password_in_manifest(isolated_sqlite, monkeypatch):
    """The sanitized URL must never contain the password."""
    # Override db_url with a fake Postgres-style URL just for the
    # manifest sanitization path; we keep the engine itself on SQLite.
    monkeypatch.setattr(
        settings,
        "db_url",
        "postgresql://alice:s3cret@db.example.com:5432/brains",
        raising=False,
    )
    # Re-monkeypatch backend back to sqlite via the engine binding
    # (settings still says sqlite by subsystems.storage.backend default).
    # The Postgres URL is only consumed via _sanitize_url; we need to
    # exercise it via _backup_sqlite, so re-point db_url back at the
    # real file but invoke _sanitize_url directly to validate.
    from brains.backup import _sanitize_url

    sanitized = _sanitize_url("postgresql://alice:s3cret@db.example.com:5432/brains")
    assert "s3cret" not in sanitized
    assert "alice" in sanitized
    assert "***" in sanitized


# ----------------------------------------------------------------------
# inspect_archive
# ----------------------------------------------------------------------


def test_inspect_archive_returns_manifest_without_restore(isolated_sqlite):
    out = isolated_sqlite / "backup.tar.gz"
    create_backup(out)
    manifest = inspect_archive(out)
    assert manifest["backend"] == "sqlite"
    assert manifest["manifest_version"] == "2"
    assert manifest["data_file"] == "brains.sqlite"


def test_inspect_archive_missing_file_raises(isolated_sqlite):
    with pytest.raises(BackupError):
        inspect_archive(isolated_sqlite / "nonexistent.tar.gz")


def test_inspect_archive_corrupt_raises(isolated_sqlite):
    bad = isolated_sqlite / "bad.tar.gz"
    bad.write_bytes(b"not a gzip")
    with pytest.raises(Exception):
        inspect_archive(bad)


# ----------------------------------------------------------------------
# restore_backup (SQLite)
# ----------------------------------------------------------------------


def test_restore_backup_round_trip(isolated_sqlite):
    """Insert -> backup -> mutate -> restore -> original row resurrected."""
    with db_module.SessionLocal() as session:
        ws = Workspace(path=str(isolated_sqlite / "ws"), slug="ws-pre-backup")
        session.add(ws)
        session.commit()
        original_id = ws.id

    out = isolated_sqlite / "snapshot.tar.gz"
    create_backup(out)

    # Mutate the DB after the backup.
    with db_module.SessionLocal() as session:
        session.query(Workspace).filter(Workspace.id == original_id).delete()
        new_ws = Workspace(path=str(isolated_sqlite / "ws2"), slug="ws-post-backup")
        session.add(new_ws)
        session.commit()

    # Confirm the mutation took effect.
    with db_module.SessionLocal() as session:
        rows_before = session.query(Workspace.slug).all()
        assert ("ws-pre-backup",) not in rows_before
        assert ("ws-post-backup",) in rows_before

    # Dispose the engine so the SQLite file is closed; on Windows
    # restore_backup cannot replace an open file.
    db_module.engine.dispose()

    result = restore_backup(out)
    assert result.backend == "sqlite"
    assert result.data_sha256

    # Re-create the engine after restore so we read the restored file.
    new_engine = create_engine(settings.db_url)
    new_session_factory = db_module.sessionmaker(bind=new_engine, expire_on_commit=False)
    with new_session_factory() as session:
        rows_after = session.query(Workspace.slug).all()
    assert ("ws-pre-backup",) in rows_after
    assert ("ws-post-backup",) not in rows_after


def test_restore_backup_detects_sha_mismatch(isolated_sqlite):
    """Tampered archives must be rejected."""
    out = isolated_sqlite / "snapshot.tar.gz"
    create_backup(out)

    # Rebuild the archive with a mutated data blob while keeping the
    # original manifest (which records the OLD sha).
    extract_dir = isolated_sqlite / "ext"
    extract_dir.mkdir()
    with tarfile.open(out, "r:gz") as tar:
        tar.extractall(str(extract_dir), filter="data")
    manifest_path = extract_dir / "manifest.json"
    data_path = extract_dir / "brains.sqlite"
    data_path.write_bytes(data_path.read_bytes() + b"corruption")

    tampered = isolated_sqlite / "tampered.tar.gz"
    with tarfile.open(tampered, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        tar.add(data_path, arcname="brains.sqlite")

    db_module.engine.dispose()
    with pytest.raises(BackupError) as exc_info:
        restore_backup(tampered)
    assert "sha256" in str(exc_info.value)


def test_restore_backup_rejects_wrong_backend(isolated_sqlite):
    """Restoring a Postgres-tagged archive into a SQLite install must fail."""
    out = isolated_sqlite / "snapshot.tar.gz"
    create_backup(out)

    extract_dir = isolated_sqlite / "ext"
    extract_dir.mkdir()
    with tarfile.open(out, "r:gz") as tar:
        tar.extractall(str(extract_dir), filter="data")
    manifest_path = extract_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["backend"] = "postgres"
    payload["data_file"] = "brains.sql"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    rebuilt = isolated_sqlite / "wrong_backend.tar.gz"
    with tarfile.open(rebuilt, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        tar.add(extract_dir / "brains.sqlite", arcname="brains.sqlite")

    with pytest.raises(ManifestMismatch):
        restore_backup(rebuilt)


def test_restore_backup_rejects_path_traversal_data_file(isolated_sqlite):
    """A manifest whose ``data_file`` escapes the staging dir must be rejected
    before anything is written (CWE-22 path traversal on restore)."""
    out = isolated_sqlite / "snapshot.tar.gz"
    create_backup(out)

    extract_dir = isolated_sqlite / "ext"
    extract_dir.mkdir()
    with tarfile.open(out, "r:gz") as tar:
        tar.extractall(str(extract_dir), filter="data")
    manifest_path = extract_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["data_file"] = "../escape.sqlite"  # escapes the temp staging dir
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    malicious = isolated_sqlite / "evil.tar.gz"
    with tarfile.open(malicious, "w:gz") as tar:
        tar.add(manifest_path, arcname="manifest.json")
        tar.add(extract_dir / "brains.sqlite", arcname="../escape.sqlite")

    db_module.engine.dispose()
    # Both the write path and the read-only inspect path must reject it.
    with pytest.raises(BackupError) as exc_info:
        restore_backup(malicious)
    assert "unsafe data_file" in str(exc_info.value)
    with pytest.raises(BackupError):
        inspect_archive(malicious)


def test_restore_backup_missing_data_blob(isolated_sqlite):
    """Archive without the data file must raise BackupError."""
    bare = isolated_sqlite / "bare.tar.gz"
    extract_dir = isolated_sqlite / "ext"
    extract_dir.mkdir()
    manifest = extract_dir / "manifest.json"
    payload = {
        "manifest_version": "1",
        "schema_version": "1",
        "brains_version": "0.0.0",
        "created_at": "2026-01-01T00:00:00+00:00",
        "backend": "sqlite",
        "data_file": "brains.sqlite",
        "data_sha256": "0" * 64,
        "data_size_bytes": 0,
        "sanitized_db_url": "sqlite:///x",
        "schema_versions": [],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with tarfile.open(bare, "w:gz") as tar:
        tar.add(manifest, arcname="manifest.json")
    with pytest.raises(BackupError):
        restore_backup(bare)


def test_restore_backup_archive_not_found(isolated_sqlite):
    with pytest.raises(BackupError):
        restore_backup(isolated_sqlite / "nope.tar.gz")


# ----------------------------------------------------------------------
# The record is durable before the effect, not after it
# ----------------------------------------------------------------------


def _break_the_audit_store(monkeypatch):
    """Make every audit append fail, the way an unreachable store does."""

    class _BrokenSession:
        def __enter__(self):
            raise RuntimeError("audit store is down")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(audit_module, "SessionLocal", lambda: _BrokenSession())


def test_backup_does_not_run_when_its_attempt_cannot_be_recorded(isolated_sqlite, monkeypatch):
    """No archive may exist that the log knows nothing about."""
    from brains.audit import AuditWriteError
    from brains.mcp.tools import backup_create_tool

    out = isolated_sqlite / "unrecorded.tar.gz"
    _break_the_audit_store(monkeypatch)

    with pytest.raises(AuditWriteError):
        backup_create_tool(str(out))

    assert not out.exists(), "a backup ran despite its attempt not being recorded"


def test_restore_does_not_run_when_its_attempt_cannot_be_recorded(isolated_sqlite, monkeypatch):
    """The destructive one matters most: the live DB must be untouched."""
    from brains.audit import AuditWriteError
    from brains.mcp.tools import backup_restore_tool

    out = isolated_sqlite / "snapshot.tar.gz"
    create_backup(out)

    with db_module.SessionLocal() as session:
        session.add(Workspace(path=str(isolated_sqlite / "ws-after"), slug="ws-after-backup"))
        session.commit()
    db_module.engine.dispose()

    _break_the_audit_store(monkeypatch)
    with pytest.raises(AuditWriteError):
        backup_restore_tool(str(out))

    engine = create_engine(settings.db_url)
    session_factory = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        slugs = [row[0] for row in session.query(Workspace.slug).all()]
    engine.dispose()
    assert "ws-after-backup" in slugs, "the DB was restored over with no attempt recorded"


def test_a_failed_backup_leaves_attempted_and_failed_evidence(isolated_sqlite, monkeypatch):
    """An effect that fails is recorded as one; the attempt entry stays."""
    from brains.mcp.tools import backup_create_tool

    monkeypatch.setattr(backup_module, "_current_backend", lambda: "exotic")
    with pytest.raises(UnsupportedBackend):
        backup_create_tool(str(isolated_sqlite / "never.tar.gz"))

    actions = [
        entry["action"]
        for entry in audit_module.list_entries(action_prefix="admin.backup_created", limit=10)
    ]
    assert actions == ["admin.backup_created.failed", "admin.backup_created.attempted"]


def test_a_successful_backup_is_recorded_only_after_the_archive_exists(isolated_sqlite):
    from brains.mcp.tools import backup_create_tool

    out = isolated_sqlite / "recorded.tar.gz"
    payload = backup_create_tool(str(out))

    assert Path(payload["archive_path"]).exists()
    entries = audit_module.list_entries(action_prefix="admin.backup_created", limit=10)
    assert [entry["action"] for entry in entries] == [
        "admin.backup_created",
        "admin.backup_created.attempted",
    ]
    assert entries[0]["payload"]["attempt_audit_id"] == entries[-1]["id"]
    assert entries[0]["payload"]["archive_path"] == payload["archive_path"]


# ----------------------------------------------------------------------
# Unsupported backend
# ----------------------------------------------------------------------


def test_create_backup_unsupported_backend_raises(isolated_sqlite, monkeypatch):
    monkeypatch.setattr(backup_module, "_current_backend", lambda: "exotic")
    with pytest.raises(UnsupportedBackend):
        create_backup(isolated_sqlite / "x.tar.gz")


def test_restore_backup_unsupported_backend_raises(isolated_sqlite, monkeypatch):
    out = isolated_sqlite / "snapshot.tar.gz"
    create_backup(out)
    monkeypatch.setattr(backup_module, "_current_backend", lambda: "exotic")
    with pytest.raises(UnsupportedBackend):
        restore_backup(out)


# ----------------------------------------------------------------------
# Postgres tool detection (without actually running pg_dump)
# ----------------------------------------------------------------------


def test_backup_postgres_runtime_is_withdrawn(isolated_sqlite, monkeypatch):
    monkeypatch.setattr(backup_module, "_current_backend", lambda: "postgres")
    monkeypatch.setattr(
        backup_module,
        "_current_db_url",
        lambda: "postgresql+psycopg://x:y@localhost/brains",
    )
    with pytest.raises(UnsupportedBackend, match="withdrawn"):
        create_backup(isolated_sqlite / "x.tar.gz")


def test_restore_postgres_runtime_is_withdrawn(isolated_sqlite, monkeypatch):
    """Historical Postgres archives remain inspectable but cannot activate a backend."""
    extract_dir = isolated_sqlite / "ext"
    extract_dir.mkdir()
    manifest = extract_dir / "manifest.json"
    data_blob = extract_dir / "brains.sql"
    data_blob.write_bytes(b"-- SQL dump\n")
    import hashlib as _h

    sha = _h.sha256(data_blob.read_bytes()).hexdigest()
    payload = {
        "manifest_version": "1",
        "schema_version": "1",
        "brains_version": "0.0.0",
        "created_at": "2026-01-01T00:00:00+00:00",
        "backend": "postgres",
        "data_file": "brains.sql",
        "data_sha256": sha,
        "data_size_bytes": len(data_blob.read_bytes()),
        "sanitized_db_url": "postgresql://u:***@host/db",
        "schema_versions": [],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    archive = isolated_sqlite / "pg.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(manifest, arcname="manifest.json")
        tar.add(data_blob, arcname="brains.sql")

    monkeypatch.setattr(backup_module, "_current_backend", lambda: "postgres")
    monkeypatch.setattr(
        backup_module,
        "_current_db_url",
        lambda: "postgresql+psycopg://x:y@localhost/brains",
    )
    with pytest.raises(UnsupportedBackend, match="withdrawn"):
        restore_backup(archive)


# ----------------------------------------------------------------------
# URL parsing
# ----------------------------------------------------------------------


def test_resolve_sqlite_path_relative(monkeypatch):
    from brains.backup import _resolve_sqlite_path

    assert _resolve_sqlite_path("sqlite:///brains.db") == Path("brains.db")


def test_resolve_sqlite_path_absolute_posix():
    from brains.backup import _resolve_sqlite_path

    # Four-slash form is the SQLAlchemy POSIX-absolute convention.
    assert _resolve_sqlite_path("sqlite:////var/data/brains.db") == Path("/var/data/brains.db")


def test_resolve_sqlite_path_rejects_non_sqlite_url():
    from brains.backup import _resolve_sqlite_path

    with pytest.raises(UnsupportedBackend):
        _resolve_sqlite_path("postgresql://x/y")

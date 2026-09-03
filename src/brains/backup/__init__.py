"""Backup + restore tooling for the brains state database.

This module is the implementation of the Phase 6 (README) /
Phase 3 (roadmap) hardening bullet *"backup + restore tooling for
the brain DB"*. The contract is intentionally pragmatic:

* **SQLite backend (default)** — use the stdlib :mod:`sqlite3` online
  backup API. The connection stays open while the backup runs so
  concurrent writers do not corrupt the dump. The resulting file is
  bundled into a gzip+tar archive together with a JSON manifest.
* **Postgres backend** — shell out to ``pg_dump`` if it is on PATH.
  We do **not** bundle pg_dump itself (it ships with Postgres) so
  the function raises :class:`BackupToolUnavailable` if it is
  missing rather than silently producing a useless dump.

Archive layout::

    <out>.tar.gz
    ├── manifest.json    # see _Manifest for the schema
    └── brains.sqlite    # raw SQLite copy
        OR
    └── brains.sql       # pg_dump --format=plain output

The manifest captures the brains version, backend, schema versions,
and a sha256 of the DB blob so the restore path can refuse to
overwrite an unrelated install. SQLite manifests additionally record
the source path, schema fingerprint, and a fingerprint of the live
source at capture time, which is what lets :func:`verify_backup` prove
an archive still represents the database it was taken from.

Manifest compatibility
----------------------

``manifest_version`` is the archive's read contract, and it moves in one
direction only:

* this build **reads** ``1`` and ``2``. A ``1`` archive written by an older
  build inspects and restores normally here; its source-identity fields are
  absent, so verification reports them as unverifiable instead of passing
  them.
* this build **writes** ``2``. A ``2`` archive therefore requires *this build
  or later* to inspect, verify, or restore: an older build reads
  ``manifest_version`` as ``1`` semantics and has no reader contract for it.
  Keep the build that wrote an archive available for as long as the archive is
  part of a recovery plan, or re-take the backup with the build you intend to
  restore with.
* a version this build does not know (a future ``3``) is refused with a
  :class:`BackupError` naming the versions it can read, never a traceback.
  Unknown *fields* inside a readable version are ignored, so a future build
  may add manifest fields without breaking this reader.

Every malformed manifest - non-JSON, non-object, missing required field,
wrong field type, unsafe ``data_file`` - is likewise reported as a
:class:`BackupError`, so the CLI surfaces one clean message and exit code.

Backup/repair serialization
---------------------------

SQLite's online backup API cannot run against a connection that is holding a
write transaction (``sqlite3_backup_step`` never leaves ``SQLITE_BUSY``), so
"take the image from the repairing transaction" is not implementable. What is
implementable, and is what :class:`SourceWriteLock` provides, is an *enforced*
quiescence protocol: the repairing connection holds ``BEGIN IMMEDIATE`` for the
whole capture-verify-mutate-commit window, and every backup step run under that
window re-proves the lock is genuinely held before and after it acts. See
:meth:`SourceWriteLock.assert_held`.

Restore is deliberately destructive — it overwrites the target DB
file (SQLite) or runs ``psql -f`` to replay the dump (Postgres) —
so it lives behind a separate CLI verb (``brains restore``) and an
explicit ``--yes`` flag in the operator-facing surface.

Both verbs are recorded in two phases by their CLI/MCP callers via
:func:`brains.audit.required_effect`: ``admin.backup_created.attempted`` /
``admin.restore_run.attempted`` commits before the call reaches this module,
and ``admin.backup_created`` / ``admin.restore_run`` (or the matching
``.failed``) is appended after it returns. This module itself is unaware of
audit, which is why the ordering lives in its callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import MISSING as _MISSING
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

from brains import __version__ as _brains_version
from brains.config import settings
from brains.storage.backends import resolve_db_url

# --------------------------------------------------------------------- errors


class BackupError(RuntimeError):
    """Base for any backup/restore failure."""


class BackupToolUnavailable(BackupError):
    """Raised when a required external tool (pg_dump / psql) is missing."""


class UnsupportedBackend(BackupError):
    """Raised when the configured storage backend is not backupable."""


class ManifestMismatch(BackupError):
    """Raised when restoring an archive into an incompatible backend."""


class SourceLockLost(BackupError):
    """The caller's claim to have quiesced the source database is not true."""


# --------------------------------------------------------------------- types


@dataclass(frozen=True)
class _Manifest:
    """Manifest written to the archive root.

    ``brains_version`` is informational — restores across patch
    versions are fine, but mixing alpha series (0.1.x → 0.2.x) is
    flagged in the log so operators notice.

    Manifest ``2`` adds the *source identity* fields that make a backup
    verifiable rather than merely present: the resolved source path, the
    SQLite header identity (``application_id`` / ``user_version`` /
    ``page_size``), a fingerprint of the exact schema objects, the per-table
    row counts at capture time, the ``foreign_key_check`` count observed on
    the copy, and ``source_fingerprint`` - the fingerprint of the *live*
    source at capture time, which is what makes staleness detectable.
    :func:`verify_backup` replays those claims against an isolated restore.
    Older ``1`` archives are still readable; their new fields are empty and
    verification reports them as unverifiable rather than pretending they
    passed. A ``2`` archive written before ``source_fingerprint`` existed is
    read the same way: usable, but not bindable to a live database. In the
    other direction the contract is strict: a ``2`` archive needs this build
    or later to inspect, verify, or restore, and an unknown future version is
    refused by name rather than misread.
    """

    schema_version: str
    brains_version: str
    created_at: str
    backend: str
    data_file: str  # name of the blob inside the tarball
    data_sha256: str
    data_size_bytes: int
    sanitized_db_url: str
    schema_versions: list[str]
    manifest_version: str = "1"
    source_path: str = ""
    source_identity: dict[str, Any] = dc_field(default_factory=dict)
    schema_fingerprint: str = ""
    schema_objects: list[str] = dc_field(default_factory=list)
    table_row_counts: dict[str, int] = dc_field(default_factory=dict)
    foreign_key_violations: int | None = None
    source_fingerprint: str = ""
    source_fingerprint_algorithm: str = ""


@dataclass(frozen=True)
class SourceWriteLock:
    """A live SQLite source whose writers the caller is holding off.

    The repair workflow must be able to state - and *prove* - that the archive
    it verifies is the exact state it is about to mutate. SQLite's online
    backup API cannot read from a connection that is inside a write
    transaction, so the image cannot simply be taken from the repairing
    transaction; instead the repairing connection holds ``BEGIN IMMEDIATE``
    (the write lock) for the whole capture-verify-mutate-commit window and
    hands this object to the backup layer.

    ``assert_held`` is the enforcement, not a comment: it checks that the
    caller's connection is still inside its transaction *and* that no other
    connection can take the write lock right now. It is re-checked before and
    after every step that depends on the source being frozen, so a released or
    never-acquired lock is a hard failure rather than a silent race.
    """

    path: Path
    connection: sqlite3.Connection

    def assert_held(self, context: str) -> None:
        if not self.connection.in_transaction:
            raise SourceLockLost(
                f"{context}: the source write transaction on {self.path} is not open, "
                "so the database is not quiesced and the archive cannot be bound to it"
            )
        probe = sqlite3.connect(str(self.path), isolation_level=None, timeout=0)
        try:
            probe.execute("PRAGMA busy_timeout=0")
            try:
                probe.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                return
            probe.execute("ROLLBACK")
        finally:
            probe.close()
        raise SourceLockLost(
            f"{context}: another connection can still acquire the write lock on "
            f"{self.path}, so concurrent writers are not held off"
        )


@dataclass(frozen=True)
class BackupResult:
    """Returned by :func:`create_backup`."""

    archive_path: str
    backend: str
    data_size_bytes: int
    data_sha256: str
    schema_versions: list[str]
    schema_fingerprint: str = ""
    table_row_counts: dict[str, int] = dc_field(default_factory=dict)
    foreign_key_violations: int | None = None
    source_fingerprint: str = ""


@dataclass(frozen=True)
class RestoreResult:
    """Returned by :func:`restore_backup`."""

    archive_path: str
    backend: str
    restored_to: str  # SQLite file path OR sanitized Postgres URL
    data_size_bytes: int
    data_sha256: str


@dataclass(frozen=True)
class BackupVerification:
    """Result of an isolated restore verification.

    ``ok`` is only true when every check passed. ``failures`` lists the
    exact contract that was broken so an operator (or the repair
    prerequisite) can act on it rather than guess.
    """

    archive_path: str
    backend: str
    ok: bool
    checks: dict[str, Any]
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_path": self.archive_path,
            "backend": self.backend,
            "ok": self.ok,
            "checks": dict(self.checks),
            "failures": list(self.failures),
        }


# --------------------------------------------------------------------- helpers


_MANIFEST_VERSION = "2"
_READABLE_MANIFEST_VERSIONS = ("1", "2")
_SQLITE_DATA_NAME = "brains.sqlite"
_POSTGRES_DATA_NAME = "brains.sql"
_MANIFEST_NAME = "manifest.json"

# Identifies how ``_Manifest.source_fingerprint`` was produced, so a future
# algorithm can be introduced without silently comparing incomparable values.
_SOURCE_FINGERPRINT_ALGORITHM = "sqlite-backup-image-sha256/1"
_LIVE_IMAGE_NAME = "live-source.sqlite"

# Declared shape of every manifest field, so a hand-edited or foreign archive
# fails as a BackupError at read time instead of as an AttributeError deep in
# a comparison later. ``foreign_key_violations`` is the only field that may be
# JSON ``null``.
_MANIFEST_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "schema_version": str,
    "brains_version": str,
    "created_at": str,
    "backend": str,
    "data_file": str,
    "data_sha256": str,
    "data_size_bytes": int,
    "sanitized_db_url": str,
    "schema_versions": list,
    "source_path": str,
    "source_identity": dict,
    "schema_fingerprint": str,
    "schema_objects": list,
    "table_row_counts": dict,
    "foreign_key_violations": int,
    "source_fingerprint": str,
    "source_fingerprint_algorithm": str,
}
_MANIFEST_NULLABLE_FIELDS = frozenset({"foreign_key_violations"})


def _as_tuple(expected: type | tuple[type, ...]) -> tuple[type, ...]:
    return expected if isinstance(expected, tuple) else (expected,)


def _sanitize_url(raw_url: str) -> str:
    """Strip the password from a SQLAlchemy URL before persisting it."""
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return raw_url
    if not parsed.password:
        return raw_url
    user = parsed.username or ""
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{user}:***@{host}" if user else f":***@{host}"
    return urlunparse(parsed._replace(netloc=netloc))


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _resolve_sqlite_path(db_url: str) -> Path:
    """Return the on-disk SQLite file path for ``db_url``.

    Accepts ``sqlite:///relative.db`` (relative to CWD) and
    ``sqlite:////absolute/path.db`` per SQLAlchemy convention.
    """
    if not db_url.startswith("sqlite"):
        raise UnsupportedBackend(f"Not a SQLite URL: {db_url!r}")
    parsed = urlparse(db_url)
    # SQLAlchemy uses 3 slashes for relative, 4 for absolute on POSIX.
    # urlparse drops the leading slash into .path.
    path = parsed.path
    if path.startswith("/") and len(path) > 1 and path[2] == ":":
        # Windows absolute path like /C:/Users/...; strip the leading /
        path = path[1:]
    elif path.startswith("/"):
        # POSIX absolute path embedded as ///abs OR relative ///rel
        # SQLAlchemy: sqlite:///rel -> path="/rel" -> relative to CWD
        # sqlite:////abs -> path="//abs" -> /abs after lstrip("/")
        path = "/" + path.lstrip("/") if path.startswith("//") else path.lstrip("/")
    return Path(path)


def _current_backend() -> str:
    return settings.subsystems.storage.backend


def _current_db_url() -> str:
    return resolve_db_url(settings)


def _current_schema_versions() -> list[str]:
    # Deferred to avoid circular import at module load.
    from brains.storage.migrations import current_schema_versions

    return current_schema_versions()


class SchemaIncompatible(BackupError):
    """The archive records schema this build cannot express."""


def schema_compatibility(manifest_versions: list[str]) -> dict[str, Any]:
    """Compare an archive's applied migrations against this build's corpus.

    The manifest records the migration IDs that had actually been *applied*
    when the archive was written. An ID this build does not ship means the
    archive was taken from a newer store: restoring it would put a schema
    under code that has no migration for it and cannot migrate forward.

    A ``1`` manifest, and a ``2`` manifest from a store whose ledger was
    empty, record nothing to compare; that is reported as unknown rather than
    as compatible.
    """
    from brains.storage.migrations import known_migration_ids

    known = known_migration_ids()
    unknown = sorted(set(manifest_versions) - known)
    return {
        "archive_migrations": len(manifest_versions),
        "unknown_migrations": unknown,
        "known_to_this_build": len(known),
        "comparable": bool(manifest_versions),
        "compatible": not unknown if manifest_versions else None,
    }


def _assert_schema_compatible(manifest: _Manifest) -> dict[str, Any]:
    compatibility = schema_compatibility(list(manifest.schema_versions))
    if compatibility["unknown_migrations"]:
        raise SchemaIncompatible(
            "this archive was taken from a store that had applied migrations this build "
            f"does not ship ({', '.join(compatibility['unknown_migrations'])}); restoring "
            "it would leave a schema no installed migration can account for. Restore it "
            "with the Brains build that wrote it."
        )
    return compatibility


def _write_manifest(target_dir: Path, manifest: _Manifest) -> Path:
    path = target_dir / _MANIFEST_NAME
    path.write_text(json.dumps(asdict(manifest), indent=2, sort_keys=True), encoding="utf-8")
    return path


def _read_manifest(archive: tarfile.TarFile) -> _Manifest:
    """Parse ``manifest.json`` defensively.

    Every failure mode - unreadable member, non-JSON bytes, a JSON value that
    is not an object, an unknown ``manifest_version``, a missing required
    field, a field of the wrong type, an unsafe ``data_file`` - is converted
    into :class:`BackupError`. Callers (and the CLI) therefore see one error
    class and one message instead of a ``TypeError``/``AttributeError``
    traceback from deep inside dataclass construction. Unknown *fields* in a
    readable version are ignored, so a future build can add manifest fields
    without breaking this reader.
    """
    try:
        member = archive.getmember(_MANIFEST_NAME)
    except KeyError as exc:
        raise BackupError(f"Archive missing {_MANIFEST_NAME!r}") from exc
    try:
        fobj = archive.extractfile(member)
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(f"Cannot read {_MANIFEST_NAME!r} from archive: {exc}") from exc
    if fobj is None:
        raise BackupError(f"Cannot read {_MANIFEST_NAME!r} from archive")
    try:
        raw = json.loads(fobj.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupError(f"{_MANIFEST_NAME} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BackupError(f"{_MANIFEST_NAME} must be a JSON object, got {type(raw).__name__}")
    raw = dict(raw)

    manifest_version = str(raw.pop("manifest_version", "1"))
    if manifest_version not in _READABLE_MANIFEST_VERSIONS:
        raise BackupError(
            f"unsupported manifest_version {manifest_version!r}; "
            f"this build reads {', '.join(_READABLE_MANIFEST_VERSIONS)}. "
            "Archives are readable by the build that wrote them or later."
        )

    fields = {f.name: f for f in dataclass_fields(_Manifest) if f.name != "manifest_version"}
    required = sorted(
        name
        for name, spec in fields.items()
        if spec.default is _MISSING and spec.default_factory is _MISSING
    )
    missing = [name for name in required if name not in raw]
    if missing:
        raise BackupError(f"manifest missing required field(s): {missing}")

    values = {name: raw[name] for name in fields if name in raw}
    for name, value in sorted(values.items()):
        expected = _MANIFEST_FIELD_TYPES.get(name)
        if expected is None:
            continue
        if value is None and name in _MANIFEST_NULLABLE_FIELDS:
            continue
        allowed = _as_tuple(expected)
        # ``bool`` is a subclass of ``int``; a JSON ``true`` is not a count.
        matches = isinstance(value, allowed) and not (
            isinstance(value, bool) and bool not in allowed
        )
        if not matches:
            raise BackupError(
                f"manifest field {name!r} must be "
                f"{' or '.join(t.__name__ for t in allowed)}, got {type(value).__name__}"
            )
    try:
        manifest = _Manifest(manifest_version=manifest_version, **values)
    except TypeError as exc:  # defensive: a field spec change must not traceback
        raise BackupError(f"manifest could not be read: {exc}") from exc

    # Harden against path traversal: the data blob is later staged at
    # ``tmp_dir / manifest.data_file`` and (for Postgres) fed to ``psql -f``.
    # A crafted archive could set ``data_file`` to an absolute path or one
    # containing ``..`` and escape the temp dir on restore. Legitimate brains
    # archives always store the blob under a bare filename, so reject anything
    # with a directory component up front (covers restore *and* inspect).
    data_file = str(manifest.data_file)
    if data_file in ("", ".", "..") or data_file != Path(data_file).name:
        raise BackupError(f"unsafe data_file in manifest: {data_file!r}")
    return manifest


# ------------------------------------------------------- SQLite introspection


def _sqlite_identity(conn: sqlite3.Connection) -> dict[str, Any]:
    """Header-level identity of a SQLite database.

    These values survive the online backup API byte-for-byte in meaning, so
    a restored copy that disagrees is not the database we backed up.
    """
    identity: dict[str, Any] = {}
    for pragma in ("application_id", "user_version", "page_size", "page_count", "encoding"):
        row = conn.execute(f"PRAGMA {pragma}").fetchone()
        identity[pragma] = row[0] if row is not None else None
    return identity


def _sqlite_schema_objects(conn: sqlite3.Connection) -> list[str]:
    """Sorted ``type|name|sql`` lines for every non-internal schema object."""
    rows = conn.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return [f"{row[0]}|{row[1]}|{' '.join(str(row[2]).split())}" for row in rows]


def _schema_fingerprint(objects: list[str]) -> str:
    digest = hashlib.sha256()
    for line in objects:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for table in tables:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        counts[table] = int(row[0]) if row is not None else 0
    return counts


def _sqlite_foreign_key_violations(conn: sqlite3.Connection) -> int:
    return len(conn.execute("PRAGMA foreign_key_check").fetchall())


def _sqlite_data_version(conn: sqlite3.Connection) -> int | None:
    """``PRAGMA data_version`` — diagnostic only.

    SQLite only guarantees this value is comparable between two reads on the
    *same* connection, so it is recorded for operators to look at and is
    never used as an equality gate.
    """
    row = conn.execute("PRAGMA data_version").fetchone()
    return int(row[0]) if row is not None else None


def _sqlite_snapshot(path: Path) -> dict[str, Any]:
    """Identity + schema + row-count snapshot of a SQLite file.

    Deliberately thorough (it runs ``integrity_check`` and
    ``foreign_key_check``) and therefore only ever used on an *isolated
    copy*. The live source is inspected with :func:`_sqlite_source_state`,
    which does not scan the database.
    """
    conn = sqlite3.connect(str(path))
    try:
        objects = _sqlite_schema_objects(conn)
        return {
            "identity": _sqlite_identity(conn),
            "schema_objects": objects,
            "schema_fingerprint": _schema_fingerprint(objects),
            "table_row_counts": _sqlite_table_row_counts(conn),
            "integrity_check": [
                str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()
            ],
            "foreign_key_violations": _sqlite_foreign_key_violations(conn),
        }
    finally:
        conn.close()


def _sqlite_backup_image(src_conn: sqlite3.Connection, dest: Path) -> None:
    """Write ``src_conn``'s committed content to ``dest`` via the backup API.

    The online backup API is the only correct way to read a live SQLite
    database: it takes a read lock for the copy, sees WAL frames that are not
    yet checkpointed into the main file, and never observes a torn page set.
    """
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()


def _sqlite_source_state(path: Path, work_dir: Path) -> dict[str, Any]:
    """Fingerprint the *live* database at ``path`` without scanning it.

    The fingerprint is the sha256 of the database image produced by the
    online backup API. That image is a deterministic function of the
    committed content, which is what makes it usable as a freshness binding:

    * it is stable across connections and processes, unlike
      ``PRAGMA data_version``, whose value is only comparable within one
      connection (it is still reported here, for diagnostics only);
    * it is WAL-safe - frames still in the ``-wal`` sidecar are included, and
      a later checkpoint that moves those frames into the main file does not
      change it, so a mere read-only open/close cannot cause a false alarm;
    * it is unaffected by write bookkeeping that does not change content, so
      it flags real drift rather than activity.

    No ``integrity_check``, ``foreign_key_check`` or row counting happens
    here: those belong on the isolated restored copy, not on the operator's
    live store.
    """
    conn = sqlite3.connect(str(path))
    try:
        objects = _sqlite_schema_objects(conn)
        data_version = _sqlite_data_version(conn)
        image = work_dir / _LIVE_IMAGE_NAME
        image.unlink(missing_ok=True)
        _sqlite_backup_image(conn, image)
    finally:
        conn.close()
    try:
        fingerprint, size = _sha256_file(image)
    finally:
        image.unlink(missing_ok=True)
    return {
        "fingerprint": fingerprint,
        "fingerprint_algorithm": _SOURCE_FINGERPRINT_ALGORITHM,
        "schema_fingerprint": _schema_fingerprint(objects),
        "image_size_bytes": size,
        "data_version": data_version,
    }


# --------------------------------------------------------------------- SQLite


def _backup_sqlite(out_path: Path, *, source_lock: SourceWriteLock | None = None) -> BackupResult:
    """Capture the SQLite source into a verified archive.

    When ``source_lock`` is given, the source is the locked database rather
    than whatever ``settings`` currently points at, and the lock is re-proved
    immediately before the image is taken and immediately after the manifest
    is derived from it. That is what makes the archive's claim to be the
    source's exact state true rather than hopeful.
    """
    if source_lock is not None:
        source_lock.assert_held("before capturing the backup image")
        src_path = source_lock.path
        db_url = f"sqlite:///{src_path}"
    else:
        db_url = _current_db_url()
        src_path = _resolve_sqlite_path(db_url)
    if not src_path.exists():
        raise BackupError(f"SQLite DB not found at {src_path}")
    src_path = src_path.resolve()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        dump_path = tmp_dir / _SQLITE_DATA_NAME

        # Online backup — safe even if other processes are writing, and the
        # only correct way to copy a live WAL database: a file copy can
        # capture a torn page set or miss committed frames still in the WAL.
        # The read connection is deliberately separate from any write
        # transaction: SQLite's backup API cannot step against a connection
        # that holds one.
        src_conn = sqlite3.connect(str(src_path))
        try:
            source = {
                "identity": _sqlite_identity(src_conn),
                "schema_objects": _sqlite_schema_objects(src_conn),
                "data_version": _sqlite_data_version(src_conn),
            }
            _sqlite_backup_image(src_conn, dump_path)
        finally:
            src_conn.close()
        if source_lock is not None:
            source_lock.assert_held("after capturing the backup image")

        # Verify the copy before it is ever presented as a backup: it must be
        # structurally sound and carry the same schema as the source.
        copy = _sqlite_snapshot(dump_path)
        if tuple(copy["integrity_check"]) != ("ok",):
            raise BackupError(
                "backup copy failed PRAGMA integrity_check: " + "; ".join(copy["integrity_check"])
            )
        source_fingerprint = _schema_fingerprint(cast("list[str]", source["schema_objects"]))
        if copy["schema_fingerprint"] != source_fingerprint:
            raise BackupError("backup copy schema does not match the source database schema")

        sha, size = _sha256_file(dump_path)
        manifest = _Manifest(
            schema_version=_MANIFEST_VERSION,
            manifest_version=_MANIFEST_VERSION,
            brains_version=_brains_version,
            created_at=datetime.now(UTC).isoformat(),
            backend="sqlite",
            data_file=_SQLITE_DATA_NAME,
            data_sha256=sha,
            data_size_bytes=size,
            sanitized_db_url=_sanitize_url(db_url),
            schema_versions=_current_schema_versions(),
            source_path=str(src_path),
            source_identity=cast("dict[str, Any]", source["identity"]),
            schema_fingerprint=copy["schema_fingerprint"],
            schema_objects=copy["schema_objects"],
            table_row_counts=copy["table_row_counts"],
            foreign_key_violations=copy["foreign_key_violations"],
            # The archived blob *is* the online-backup image of the source at
            # this instant, so its hash doubles as the source fingerprint that
            # :func:`verify_backup` re-derives from the live database to prove
            # the archive still represents it.
            source_fingerprint=sha,
            source_fingerprint_algorithm=_SOURCE_FINGERPRINT_ALGORITHM,
        )
        _write_manifest(tmp_dir, manifest)

        out_path = out_path.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(tmp_dir / _MANIFEST_NAME, arcname=_MANIFEST_NAME)
            tar.add(dump_path, arcname=_SQLITE_DATA_NAME)

    return BackupResult(
        archive_path=str(out_path),
        backend="sqlite",
        data_size_bytes=size,
        data_sha256=sha,
        schema_versions=manifest.schema_versions,
        schema_fingerprint=manifest.schema_fingerprint,
        table_row_counts=manifest.table_row_counts,
        foreign_key_violations=manifest.foreign_key_violations,
        source_fingerprint=manifest.source_fingerprint,
    )


def _restore_sqlite(archive_path: Path, *, target_url: str | None) -> RestoreResult:
    db_url = target_url or _current_db_url()
    dst_path = _resolve_sqlite_path(db_url)

    with tarfile.open(archive_path, "r:gz") as tar:
        manifest = _read_manifest(tar)
        if manifest.backend != "sqlite":
            raise ManifestMismatch(
                f"Archive backend {manifest.backend!r} is not 'sqlite'. "
                "Use --backend postgres or restore into a Postgres install."
            )
        _assert_schema_compatible(manifest)
        try:
            data_member = tar.getmember(manifest.data_file)
        except KeyError as exc:
            raise BackupError(f"Archive missing data blob {manifest.data_file!r}") from exc
        data_fobj = tar.extractfile(data_member)
        if data_fobj is None:
            raise BackupError("Cannot read data blob from archive")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            staged = tmp_dir / manifest.data_file
            with staged.open("wb") as out:
                shutil.copyfileobj(data_fobj, out)
            sha, size = _sha256_file(staged)
            if sha != manifest.data_sha256:
                raise BackupError(
                    f"sha256 mismatch on archived blob (expected {manifest.data_sha256}, got {sha})"
                )

            dst_path.parent.mkdir(parents=True, exist_ok=True)
            # Replace atomically: write to ``<dst>.restoring`` then
            # os.replace into place so a crash mid-restore does not
            # half-clobber the live DB.
            stage_target = dst_path.with_suffix(dst_path.suffix + ".restoring")
            shutil.copyfile(staged, stage_target)
            os.replace(stage_target, dst_path)

    return RestoreResult(
        archive_path=str(archive_path),
        backend="sqlite",
        restored_to=str(dst_path),
        data_size_bytes=size,
        data_sha256=sha,
    )


# ------------------------------------------------------------------- Postgres


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise BackupToolUnavailable(
            f"{name!r} not found on PATH; required for Postgres backup/restore"
        )
    return path


def _backup_postgres(out_path: Path) -> BackupResult:
    pg_dump = _require_tool("pg_dump")
    db_url = _current_db_url()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        dump_path = tmp_dir / _POSTGRES_DATA_NAME

        # --format=plain is the most portable. We do not include
        # --clean so the operator can decide whether to drop+recreate
        # the target schema in the restore step.
        result = subprocess.run(
            [pg_dump, "--format=plain", "--no-owner", "--no-acl", db_url],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise BackupError("pg_dump failed: " + result.stderr.decode("utf-8", "replace"))
        dump_path.write_bytes(result.stdout)

        sha, size = _sha256_file(dump_path)
        manifest = _Manifest(
            schema_version=_MANIFEST_VERSION,
            manifest_version=_MANIFEST_VERSION,
            brains_version=_brains_version,
            created_at=datetime.now(UTC).isoformat(),
            backend="postgres",
            data_file=_POSTGRES_DATA_NAME,
            data_sha256=sha,
            data_size_bytes=size,
            sanitized_db_url=_sanitize_url(db_url),
            schema_versions=_current_schema_versions(),
        )
        _write_manifest(tmp_dir, manifest)

        out_path = out_path.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(tmp_dir / _MANIFEST_NAME, arcname=_MANIFEST_NAME)
            tar.add(dump_path, arcname=_POSTGRES_DATA_NAME)

    return BackupResult(
        archive_path=str(out_path),
        backend="postgres",
        data_size_bytes=size,
        data_sha256=sha,
        schema_versions=manifest.schema_versions,
    )


def _restore_postgres(archive_path: Path, *, target_url: str | None) -> RestoreResult:
    psql = _require_tool("psql")
    db_url = target_url or _current_db_url()

    with tarfile.open(archive_path, "r:gz") as tar:
        manifest = _read_manifest(tar)
        if manifest.backend != "postgres":
            raise ManifestMismatch(f"Archive backend {manifest.backend!r} is not 'postgres'.")
        _assert_schema_compatible(manifest)
        data_member = tar.getmember(manifest.data_file)
        data_fobj = tar.extractfile(data_member)
        if data_fobj is None:
            raise BackupError("Cannot read data blob from archive")

        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / manifest.data_file
            with staged.open("wb") as out:
                shutil.copyfileobj(data_fobj, out)
            sha, size = _sha256_file(staged)
            if sha != manifest.data_sha256:
                raise BackupError(
                    f"sha256 mismatch on archived blob (expected {manifest.data_sha256}, got {sha})"
                )

            result = subprocess.run(
                [psql, "--quiet", "-v", "ON_ERROR_STOP=1", "-f", str(staged), db_url],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise BackupError(
                    "psql restore failed: " + result.stderr.decode("utf-8", "replace")
                )

    return RestoreResult(
        archive_path=str(archive_path),
        backend="postgres",
        restored_to=_sanitize_url(db_url),
        data_size_bytes=size,
        data_sha256=sha,
    )


# ---------------------------------------------------------------- public API


def create_backup(
    out_path: str | Path,
    *,
    source_lock: SourceWriteLock | None = None,
) -> BackupResult:
    """Create a backup archive of the current brains DB.

    SQLite is the only shipped runtime backend. The output is ``.tar.gz``.

    ``source_lock`` is passed by the repair workflow, which holds the SQLite
    write lock across capture, verification, and mutation. When it is given
    the archive is captured from the locked database and the lock is re-proved
    around the capture, so the archive is the exact state the repair will
    mutate rather than a state a concurrent writer may already have moved on
    from.
    """
    backend = _current_backend()
    target = Path(out_path)
    if backend == "sqlite":
        return _backup_sqlite(target, source_lock=source_lock)
    raise UnsupportedBackend(f"Runtime backend {backend!r} is withdrawn; SQLite is required")


def restore_backup(
    archive_path: str | Path,
    *,
    target_url: str | None = None,
) -> RestoreResult:
    """Restore a brains DB from an archive.

    ``target_url`` may select another SQLite file. Non-SQLite targets fail
    closed even when a historical Postgres driver happens to be installed.
    """
    backend = _current_backend()
    archive = Path(archive_path).expanduser().resolve()
    if not archive.exists():
        raise BackupError(f"Archive not found: {archive}")
    if backend != "sqlite":
        raise UnsupportedBackend(f"Runtime backend {backend!r} is withdrawn; SQLite is required")
    if target_url is not None and not target_url.startswith("sqlite:"):
        raise UnsupportedBackend("Restore targets must use SQLite")
    return _restore_sqlite(archive, target_url=target_url)


def inspect_archive(archive_path: str | Path) -> dict[str, Any]:
    """Read the manifest without restoring.

    Any way the archive can be unreadable - not a tarball, truncated, no
    manifest, malformed manifest, unknown ``manifest_version`` - surfaces as
    :class:`BackupError`, so the CLI prints one line instead of a traceback.
    """
    archive = Path(archive_path).expanduser().resolve()
    if not archive.exists():
        raise BackupError(f"Archive not found: {archive}")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            manifest = _read_manifest(tar)
    except BackupError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise BackupError(f"archive unreadable or corrupt: {exc}") from exc
    return asdict(manifest)


def _stage_sqlite_blob(archive: Path, tmp_dir: Path) -> tuple[_Manifest, Path]:
    """Extract the archive's data blob into ``tmp_dir`` and hash-check it."""
    with tarfile.open(archive, "r:gz") as tar:
        manifest = _read_manifest(tar)
        try:
            member = tar.getmember(manifest.data_file)
        except KeyError as exc:
            raise BackupError(f"Archive missing data blob {manifest.data_file!r}") from exc
        fobj = tar.extractfile(member)
        if fobj is None:
            raise BackupError("Cannot read data blob from archive")
        staged = tmp_dir / manifest.data_file
        with staged.open("wb") as out:
            shutil.copyfileobj(fobj, out)
    sha, size = _sha256_file(staged)
    if sha != manifest.data_sha256:
        raise BackupError(
            f"sha256 mismatch on archived blob (expected {manifest.data_sha256}, got {sha})"
        )
    if manifest.data_size_bytes and size != manifest.data_size_bytes:
        raise BackupError(
            f"size mismatch on archived blob (expected {manifest.data_size_bytes}, got {size})"
        )
    return manifest, staged


def verify_backup(
    archive_path: str | Path,
    *,
    expected_source_path: str | Path | None = None,
    source_lock: SourceWriteLock | None = None,
) -> BackupVerification:
    """Restore an archive into an isolated temporary directory and verify it.

    Nothing outside the temporary directory is touched, so this is safe to
    run against a live install: it proves the archive can actually be
    restored and that the restored database matches every claim in its
    manifest. A backup is not valid until this passes — see
    ``docs/OPERATIONS.md``.

    ``expected_source_path`` additionally binds the archive to one database
    *and to its current state*: the manifest's ``source_path`` must match, the
    live schema must still match the archived schema, and the live content
    fingerprint must still equal the one recorded when the archive was
    written. A backup taken before other writes landed is therefore refused,
    so a stale, foreign, or partially superseded archive cannot be used as
    the safety net for a repair.

    ``source_lock`` is how the repair workflow closes the gap between "the
    fingerprint matched" and "the repair wrote". It names the database whose
    write lock the caller is holding, defaults ``expected_source_path`` to it,
    and re-proves the lock before and after the binding, so the freshness
    verdict cannot go stale between this call and the mutation that follows.
    """
    archive = Path(archive_path).expanduser().resolve()
    if not archive.exists():
        raise BackupError(f"Archive not found: {archive}")
    if source_lock is not None:
        source_lock.assert_held("before verifying the backup archive")
        if expected_source_path is None:
            expected_source_path = source_lock.path

    checks: dict[str, Any] = {"archive_exists": True}
    failures: list[str] = []
    backend = "unknown"

    with tempfile.TemporaryDirectory(prefix="brains-verify-") as tmp:
        tmp_dir = Path(tmp)
        try:
            manifest, staged = _stage_sqlite_blob(archive, tmp_dir)
        except (BackupError, tarfile.TarError, OSError, ValueError) as exc:
            return BackupVerification(
                archive_path=str(archive),
                backend=backend,
                ok=False,
                checks=checks,
                failures=(f"archive unreadable or corrupt: {exc}",),
            )

        backend = manifest.backend
        checks["manifest_backend"] = manifest.backend
        checks["data_sha256"] = manifest.data_sha256
        checks["blob_sha256_matches"] = True
        compatibility = schema_compatibility(list(manifest.schema_versions))
        checks["schema_compatibility"] = compatibility
        if compatibility["unknown_migrations"]:
            failures.append(
                "archive records migrations this build does not ship: "
                + ", ".join(compatibility["unknown_migrations"])
            )
        if manifest.backend != "sqlite":
            return BackupVerification(
                archive_path=str(archive),
                backend=backend,
                ok=False,
                checks=checks,
                failures=tuple(failures)
                + (
                    f"isolated restore verification supports the sqlite backend only "
                    f"(archive backend: {manifest.backend})",
                ),
            )

        try:
            restored = _sqlite_snapshot(staged)
        except sqlite3.DatabaseError as exc:
            return BackupVerification(
                archive_path=str(archive),
                backend=backend,
                ok=False,
                checks=checks,
                failures=(f"restored copy is not a readable SQLite database: {exc}",),
            )

        checks["integrity_check"] = restored["integrity_check"]
        if tuple(restored["integrity_check"]) != ("ok",):
            failures.append(
                "restored copy failed PRAGMA integrity_check: "
                + "; ".join(restored["integrity_check"])
            )

        checks["foreign_key_violations"] = restored["foreign_key_violations"]
        checks["restored_table_count"] = len(restored["table_row_counts"])

        if manifest.schema_fingerprint:
            checks["schema_fingerprint"] = manifest.schema_fingerprint
            if restored["schema_fingerprint"] != manifest.schema_fingerprint:
                failures.append("restored schema fingerprint does not match the manifest")
        else:
            checks["schema_fingerprint"] = None
            failures.append(
                "manifest predates source-identity capture; it cannot be verified "
                "(create a new backup with this build)"
            )

        if manifest.table_row_counts:
            differences = {
                table: {
                    "manifest": count,
                    "restored": restored["table_row_counts"].get(table),
                }
                for table, count in sorted(manifest.table_row_counts.items())
                if restored["table_row_counts"].get(table) != count
            }
            checks["row_count_differences"] = differences
            if differences:
                failures.append(f"restored row counts differ for {sorted(differences)}")

        if manifest.source_identity:
            identity_differences = {
                key: {"manifest": value, "restored": restored["identity"].get(key)}
                for key, value in sorted(manifest.source_identity.items())
                # ``page_count`` legitimately differs: the online backup
                # rewrites a fully packed copy of a possibly sparse source.
                if key != "page_count" and restored["identity"].get(key) != value
            }
            checks["identity_differences"] = identity_differences
            if identity_differences:
                failures.append(
                    f"restored SQLite identity differs for {sorted(identity_differences)}"
                )

        if expected_source_path is not None:
            failures.extend(
                _bind_to_live_source(
                    manifest,
                    Path(expected_source_path).expanduser().resolve(),
                    checks,
                    work_dir=tmp_dir,
                    source_lock=source_lock,
                )
            )

    if source_lock is not None:
        source_lock.assert_held("after verifying the backup archive")
        checks["source_write_lock_held"] = True

    return BackupVerification(
        archive_path=str(archive),
        backend=backend,
        ok=not failures,
        checks=checks,
        failures=tuple(failures),
    )


def _bind_to_live_source(
    manifest: _Manifest,
    expected: Path,
    checks: dict[str, Any],
    *,
    work_dir: Path,
    source_lock: SourceWriteLock | None = None,
) -> list[str]:
    """Prove the archive is *this* database's *current* state.

    Two separate claims are checked, because they fail for different reasons
    and an operator needs to know which one broke:

    * *binding* - the manifest was taken from ``expected`` and the live schema
      still matches the archived schema;
    * *freshness* - the live content fingerprint still equals the one recorded
      when the archive was written. A backup that no longer represents the
      database is not a safety net for a destructive repair, so a single
      committed write between backup and repair invalidates it.

    Only header/schema reads and one online-backup image pass over the live
    file are performed: no ``integrity_check``, ``foreign_key_check`` or row
    counting, and no long-lived read transaction.

    A freshness verdict is only durable while writers are held off. When
    ``source_lock`` is given, the lock is re-proved immediately around the
    fingerprint read, so the verdict still holds for the caller's next
    statement; when it is not, the verdict describes the instant it was taken
    and the caller owns the gap.
    """
    failures: list[str] = []
    checks["expected_source_path"] = str(expected)
    checks["manifest_source_path"] = manifest.source_path
    checks["bound_under_source_write_lock"] = source_lock is not None

    if source_lock is not None and source_lock.path != expected:
        failures.append(
            f"the held write lock is on {source_lock.path}, not the expected source {expected}"
        )
        return failures
    if not manifest.source_path:
        failures.append("manifest records no source path; cannot bind it to a database")
        return failures
    if Path(manifest.source_path) != expected:
        failures.append(f"archive was taken from {manifest.source_path}, not {expected}")
        return failures
    if not expected.exists():
        failures.append(f"expected source database does not exist: {expected}")
        return failures
    if not manifest.source_fingerprint:
        failures.append(
            "manifest records no source fingerprint, so this archive cannot be shown "
            "to represent the current database (create a new backup with this build)"
        )
        return failures
    if manifest.source_fingerprint_algorithm != _SOURCE_FINGERPRINT_ALGORITHM:
        failures.append(
            f"manifest source fingerprint algorithm "
            f"{manifest.source_fingerprint_algorithm!r} is not "
            f"{_SOURCE_FINGERPRINT_ALGORITHM!r}; it cannot be compared"
        )
        return failures

    try:
        live = _sqlite_source_state(expected, work_dir)
    except sqlite3.DatabaseError as exc:
        failures.append(f"live source database could not be fingerprinted: {exc}")
        return failures
    if source_lock is not None:
        source_lock.assert_held("after fingerprinting the live source")

    checks["live_data_version"] = live["data_version"]
    checks["source_fingerprint"] = manifest.source_fingerprint
    checks["live_source_fingerprint"] = live["fingerprint"]
    checks["live_schema_matches"] = live["schema_fingerprint"] == manifest.schema_fingerprint
    checks["live_source_matches"] = live["fingerprint"] == manifest.source_fingerprint

    if not checks["live_schema_matches"]:
        failures.append(
            "the live database schema no longer matches this archive; take a current backup"
        )
    if not checks["live_source_matches"]:
        failures.append(
            "the live database has changed since this archive was written "
            f"(source fingerprint {live['fingerprint']} != {manifest.source_fingerprint}); "
            "take a current backup"
        )
    return failures


__all__ = [
    "BackupError",
    "BackupResult",
    "BackupToolUnavailable",
    "BackupVerification",
    "ManifestMismatch",
    "RestoreResult",
    "SourceLockLost",
    "SourceWriteLock",
    "UnsupportedBackend",
    "create_backup",
    "inspect_archive",
    "restore_backup",
    "verify_backup",
]

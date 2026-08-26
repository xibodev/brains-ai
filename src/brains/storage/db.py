"""SQLAlchemy engine + session factory.

The engine URL is resolved by :func:`brains.storage.backends.resolve_db_url`,
which validates the configured backend (``subsystems.storage.backend``) and
enforces the matching pip extra. For SQLite (the default) we keep the
historical behaviour: ``settings.db_url`` is returned verbatim and used to
build a synchronous engine.

When ``backend == "postgres"`` the resolver coerces the URL onto the
``postgresql+psycopg`` driver so the existing synchronous SQLAlchemy code
path keeps working without a rewrite to async.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from brains.config import settings
from brains.storage.backends import resolve_db_url

_db_url = resolve_db_url(settings)

# Postgres pools real network connections and benefits from a liveness check
# before checkout. SQLite is a local file and instead needs per-connection
# PRAGMAs (applied via the ``connect`` listener below).
_engine_kwargs: dict = {}
if _db_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {
        "timeout": settings.sqlite_busy_timeout_ms / 1000,
    }
else:
    _engine_kwargs["pool_pre_ping"] = True

engine = create_engine(_db_url, **_engine_kwargs)


if engine.dialect.name == "sqlite":
    # Databases whose ``foreign_key_check`` has already been proven clean in
    # this process. Enforcement is verified once per file, not per pooled
    # connection, so the check does not run on every checkout.
    _fk_verified_databases: set[str] = set()

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        """Make SQLite safe under the multi-process ``serve-all`` topology.

        ``serve-all`` runs the gateway, dashboard, and MCP server as three
        separate processes against one ``~/.brains/brains.db`` file. SQLite's
        default rollback journal plus a zero busy-timeout means concurrent
        writers fail immediately with ``SQLITE_BUSY``. WAL lets concurrent
        readers proceed alongside a single writer, and a busy-timeout makes a
        contending writer wait for the lock instead of erroring out. Idempotent:
        WAL is persisted on the database file, while ``busy_timeout`` is
        per-connection and is re-applied on every connect.

        Foreign-key enforcement is opt-in
        (``settings.sqlite_enforce_foreign_keys``) and never enabled over a
        store that still violates its own schema: the first connection to a
        given database proves ``PRAGMA foreign_key_check`` is empty and raises
        otherwise, instead of leaving the violations latent or letting later
        writes fail in unrelated places.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            if settings.sqlite_enforce_foreign_keys:
                _enforce_sqlite_foreign_keys(cursor)
        finally:
            cursor.close()

    def _enforce_sqlite_foreign_keys(cursor) -> None:  # noqa: ANN001
        from brains.storage.integrity import ForeignKeyViolationsError

        database = _db_url
        if database not in _fk_verified_databases:
            violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                summary: dict[str, int] = {}
                for row in violations:
                    summary[str(row[0])] = summary.get(str(row[0]), 0) + 1
                breakdown = ", ".join(
                    f"{table}={count}" for table, count in sorted(summary.items())
                )
                raise ForeignKeyViolationsError(
                    f"sqlite_enforce_foreign_keys is on but {len(violations)} foreign-key "
                    f"violation(s) are present ({breakdown}); run `brains-ai db diagnose` "
                    "to inspect them, then unset BRAINS_SQLITE_ENFORCE_FOREIGN_KEYS to run "
                    "`brains-ai db repair --apply` before turning enforcement back on"
                )
            _fk_verified_databases.add(database)
        cursor.execute("PRAGMA foreign_keys=ON")


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass

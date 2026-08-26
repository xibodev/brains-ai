"""SQLite connection policy for the shared multi-process brain."""

from brains.config import settings
from brains.storage.db import engine


def test_sqlite_connections_receive_configured_busy_timeout() -> None:
    with engine.connect() as connection:
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    assert timeout == settings.sqlite_busy_timeout_ms
    assert timeout >= 30_000

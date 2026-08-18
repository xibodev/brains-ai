"""Pluggable storage backends.

Today there are two backends:

- ``sqlite`` (default) — the lean-core path. ``settings.db_url`` is used verbatim.
- ``postgres`` — opt-in via ``brains-ai[postgres]`` + ``subsystems.storage.backend: postgres``.

The contract is intentionally minimal: pick a SQLAlchemy connection URL based
on the configured backend. For Postgres we coerce the URL onto the
``postgresql+psycopg`` driver (psycopg3 sync) so the existing synchronous
SQLAlchemy code path keeps working without a rewrite to async.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, urlunparse

from brains.extras import require_extra

_SUPPORTED = ("sqlite", "postgres")
_PSYCOPG_DRIVER = "postgresql+psycopg"


def _coerce_postgres_url(raw_url: str) -> str:
    """Normalise a Postgres URL onto the psycopg3 sync driver.

    Accepted inputs:
      - ``postgresql://user:pass@host/db``
      - ``postgres://user:pass@host/db``        (Heroku-style alias)
      - ``postgresql+psycopg://...``            (already correct)
      - ``postgresql+asyncpg://...``            (we override to psycopg for sync)

    Anything else is returned unchanged so operators with exotic drivers
    can override the resolver in their own deployment if needed.
    """
    parsed = urlparse(raw_url)
    scheme = parsed.scheme.lower()
    if scheme in ("postgres", "postgresql"):
        new_scheme = _PSYCOPG_DRIVER
    elif scheme.startswith("postgresql+"):
        # Always coerce onto sync psycopg — we don't run async SQLAlchemy.
        new_scheme = _PSYCOPG_DRIVER
    else:
        return raw_url
    return urlunparse(parsed._replace(scheme=new_scheme))


def resolve_db_url(settings_obj: Any) -> str:
    """Return the SQLAlchemy URL for the configured backend.

    For SQLite the URL passes through unchanged. For Postgres we enforce
    the extra (raising :class:`brains.extras.ExtraNotInstalledError` if
    missing) and coerce the URL onto the ``postgresql+psycopg`` driver.

    ``brains.config._enforce_subsystem_extras`` already calls
    :func:`brains.extras.require_extra` at startup, but we re-check here
    so direct callers (tests, scripts) can't bypass the gate.
    """
    backend = settings_obj.subsystems.storage.backend
    if backend not in _SUPPORTED:
        raise ValueError(f"Unsupported storage backend {backend!r}. Supported: {_SUPPORTED}.")
    if backend == "postgres":
        require_extra("postgres", "subsystems.storage (backend=postgres)")
        return _coerce_postgres_url(settings_obj.db_url)
    return settings_obj.db_url


__all__ = ["resolve_db_url"]

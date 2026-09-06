"""Tests for the ``Settings.db_url`` canonicalisation validator.

The historical default ``sqlite:///brains.db`` is CWD-relative — every bare
``brains-ai`` invocation in a different repo silently opened a fresh
``./brains.db`` there, fragmenting the "shared per-machine brain" across
project directories. The validator rewrites that literal sentinel to the
absolute per-machine path under ``BRAINS_STATE_DIR`` (or ``~/.brains``)
so the CLI, the SSE server, and stdio MCP children all key off one DB.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brains.config import (
    DEFAULT_DB_URL,
    Settings,
    _canonical_default_db_url,
    _enforce_subsystem_extras,
)


@pytest.fixture
def isolated_env(monkeypatch):
    """Strip every ambient ``BRAINS_DB_URL`` / ``BRAINS_STATE_DIR`` override
    so the validator is exercised against constructor-provided values only.

    ``tests/conftest.py`` deliberately points these at a tmp dir for the
    rest of the suite; for this test file we want to control them per-test.
    """
    monkeypatch.delenv("BRAINS_DB_URL", raising=False)
    monkeypatch.delenv("BRAINS_STATE_DIR", raising=False)
    yield monkeypatch


def test_bare_default_rewrites_to_home_brains(isolated_env):
    s = Settings(db_url=DEFAULT_DB_URL)
    expected = "sqlite:///" + (Path.home() / ".brains" / "brains.db").as_posix()
    assert s.db_url == expected


def test_bare_default_honours_brains_state_dir(isolated_env, tmp_path):
    isolated_env.setenv("BRAINS_STATE_DIR", str(tmp_path))
    s = Settings(db_url=DEFAULT_DB_URL)
    expected = "sqlite:///" + (tmp_path / "brains.db").as_posix()
    assert s.db_url == expected


def test_explicit_absolute_sqlite_url_is_preserved(isolated_env, tmp_path):
    explicit = f"sqlite:///{(tmp_path / 'my.db').as_posix()}"
    s = Settings(db_url=explicit)
    assert s.db_url == explicit


def test_historical_postgres_url_is_rejected_at_activation(isolated_env):
    explicit = "postgresql+psycopg://user:pw@db.example.com:5432/brains"
    s = Settings(db_url=explicit)
    with pytest.raises(ValueError, match="withdrawn"):
        _enforce_subsystem_extras(s)


def test_default_field_resolves_when_no_env(isolated_env):
    """No env, no kwarg — Settings still produces the canonical absolute URL.

    Guards the case the original bug actually exercised: a bare CLI
    process with no ``.env`` and no ``BRAINS_DB_URL`` in the environment.
    """
    s = Settings()
    assert s.db_url == _canonical_default_db_url()
    assert s.db_url != DEFAULT_DB_URL
    assert s.db_url.startswith("sqlite:///")


def test_sqlite_busy_timeout_defaults_to_multi_session_window(isolated_env):
    assert Settings().sqlite_busy_timeout_ms == 30_000


def test_sqlite_busy_timeout_honours_environment(isolated_env):
    isolated_env.setenv("BRAINS_SQLITE_BUSY_TIMEOUT_MS", "45000")
    assert Settings().sqlite_busy_timeout_ms == 45_000

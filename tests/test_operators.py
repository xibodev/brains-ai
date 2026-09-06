"""Tests for the Layer 1 multi-operator model.

Covers:

* ``ensure_admin_operator`` auto-provisions the ``admin`` row on first
  call, is idempotent on the second, and refreshes the fingerprint
  after a key rotation.
* ``add_operator`` mints a fresh URL-safe key, persists it under
  ``operator_keys_dir()`` with restrictive permissions, and records the
  matching fingerprint.
* ``load_operator_api_keys`` returns every persisted operator key.
* ``_valid_keys`` (in ``brains.api.auth``) merges operator keys with
  the admin key without breaking the existing rotation semantics.
* ``resolve_current_operator`` honours, in priority order, the explicit
  ``operator`` argument, the request-scoped ``current_operator``
  ContextVar, the ``BRAINS_OPERATOR`` env override, and the admin
  fallback — so pre-Layer-1 callers keep stamping ``admin``.
* ``start_session`` writes ``created_by_operator_id`` and bubbles the
  operator slug back to the caller + welcome metadata.
* The disk migration is discoverable so existing installs pick up the
  new table + column on next ``init_db()``.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest
from sqlalchemy import create_engine

import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.storage.migrations import _list_disk_migrations, init_db


@pytest.fixture
def isolated_brains(tmp_path, monkeypatch):
    """Per-test brains state: temp DB + temp ``~/.brains`` + admin key.

    Rebinds the module-level SQLAlchemy engine to a temp SQLite file
    (mirrors ``test_consolidation_migration.isolated_db``) and points
    ``BRAINS_STATE_DIR`` at a temp dir so admin/operator key files
    don't bleed into the developer's real ``~/.brains``. Returns the
    state dir for direct filesystem assertions.

    Also rebinds the ``SessionLocal`` symbol on every module that
    imported it at load time — without this, those modules keep using
    the original engine and tests appear to "share" state across runs.
    """
    db_path = tmp_path / "isolated.sqlite"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))

    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", SessionLocal)
    # Modules that did ``from brains.storage.db import SessionLocal``
    # captured a reference at import time; rebind that reference too.
    import brains.control.events as events_module
    import brains.control.sessions as sessions_module

    monkeypatch.setattr(sessions_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(events_module, "SessionLocal", SessionLocal)
    yield state


def test_disk_migration_050_is_discoverable() -> None:
    names = {p.name for p in _list_disk_migrations()}
    assert "050_operators.py" in names


def test_init_db_creates_operators_table_and_attribution_column(
    isolated_brains: Path,
) -> None:
    init_db()
    # The fixture wires the engine at ``<tmp_path>/isolated.sqlite``;
    # ``isolated_brains`` itself is ``<tmp_path>/state``.
    db_path = isolated_brains.parent / "isolated.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "operators" in tables
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_sessions)")}
        assert "created_by_operator_id" in cols
    finally:
        conn.close()


def test_ensure_admin_operator_idempotent_and_syncs_fingerprint(
    isolated_brains: Path,
) -> None:
    from brains.config import settings
    from brains.control.operators import ADMIN_SLUG, ensure_admin_operator

    # First call provisions; second call is a no-op (same row, same id).
    first = ensure_admin_operator()
    second = ensure_admin_operator()
    assert first["slug"] == ADMIN_SLUG
    assert first["id"] == second["id"]
    assert first["key_fingerprint"] == second["key_fingerprint"]

    # Rotate the admin key in-process and re-run; fingerprint updates,
    # id stays the same (single admin row, not duplicated).
    original = settings.api_key
    try:
        settings.api_key = "rotated-admin-key"
        third = ensure_admin_operator()
        assert third["id"] == first["id"]
        assert third["key_fingerprint"] != first["key_fingerprint"]
    finally:
        settings.api_key = original
        ensure_admin_operator()  # restore fingerprint for follow-on tests


def test_add_operator_persists_key_and_fingerprint(
    isolated_brains: Path,
) -> None:
    from brains.control.operators import (
        add_operator,
        ensure_admin_operator,
        list_operators,
        operator_keys_dir,
    )

    ensure_admin_operator()
    record, key = add_operator("alice", display_name="Alice")

    assert record["slug"] == "alice"
    assert record["display_name"] == "Alice"
    assert record["key_fingerprint"]
    assert key  # raw key returned once
    # Key file lives at the documented path and contains the value.
    path = operator_keys_dir() / "alice.key"
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip() == key
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    # Listing includes both admin and the new operator.
    slugs = {row["slug"] for row in list_operators()}
    assert slugs == {"admin", "alice"}


def test_add_operator_filesystem_failure_removes_operator_and_mailbox(
    isolated_brains: Path,
    monkeypatch,
) -> None:
    import brains.control.operators as operators_module
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.storage.db import SessionLocal
    from brains.storage.models import Mailbox, Operator

    ensure_admin_operator()
    blocked_parent = isolated_brains / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(operators_module, "operator_keys_dir", lambda: blocked_parent)

    with pytest.raises(OSError):
        add_operator("filesystem-failure")

    with SessionLocal() as session:
        assert session.query(Operator).filter(Operator.slug == "filesystem-failure").count() == 0
        assert (
            session.query(Mailbox)
            .filter(Mailbox.address == "operator:filesystem-failure@brains")
            .count()
            == 0
        )


def test_add_operator_rejects_admin_and_invalid_slugs(
    isolated_brains: Path,
) -> None:
    from brains.control.operators import (
        OperatorSlugError,
        add_operator,
        ensure_admin_operator,
    )

    ensure_admin_operator()
    with pytest.raises(OperatorSlugError):
        add_operator("admin")
    with pytest.raises(OperatorSlugError):
        add_operator("Has Space")
    with pytest.raises(OperatorSlugError):
        add_operator("CapsLock")


def test_add_operator_refuses_duplicate(isolated_brains: Path) -> None:
    from brains.control.operators import (
        OperatorExistsError,
        add_operator,
        ensure_admin_operator,
    )

    ensure_admin_operator()
    add_operator("bob")
    with pytest.raises(OperatorExistsError):
        add_operator("bob")


def test_valid_keys_includes_operator_keys(isolated_brains: Path) -> None:
    from brains.api.auth import _valid_keys
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    _, alice_key = add_operator("alice")
    _, bob_key = add_operator("bob")

    keys = _valid_keys()
    # Admin key (from settings/env) is still first.
    assert keys[0] == "local-dev-key"
    assert alice_key in keys
    assert bob_key in keys


def test_load_operator_api_keys_returns_persisted_values(
    isolated_brains: Path,
) -> None:
    from brains.control.operators import (
        add_operator,
        ensure_admin_operator,
        load_operator_api_keys,
    )

    ensure_admin_operator()
    _, key = add_operator("alice")
    on_disk = load_operator_api_keys()
    assert key in on_disk


def test_resolve_current_operator_default_is_admin(isolated_brains: Path, monkeypatch) -> None:
    from brains.control.operators import resolve_current_operator

    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)
    record = resolve_current_operator()
    assert record["slug"] == "admin"


def test_resolve_current_operator_honours_explicit_arg(
    isolated_brains: Path,
) -> None:
    from brains.control.operators import (
        add_operator,
        ensure_admin_operator,
        resolve_current_operator,
    )

    ensure_admin_operator()
    add_operator("alice")
    record = resolve_current_operator(operator="alice")
    assert record["slug"] == "alice"


def test_resolve_current_operator_honours_context_var(
    isolated_brains: Path,
) -> None:
    from brains.control.operators import (
        add_operator,
        current_operator,
        ensure_admin_operator,
        resolve_current_operator,
    )

    ensure_admin_operator()
    add_operator("alice")
    token = current_operator.set("alice")
    try:
        record = resolve_current_operator()
        assert record["slug"] == "alice"
    finally:
        current_operator.reset(token)


def test_resolve_current_operator_honours_env(isolated_brains: Path, monkeypatch) -> None:
    from brains.control.operators import (
        add_operator,
        ensure_admin_operator,
        resolve_current_operator,
    )

    ensure_admin_operator()
    add_operator("bob")
    monkeypatch.setenv("BRAINS_OPERATOR", "bob")
    record = resolve_current_operator()
    assert record["slug"] == "bob"


def test_resolve_operator_for_key_matches_by_fingerprint(
    isolated_brains: Path,
) -> None:
    from brains.control.operators import (
        add_operator,
        ensure_admin_operator,
        resolve_operator_for_key,
    )

    ensure_admin_operator()
    _, alice_key = add_operator("alice")
    record = resolve_operator_for_key(alice_key)
    assert record is not None
    assert record["slug"] == "alice"
    # Unknown key returns None (not a fallback).
    assert resolve_operator_for_key("not-a-real-key") is None


def test_start_session_stamps_admin_by_default(isolated_brains: Path, tmp_path) -> None:
    from brains.control.operators import ensure_admin_operator
    from brains.control.sessions import start_session
    from brains.storage.db import SessionLocal
    from brains.storage.models import AgentSession

    init_db()
    ensure_admin_operator()
    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = start_session(str(workspace), tool="pytest")
    assert result["operator"] == "admin"

    with SessionLocal() as session:
        row = session.query(AgentSession).filter(AgentSession.id == result["session_id"]).one()
        assert row.created_by_operator_id is not None


def test_start_session_honours_explicit_operator(isolated_brains: Path, tmp_path) -> None:
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.sessions import start_session

    init_db()
    ensure_admin_operator()
    add_operator("alice")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = start_session(str(workspace), tool="pytest", operator="alice")
    assert result["operator"] == "alice"

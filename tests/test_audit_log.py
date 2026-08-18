"""Tests for the signed audit log (Phase 3 hardening).

Covers :mod:`brains.audit` and the storage model + 070 migration that
back it. The audit log is best-effort by design — a failed append must
not raise into the calling code path — but the chain itself must be
strictly verifiable so any out-of-band mutation is detectable.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

import brains.audit as audit_module
import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.audit import (
    GENESIS_HASH,
    INIT_ACTION,
    _canonical_payload,
    _reset_key_cache,
    audit_key_fingerprint,
    list_entries,
    record,
    verify_chain,
)
from brains.storage.migrations import init_db
from brains.storage.models import AuditLogEntry


@pytest.fixture
def isolated_brains(tmp_path, monkeypatch):
    """Per-test DB + audit-key isolation.

    Rebinds the storage engine to a fresh SQLite DB under tmp_path and
    points the audit key at a tmp file so concurrent test runs do not
    fight over ``~/.brains/audit-key``.
    """
    db_path = tmp_path / "isolated.sqlite"
    key_path = tmp_path / "audit-key"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    monkeypatch.setenv("BRAINS_AUDIT_KEY_FILE", str(key_path))
    # Drop any leaked env override from a prior test.
    monkeypatch.delenv("BRAINS_AUDIT_KEY", raising=False)

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
# Key resolution
# ----------------------------------------------------------------------


def test_audit_key_is_auto_generated_on_first_use(isolated_brains, monkeypatch):
    key_path = isolated_brains / "audit-key"
    assert not key_path.exists()
    fp1 = audit_key_fingerprint()
    assert key_path.exists()
    assert len(fp1) == 12
    # Stable across calls.
    fp2 = audit_key_fingerprint()
    assert fp1 == fp2
    # Persisted to disk: nuking the cache + re-reading still matches.
    _reset_key_cache()
    fp3 = audit_key_fingerprint()
    assert fp3 == fp1


def test_raw_audit_key_file_preserves_whitespace_bytes(isolated_brains):
    key = b"\n" + (b"x" * 30) + b"\t"
    (isolated_brains / "audit-key").write_bytes(key)
    _reset_key_cache()
    assert audit_key_fingerprint() == hashlib.sha256(key).hexdigest()[:12]


def test_audit_key_env_overrides_file(isolated_brains, monkeypatch):
    # Set env BEFORE the cache is populated.
    explicit = b"\xa5" * 32
    monkeypatch.setenv("BRAINS_AUDIT_KEY", explicit.hex())
    _reset_key_cache()
    fp = audit_key_fingerprint()
    expected = hashlib.sha256(explicit).hexdigest()[:12]
    assert fp == expected
    # Disk file must NOT have been written.
    assert not (isolated_brains / "audit-key").exists()


def test_audit_key_env_accepts_raw_string(isolated_brains, monkeypatch):
    monkeypatch.setenv("BRAINS_AUDIT_KEY", "not-hex-but-valid")
    _reset_key_cache()
    fp = audit_key_fingerprint()
    expected = hashlib.sha256(b"not-hex-but-valid").hexdigest()[:12]
    assert fp == expected


# ----------------------------------------------------------------------
# Append + list
# ----------------------------------------------------------------------


def test_record_returns_id_and_chains_to_genesis(isolated_brains):
    entry_id = record(
        actor="session:abc",
        action="task.create",
        payload={"code": "BRAINS-1", "title": "first"},
    )
    assert isinstance(entry_id, int)
    entries = list_entries()
    # The genesis marker is entry one on a fresh store; this is entry two.
    assert len(entries) == 2
    assert entries[1]["action"] == INIT_ACTION
    assert entries[1]["prev_hash"] == GENESIS_HASH
    e = entries[0]
    assert e["id"] == entry_id
    assert e["actor"] == "session:abc"
    assert e["action"] == "task.create"
    assert e["prev_hash"] == entries[1]["entry_hash"]
    assert e["entry_hash"]
    assert e["payload"] == {"code": "BRAINS-1", "title": "first"}


def test_record_chains_subsequent_entries(isolated_brains):
    e1 = record(actor="a", action="x", payload={"i": 1})
    e2 = record(actor="b", action="y", payload={"i": 2})
    e3 = record(actor="c", action="z", payload={"i": 3})
    entries = list_entries(limit=10)
    # list_entries returns newest first; the oldest entry is the genesis marker.
    assert [e["id"] for e in entries][:3] == [e3, e2, e1]
    by_id = {e["id"]: e for e in entries}
    assert by_id[e1]["prev_hash"] == entries[-1]["entry_hash"]
    assert entries[-1]["action"] == INIT_ACTION
    assert entries[-1]["prev_hash"] == GENESIS_HASH
    assert by_id[e2]["prev_hash"] == by_id[e1]["entry_hash"]
    assert by_id[e3]["prev_hash"] == by_id[e2]["entry_hash"]


def test_record_uses_anonymous_actor_for_empty_actor(isolated_brains):
    entry_id = record(actor="", action="bg.tick", payload={})
    assert entry_id is not None
    entries = list_entries()
    assert entries[0]["actor"] == "anonymous"


def test_record_returns_none_on_failure(isolated_brains, monkeypatch):
    """If the DB call blows up, ``record`` swallows the error."""

    class _BrokenSession:
        def __enter__(self):
            raise RuntimeError("db is down")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(audit_module, "SessionLocal", lambda: _BrokenSession())
    assert record(actor="a", action="x", payload={}) is None


def test_list_entries_action_prefix_filter(isolated_brains):
    record(actor="a", action="task.create", payload={})
    record(actor="a", action="task.complete", payload={})
    record(actor="a", action="provider.invoke", payload={})
    record(actor="a", action="admin.overlay_write", payload={})

    tasks = list_entries(action_prefix="task.")
    assert all(e["action"].startswith("task.") for e in tasks)
    assert len(tasks) == 2


def test_list_entries_actor_filter(isolated_brains):
    record(actor="session:a", action="x", payload={})
    record(actor="session:b", action="x", payload={})
    record(actor="session:a", action="y", payload={})
    only_a = list_entries(actor="session:a")
    assert len(only_a) == 2
    assert all(e["actor"] == "session:a" for e in only_a)


def test_list_entries_since_id_pagination(isolated_brains):
    e1 = record(actor="a", action="x", payload={})
    e2 = record(actor="a", action="x", payload={})
    e3 = record(actor="a", action="x", payload={})
    rest = list_entries(since_id=e1)
    assert sorted(e["id"] for e in rest) == [e2, e3]


def test_list_entries_limit_clamped(isolated_brains):
    for _ in range(5):
        record(actor="a", action="x", payload={})
    assert len(list_entries(limit=0)) == 1  # clamped up to 1
    # Clamped to 1000; only these 5 plus the genesis marker exist.
    assert len(list_entries(limit=10_000)) == 6


# ----------------------------------------------------------------------
# verify_chain
# ----------------------------------------------------------------------


def test_verify_chain_intact_returns_none(isolated_brains):
    for i in range(5):
        record(actor="a", action="x", payload={"i": i})
    assert verify_chain() is None


def test_verify_chain_detects_payload_mutation(isolated_brains):
    e1 = record(actor="a", action="x", payload={"i": 1})
    record(actor="a", action="x", payload={"i": 2})
    record(actor="a", action="x", payload={"i": 3})

    # Mutate payload of first entry directly via the model.
    with db_module.SessionLocal() as session:
        row = session.query(AuditLogEntry).filter(AuditLogEntry.id == e1).one()
        row.payload_json = row.payload_json.replace('"i":1', '"i":999')
        session.commit()

    div = verify_chain()
    assert div is not None
    assert div.entry_id == e1
    assert "entry_hash" in div.reason


def test_verify_chain_detects_row_deletion(isolated_brains):
    record(actor="a", action="x", payload={"i": 1})
    e2 = record(actor="a", action="x", payload={"i": 2})
    e3 = record(actor="a", action="x", payload={"i": 3})

    # Delete the middle row.
    with db_module.SessionLocal() as session:
        session.query(AuditLogEntry).filter(AuditLogEntry.id == e2).delete()
        session.commit()

    div = verify_chain()
    assert div is not None
    # After deleting e2, e3's prev_hash points at e2's hash, but the
    # walker expects e1's hash now.
    assert div.entry_id == e3
    assert "prev_hash" in div.reason


def test_verify_chain_detects_forged_row(isolated_brains):
    """A row inserted out-of-band lacks a valid HMAC."""
    record(actor="a", action="x", payload={"i": 1})

    with db_module.SessionLocal() as session:
        last = session.query(AuditLogEntry).order_by(AuditLogEntry.id.desc()).first()
        forged = AuditLogEntry(
            actor="attacker",
            action="forged.action",
            workspace_id=None,
            payload_json="forged",
            prev_hash=last.entry_hash,
            entry_hash="0" * 64,
            created_at=datetime.now(UTC),
        )
        session.add(forged)
        session.commit()

    div = verify_chain()
    assert div is not None
    assert div.reason == "entry_hash does not match recomputed HMAC"


def test_verify_chain_empty_log_returns_none(isolated_brains):
    assert verify_chain() is None


# ----------------------------------------------------------------------
# Canonicalisation
# ----------------------------------------------------------------------


def test_canonical_payload_is_deterministic(isolated_brains):
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    a = _canonical_payload(
        created_at=ts,
        actor="x",
        action="y",
        workspace_id=42,
        payload={"b": 1, "a": 2},
    )
    b = _canonical_payload(
        created_at=ts,
        actor="x",
        action="y",
        workspace_id=42,
        payload={"a": 2, "b": 1},
    )
    assert a == b


# ----------------------------------------------------------------------
# Hook integration: task lifecycle
# ----------------------------------------------------------------------


def test_create_task_records_audit_entry(tmp_path, monkeypatch):
    """End-to-end: create_task must emit a ``task.create`` audit row.

    We rebind SessionLocal on every control module that captured it at
    import time, mirroring ``tests/test_presence.py``.
    """
    db_path = tmp_path / "isolated.sqlite"
    key_path = tmp_path / "audit-key"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    monkeypatch.setenv("BRAINS_AUDIT_KEY_FILE", str(key_path))
    monkeypatch.delenv("BRAINS_AUDIT_KEY", raising=False)

    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(audit_module, "SessionLocal", SessionLocal)

    import brains.control.claims as claims_module
    import brains.control.decisions as decisions_module
    import brains.control.events as events_module
    import brains.control.sessions as sessions_module
    import brains.control.tasks as tasks_module

    for mod in (
        sessions_module,
        tasks_module,
        events_module,
        decisions_module,
        claims_module,
    ):
        monkeypatch.setattr(mod, "SessionLocal", SessionLocal, raising=False)

    _reset_key_cache()
    init_db()

    workspace = str(tmp_path / "ws")
    s = sessions_module.start_session(workspace, tool="pytest")
    t = tasks_module.create_task(workspace, title="hello", session_id=s["session_id"])

    audit_entries = list_entries(action_prefix="task.")
    matching = [
        e
        for e in audit_entries
        if e["action"] == "task.create"
        and e["payload"].get("code") == t["code"]
        and e["actor"] == f"session:{s['session_id']}"
    ]
    assert matching, f"no task.create audit row for {t['code']!r}: {audit_entries!r}"

    _reset_key_cache()

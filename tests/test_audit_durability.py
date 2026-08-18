"""Durability of the audit chain: transactional, multi-process, fail-closed.

The chain used to be appended best-effort under a process-local lock, which
made two independent failures possible: a governed action could commit while
its record silently did not, and two Brains processes sharing one store could
fork the chain by reading the same predecessor. Both are asserted against here,
including the multi-process case, which needs real processes - threads share
the lock that used to be the (insufficient) guarantee.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text

import brains.audit as audit_module
import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.audit import (
    AuditChainCorruptError,
    AuditWriteError,
    adopt_legacy_chain,
    append_in_session,
    assert_chain_intact,
    chain_status,
    record,
    record_required,
    required_effect,
    verify_chain,
)
from brains.storage.migrations import init_db
from brains.storage.models import AuditChainHead, AuditLogEntry

_KEY = "ab" * 32


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """A per-test SQLite file every storage-facing module is bound to."""
    db_path = tmp_path / "brains.sqlite"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    monkeypatch.setenv("BRAINS_AUDIT_KEY", _KEY)

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    _apply_runtime_pragmas(engine)
    session_factory = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    _bind_modules(monkeypatch, engine, session_factory)

    migrations_module.reset_migration_cache()
    audit_module._reset_key_cache()
    init_db()
    yield db_path
    audit_module._reset_key_cache()
    migrations_module.reset_migration_cache()
    engine.dispose()


def _apply_runtime_pragmas(engine) -> None:
    """The PRAGMAs ``brains.storage.db`` sets on every real connection.

    WAL and a busy timeout are what the concurrency assertions below are about:
    without them the test store is not the store operators run, and a
    contended append fails instantly instead of waiting for the lock.
    """

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def _bind_modules(monkeypatch, engine, session_factory) -> None:
    """Point every module that captured ``SessionLocal`` at this store."""
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", session_factory)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", session_factory)
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if name.startswith("brains.") and getattr(module, "SessionLocal", None) is not None:
            monkeypatch.setattr(module, "SessionLocal", session_factory, raising=False)


def _head(session_factory) -> tuple[int, str, int | None]:
    with session_factory() as session:
        row = session.get(AuditChainHead, 1)
        return row.seq, row.head_hash, row.head_entry_id


_PG_URL_ENV = "BRAINS_TEST_PG_URL"
_pg_url = os.environ.get(_PG_URL_ENV)
_pg_skip = pytest.mark.skipif(
    not _pg_url,
    reason=f"set {_PG_URL_ENV} to a Postgres URL to run the Postgres concurrency regressions",
)


@pytest.fixture
def pg_store(monkeypatch):
    """The same store contract on a live Postgres, in a throwaway schema.

    The isolation the verifier needs is backend-specific - a SQLite read
    transaction and a Postgres ``REPEATABLE READ`` snapshot are different
    mechanisms - so "no false tamper under concurrency" has to be proven on
    both, not inferred from one.
    """
    pytest.importorskip("psycopg")
    monkeypatch.setenv("BRAINS_AUDIT_KEY", _KEY)

    schema = f"brains_audit_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_pg_url or "", future=True)
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    finally:
        admin.dispose()

    # The schema is pinned as a connection option rather than a ``SET`` on
    # connect: ``SET`` is transactional, so the pool's reset-on-return rollback
    # would silently put later checkouts back on the default search path.
    engine = create_engine(
        _pg_url or "",
        future=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    session_factory = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    _bind_modules(monkeypatch, engine, session_factory)
    migrations_module.reset_migration_cache()
    audit_module._reset_key_cache()
    init_db()
    try:
        yield engine
    finally:
        audit_module._reset_key_cache()
        migrations_module.reset_migration_cache()
        engine.dispose()
        cleanup = create_engine(_pg_url or "", future=True)
        try:
            with cleanup.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            cleanup.dispose()


def _verify_under_load(*, workers: int, appends: int) -> tuple[list, int]:
    """Verify repeatedly while ``workers`` threads append; return verdicts.

    The verdicts are the assertion: every one of them must be ``None``. A
    verifier that reads the log and the head at two different instants sees a
    head one entry ahead of the rows and calls an untouched chain truncated,
    and it does so under exactly this load - three ordinary Brains processes
    sharing one store.
    """
    errors: list[BaseException] = []

    def _append(worker: int) -> None:
        try:
            for index in range(appends):
                record_required(
                    actor=f"worker-{worker}",
                    action="probe.race",
                    payload={"worker": worker, "index": index},
                )
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_append, args=(n,)) for n in range(workers)]
    for thread in threads:
        thread.start()

    verdicts: list = []
    deadline = time.time() + 120
    while any(thread.is_alive() for thread in threads) and time.time() < deadline:
        verdicts.append(verify_chain())
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [], f"appends failed under contention: {errors[0]!r}"
    assert verdicts, "the verifier never ran while appends were in flight"
    return verdicts, len(verdicts)


# ----------------------------------------------------------------------
# Transactional append
# ----------------------------------------------------------------------


def test_append_rolls_back_with_the_caller_transaction(isolated_store):
    """A rolled-back governed mutation must not leave its audit entry behind."""
    with db_module.SessionLocal() as session:
        append_in_session(session, actor="tester", action="probe.rollback", payload={"a": 1})
        session.rollback()

    assert audit_module.list_entries(action_prefix="probe.") == []
    assert verify_chain() is None
    assert _head(db_module.SessionLocal) == (0, "GENESIS", None)


def test_append_advances_the_head_with_the_entry(isolated_store):
    entry_id = record_required(actor="tester", action="probe.head", payload={})
    seq, head_hash, head_entry_id = _head(db_module.SessionLocal)

    # Two entries: the genesis marker the first append writes, then this one.
    assert seq == 2
    assert head_entry_id == entry_id
    with db_module.SessionLocal() as session:
        stored = session.get(AuditLogEntry, entry_id)
    assert head_hash == stored.entry_hash
    assert audit_module.list_entries(action_prefix=audit_module.INIT_ACTION, limit=5)


def test_record_required_raises_where_record_returns_none(isolated_store, monkeypatch):
    """The difference between the two append surfaces is the whole contract."""

    class _BrokenSession:
        def __enter__(self):
            raise RuntimeError("db is down")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(audit_module, "SessionLocal", lambda: _BrokenSession())

    assert record(actor="a", action="x", payload={}) is None
    with pytest.raises(AuditWriteError):
        record_required(actor="a", action="x", payload={})


# ----------------------------------------------------------------------
# Two-phase records for effects that are not database writes
# ----------------------------------------------------------------------


def _broken_session_factory():
    class _BrokenSession:
        def __enter__(self):
            raise RuntimeError("db is down")

        def __exit__(self, *args):
            return False

    return _BrokenSession()


def _effect_actions() -> list[str]:
    """Actions recorded for the demo effect, newest first."""
    return [e["action"] for e in audit_module.list_entries(action_prefix="effect.", limit=50)]


def test_the_attempt_is_committed_before_the_effect_runs(isolated_store):
    """Ordering is the contract: the record exists while the effect is running.

    An overlay write, an archive or a restore cannot be taken back, so a
    record appended afterwards protects nothing - by the time it fails, the
    thing it was supposed to gate has already happened.
    """
    visible_during_effect: list[list[str]] = []

    with required_effect(actor="admin", action="effect.demo", payload={"target": "x"}) as effect:
        visible_during_effect.append(_effect_actions())
        effect.record_outcome({"rows": 3})

    assert visible_during_effect == [["effect.demo.attempted"]]
    assert _effect_actions() == ["effect.demo", "effect.demo.attempted"]

    entries = audit_module.list_entries(action_prefix="effect.demo", limit=10)
    success, attempt = entries[0], entries[-1]
    assert success["payload"] == {"target": "x", "rows": 3, "attempt_audit_id": attempt["id"]}
    assert verify_chain() is None


def test_the_effect_never_runs_when_the_attempt_cannot_be_recorded(isolated_store, monkeypatch):
    """Fail closed *before* the effect, not after it."""
    ran: list[bool] = []
    monkeypatch.setattr(audit_module, "SessionLocal", _broken_session_factory)

    with pytest.raises(AuditWriteError), required_effect(actor="admin", action="effect.demo"):
        ran.append(True)

    assert ran == [], "the effect ran even though its attempt was not recorded"


def test_an_effect_that_fails_keeps_attempted_and_failed_evidence(isolated_store):
    """A failure is recorded as one, and never as the success it is not."""
    with (
        pytest.raises(RuntimeError, match="disk full"),
        required_effect(actor="admin", action="effect.demo", payload={"target": "x"}),
    ):
        raise RuntimeError("disk full")

    assert _effect_actions() == ["effect.demo.failed", "effect.demo.attempted"]
    entries = audit_module.list_entries(action_prefix="effect.demo", limit=10)
    failure, attempt = entries[0], entries[-1]
    assert failure["payload"]["target"] == "x"
    assert failure["payload"]["attempt_audit_id"] == attempt["id"]
    assert "disk full" in failure["payload"]["error"]
    assert verify_chain() is None


def test_success_is_not_claimed_when_the_outcome_cannot_be_recorded(isolated_store):
    """The effect happened; saying so in the log is a separate, honest step."""
    original = audit_module.SessionLocal
    try:
        with (
            pytest.raises(AuditWriteError, match="could not be recorded"),
            required_effect(actor="admin", action="effect.demo") as effect,
        ):
            audit_module.SessionLocal = _broken_session_factory
            effect.record_outcome({"rows": 3})
    finally:
        audit_module.SessionLocal = original

    assert _effect_actions() == ["effect.demo.attempted"]
    assert verify_chain() is None


# ----------------------------------------------------------------------
# Concurrency
# ----------------------------------------------------------------------


def test_concurrent_threads_do_not_fork_the_chain(isolated_store):
    errors: list[BaseException] = []

    def _append(worker: int) -> None:
        try:
            for index in range(10):
                record_required(
                    actor=f"worker-{worker}",
                    action="probe.concurrent",
                    payload={"worker": worker, "index": index},
                )
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_append, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert verify_chain() is None
    status = chain_status()
    assert status["ok"] is True
    # 40 appends plus the genesis marker the first of them wrote.
    assert status["stored_entries"] == 41
    assert status["appended_entries"] == 41


_CHILD_APPEND = """
import sys
sys.path.insert(0, {src!r})
from brains.audit import record_required

for index in range({count}):
    record_required(actor={actor!r}, action="probe.multiprocess", payload={{"index": index}})
"""


def test_concurrent_processes_do_not_fork_the_chain(isolated_store, tmp_path):
    """The guarantee a process-local lock could never give.

    Two Brains processes against one store used to be able to read the same
    predecessor and append two entries with the same ``prev_hash``. The head
    row makes the predecessor read a contended write, so the chain stays
    linear across processes.
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["BRAINS_DB_URL"] = f"sqlite:///{isolated_store.as_posix()}"
    env["BRAINS_STATE_DIR"] = str(tmp_path / "state")
    env["BRAINS_AUDIT_KEY"] = _KEY
    env["BRAINS_TEST_KEEP_AMBIENT_DB"] = "1"

    processes = [
        subprocess.Popen(  # noqa: S603 - fixed interpreter, generated script
            [
                sys.executable,
                "-c",
                _CHILD_APPEND.format(src=src, count=8, actor=f"child-{n}"),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(3)
    ]
    outputs = [proc.communicate(timeout=180) for proc in processes]
    for proc, (_out, err) in zip(processes, outputs, strict=True):
        assert proc.returncode == 0, err

    assert verify_chain() is None
    status = chain_status()
    assert status["stored_entries"] == 25
    assert status["appended_entries"] == 25

    with db_module.SessionLocal() as session:
        prevs = [row[0] for row in session.execute(text("SELECT prev_hash FROM audit_log"))]
    assert len(prevs) == len(set(prevs)), "two entries share a predecessor: the chain forked"


def _tamper_with_one_entry() -> int:
    """Rewrite a stored payload in place - the tamper verification exists for."""
    with db_module.SessionLocal() as session:
        victim = (
            session.query(AuditLogEntry)
            .filter(AuditLogEntry.actor.like("worker-%"))
            .order_by(AuditLogEntry.id.asc())
            .first()
        )
        assert victim is not None, "no concurrent entry to tamper with"
        victim.payload_json = victim.payload_json.replace("worker-", "forged-")
        session.commit()
        return int(victim.id)


def test_verification_under_concurrent_appends_reports_no_false_tamper(isolated_store):
    """The false-positive case, which is the *common* case.

    Verification compares the log with the head. Read the two at different
    instants and an ordinary concurrent append - the thing that happens all day
    under ``serve-all`` - leaves a head one entry ahead of the rows, which is
    indistinguishable from a truncated tail. The chain is intact; the report
    says tampered. An alarm that fires on normal load is an alarm operators
    learn to ignore, so it is asserted here before the real tamper is.
    """
    record_required(actor="seed", action="probe.seed", payload={})

    verdicts, runs = _verify_under_load(workers=4, appends=15)

    false_reports = [verdict for verdict in verdicts if verdict is not None]
    assert false_reports == [], (
        f"{len(false_reports)} of {runs} verifications called an intact chain broken: "
        f"{false_reports[0].reason if false_reports else ''}"
    )
    assert verify_chain() is None
    status = chain_status()
    assert status["ok"] is True
    assert status["stored_entries"] == status["appended_entries"] == 62


def test_a_real_tamper_still_fails_after_concurrent_appends(isolated_store):
    """The other half: not crying wolf must not mean sleeping through a wolf."""
    record_required(actor="seed", action="probe.seed", payload={})
    _verify_under_load(workers=3, appends=8)
    assert verify_chain() is None

    victim = _tamper_with_one_entry()

    divergence = verify_chain()
    assert divergence is not None
    assert divergence.entry_id == victim
    assert "entry_hash" in divergence.reason
    assert chain_status()["ok"] is False
    with pytest.raises(AuditChainCorruptError):
        assert_chain_intact()


@_pg_skip
def test_postgres_verification_under_concurrent_appends_is_consistent(pg_store):
    """The same guarantee on the other backend, through its own isolation."""
    record_required(actor="seed", action="probe.seed", payload={})

    verdicts, runs = _verify_under_load(workers=4, appends=15)

    false_reports = [verdict for verdict in verdicts if verdict is not None]
    assert false_reports == [], (
        f"{len(false_reports)} of {runs} verifications called an intact chain broken: "
        f"{false_reports[0].reason if false_reports else ''}"
    )
    assert verify_chain() is None
    status = chain_status()
    assert status["ok"] is True
    assert status["stored_entries"] == status["appended_entries"] == 62


@_pg_skip
def test_postgres_still_reports_a_real_tamper(pg_store):
    record_required(actor="seed", action="probe.seed", payload={})
    _verify_under_load(workers=3, appends=8)
    assert verify_chain() is None

    victim = _tamper_with_one_entry()

    divergence = verify_chain()
    assert divergence is not None
    assert divergence.entry_id == victim
    assert chain_status()["ok"] is False


# ----------------------------------------------------------------------
# Corruption is fail-closed
# ----------------------------------------------------------------------


def test_truncated_tail_is_reported_even_though_the_rest_still_chains(isolated_store):
    record_required(actor="a", action="probe.keep", payload={"i": 1})
    doomed = record_required(actor="a", action="probe.drop", payload={"i": 2})

    with db_module.SessionLocal() as session:
        session.query(AuditLogEntry).filter(AuditLogEntry.id == doomed).delete()
        session.commit()

    divergence = verify_chain()
    assert divergence is not None
    assert "chain head" in divergence.reason
    assert chain_status()["ok"] is False


def test_append_over_a_broken_chain_is_refused(isolated_store):
    record_required(actor="a", action="probe.one", payload={})
    doomed = record_required(actor="a", action="probe.two", payload={})
    with db_module.SessionLocal() as session:
        session.query(AuditLogEntry).filter(AuditLogEntry.id == doomed).delete()
        session.commit()

    with pytest.raises(AuditChainCorruptError):
        record_required(actor="a", action="probe.three", payload={})


def test_head_count_mismatch_is_reported_as_a_gap(isolated_store):
    """A signed head whose count disagrees with the log is still a gap.

    The head is re-signed here with the real key, which is what the count
    check is *for*: an attacker without the key cannot get this far, but a
    head that was mis-seeded (or re-signed by a buggy build) must not pass.
    """
    record_required(actor="a", action="probe.gap", payload={})
    with db_module.SessionLocal() as session:
        head = session.get(AuditChainHead, 1)
        head.seq = head.seq + 3
        head.head_mac = audit_module._head_mac(head.seq, head.head_hash, head.head_entry_id)
        session.commit()

    divergence = verify_chain()
    assert divergence is not None
    assert "counted" in divergence.reason
    with pytest.raises(AuditChainCorruptError):
        assert_chain_intact()


def test_assert_chain_intact_passes_on_a_healthy_chain(isolated_store):
    for index in range(3):
        record_required(actor="a", action="probe.ok", payload={"i": index})
    assert_chain_intact()


def test_signed_head_makes_a_matching_truncation_detectable(isolated_store):
    """Deleting the tail *and* moving the head must not produce a clean report.

    The head is the anchor that makes truncation visible, so an attacker who
    can write the database would simply move it. Signing the head triple with
    the audit key closes that.
    """
    for index in range(5):
        record_required(actor="a", action="probe.trunc", payload={"i": index})
    assert verify_chain() is None

    with db_module.SessionLocal() as session:
        survivor = session.query(AuditLogEntry).order_by(AuditLogEntry.id.asc()).limit(2).all()[-1]
        session.query(AuditLogEntry).filter(AuditLogEntry.id > survivor.id).delete()
        head = session.get(AuditChainHead, 1)
        head.seq = 2
        head.head_hash = survivor.entry_hash
        head.head_entry_id = survivor.id
        session.commit()

    divergence = verify_chain()
    assert divergence is not None
    assert "signature" in divergence.reason
    assert chain_status()["ok"] is False


def test_append_over_a_forged_head_is_refused(isolated_store):
    record_required(actor="a", action="probe.sign", payload={})
    with db_module.SessionLocal() as session:
        head = session.get(AuditChainHead, 1)
        head.seq = head.seq + 5
        session.commit()

    with pytest.raises(AuditChainCorruptError):
        record_required(actor="a", action="probe.sign", payload={})


def test_chain_status_reports_the_signed_and_adopted_state(isolated_store):
    """A fresh store initialises signed and marked; the report says so."""
    record_required(actor="a", action="probe.sign", payload={})

    status = chain_status()
    assert status["ok"] is True
    assert status["head_signed"] is True
    assert status["adopted_version"] == audit_module.CHAIN_SIGNATURE_VERSION
    assert status["adoption_required"] is False


# ----------------------------------------------------------------------
# An unsigned head cannot launder a truncation
# ----------------------------------------------------------------------


def _legacy_unsigned_head(session_factory) -> None:
    """Put the store in the pre-signature shape: unsigned head, no marker."""
    with session_factory() as session:
        head = session.get(AuditChainHead, 1)
        head.head_mac = None
        head.adopted_version = None
        head.adopted_at = None
        session.commit()


def _write_legacy_chain(count: int, *, action: str = "probe.legacy") -> None:
    """Recreate a genuine pre-signature store.

    Entries chained exactly as the old appender wrote them - no genesis
    marker, no adoption entry - under an unsigned, unmarked head. This is what
    a store upgraded from before signed heads actually looks like, and it is
    the only shape ``adopt_legacy_chain`` may sign.
    """
    with db_module.SessionLocal() as session:
        session.query(AuditLogEntry).delete()
        prev = audit_module.GENESIS_HASH
        newest_id: int | None = None
        for index in range(count):
            now = datetime.now(UTC)
            canonical = audit_module._canonical_payload(
                created_at=now,
                actor="legacy",
                action=action,
                workspace_id=None,
                payload={"i": index},
            )
            entry_hash = audit_module._compute_hash(prev, canonical)
            entry = AuditLogEntry(
                created_at=now,
                actor="legacy",
                action=action,
                workspace_id=None,
                payload_json=canonical,
                prev_hash=prev,
                entry_hash=entry_hash,
            )
            session.add(entry)
            session.flush()
            prev, newest_id = entry_hash, entry.id
        head = session.get(AuditChainHead, 1)
        head.seq = count
        head.head_hash = prev
        head.head_entry_id = newest_id
        head.head_mac = None
        head.adopted_version = None
        head.adopted_at = None
        session.commit()


def test_truncate_rewrite_and_clear_the_signature_is_still_reported(isolated_store):
    """The exact laundering shape: delete the tail, move the head, drop the MAC.

    Every step is available to an attacker who can write the database, and
    with an unsigned head treated as "adopted at upgrade" the result verified
    clean. It must not.
    """
    for index in range(5):
        record_required(actor="a", action="probe.launder", payload={"i": index})
    assert verify_chain() is None

    with db_module.SessionLocal() as session:
        survivor = session.query(AuditLogEntry).order_by(AuditLogEntry.id.asc()).limit(2).all()[-1]
        session.query(AuditLogEntry).filter(AuditLogEntry.id > survivor.id).delete()
        head = session.get(AuditChainHead, 1)
        head.seq = 2
        head.head_hash = survivor.entry_hash
        head.head_entry_id = survivor.id
        head.head_mac = None
        session.commit()

    divergence = verify_chain()
    assert divergence is not None
    assert "cleared" in divergence.reason
    assert chain_status()["ok"] is False


def test_append_over_an_unsigned_head_is_refused(isolated_store):
    """A non-empty chain with no head signature refuses every append."""
    _write_legacy_chain(2)

    with pytest.raises(AuditChainCorruptError):
        record_required(actor="a", action="probe.unsigned", payload={})

    status = chain_status()
    assert status["ok"] is False
    assert status["adoption_required"] is True


def test_clearing_the_signature_after_adoption_is_tamper_not_legacy(isolated_store):
    """The marker persists, so a cleared MAC cannot recreate legacy state."""
    record_required(actor="a", action="probe.marker", payload={})
    with db_module.SessionLocal() as session:
        head = session.get(AuditChainHead, 1)
        head.head_mac = None  # marker deliberately left in place
        session.commit()

    divergence = verify_chain()
    assert divergence is not None
    assert "cleared" in divergence.reason
    assert chain_status()["adoption_required"] is False
    with pytest.raises(AuditChainCorruptError):
        adopt_legacy_chain()
    with pytest.raises(AuditChainCorruptError):
        record_required(actor="a", action="probe.marker", payload={})


def test_empty_store_initialises_a_signed_head(isolated_store):
    """Nothing to launder, so a fresh chain never needs adoption."""
    _legacy_unsigned_head(db_module.SessionLocal)
    assert verify_chain() is None, "an empty unsigned head is not a divergence"
    assert chain_status()["adoption_required"] is False

    record_required(actor="a", action="probe.fresh", payload={})

    status = chain_status()
    assert status["ok"] is True
    assert status["head_signed"] is True
    assert status["adopted_version"] == audit_module.CHAIN_SIGNATURE_VERSION


def test_legitimate_legacy_chain_is_adopted_once_and_becomes_appendable(isolated_store):
    _write_legacy_chain(3)
    assert verify_chain() is not None

    outcome = adopt_legacy_chain(actor="operator")

    assert outcome["status"] == "adopted"
    assert outcome["entries"] == 3
    assert verify_chain() is None
    status = chain_status()
    assert status["head_signed"] is True
    assert status["adoption_required"] is False
    # Adoption records itself, and the record is a chain link like any other.
    adoptions = audit_module.list_entries(action_prefix=audit_module.ADOPTION_ACTION, limit=10)
    assert len(adoptions) == 1
    assert adoptions[0]["payload"]["entries_at_adoption"] == 3
    # And the chain accepts appends again.
    record_required(actor="a", action="probe.after-adopt", payload={})
    assert verify_chain() is None
    # A second adoption is a no-op, not a second signature.
    assert adopt_legacy_chain()["status"] == "already_adopted"


def test_adoption_refuses_a_chain_that_does_not_verify(isolated_store):
    """Adoption is not a repair tool: a broken log is refused, not signed."""
    _write_legacy_chain(4)
    with db_module.SessionLocal() as session:
        doomed = session.query(AuditLogEntry).order_by(AuditLogEntry.id.asc()).all()[-1]
        session.query(AuditLogEntry).filter(AuditLogEntry.id == doomed.id).delete()
        session.commit()

    with pytest.raises(AuditChainCorruptError):
        adopt_legacy_chain()

    with db_module.SessionLocal() as session:
        assert session.get(AuditChainHead, 1).head_mac is None, "a refused adoption signed anyway"


def test_adoption_refuses_a_mutated_legacy_entry(isolated_store):
    _write_legacy_chain(3)
    with db_module.SessionLocal() as session:
        middle = session.query(AuditLogEntry).order_by(AuditLogEntry.id.asc()).all()[1]
        middle.payload_json = middle.payload_json.replace('"i":1', '"i":99')
        session.commit()

    with pytest.raises(AuditChainCorruptError):
        adopt_legacy_chain()


def test_adoption_refuses_a_store_whose_log_records_a_signed_origin(isolated_store):
    """Stripping the marker does not buy a second adoption.

    A store that was signed from the start begins its log with the genesis
    marker, and an adopted one carries its adoption entry, so clearing both
    head columns is reported as tamper rather than re-signed.
    """
    record_required(actor="a", action="probe.readopt", payload={})
    _legacy_unsigned_head(db_module.SessionLocal)

    with pytest.raises(AuditChainCorruptError):
        adopt_legacy_chain()

    # The same holds after a legitimate adoption, whose entry is the evidence.
    _write_legacy_chain(2)
    assert adopt_legacy_chain()["status"] == "adopted"
    _legacy_unsigned_head(db_module.SessionLocal)
    with pytest.raises(AuditChainCorruptError):
        adopt_legacy_chain()


def test_adoption_refuses_a_legacy_head_whose_count_disagrees(isolated_store):
    """Signing a head that disagrees with its log would brick the store.

    The result would be adopted (so it can never be adopted again) *and*
    permanently unverifiable, with the CLI having reported success.
    """
    _write_legacy_chain(3)
    with db_module.SessionLocal() as session:
        head = session.get(AuditChainHead, 1)
        head.seq = 10
        session.commit()

    with pytest.raises(AuditChainCorruptError):
        adopt_legacy_chain()

    with db_module.SessionLocal() as session:
        assert session.get(AuditChainHead, 1).head_mac is None
        assert session.get(AuditChainHead, 1).adopted_version is None


def test_adoption_refuses_a_legacy_head_that_points_at_the_wrong_entry(isolated_store):
    _write_legacy_chain(3)
    with db_module.SessionLocal() as session:
        rows = session.query(AuditLogEntry).order_by(AuditLogEntry.id.asc()).all()
        head = session.get(AuditChainHead, 1)
        head.head_entry_id = rows[0].id
        head.head_hash = rows[0].entry_hash
        session.commit()

    with pytest.raises(AuditChainCorruptError):
        adopt_legacy_chain()


def test_adoption_refuses_when_the_head_row_itself_is_gone(isolated_store):
    """The head is the only independent record of how long the log was.

    Deleting it and re-seeding from the surviving rows would make the triple
    and count checks compare the log with itself, so a truncation would be
    signed as if it were legacy state.
    """
    _write_legacy_chain(6)
    with db_module.SessionLocal() as session:
        rows = session.query(AuditLogEntry).order_by(AuditLogEntry.id.asc()).all()
        session.query(AuditLogEntry).filter(AuditLogEntry.id > rows[2].id).delete()
        session.query(AuditChainHead).filter(AuditChainHead.id == 1).delete()
        session.commit()

    with pytest.raises(AuditChainCorruptError):
        adopt_legacy_chain()

    assert verify_chain() is not None, "a truncated store reported clean after adoption"


def test_concurrent_adoption_signs_once(isolated_store):
    """Two operators racing the same store must not produce two adoptions."""
    _write_legacy_chain(2)

    outcomes: list[dict] = []
    errors: list[BaseException] = []

    def _adopt() -> None:
        try:
            outcomes.append(adopt_legacy_chain())
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_adopt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert [o["status"] for o in outcomes].count("adopted") == 1
    assert [o["status"] for o in outcomes].count("already_adopted") == 3
    assert verify_chain() is None
    assert len(audit_module.list_entries(action_prefix=audit_module.ADOPTION_ACTION, limit=10)) == 1


def test_workspace_prune_keeps_audit_entries(isolated_store):
    """A supported maintenance command must not brick governed execution.

    ``audit_log`` rows are chain links: deleting the newest one leaves a head
    the log no longer matches, and every later append is refused. The Workspace
    cascade therefore clears the reference and keeps the entry.
    """
    from brains.storage.integrity import WORKSPACE_SCOPED_TABLES, workspace_cascade_tables

    assert "audit_log" not in WORKSPACE_SCOPED_TABLES

    connection = sqlite3.connect(str(isolated_store))
    try:
        steps = {step.table: step for step in workspace_cascade_tables(connection)}
    finally:
        connection.close()

    assert "audit_log" in steps
    assert steps["audit_log"].sql("id = 1").upper().startswith("UPDATE"), (
        "the Workspace cascade deletes audit entries"
    )

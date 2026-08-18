"""Signed, tamper-evident, transactionally-appended audit log.

Threat model
------------

The audit log defends against an attacker who can read or mutate the
brains SQLite database file (or the Postgres ``audit_log`` table) but
who **cannot** read the audit-key file (``~/.brains/audit-key``,
0600) or the ``BRAINS_AUDIT_KEY`` environment variable. Under that
assumption every tamper is detectable:

* **Row deletion / row insertion** - breaks the ``prev_hash`` chain.
* **Row mutation** - breaks the ``entry_hash`` of the mutated row.
* **Truncating the log** - ``audit_chain_head`` names the newest entry that
  was ever appended and counts every append, and the head triple is itself
  HMAC-signed, so a truncated tail is reported as a gap even though the
  surviving rows chain cleanly among themselves and even if the attacker also
  rewrites the head.
* **Laundering a truncation through an unsigned head** - an unsigned head over
  a non-empty log is *itself* a divergence: verification fails and every append
  is refused. Clearing ``head_mac`` therefore cannot turn a signed store back
  into a "pre-signature" one, because the adoption marker
  (``adopted_version``) persists next to the signature, an adopted store whose
  signature is missing is reported as tampered, and the log itself begins with
  a genesis marker (``audit.chain.initialized``) or carries an adoption entry
  (``audit.chain.adopted``) that adoption refuses to sign over. Deleting either
  marker breaks the ``prev_hash`` of everything appended after it.

Legacy adoption (once, explicitly, verified first)
--------------------------------------------------

A store written before the head was signed genuinely has an unsigned head. It
is distinguished from a laundered one exactly once, by
:func:`adopt_legacy_chain`, which an operator runs (``brains-ai
audit-adopt``). Adoption verifies the whole chain, the head triple and the
append count *before* signing, refuses when the log already carries an
adoption entry, and signs, marks (``adopted_version``/``adopted_at``) and
records itself in one transaction. A fresh store never needs it: its first
append initialises a signed, marked head and writes the genesis marker.

The residual limit is stated rather than hidden: an attacker who can write the
database *and* delete every entry back to and including the genesis marker
(destroying the whole history the marker anchors) leaves a store that looks
pre-signature again. Adoption is never automatic on a non-empty log, so
re-laundering also needs an operator to run the adoption command against a
store whose history has visibly vanished.

It explicitly does **not** defend against:

* An attacker who also stole the audit key - they can re-sign a
  forged chain. Treat the key like any other secret (file mode 0600,
  rotate via ``BRAINS_AUDIT_KEY``).
* An attacker who can prevent the process from reaching the database
  at all. That case is handled by *refusing the governed action*, not
  by continuing without a record - see the durability contract below.

Durability contract (BL-P0-04)
------------------------------

There are three append surfaces and the difference between them is the
whole point:

:func:`append_in_session`
    Appends inside the caller's transaction. The governed state
    transition and its audit entry commit together or not at all, so a
    store cannot contain an authorised action with no audit record.
    Raises :class:`AuditWriteError` - callers must let it propagate and
    fail closed *before* the effect.

:func:`record_required`
    The same guarantee for a caller that has no transaction of its own:
    it owns the session, commits, and raises on failure.

:func:`record`
    The historical best-effort observer, kept only for telemetry-shaped
    appends whose loss does not change what a user is told happened. It
    returns ``None`` on failure. It must not be used on a path that
    authorises, executes, or reports an outward effect.

:func:`required_effect`
    The ordering that :func:`record_required` alone cannot give an effect
    that is not a database write - an overlay file, an archive, a restore
    over the live store. It commits ``<action>.attempted`` *before* the
    effect and appends ``<action>`` or ``<action>.failed`` after it, so a
    store never carries a success entry for an effect that had not happened
    when it was written, and never carries an effect with no evidence at all.
    A bare ``record_required`` placed after the mutation has the opposite
    property: it raises, but only once the effect is already taken.

Multi-process append order
--------------------------

``prev_hash`` used to be computed from ``MAX(id)`` under a
process-local :class:`threading.Lock`, so two Brains processes sharing
one store could read the same predecessor and fork the chain. Every
append now claims the single ``audit_chain_head`` row first - a write on
SQLite (which escalates the transaction to a reserved lock) and
``SELECT ... FOR UPDATE`` on Postgres - so the predecessor read is
serialised across processes, not just across threads. A contended
append retries with backoff; a store whose head no longer matches its
newest row is refused rather than extended.

Verification reads one instant, not two
---------------------------------------

Verification compares the log against the head, so the two reads must describe
the same instant: an append that commits between them leaves a head one entry
ahead of the rows, which is indistinguishable from a truncation. That is a
false tamper report on an intact chain, and a verifier that cries wolf is worse
than none.

:func:`_read_snapshot` therefore gives every verification path (
:func:`verify_chain`, :func:`chain_status`, :func:`adoption_required`) a single
consistent read view, with backend-correct isolation rather than a lock held
over the scan: Postgres takes a ``REPEATABLE READ`` snapshot, and SQLite opens
an explicit read transaction, because pysqlite starts one for DML but not for
``SELECT`` - which is exactly what let two reads straddle a commit. Under WAL
(what Brains configures) a SQLite read transaction does not block appends, and
nothing on these paths writes, so verification never mutates, blocks, or
extends the chain it is checking.

On-the-wire layout
------------------

Each entry is a dict canonicalised via
:func:`json.dumps(..., sort_keys=True, separators=(",", ":"))`. The
hash input is::

    HMAC-SHA256(audit_key,
                prev_hash + "|" + canonical_json(entry_fields))

where ``entry_fields`` is::

    {
        "created_at": "<ISO-8601 UTC, microsecond precision>",
        "actor":      "<string>",
        "action":     "<dotted name>",
        "workspace_id": <int | null>,
        "payload":    <arbitrary JSON-serialisable dict>,
    }

``id`` is **not** part of the signed payload because SQLite assigns
it post-insert; the chain is anchored on ``created_at`` instead.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from brains.storage.db import SessionLocal
from brains.storage.models import AuditChainHead, AuditLogEntry

logger = logging.getLogger("brains.audit")


GENESIS_HASH = "GENESIS"
DEFAULT_AUDIT_KEY_PATH = Path.home() / ".brains" / "audit-key"
_AUDIT_KEY_ENV = "BRAINS_AUDIT_KEY"
_AUDIT_KEY_PATH_ENV = "BRAINS_AUDIT_KEY_FILE"

#: The head row is a singleton; its primary key is fixed.
CHAIN_HEAD_ID = 1

#: Bumped only when the signed-head format changes. A non-NULL
#: ``adopted_version`` is the store's persisted commitment to a signed head:
#: once it is set, a missing signature is tamper, never "legacy".
CHAIN_SIGNATURE_VERSION = 1

#: The action name of the entry adoption appends. Its presence in the log is
#: what stops a second "adoption" from re-signing a chain that was already
#: adopted and then stripped of its marker.
ADOPTION_ACTION = "audit.chain.adopted"

#: The genesis marker a fresh store writes as its first entry. A log that
#: begins with it was signed from the start, so it can never be adopted as a
#: pre-signature chain - and deleting the marker breaks every ``prev_hash``
#: after it.
INIT_ACTION = "audit.chain.initialized"

#: Both markers together: the chain's own evidence that this store has already
#: committed to signed heads.
SIGNED_ORIGIN_ACTIONS = (ADOPTION_ACTION, INIT_ACTION)

#: A contended append (SQLite ``SQLITE_BUSY``/snapshot conflict, or a lost
#: race on the head row) is retried rather than failed: the loser of a race
#: has not written anything yet, so retrying is safe and keeps the chain
#: linear. Exhausting the budget is a hard failure, never a silent skip.
_APPEND_ATTEMPTS = 6
_APPEND_BACKOFF_SECONDS = 0.05

# Module-level cache for the resolved key so we don't hit the disk on
# every append. A simple lock guards concurrent first-time resolution
# (gateway request fan-out hits this from multiple threads).
_key_cache: bytes | None = None
_key_lock = threading.Lock()


class AuditWriteError(RuntimeError):
    """The audit entry could not be appended, so the caller must fail closed."""


class AuditChainCorruptError(AuditWriteError):
    """The stored chain does not match its head; appending would hide the break."""


@dataclass
class VerifyDivergence:
    """Reported by :func:`verify_chain` when the chain breaks."""

    entry_id: int
    reason: str
    expected_hash: str
    actual_hash: str


def _resolve_key_path() -> Path:
    override = os.environ.get(_AUDIT_KEY_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return DEFAULT_AUDIT_KEY_PATH


def _generate_and_persist_key() -> bytes:
    """Create a fresh 32-byte key and persist to the key file (0600)."""
    path = _resolve_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new_key = secrets.token_bytes(32)
    # Write atomically + tighten permissions before any other process
    # can open the file.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(new_key)
    # Windows doesn't honor POSIX permissions; chmod is a best-effort.
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    logger.info(
        "audit_key_generated path=%s fingerprint=%s",
        path,
        _fingerprint(new_key),
    )
    return new_key


def _load_key_from_disk() -> bytes | None:
    path = _resolve_key_path()
    if not path.exists():
        return None
    raw = path.read_bytes()
    data = raw.strip()
    if not data:
        return None
    if len(raw) == 32:
        return raw
    # Operators may paste a hex/base64 key into the file. Accept
    # raw bytes as the canonical form; if the file looks like ASCII
    # hex (64 chars) decode it for convenience.
    if len(data) == 64:
        try:
            return bytes.fromhex(data.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return data
    return data


def _resolve_audit_key() -> bytes:
    """Return the operator-local audit key, generating one on first use."""
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    with _key_lock:
        if _key_cache is not None:
            return _key_cache
        env_value = os.environ.get(_AUDIT_KEY_ENV)
        if env_value:
            # Env values are normally hex; fall back to literal bytes.
            try:
                _key_cache = bytes.fromhex(env_value)
            except ValueError:
                _key_cache = env_value.encode("utf-8")
            return _key_cache
        loaded = _load_key_from_disk()
        if loaded is not None:
            _key_cache = loaded
            return _key_cache
        _key_cache = _generate_and_persist_key()
        return _key_cache


def _reset_key_cache() -> None:
    """Test helper: drop the cached key so the next call re-resolves."""
    global _key_cache
    with _key_lock:
        _key_cache = None


def _fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:12]


def audit_key_fingerprint() -> str:
    """First 12 hex chars of ``SHA-256(audit_key)``.

    Safe to log: lets two installs confirm they share the same key
    without exposing the key itself.
    """
    return _fingerprint(_resolve_audit_key())


def _canonical_payload(
    *,
    created_at: datetime,
    actor: str,
    action: str,
    workspace_id: int | None,
    payload: dict[str, Any],
) -> str:
    """JSON-canonicalise the signed fields.

    Sort keys, no whitespace, ISO-8601 UTC timestamps with microseconds.
    """
    # Force UTC + isoformat with microseconds for round-trip determinism.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    ts = created_at.astimezone(UTC).isoformat(timespec="microseconds")
    body = {
        "created_at": ts,
        "actor": actor,
        "action": action,
        "workspace_id": workspace_id,
        "payload": payload,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _compute_hash(prev_hash: str, canonical: str) -> str:
    key = _resolve_audit_key()
    mac = hmac.new(
        key,
        (prev_hash + "|" + canonical).encode("utf-8"),
        hashlib.sha256,
    )
    return mac.hexdigest()


# --------------------------------------------------------------------------- #
# Chain head
# --------------------------------------------------------------------------- #


def _head_mac(seq: int, head_hash: str, head_entry_id: int | None) -> str:
    """HMAC over the head triple, so the head itself is not forgeable.

    Without this an attacker who can write the database could truncate
    ``audit_log`` and move the head to match: the surviving rows would still
    chain cleanly and the count would still agree.
    """
    key = _resolve_audit_key()
    payload = f"{seq}|{head_hash}|{head_entry_id if head_entry_id is not None else ''}"
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _newest_entry(session) -> tuple[int | None, str]:  # noqa: ANN001 - SQLAlchemy Session
    row = session.execute(
        select(AuditLogEntry.id, AuditLogEntry.entry_hash)
        .order_by(AuditLogEntry.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None, GENESIS_HASH
    return row[0], row[1]


def _ensure_head_row(session) -> None:  # noqa: ANN001 - SQLAlchemy Session
    """Seed the singleton head row from the existing log if it is absent.

    Present for a store whose ``audit_chain_head`` row was removed out of
    band; the 126 migration seeds it for every normal upgrade. Seeding adopts
    what the log already says rather than resetting it, so a chain that was
    already forked stays reportable, and it never signs: an *empty* log is
    signed by the first append, which also writes the genesis marker, and a
    non-empty log is left unsigned, which fails verification and refuses every
    append until an operator adopts it deliberately. A head row that reappeared
    next to entries nobody can account for is not evidence.
    """
    exists = session.execute(
        select(AuditChainHead.id).where(AuditChainHead.id == CHAIN_HEAD_ID)
    ).first()
    if exists is not None:
        return
    entry_id, entry_hash = _newest_entry(session)
    total = int(session.execute(select(func.count(AuditLogEntry.id))).scalar_one())
    with contextlib.suppress(IntegrityError), session.begin_nested():
        session.add(
            AuditChainHead(
                id=CHAIN_HEAD_ID,
                seq=total,
                head_hash=entry_hash,
                head_entry_id=entry_id,
                head_mac=None,
                adopted_version=None,
                adopted_at=None,
                updated_at=datetime.now(UTC),
            )
        )


_UNSIGNED_HEAD_MESSAGE = (
    "the audit chain head is unsigned over a non-empty log, so a truncation could "
    "not be detected. A store written before signed heads is adopted once, "
    "explicitly and only after it verifies, with `brains-ai audit-adopt`; "
    "otherwise this is tamper - run `brains-ai audit-verify`"
)
_CLEARED_SIGNATURE_MESSAGE = (
    "the audit chain head signature was cleared after this store adopted signed "
    "heads; a missing signature on an adopted store is tamper, not legacy state"
)


def _read_head(session):  # noqa: ANN001, ANN201 - SQLAlchemy Session/Row
    return session.execute(
        select(
            AuditChainHead.seq,
            AuditChainHead.head_hash,
            AuditChainHead.head_entry_id,
            AuditChainHead.head_mac,
            AuditChainHead.adopted_version,
        ).where(AuditChainHead.id == CHAIN_HEAD_ID)
    ).first()


def _lock_head(session):  # noqa: ANN001, ANN201 - SQLAlchemy Session/Row
    """Take the cross-process append lock and return the head row.

    SQLite has no row-level lock and a plain ``SELECT`` never blocks a
    competing writer, so the claim is a write: it escalates the transaction to
    a reserved lock and every other appender waits on ``busy_timeout`` (or
    retries). Postgres uses the row lock directly.
    """
    _ensure_head_row(session)
    if session.get_bind().dialect.name == "postgresql":
        return session.execute(
            select(
                AuditChainHead.seq,
                AuditChainHead.head_hash,
                AuditChainHead.head_entry_id,
                AuditChainHead.head_mac,
                AuditChainHead.adopted_version,
            )
            .where(AuditChainHead.id == CHAIN_HEAD_ID)
            .with_for_update()
        ).first()
    session.execute(
        update(AuditChainHead)
        .where(AuditChainHead.id == CHAIN_HEAD_ID)
        .values(updated_at=datetime.now(UTC))
    )
    return _read_head(session)


def _claim_head(session) -> tuple[int, str, int | None, bool]:  # noqa: ANN001 - Session
    """Take the append lock and return ``(seq, head_hash, head_id, initialised)``.

    A head that carries a signature must still verify: appending on top of a
    head that was moved out of band would launder the tamper into the chain. A
    head that carries *no* signature is refused for the same reason, unless the
    log is empty - the one state where there is nothing to launder, so the head
    is initialised signed and marked here and the caller writes the genesis
    marker that makes this store's signed origin part of the chain.
    """
    row = _lock_head(session)
    if row is None:
        raise AuditChainCorruptError("audit chain head row is missing and could not be seeded")
    seq, head_hash, head_entry_id = int(row[0]), str(row[1]), row[2]
    mac, adopted = row[3], row[4]
    initialised = False
    if mac is None:
        if adopted is not None:
            raise AuditChainCorruptError(_CLEARED_SIGNATURE_MESSAGE)
        stored = int(session.execute(select(func.count(AuditLogEntry.id))).scalar_one())
        if stored or seq or head_hash != GENESIS_HASH or head_entry_id is not None:
            raise AuditChainCorruptError(_UNSIGNED_HEAD_MESSAGE)
        _sign_head(session, seq, head_hash, head_entry_id)
        initialised = True
    elif not hmac.compare_digest(mac, _head_mac(seq, head_hash, head_entry_id)):
        raise AuditChainCorruptError(
            "audit chain head signature does not match its contents; refusing to append "
            "over a head that was moved out of band - run `brains-ai audit-verify`"
        )
    elif adopted is None:
        # Signed by a build that predates the marker: recording the marker is
        # not a new claim, it only stops the signature from being clearable.
        _mark_adopted(session)
    return seq, head_hash, head_entry_id, initialised


def _sign_head(session, seq: int, head_hash: str, head_entry_id: int | None) -> None:  # noqa: ANN001
    """Sign and mark the head in the caller's transaction."""
    session.execute(
        update(AuditChainHead)
        .where(AuditChainHead.id == CHAIN_HEAD_ID)
        .values(
            head_mac=_head_mac(seq, head_hash, head_entry_id),
            adopted_version=CHAIN_SIGNATURE_VERSION,
            adopted_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )


def _mark_adopted(session) -> None:  # noqa: ANN001 - SQLAlchemy Session
    session.execute(
        update(AuditChainHead)
        .where(AuditChainHead.id == CHAIN_HEAD_ID, AuditChainHead.adopted_version.is_(None))
        .values(adopted_version=CHAIN_SIGNATURE_VERSION, adopted_at=datetime.now(UTC))
    )


def _write_entry(
    session,  # noqa: ANN001 - SQLAlchemy Session
    *,
    seq: int,
    prev_hash: str,
    actor: str,
    action: str,
    payload: dict[str, Any],
    workspace_id: int | None,
) -> AuditLogEntry:
    """Write one entry and advance the signed head, in the caller's transaction."""
    now = datetime.now(UTC)
    canonical = _canonical_payload(
        created_at=now,
        actor=actor,
        action=action,
        workspace_id=workspace_id,
        payload=payload,
    )
    entry_hash = _compute_hash(prev_hash, canonical)
    entry = AuditLogEntry(
        created_at=now,
        actor=actor,
        action=action,
        workspace_id=workspace_id,
        payload_json=canonical,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    session.add(entry)
    session.flush()
    session.execute(
        update(AuditChainHead)
        .where(AuditChainHead.id == CHAIN_HEAD_ID)
        .values(
            seq=seq + 1,
            head_hash=entry_hash,
            head_entry_id=entry.id,
            head_mac=_head_mac(seq + 1, entry_hash, entry.id),
            adopted_version=CHAIN_SIGNATURE_VERSION,
            updated_at=now,
        )
    )
    return entry


def append_in_session(
    session,  # noqa: ANN001 - SQLAlchemy Session
    *,
    actor: str,
    action: str,
    payload: dict[str, Any] | None = None,
    workspace_id: int | None = None,
) -> AuditLogEntry:
    """Append one entry inside ``session``'s transaction.

    The caller commits. If this raises, the caller must let the
    transaction roll back and refuse the action it was about to take:
    that is the whole fail-closed contract.

    The very first append on an empty store writes a genesis marker ahead of
    the caller's entry. It costs one row and it is what makes a signed origin
    part of the chain: a store whose log begins with the marker cannot later be
    presented as pre-signature, because removing the marker breaks every
    ``prev_hash`` that follows it.
    """
    if not actor:
        actor = "anonymous"
    payload = dict(payload or {})
    # Flush whatever the caller already staged *before* the chain work starts.
    # The first SELECT below would autoflush it anyway, inside the ``try`` that
    # normalises every failure into ``AuditWriteError`` - so a caller's own
    # unique-constraint violation (a duplicated approval code, say) would be
    # reported as a broken audit chain and send an operator to verify a chain
    # that is intact. Flushed here, the caller's error stays the caller's.
    session.flush()
    try:
        seq, head_hash, head_entry_id, initialised = _claim_head(session)
        newest_id, newest_hash = _newest_entry(session)
        if head_hash != newest_hash or head_entry_id != newest_id:
            raise AuditChainCorruptError(
                f"audit chain head points at entry {head_entry_id!r}/{head_hash[:12]!r} but the "
                f"newest stored entry is {newest_id!r}/{newest_hash[:12]!r}; refusing to append "
                "over a broken chain - run `brains-ai audit-verify`"
            )
        if initialised:
            marker = _write_entry(
                session,
                seq=seq,
                prev_hash=newest_hash,
                actor="brains",
                action=INIT_ACTION,
                payload={
                    "signature_version": CHAIN_SIGNATURE_VERSION,
                    "fingerprint": audit_key_fingerprint(),
                },
                workspace_id=None,
            )
            seq, newest_hash = seq + 1, marker.entry_hash
        return _write_entry(
            session,
            seq=seq,
            prev_hash=newest_hash,
            actor=actor,
            action=action,
            payload=payload,
            workspace_id=workspace_id,
        )
    except AuditWriteError:
        raise
    except Exception as exc:
        raise AuditWriteError(f"audit append failed: {exc}") from exc


def _is_retryable(exc: BaseException | None) -> bool:
    return isinstance(exc, OperationalError | IntegrityError)


def record_required(
    *,
    actor: str,
    action: str,
    payload: dict[str, Any] | None = None,
    workspace_id: int | None = None,
) -> int:
    """Append one entry in its own transaction, raising on failure.

    For callers that have no transaction of their own but still must not
    proceed when the record cannot be written.
    """
    last_error: BaseException | None = None
    for attempt in range(_APPEND_ATTEMPTS):
        try:
            with SessionLocal() as session:
                entry = append_in_session(
                    session,
                    actor=actor,
                    action=action,
                    payload=payload,
                    workspace_id=workspace_id,
                )
                session.commit()
                return int(entry.id)
        except AuditChainCorruptError:
            raise
        except Exception as exc:
            last_error = exc
            cause = exc.__cause__ if isinstance(exc, AuditWriteError) else exc
            if not _is_retryable(cause) or attempt == _APPEND_ATTEMPTS - 1:
                break
            time.sleep(_APPEND_BACKOFF_SECONDS * (2**attempt) * (0.5 + random.random()))  # noqa: S311
    raise AuditWriteError(f"audit append failed after {_APPEND_ATTEMPTS} attempts: {last_error}")


def record(
    *,
    actor: str,
    action: str,
    payload: dict[str, Any] | None = None,
    workspace_id: int | None = None,
) -> int | None:
    """Best-effort append for telemetry-shaped entries.

    Returns the new entry id, or ``None`` when the append failed. Use
    :func:`record_required` or :func:`append_in_session` on any path that
    authorises, executes, or reports a governed effect: a ``None`` here
    means the record is simply gone.
    """
    try:
        return record_required(
            actor=actor, action=action, payload=payload, workspace_id=workspace_id
        )
    except Exception:
        logger.exception("audit_record_failed actor=%s action=%s", actor, action)
        return None


#: Appended *before* the effect. Its presence alone never means the effect
#: happened - only that it was authorised and about to be attempted.
ATTEMPT_SUFFIX = ".attempted"

#: Appended after an effect that raised. The attempt entry stays: together
#: they say "we tried, and it did not work", which is what happened.
FAILURE_SUFFIX = ".failed"


class EffectRecord:
    """Handle for the outcome half of a :func:`required_effect` block."""

    def __init__(self, action: str, attempt_audit_id: int) -> None:
        self.action = action
        #: The id of the durable pre-effect entry, carried on both outcomes so
        #: an attempt and its result can be joined in the log.
        self.attempt_audit_id = attempt_audit_id
        self.outcome: dict[str, Any] = {}

    def record_outcome(self, payload: dict[str, Any] | None = None, **fields: Any) -> None:
        """Add observed detail to the success entry (archive path, rows, ...)."""
        self.outcome.update(dict(payload or {}))
        self.outcome.update(fields)


@contextlib.contextmanager
def required_effect(
    *,
    actor: str,
    action: str,
    payload: dict[str, Any] | None = None,
    workspace_id: int | None = None,
):  # noqa: ANN201 - yields EffectRecord
    """Wrap one irreversible effect in a durable two-phase record.

    A single :func:`record_required` *after* the effect is the wrong shape for
    anything irreversible. It raises rather than losing the record, but by then
    the overlay is written, the archive is on disk, or the database has been
    restored over - so the failure mode it protects against (an effect with no
    record) is exactly the one it produces. The ordering has to be the other
    way round:

    1. ``<action>.attempted`` is appended **and committed** before the effect
       runs. If it cannot be written the effect never happens: the block
       raises :class:`AuditWriteError` before yielding.
    2. The effect runs.
    3. ``<action>`` is appended once the effect returned - so the success name
       is only ever in the log *after* the thing it names - or
       ``<action>.failed`` when it raised.

    Failing to record step 3 does not turn into a silent success: on the
    success path the :class:`AuditWriteError` propagates, with the effect
    honestly reported as taken but unrecorded, and on the failure path the
    original exception is preserved because the attempt entry already stands
    as evidence.

    This is the weaker of the two durable shapes and is only for effects that
    are *not* database writes. When the mutation goes through a SQLAlchemy
    session, use :func:`append_in_session` inside that transaction instead:
    the mutation and its record then commit or roll back together, which is
    strictly stronger than any ordering of two transactions.
    """
    request = dict(payload or {})
    attempt_id = record_required(
        actor=actor,
        action=f"{action}{ATTEMPT_SUFFIX}",
        payload=request,
        workspace_id=workspace_id,
    )
    handle = EffectRecord(action, attempt_id)
    try:
        yield handle
    except BaseException as exc:
        try:
            record_required(
                actor=actor,
                action=f"{action}{FAILURE_SUFFIX}",
                payload={
                    **request,
                    "attempt_audit_id": attempt_id,
                    "error": f"{type(exc).__name__}: {exc}"[:2000],
                },
                workspace_id=workspace_id,
            )
        except Exception:
            # The attempt entry is already durable, so the log still shows
            # that this ran. Losing the *detail* of the failure must not
            # replace the failure itself.
            logger.exception("audit_effect_failure_record_failed action=%s", action)
        raise
    try:
        record_required(
            actor=actor,
            action=action,
            payload={**request, **handle.outcome, "attempt_audit_id": attempt_id},
            workspace_id=workspace_id,
        )
    except AuditWriteError as exc:
        raise AuditWriteError(
            f"{action} completed but its outcome could not be recorded "
            f"(attempt entry {attempt_id} stands): {exc}"
        ) from exc


def list_entries(
    *,
    limit: int = 100,
    since_id: int | None = None,
    action_prefix: str | None = None,
    actor: str | None = None,
) -> list[dict[str, Any]]:
    """Return a list of entries (most recent first) as plain dicts.

    ``since_id`` restricts to ``id > since_id`` (useful for tail
    pagination). ``action_prefix`` matches via SQL ``LIKE`` so callers
    can filter to a subsystem (``provider.``, ``task.``, etc.).
    """
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000
    with SessionLocal() as session:
        q = session.query(AuditLogEntry)
        if since_id is not None:
            q = q.filter(AuditLogEntry.id > since_id)
        if action_prefix:
            q = q.filter(AuditLogEntry.action.like(f"{action_prefix}%"))
        if actor:
            q = q.filter(AuditLogEntry.actor == actor)
        rows = q.order_by(AuditLogEntry.id.desc()).limit(limit).all()
        return [_entry_to_dict(r) for r in rows]


def _entry_to_dict(entry: AuditLogEntry) -> dict[str, Any]:
    try:
        payload_obj = json.loads(entry.payload_json)
    except (json.JSONDecodeError, TypeError):
        payload_obj = {"raw": entry.payload_json}
    return {
        "id": entry.id,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "actor": entry.actor,
        "action": entry.action,
        "workspace_id": entry.workspace_id,
        "payload": payload_obj.get("payload", payload_obj),
        "prev_hash": entry.prev_hash,
        "entry_hash": entry.entry_hash,
    }


def _head_row(session):  # noqa: ANN001, ANN201 - SQLAlchemy Session/Row
    return _read_head(session)


@contextlib.contextmanager
def _read_snapshot():  # noqa: ANN201 - yields a SQLAlchemy Session
    """Yield a session whose reads all come from one consistent view.

    Verification reads the log and then the head. Without a snapshot those are
    two separate instants, and an append committing between them shows a head
    one entry ahead of the rows - reported as a truncation on a chain that is
    perfectly intact. Concurrency is the normal case (``serve-all`` is three
    processes), so that false report is the common one.

    The isolation is backend-correct rather than a lock held across the scan:

    * **Postgres** takes a ``REPEATABLE READ`` snapshot, so appends continue.
    * **SQLite** opens an explicit read transaction. pysqlite emits ``BEGIN``
      before DML but not before ``SELECT``, so without this each read ran in
      its own implicit transaction. Under WAL - which
      :mod:`brains.storage.db` configures - a read transaction takes a stable
      snapshot without blocking the writer.

    Nothing on these paths writes, and the transaction is rolled back, so
    verification neither mutates the chain nor holds a lock over it.
    """
    session = SessionLocal()
    try:
        connection = session.connection()
        dialect = connection.dialect.name
        if dialect == "postgresql":
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        elif dialect == "sqlite" and not _sqlite_in_transaction(connection):
            connection.exec_driver_sql("BEGIN")
        yield session
    finally:
        session.rollback()
        session.close()


def _sqlite_in_transaction(connection) -> bool:  # noqa: ANN001 - SQLAlchemy Connection
    """Whether pysqlite already opened a transaction on this connection."""
    try:
        return bool(connection.connection.driver_connection.in_transaction)
    except AttributeError:  # pragma: no cover - non-pysqlite SQLite driver
        return True


def _chain_rows(session) -> list[Any]:  # noqa: ANN001 - SQLAlchemy Session
    """Every entry, oldest first, as plain rows.

    Columns rather than ORM instances: the snapshot session is rolled back and
    closed before the scan, which would expire attributes on mapped objects.
    """
    return list(
        session.execute(
            select(
                AuditLogEntry.id,
                AuditLogEntry.prev_hash,
                AuditLogEntry.entry_hash,
                AuditLogEntry.payload_json,
                AuditLogEntry.action,
            ).order_by(AuditLogEntry.id.asc())
        ).all()
    )


def _read_chain() -> tuple[list[Any], Any]:
    """The whole log and its head, read from one consistent snapshot."""
    with _read_snapshot() as session:
        return _chain_rows(session), _read_head(session)


def _scan_entries(rows: list[Any]) -> VerifyDivergence | None:
    """Recompute every hash and prev link; report the first break."""
    expected_prev = GENESIS_HASH
    for row in rows:
        if row.prev_hash != expected_prev:
            return VerifyDivergence(
                entry_id=row.id,
                reason="prev_hash does not match preceding entry",
                expected_hash=expected_prev,
                actual_hash=row.prev_hash,
            )
        # Re-canonicalise the signed payload from the stored fields. The
        # stored payload_json IS the canonical form (we wrote it that
        # way) so we hash it directly.
        recomputed = _compute_hash(row.prev_hash, row.payload_json)
        if recomputed != row.entry_hash:
            return VerifyDivergence(
                entry_id=row.id,
                reason="entry_hash does not match recomputed HMAC",
                expected_hash=recomputed,
                actual_hash=row.entry_hash,
            )
        expected_prev = row.entry_hash
    return None


def _check_head(rows: list[Any], head) -> VerifyDivergence | None:  # noqa: ANN001 - Row
    """Compare the head triple, its signature and its count against the log."""
    newest_id = rows[-1].id if rows else None
    expected_prev = rows[-1].entry_hash if rows else GENESIS_HASH
    if head is None:
        if not rows:
            return None
        return VerifyDivergence(
            entry_id=newest_id or 0,
            reason="audit chain head row is missing",
            expected_hash=expected_prev,
            actual_hash="",
        )
    seq, head_hash, head_entry_id = int(head[0]), str(head[1]), head[2]
    mac, adopted = head[3], head[4]
    if mac is None:
        if adopted is not None:
            return VerifyDivergence(
                entry_id=newest_id or 0,
                reason=_CLEARED_SIGNATURE_MESSAGE,
                expected_hash=_head_mac(seq, head_hash, head_entry_id),
                actual_hash="",
            )
        if rows or seq or head_hash != GENESIS_HASH or head_entry_id is not None:
            # An unsigned head over a non-empty log cannot prove the log was
            # not truncated, so it is a divergence rather than a caveat.
            return VerifyDivergence(
                entry_id=newest_id or 0,
                reason=_UNSIGNED_HEAD_MESSAGE,
                expected_hash=_head_mac(seq, head_hash, head_entry_id),
                actual_hash="",
            )
    elif not hmac.compare_digest(mac, _head_mac(seq, head_hash, head_entry_id)):
        return VerifyDivergence(
            entry_id=newest_id or 0,
            reason="chain head signature does not match its contents",
            expected_hash=_head_mac(seq, head_hash, head_entry_id),
            actual_hash=str(mac),
        )
    if head_hash != expected_prev or head_entry_id != newest_id:
        return VerifyDivergence(
            entry_id=newest_id or 0,
            reason="chain head does not match the newest stored entry",
            expected_hash=expected_prev,
            actual_hash=head_hash,
        )
    if seq != len(rows):
        return VerifyDivergence(
            entry_id=newest_id or 0,
            reason=f"chain head counted {seq} appended entries but {len(rows)} are stored",
            expected_hash=str(seq),
            actual_hash=str(len(rows)),
        )
    return None


def verify_chain() -> VerifyDivergence | None:
    """Recompute the chain from genesis and report the first divergence.

    Returns ``None`` only when every entry hashes correctly, every
    ``prev_hash`` points at its predecessor, the head row is signed, **and**
    the head agrees with the stored log on both the newest entry and the
    number of entries ever appended. The head comparison is what makes
    truncation and out-of-band deletion visible: rows that were removed leave
    a chain that is internally consistent but shorter than the head says, and
    the signature is what stops the head from simply being moved to match.

    Rows and head come from one snapshot (:func:`_read_snapshot`), so an append
    that commits while this runs is either wholly visible or wholly invisible.
    A concurrent appender can therefore never make an intact chain look broken,
    and verification never blocks the appender to achieve that.
    """
    rows, head = _read_chain()
    divergence = _scan_entries(rows)
    if divergence is not None:
        return divergence
    return _check_head(rows, head)


def adoption_required() -> bool:
    """True when this store is a genuine pre-signature chain awaiting adoption."""
    with _read_snapshot() as session:
        head = _read_head(session)
        stored = int(session.execute(select(func.count(AuditLogEntry.id))).scalar_one())
    if head is None:
        return stored > 0
    return head[3] is None and head[4] is None and stored > 0


def adopt_legacy_chain(*, actor: str = "operator") -> dict[str, Any]:
    """Sign a genuine pre-signature chain, once, after verifying it.

    This is the only path from an unsigned head to a signed one on a non-empty
    log, and it is deliberately an explicit operator gesture (``brains-ai
    audit-adopt``) rather than something an append does on the quiet.

    It refuses unless every one of these holds, so it can never launder a
    truncated or mutated log into a signed chain:

    * the store has no adoption marker (an adopted store with a missing
      signature is tamper, and says so);
    * the log carries no earlier adoption entry (which is what stops a second
      "adoption" after the marker was stripped);
    * every entry hashes, every ``prev_hash`` links, and the head triple and
      append count already agree with the stored log.

    The signature, the marker and the ``audit.chain.adopted`` entry commit in
    one transaction: an adoption that cannot be recorded does not happen.
    """
    from brains.storage.migrations import init_db

    init_db()
    last_error: BaseException | None = None
    for attempt in range(_APPEND_ATTEMPTS):
        session = SessionLocal()
        try:
            outcome = _adopt_in_session(session, actor=actor)
            session.commit()
            return outcome
        except AuditChainCorruptError:
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            last_error = exc
            cause = exc.__cause__ if isinstance(exc, AuditWriteError) else exc
            if not _is_retryable(cause) or attempt == _APPEND_ATTEMPTS - 1:
                break
            time.sleep(_APPEND_BACKOFF_SECONDS * (2**attempt) * (0.5 + random.random()))  # noqa: S311
        finally:
            session.close()
    raise AuditWriteError(f"audit chain adoption failed: {last_error}")


def _adopt_in_session(session, *, actor: str) -> dict[str, Any]:  # noqa: ANN001 - Session
    # The head row is the only independent record of how long the log used to
    # be, so a store that has lost it has nothing to verify a legacy chain
    # against: re-seeding one from the surviving rows would make the triple and
    # count checks below compare the log with itself.
    head_exists = session.execute(
        select(AuditChainHead.id).where(AuditChainHead.id == CHAIN_HEAD_ID)
    ).first()
    stored = int(session.execute(select(func.count(AuditLogEntry.id))).scalar_one())
    if head_exists is None and stored:
        raise AuditChainCorruptError(
            "there is no audit chain head to verify this log against, so its length cannot "
            "be checked; refusing to adopt - run `brains-ai audit-verify`"
        )
    head = _lock_head(session)
    if head is None:
        raise AuditChainCorruptError("audit chain head row is missing and could not be seeded")
    seq, head_hash, head_entry_id = int(head[0]), str(head[1]), head[2]
    mac, adopted = head[3], head[4]
    if mac is not None:
        if not hmac.compare_digest(mac, _head_mac(seq, head_hash, head_entry_id)):
            raise AuditChainCorruptError(
                "audit chain head signature does not match its contents; adoption cannot "
                "re-sign a head that was moved out of band"
            )
        if adopted is None:
            _mark_adopted(session)
        return {
            "status": "already_adopted",
            "adopted_version": CHAIN_SIGNATURE_VERSION,
            "entries": seq,
            "head_hash": head_hash,
        }
    if adopted is not None:
        raise AuditChainCorruptError(_CLEARED_SIGNATURE_MESSAGE)

    rows = session.query(AuditLogEntry).order_by(AuditLogEntry.id.asc()).all()
    previously_signed = any(row.action in SIGNED_ORIGIN_ACTIONS for row in rows)
    if previously_signed:
        raise AuditChainCorruptError(
            "this store's log already records a signed origin (initialised or adopted), so "
            "its unsigned head is not legacy state; refusing to re-sign - run "
            "`brains-ai audit-verify`"
        )
    divergence = _scan_entries(rows)
    if divergence is not None:
        raise AuditChainCorruptError(
            f"refusing to adopt a chain that does not verify: entry {divergence.entry_id}: "
            f"{divergence.reason}"
        )
    # The head triple and the append count are checked here explicitly rather
    # than through _check_head, whose unsigned-head divergence would otherwise
    # short-circuit both: signing a head that disagrees with its log would
    # produce a store that is adopted *and* permanently unverifiable.
    newest_id = rows[-1].id if rows else None
    newest_hash = rows[-1].entry_hash if rows else GENESIS_HASH
    if head_hash != newest_hash or head_entry_id != newest_id:
        raise AuditChainCorruptError(
            f"refusing to adopt: the chain head points at entry {head_entry_id!r} but the "
            f"newest stored entry is {newest_id!r} - run `brains-ai audit-verify`"
        )
    if seq != len(rows):
        raise AuditChainCorruptError(
            f"refusing to adopt: the chain head counted {seq} appended entries but "
            f"{len(rows)} are stored - run `brains-ai audit-verify`"
        )

    _sign_head(session, seq, head_hash, head_entry_id)
    session.flush()
    entry = append_in_session(
        session,
        actor=actor or "operator",
        action=ADOPTION_ACTION,
        payload={
            "adopted_version": CHAIN_SIGNATURE_VERSION,
            "entries_at_adoption": len(rows),
            "head_entry_id": head_entry_id,
            "head_hash": head_hash,
            "fingerprint": audit_key_fingerprint(),
        },
    )
    return {
        "status": "adopted",
        "adopted_version": CHAIN_SIGNATURE_VERSION,
        "entries": len(rows),
        "head_hash": head_hash,
        "adoption_entry_id": int(entry.id),
    }


def assert_chain_intact() -> None:
    """Raise :class:`AuditChainCorruptError` when :func:`verify_chain` diverges."""
    divergence = verify_chain()
    if divergence is not None:
        raise AuditChainCorruptError(
            f"audit chain diverges at entry {divergence.entry_id}: {divergence.reason}"
        )


def chain_status() -> dict[str, Any]:
    """Report the chain for operators; ``ok`` is false on any divergence.

    The verdict and the counts it is reported next to come from the same
    snapshot, so a report cannot pair a divergence with the numbers from a
    later instant (or the reverse: call an intact chain broken because an
    append landed between the verdict and the count).
    """
    rows, head = _read_chain()
    divergence = _scan_entries(rows) or _check_head(rows, head)
    stored = len(rows)
    signed = bool(head is not None and head[3] is not None)
    status: dict[str, Any] = {
        "ok": divergence is None,
        "fingerprint": audit_key_fingerprint(),
        "stored_entries": stored,
        "appended_entries": int(head[0]) if head is not None else None,
        "head_hash": str(head[1]) if head is not None else None,
        "head_entry_id": head[2] if head is not None else None,
        # An unsigned head over a non-empty log cannot prove the log was not
        # truncated, so it is a divergence, not a caveat. These two fields say
        # whether the store is signed and whether a genuine pre-signature
        # chain is waiting for `brains-ai audit-adopt`.
        "head_signed": signed,
        "adopted_version": (head[4] if head is not None else None),
        "adoption_required": bool(
            head is not None and head[3] is None and head[4] is None and stored > 0
        ),
    }
    if divergence is not None:
        status.update(
            {
                "entry_id": divergence.entry_id,
                "reason": divergence.reason,
                "expected_hash": divergence.expected_hash,
                "actual_hash": divergence.actual_hash,
            }
        )
    return status


__all__ = [
    "ADOPTION_ACTION",
    "ATTEMPT_SUFFIX",
    "CHAIN_HEAD_ID",
    "CHAIN_SIGNATURE_VERSION",
    "FAILURE_SUFFIX",
    "GENESIS_HASH",
    "INIT_ACTION",
    "SIGNED_ORIGIN_ACTIONS",
    "AuditChainCorruptError",
    "AuditWriteError",
    "EffectRecord",
    "VerifyDivergence",
    "adopt_legacy_chain",
    "adoption_required",
    "append_in_session",
    "assert_chain_intact",
    "audit_key_fingerprint",
    "chain_status",
    "list_entries",
    "record",
    "record_required",
    "required_effect",
    "verify_chain",
]

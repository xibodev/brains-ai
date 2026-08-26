"""The credential store: every accepted secret resolves to one principal.

Authentication used to be *membership in a set*: the gateway collected the
admin key, the rotation keys and every per-operator key file, and accepted any
of them. Nothing downstream could say which one had been presented, so no
authorization decision could be attached to it.

This module replaces that with a store. A raw secret is looked up by its
sha256 hash in ``api_credentials``; the row names the credential's kind, its
operator, its Org and, for a Runtime credential, the machine it was minted
for. The raw secret is never persisted and never logged.

Back-compat is preserved by *adoption*, not by a fallback: the keys an install
already has on disk (``settings.api_key``, ``settings.api_keys`` and
``~/.brains/operator-keys/*.key``) are adopted into the store the first time
they are seen, so an existing install keeps working and still resolves to an
explicit principal. A secret that is not in the store and not on disk does not
authenticate.

Adoption records *where* a credential came from (``source``), and adoption is
**all** :func:`sync_local_credentials` does. Revocation is a separate, explicit
act: :func:`revoke_local_secret` names the one superseded secret - the admin key
that was just rotated out, the operator key file that was just deleted - and
revokes exactly that hash.

The split is the security property. A process's view of the on-disk sources is
neither authoritative nor complete: a worker started with a different
``BRAINS_API_KEY``, a container without ``~/.brains/operator-keys`` mounted, an
``EACCES`` on the key directory and an unauthenticated request carrying a bad
token all produce a *narrower* view of the same install. Deriving revocation
from that view lets any of them revoke, install-wide and persistently, a
credential it simply could not see. So passive reconciliation only ever adds,
and the only way a credential stops being accepted is an explicit rotation, an
explicit key-file deletion or an explicit :func:`revoke_credential`.

A filesystem error is a diagnosis, not a conclusion: reading the sources raises
:class:`LocalSourceError` rather than reporting an empty install, adoption is
skipped for that call, and :func:`diagnose` reports it.

Re-enabling works the same way round. A revoked credential is never reinstated
by a passive resync - restoring a deleted key file does not undo an operator's
revocation - and comes back only through an explicit rotation or registration.

Two caches keep an unauthenticated caller from turning a bad token into
filesystem work: the on-disk sources are re-checked by ``stat`` at most once
every :data:`SOURCE_CACHE_TTL_SECONDS` and re-read only when they change, and a
bounded, short-lived negative cache remembers digests that resolved to nothing.
Both are invalidated explicitly when a credential is minted, revoked, or
reconciled, so a rotation takes effect on the next request rather than at the
end of a TTL.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import ApiCredential, Operator

log = logging.getLogger(__name__)

KIND_ADMIN = "admin"
KIND_OPERATOR = "operator"
KIND_RUNTIME = "runtime"

VALID_KINDS = frozenset({KIND_ADMIN, KIND_OPERATOR, KIND_RUNTIME})

# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #

#: ``settings.api_key`` - the bootstrap admin key on disk / in the environment.
SOURCE_ADMIN_KEY = "local:admin_key"
#: ``settings.api_keys`` - the admin rotation list.
SOURCE_ADMIN_KEYS = "local:api_keys"
#: ``~/.brains/operator-keys/<slug>.key``.
SOURCE_OPERATOR_KEY = "local:operator_key"
#: Minted by enrollment redemption. Never reconciled against local files.
SOURCE_ENROLMENT = "enrolment"
#: Registered explicitly by an operator or by a caller that named no source.
SOURCE_MANUAL = "manual"

#: The sources a local adoption may claim. A credential adopted from one of
#: these is *superseded* - and revoked - only by the explicit path that
#: supersedes it: :func:`revoke_local_secret`, called by an admin-key rotation
#: or an operator-key deletion, which names the exact hash it is retiring.
#: Nothing is ever revoked because it is *missing* from a process's view of
#: disk, so a narrow or unreadable view cannot deny a credential it cannot see.
LOCAL_SOURCES: frozenset[str] = frozenset(
    {SOURCE_ADMIN_KEY, SOURCE_ADMIN_KEYS, SOURCE_OPERATOR_KEY}
)

#: Default lifetime of a Runtime credential minted by enrollment redemption.
#: Long enough that a daemon does not churn, short enough that a leaked
#: credential expires without an operator noticing. Renewal is re-enrollment.
RUNTIME_CREDENTIAL_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days

#: How long a *stat-only* view of the on-disk key sources is trusted before it
#: is re-checked. Bounds the filesystem work an unauthenticated caller can
#: force: without it, every bad token re-listed ``~/.brains/operator-keys`` and
#: re-read every file in it.
SOURCE_CACHE_TTL_SECONDS = 5.0

#: How long a secret that resolved to nothing is remembered as a miss, and how
#: many such digests are kept. Bounded so a flood of distinct bad tokens cannot
#: grow the process, and short so a newly minted credential starts working
#: promptly; minting and revocation invalidate it explicitly anyway.
NEGATIVE_CACHE_TTL_SECONDS = 10.0
NEGATIVE_CACHE_MAX_ENTRIES = 4096

_sync_lock = threading.Lock()
#: Source fingerprint each store was last reconciled against. Test fixtures
#: (and a re-pointed ``BRAINS_DB_URL``) rebind the engine to a different store,
#: which has none of those rows: a single flat marker would then make an
#: adopted key permanently unresolvable there.
_synced_fingerprints: dict[str, object] = {}

_source_lock = threading.Lock()
#: ``(fingerprint, entries, expires_at)`` for the on-disk key sources. The raw
#: secrets held here are the same values ``settings`` and the key files already
#: hold in this process; nothing is persisted and no new secret is derived.
_source_cache: tuple[object, tuple[tuple[str, str, str], ...], float] | None = None

_negative_lock = threading.Lock()
#: digest -> expiry (monotonic). Digests only - never a raw secret.
_negative_cache: OrderedDict[str, float] = OrderedDict()

_source_error_lock = threading.Lock()
#: The last failure to read the on-disk key sources, for :func:`diagnose`.
#: A read error means "this process cannot see the sources", which is a
#: diagnosis to surface, never a conclusion that the sources are empty.
_last_source_error: str | None = None


def _store_key() -> str:
    """Identity of the store credentials are being adopted into."""
    try:
        return str(_db_module.SessionLocal.kw["bind"].url)
    except Exception:
        return "default"


class CredentialRecord(TypedDict):
    id: int
    credential_id: str
    kind: str
    source: str | None
    operator_id: int | None
    operator_slug: str | None
    org_id: int | None
    runtime_id: int | None
    machine_id: str | None
    label: str | None
    created_at: str | None
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None


class CredentialError(ValueError):
    """Raised when a credential cannot be minted or revoked."""


class LocalSourceError(RuntimeError):
    """Raised when the on-disk key sources cannot be read.

    Propagated rather than swallowed: an unreadable ``~/.brains/operator-keys``
    is an install fault to diagnose, and reporting it as "no keys on disk"
    would be a *narrower* view of the install than the truth - exactly the kind
    of view no revocation is allowed to be derived from.
    """


def hash_secret(raw: str) -> str:
    """sha256 hex of a raw secret. This is what the store persists."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fingerprint(raw: str) -> str:
    """Truncated fingerprint, matching ``operators.key_fingerprint``."""
    return hash_secret(raw)[:16]


def _new_credential_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # SQLite hands back tz-naive datetimes; values are stored UTC.
        return value.replace(tzinfo=UTC)
    return value


def _iso(value: datetime | None) -> str | None:
    value = _as_utc(value)
    return value.isoformat() if value is not None else None


def _to_record(row: ApiCredential, operator_slug: str | None) -> CredentialRecord:
    return {
        "id": row.id,
        "credential_id": row.credential_id,
        "kind": row.kind,
        "source": row.source,
        "operator_id": row.operator_id,
        "operator_slug": operator_slug,
        "org_id": row.org_id,
        "runtime_id": row.runtime_id,
        "machine_id": row.machine_id,
        "label": row.label,
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "revoked_at": _iso(row.revoked_at),
        "last_used_at": _iso(row.last_used_at),
    }


def is_active(row: ApiCredential, *, now: datetime | None = None) -> bool:
    """True when the credential is neither revoked nor expired."""
    moment = now or _utc_now()
    if row.revoked_at is not None:
        return False
    expires_at = _as_utc(row.expires_at)
    return not (expires_at is not None and moment > expires_at)


# --------------------------------------------------------------------------- #
# Minting
# --------------------------------------------------------------------------- #


def register_credential(
    raw_secret: str,
    *,
    kind: str,
    operator_id: int | None = None,
    org_id: int | None = None,
    runtime_id: int | None = None,
    machine_id: str | None = None,
    label: str | None = None,
    source: str = SOURCE_MANUAL,
    created_by_operator_id: int | None = None,
    expires_at: datetime | None = None,
    reinstate: bool = False,
    session=None,
) -> CredentialRecord:
    """Adopt ``raw_secret`` into the store, or return the row it already has.

    Idempotent on the secret hash: re-registering the same secret updates the
    binding fields rather than creating a second row, so an install whose
    admin key is also listed in ``BRAINS_API_KEYS`` still has exactly one
    principal for it.

    A previously revoked row for the same secret is *not* silently reinstated
    by a local adoption. Only an explicit act clears ``revoked_at``: a
    registration whose ``source`` is outside :data:`LOCAL_SOURCES`, or one that
    passes ``reinstate=True`` (what a rotation does when it re-registers the
    key it just wrote). Restoring a deleted operator key file therefore does
    not undo the operator's revocation - re-issuing the key does.

    ``session`` lets a caller enlist this write in a transaction it already
    holds, so the credential and whatever authorized it commit or roll back
    together. The row is flushed rather than committed in that case: the
    caller owns the commit, and a caller that rolls back leaves no credential
    behind.
    """
    if kind not in VALID_KINDS:
        raise CredentialError(f"credential kind must be one of {sorted(VALID_KINDS)}")
    if not raw_secret:
        raise CredentialError("refusing to register an empty credential")
    digest = hash_secret(raw_secret)
    init_db()
    if session is not None:
        record = _register_credential_in(
            session,
            digest,
            kind=kind,
            operator_id=operator_id,
            org_id=org_id,
            runtime_id=runtime_id,
            machine_id=machine_id,
            label=label,
            source=source,
            created_by_operator_id=created_by_operator_id,
            expires_at=expires_at,
            reinstate=reinstate,
            commit=False,
        )
        _forget_miss(digest)
        return record
    with _db_module.SessionLocal() as owned:
        record = _register_credential_in(
            owned,
            digest,
            kind=kind,
            operator_id=operator_id,
            org_id=org_id,
            runtime_id=runtime_id,
            machine_id=machine_id,
            label=label,
            source=source,
            created_by_operator_id=created_by_operator_id,
            expires_at=expires_at,
            reinstate=reinstate,
            commit=True,
        )
    _forget_miss(digest)
    return record


def _register_credential_in(
    session,
    digest: str,
    *,
    kind: str,
    operator_id: int | None,
    org_id: int | None,
    runtime_id: int | None,
    machine_id: str | None,
    label: str | None,
    source: str,
    created_by_operator_id: int | None,
    expires_at: datetime | None,
    reinstate: bool,
    commit: bool,
) -> CredentialRecord:
    """Upsert one credential row on ``session``. See :func:`register_credential`."""
    row = session.query(ApiCredential).filter(ApiCredential.secret_hash == digest).one_or_none()
    if row is None:
        row = ApiCredential(
            credential_id=_new_credential_id(kind[:3]),
            kind=kind,
            secret_hash=digest,
            fingerprint=digest[:16],
            operator_id=operator_id,
            org_id=org_id,
            runtime_id=runtime_id,
            machine_id=machine_id,
            label=label,
            source=source,
            created_by_operator_id=created_by_operator_id,
            created_at=_utc_now(),
            expires_at=expires_at,
        )
        session.add(row)
    else:
        row.kind = kind
        row.operator_id = operator_id if operator_id is not None else row.operator_id
        row.org_id = org_id if org_id is not None else row.org_id
        row.runtime_id = runtime_id if runtime_id is not None else row.runtime_id
        row.machine_id = machine_id if machine_id is not None else row.machine_id
        row.label = label or row.label
        row.source = source or row.source
        if reinstate or source not in LOCAL_SOURCES:
            row.revoked_at = None
        if expires_at is not None:
            row.expires_at = expires_at
    if commit:
        session.commit()
        session.refresh(row)
    else:
        session.flush()
    slug = _operator_slug(session, row.operator_id)
    return _to_record(row, slug)


def mint_runtime_credential(
    *,
    org_id: int | None,
    machine_id: str,
    runtime_id: int | None = None,
    label: str | None = None,
    created_by_operator_id: int | None = None,
    ttl_seconds: int = RUNTIME_CREDENTIAL_TTL_SECONDS,
    session=None,
) -> tuple[CredentialRecord, str]:
    """Mint a Runtime-narrow, Org-bound credential. Returns ``(record, raw)``.

    The raw secret is returned exactly once, to the machine that redeemed the
    enrollment token. Only its hash is persisted, so the hub cannot leak it
    back out later - not through the API, not through the console, not through
    a backup of the store.

    ``ttl_seconds`` of ``0`` mints a credential with no expiry; a negative
    value mints one that is already expired (used by tests and by an operator
    that wants a pre-revoked row).

    ``session`` enlists the mint in the caller's transaction, so the credential
    and the machine-Org claim that authorized it are one atomic write.
    """
    if not machine_id:
        raise CredentialError("a Runtime credential must be bound to a machine")
    raw = secrets.token_urlsafe(32)
    expires_at = _utc_now() + timedelta(seconds=ttl_seconds) if ttl_seconds else None
    record = register_credential(
        raw,
        kind=KIND_RUNTIME,
        org_id=org_id,
        runtime_id=runtime_id,
        machine_id=machine_id,
        label=label or f"runtime {machine_id}",
        source=SOURCE_ENROLMENT,
        created_by_operator_id=created_by_operator_id,
        expires_at=expires_at,
        session=session,
    )
    return record, raw


# --------------------------------------------------------------------------- #
# Adoption of on-disk keys
# --------------------------------------------------------------------------- #


def _operator_slug(session, operator_id: int | None) -> str | None:
    if operator_id is None:
        return None
    row = session.get(Operator, operator_id)
    return row.slug if row is not None else None


def _admin_operator_id() -> int | None:
    try:
        from brains.control.operators import ensure_admin_operator

        return ensure_admin_operator()["id"]
    except Exception:
        return None


def _operator_id_for_fingerprint(value: str) -> int | None:
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(Operator).filter(Operator.key_fingerprint == value).one_or_none()
        return row.id if row is not None else None


# --------------------------------------------------------------------------- #
# On-disk key sources: stat-cached, read only when they change
# --------------------------------------------------------------------------- #


def _operator_key_files() -> list:
    from brains.control.operators import operator_keys_dir

    directory = operator_keys_dir()
    try:
        if not directory.exists():
            return []
        return sorted(path for path in directory.iterdir() if path.suffix == ".key")
    except OSError as exc:
        raise LocalSourceError(
            f"cannot list the operator key directory {directory}: {exc}"
        ) from exc


def _source_fingerprint() -> object:
    """A cheap identity of the on-disk sources: settings plus file stats.

    Deliberately ``stat``-only. Re-reading every operator key file on every
    unauthenticated request is the filesystem scan this cache exists to avoid;
    the fingerprint changes whenever a file is written, added or removed, which
    is the only time the contents matter.
    """
    from brains.config import settings

    parts: list[object] = [
        hash_secret(settings.api_key) if settings.api_key else "",
        tuple(hash_secret(key) for key in settings.api_keys if key),
    ]
    files: list[tuple[str, int, int]] = []
    for path in _operator_key_files():
        try:
            stat = path.stat()
        except FileNotFoundError:
            # Raced with a deletion. The next call re-fingerprints.
            continue
        except OSError as exc:
            raise LocalSourceError(f"cannot stat the operator key file {path}: {exc}") from exc
        files.append((path.name, stat.st_mtime_ns, stat.st_size))
    parts.append(tuple(files))
    return tuple(parts)


def _read_local_key_sources() -> tuple[tuple[str, str, str], ...]:
    """``(raw_secret, kind, source)`` for every key this install holds on disk.

    Raises :class:`LocalSourceError` when a source cannot be read, so a partial
    view is never mistaken for the whole install.
    """
    from brains.config import settings

    entries: list[tuple[str, str, str]] = []
    if settings.api_key:
        entries.append((settings.api_key, KIND_ADMIN, SOURCE_ADMIN_KEY))
    entries.extend((key, KIND_ADMIN, SOURCE_ADMIN_KEYS) for key in settings.api_keys if key)
    for path in _operator_key_files():
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LocalSourceError(f"cannot read the operator key file {path}: {exc}") from exc
        if value:
            entries.append((value, KIND_OPERATOR, SOURCE_OPERATOR_KEY))
    return tuple(entries)


def local_key_sources(*, force: bool = False) -> tuple[tuple[str, str, str], ...]:
    """The on-disk key sources, cached by fingerprint with a short TTL.

    Within :data:`SOURCE_CACHE_TTL_SECONDS` the cached tuple is returned with
    no filesystem access at all; after that the fingerprint is re-``stat``ed
    and the files are re-read only when it changed.

    Raises :class:`LocalSourceError` when the sources cannot be read; nothing
    is cached in that case, so the next call retries rather than serving an
    incomplete view for a TTL.
    """
    global _source_cache

    now = time.monotonic()
    with _source_lock:
        cached = _source_cache
        if not force and cached is not None and now < cached[2]:
            return cached[1]
        fingerprint = _source_fingerprint()
        if not force and cached is not None and cached[0] == fingerprint:
            _source_cache = (fingerprint, cached[1], now + SOURCE_CACHE_TTL_SECONDS)
            return cached[1]
        entries = _read_local_key_sources()
        _source_cache = (fingerprint, entries, now + SOURCE_CACHE_TTL_SECONDS)
        return entries


def invalidate_source_cache() -> None:
    """Forget the cached view of the on-disk key sources.

    Called by anything that rewrites a key file (admin-key rotation, operator
    minting) so the change is visible on the very next request rather than at
    the end of the TTL.
    """
    global _source_cache

    with _source_lock:
        _source_cache = None
    with _sync_lock:
        _synced_fingerprints.clear()
    _forget_all_misses()


# --------------------------------------------------------------------------- #
# Negative cache: a bad token must not cost a disk scan
# --------------------------------------------------------------------------- #


def _negative_cached(digest: str) -> bool:
    now = time.monotonic()
    with _negative_lock:
        expiry = _negative_cache.get(digest)
        if expiry is None:
            return False
        if expiry <= now:
            _negative_cache.pop(digest, None)
            return False
        return True


def _remember_miss(digest: str) -> None:
    now = time.monotonic()
    with _negative_lock:
        _negative_cache.pop(digest, None)
        _negative_cache[digest] = now + NEGATIVE_CACHE_TTL_SECONDS
        while len(_negative_cache) > NEGATIVE_CACHE_MAX_ENTRIES:
            _negative_cache.popitem(last=False)


def _forget_miss(digest: str) -> None:
    with _negative_lock:
        _negative_cache.pop(digest, None)


def _forget_all_misses() -> None:
    with _negative_lock:
        _negative_cache.clear()


def sync_local_credentials() -> int:
    """Adopt the raw keys this install holds on disk. **Never revokes.**

    Covers the three legacy sources, each mapped to an explicit principal:

    * ``settings.api_key`` - the bootstrap admin key, bound to ``admin``.
    * ``settings.api_keys`` - admin rotation keys, also bound to ``admin``
      (they are alternative spellings of the same bootstrap credential; the
      rotation list has never been per-operator).
    * ``~/.brains/operator-keys/<slug>.key`` - bound to the operator whose
      fingerprint matches, and to *no* operator when the fingerprint matches
      none, which leaves the credential with zero Org roles rather than a
      guessed identity.

    Reconciliation runs in one direction only. What this call sees is one
    process's view of one host at one instant - a view that a different
    environment, an unmounted state directory, a permission error or simply a
    second process with a different ``BRAINS_API_KEY`` all narrow. Adding a
    credential from a narrow view is harmless; *removing* one is not, because
    the removal is written to the shared store and denies the credential
    install-wide. Superseding a credential is therefore an explicit act -
    :func:`revoke_local_secret`, driven by a rotation or a key-file deletion,
    naming the exact hash it retires - and never an inference from absence.

    A source that cannot be read raises :class:`LocalSourceError` internally;
    this call records it for :func:`diagnose`, adopts nothing, and leaves the
    store exactly as it was.

    Returns the number of credentials adopted in this call.
    """
    changed = 0
    store = _store_key()
    with _sync_lock:
        try:
            fingerprint = _source_fingerprint()
            entries = local_key_sources()
        except LocalSourceError as exc:
            _record_source_error(str(exc))
            log.warning("cannot read the local key sources; adopting nothing: %s", exc)
            return 0
        _record_source_error(None)
        if _synced_fingerprints.get(store) == fingerprint:
            return 0

        admin_id: int | None = None
        for raw, kind, source in entries:
            digest = hash_secret(raw)
            operator_id: int | None
            if kind == KIND_ADMIN:
                if admin_id is None:
                    admin_id = _admin_operator_id()
                operator_id = admin_id
            else:
                operator_id = _operator_id_for_fingerprint(digest[:16])
            try:
                if _adopt_if_needed(
                    raw,
                    digest,
                    kind=kind,
                    source=source,
                    operator_id=operator_id,
                ):
                    changed += 1
            except Exception:
                log.debug("failed to adopt a local key into the credential store", exc_info=True)
                continue

        _synced_fingerprints[store] = fingerprint
    if changed:
        _forget_all_misses()
    return changed


def _record_source_error(message: str | None) -> None:
    global _last_source_error

    with _source_error_lock:
        _last_source_error = message


def last_source_error() -> str | None:
    """The last failure to read the on-disk key sources, or ``None``."""
    with _source_error_lock:
        return _last_source_error


def _adopt_if_needed(
    raw: str,
    digest: str,
    *,
    kind: str,
    source: str,
    operator_id: int | None,
) -> bool:
    """Register ``raw`` when the store has no row for it at all. Returns changed.

    A row that exists is left alone, revoked or not: adoption is how a key
    *starts* resolving, never how a revoked one starts resolving again.
    """
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(ApiCredential).filter(ApiCredential.secret_hash == digest).one_or_none()
        if row is not None:
            # Backfill provenance for a row adopted before it was recorded, so
            # an explicit supersede can own it from now on. A row that already
            # names a non-local source keeps it.
            if row.source is None:
                row.source = source
                session.commit()
            return False
    register_credential(
        raw,
        kind=kind,
        operator_id=operator_id,
        label="adopted local key",
        source=source,
    )
    return True


def revoke_local_secret(raw_secret: str) -> CredentialRecord | None:
    """Revoke the exact credential ``raw_secret`` hashes to. Explicit paths only.

    The one revocation path reconciliation offers, and it takes the superseded
    secret itself rather than a view of what is left on disk: an admin-key
    rotation passes the key it just overwrote, an operator-key deletion passes
    the file's contents read immediately before the unlink. A process that
    cannot see a key therefore cannot revoke it - it has nothing to pass.

    Returns the revoked record, or ``None`` when the secret names no locally
    adopted credential. A Runtime credential is never revoked here: it is not
    sourced from disk, and taking a live daemon down is not what a key rotation
    was asked to do.
    """
    if not raw_secret:
        return None
    digest = hash_secret(raw_secret)
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(ApiCredential).filter(ApiCredential.secret_hash == digest).one_or_none()
        if row is None or row.kind == KIND_RUNTIME:
            return None
        if row.source is not None and row.source not in LOCAL_SOURCES:
            # Minted by enrollment or registered explicitly; a local rotation
            # does not own it.
            return None
        if row.revoked_at is None:
            row.revoked_at = _utc_now()
            session.commit()
            session.refresh(row)
        record = _to_record(row, _operator_slug(session, row.operator_id))
    _forget_all_misses()
    _remember_miss(digest)
    return record


def reset_adoption_cache() -> None:
    """Test-only helper: forget every cached view of the local key sources."""
    invalidate_source_cache()


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def resolve_secret(raw_secret: str | None, *, touch: bool = True) -> CredentialRecord | None:
    """Return the active credential for ``raw_secret``, or ``None``.

    A revoked or expired row resolves to ``None`` - the credential is known but
    no longer accepted, which is the same answer the caller needs and never
    reveals that the secret was once valid.

    A secret that resolved to nothing recently is answered from the bounded
    negative cache, *before* any database read and before the on-disk sources
    are consulted, so a flood of invented tokens costs neither a filesystem
    scan nor an unbounded amount of memory.
    """
    if not raw_secret:
        return None
    digest = hash_secret(raw_secret)
    if _negative_cached(digest):
        return None
    record = _lookup(digest, touch=False)
    if record is not None:
        return record
    # The secret may be one this process has not adopted yet (first request
    # after a key rotation, or a fresh operator key file). ``sync`` is itself
    # fingerprint-gated, so this is a no-op unless the sources actually moved.
    # Always look again after synchronization. Another request may have won the
    # sync lock and adopted this key while we waited; that request reports the
    # change, while this one sees a no-op sync but must still observe the row.
    sync_local_credentials()
    record = _lookup(digest, touch=False)
    if record is not None:
        return record
    _remember_miss(digest)
    return None


def _lookup(digest: str, *, touch: bool) -> CredentialRecord | None:
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(ApiCredential).filter(ApiCredential.secret_hash == digest).one_or_none()
        if row is None or not is_active(row):
            return None
        slug = _operator_slug(session, row.operator_id)
        # Authentication is a read boundary. Synchronously updating
        # last_used_at on every HTTP/SSE handshake made a valid credential
        # acquire SQLite's single writer lock and could turn read traffic into
        # a machine-wide outage. Keep the argument for API compatibility; usage
        # telemetry must be sampled/batched outside credential resolution.
        return _to_record(row, slug)


def get_credential(credential_id: str) -> CredentialRecord | None:
    init_db()
    with _db_module.SessionLocal() as session:
        row = (
            session.query(ApiCredential)
            .filter(ApiCredential.credential_id == credential_id)
            .one_or_none()
        )
        if row is None:
            return None
        return _to_record(row, _operator_slug(session, row.operator_id))


def list_credentials(
    *,
    kind: str | None = None,
    org_id: int | None = None,
    machine_id: str | None = None,
    include_revoked: bool = False,
) -> list[CredentialRecord]:
    init_db()
    with _db_module.SessionLocal() as session:
        query = session.query(ApiCredential)
        if kind is not None:
            query = query.filter(ApiCredential.kind == kind)
        if org_id is not None:
            query = query.filter(ApiCredential.org_id == org_id)
        if machine_id is not None:
            query = query.filter(ApiCredential.machine_id == machine_id)
        if not include_revoked:
            query = query.filter(ApiCredential.revoked_at.is_(None))
        rows = query.order_by(ApiCredential.id.asc()).all()
        return [_to_record(row, _operator_slug(session, row.operator_id)) for row in rows]


def revoke_credential(credential_id: str) -> CredentialRecord:
    """Revoke one credential by its public handle. Idempotent."""
    init_db()
    with _db_module.SessionLocal() as session:
        row = (
            session.query(ApiCredential)
            .filter(ApiCredential.credential_id == credential_id)
            .one_or_none()
        )
        if row is None:
            raise CredentialError(f"unknown credential: {credential_id!r}")
        if row.revoked_at is None:
            row.revoked_at = _utc_now()
            session.commit()
            session.refresh(row)
        record = _to_record(row, _operator_slug(session, row.operator_id))
    _forget_all_misses()
    return record


def revoke_machine_credentials(machine_id: str) -> int:
    """Revoke every Runtime credential bound to ``machine_id``."""
    init_db()
    now = _utc_now()
    with _db_module.SessionLocal() as session:
        rows = (
            session.query(ApiCredential)
            .filter(
                ApiCredential.kind == KIND_RUNTIME,
                ApiCredential.machine_id == machine_id,
                ApiCredential.revoked_at.is_(None),
            )
            .all()
        )
        for row in rows:
            row.revoked_at = now
        session.commit()
    _forget_all_misses()
    return len(rows)


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def diagnose() -> dict:
    """Report credentials whose principal cannot be resolved unambiguously.

    Two shapes matter for an upgrade:

    ``unbound_credentials``
        Rows in the store with no operator and no Runtime binding. They
        authenticate but hold no Org role, so every scoped route denies them.
        They are reported rather than deleted, because deleting a credential
        an operator still uses is worse than refusing it.
    ``legacy_daemon_operators``
        ``operators`` rows minted by pre-BL-P0-01 enrollment (``daemon-*``).
        Nothing in the store says which Runtime they belonged to, so they are
        never promoted to Runtime credentials; re-enrolling the machine mints
        a proper Runtime-narrow credential and the stale operator can then be
        removed.
    ``local_source_error``
        The last failure to read the on-disk key sources. Adoption is skipped
        while it persists, so a key file this process cannot read is a
        credential that never starts working - reported here rather than
        silently treated as absent.
    """
    init_db()
    with _db_module.SessionLocal() as session:
        unbound = [
            {
                "credential_id": row.credential_id,
                "kind": row.kind,
                "label": row.label,
            }
            for row in session.query(ApiCredential)
            .filter(
                ApiCredential.revoked_at.is_(None),
                ApiCredential.operator_id.is_(None),
                ApiCredential.runtime_id.is_(None),
                ApiCredential.machine_id.is_(None),
            )
            .order_by(ApiCredential.id.asc())
            .all()
        ]
        legacy = [
            {"operator_id": row.id, "slug": row.slug}
            for row in session.query(Operator)
            .filter(Operator.slug.like("daemon-%"))
            .order_by(Operator.id.asc())
            .all()
        ]
    return {
        "unbound_credentials": unbound,
        "legacy_daemon_operators": legacy,
        "local_source_error": last_source_error(),
        "ok": not unbound and not legacy and last_source_error() is None,
    }


__all__ = [
    "KIND_ADMIN",
    "KIND_OPERATOR",
    "KIND_RUNTIME",
    "LOCAL_SOURCES",
    "NEGATIVE_CACHE_MAX_ENTRIES",
    "NEGATIVE_CACHE_TTL_SECONDS",
    "RUNTIME_CREDENTIAL_TTL_SECONDS",
    "SOURCE_ADMIN_KEY",
    "SOURCE_ADMIN_KEYS",
    "SOURCE_CACHE_TTL_SECONDS",
    "SOURCE_ENROLMENT",
    "SOURCE_MANUAL",
    "SOURCE_OPERATOR_KEY",
    "CredentialError",
    "CredentialRecord",
    "LocalSourceError",
    "diagnose",
    "fingerprint",
    "get_credential",
    "hash_secret",
    "invalidate_source_cache",
    "is_active",
    "last_source_error",
    "list_credentials",
    "local_key_sources",
    "mint_runtime_credential",
    "register_credential",
    "reset_adoption_cache",
    "resolve_secret",
    "revoke_credential",
    "revoke_local_secret",
    "revoke_machine_credentials",
    "sync_local_credentials",
]

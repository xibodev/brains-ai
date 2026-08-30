"""Operator identities and API-key resolution.

The current identity and trust boundary is documented in
``docs/ARCHITECTURE.md``:

* The ``admin`` operator is auto-provisioned on first run and bound to
  the existing admin key (``settings.api_key`` / ``~/.brains/admin-key``)
  by SHA-256 fingerprint. Every pre-existing single-operator install
  resolves to ``admin`` with zero migration.
* Additional operators are minted by :func:`add_operator`, which writes
  the new key to ``~/.brains/operator-keys/<slug>.key`` (mode 0600) and
  records the fingerprint in the database.
* :func:`load_operator_api_keys` returns every operator key on disk so
  the gateway's :func:`brains.api.auth._valid_keys` can accept them.
* :func:`resolve_operator_for_key` maps an inbound API key back to the
  matching :class:`Operator` row by fingerprint, defaulting to ``admin``
  when no other operator owns the key (preserves back-compat with
  ``BRAINS_API_KEY`` / ``BRAINS_API_KEYS``).
* :func:`resolve_current_operator` is the public resolver used by
  :func:`brains.control.sessions.start_session`. Priority:

      1. explicit ``operator`` argument,
      2. ``current_operator`` :class:`contextvars.ContextVar` set by the
         MCP SSE auth middleware,
      3. the ``BRAINS_OPERATOR`` env var (CLI / stdio launch),
      4. fingerprint match against ``BRAINS_API_KEY`` in env,
      5. the ``admin`` operator.

The resolver never raises — it always returns *some* operator so
back-compat installs keep working even when the admin row hasn't been
provisioned yet. Callers can opt into stricter behaviour by inspecting
the returned ``slug``.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import TypedDict

from brains.api.admin_key import state_dir
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import Operator

# Set by the SSE auth middleware once it has resolved the inbound key
# to an operator slug. ``None`` means "no per-request operator context"
# and resolution falls back to env / admin.
current_operator: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "brains_current_operator", default=None
)

ADMIN_SLUG = "admin"
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class OperatorRecord(TypedDict):
    id: int
    slug: str
    display_name: str | None
    key_fingerprint: str | None


class OperatorSlugError(ValueError):
    """Raised when a CLI caller provides an invalid operator slug."""


class OperatorExistsError(ValueError):
    """Raised when :func:`add_operator` is called with a slug already in use."""


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def operator_keys_dir() -> Path:
    """Directory holding per-operator API key files.

    Layered on top of :func:`brains.api.admin_key.state_dir` so the
    ``BRAINS_STATE_DIR`` override and ``~/.brains`` fallback apply
    uniformly. Created on demand by :func:`add_operator`.
    """
    return state_dir() / "operator-keys"


def _operator_key_path(slug: str) -> Path:
    return operator_keys_dir() / f"{slug}.key"


def _validate_slug(slug: str) -> str:
    raw = (slug or "").strip()
    # Reject before normalising so the CLI never silently rewrites the
    # operator's input (e.g. ``"Alice"`` quietly becoming ``"alice"``).
    if not SLUG_PATTERN.match(raw):
        raise OperatorSlugError(
            f"operator slug {slug!r} must match {SLUG_PATTERN.pattern} "
            "(lowercase letters, digits, '_' or '-', 1-63 chars, must "
            "start with a letter or digit)"
        )
    if raw == ADMIN_SLUG:
        raise OperatorSlugError(
            "operator slug 'admin' is reserved for the auto-provisioned admin "
            "operator; the admin row is updated automatically when the admin "
            "key rotates."
        )
    return raw


def _to_record(row: Operator) -> OperatorRecord:
    return {
        "id": row.id,
        "slug": row.slug,
        "display_name": row.display_name,
        "key_fingerprint": row.key_fingerprint,
    }


def ensure_admin_operator() -> OperatorRecord:
    """Auto-provision / sync the ``admin`` operator from the current admin key.

    Idempotent: on first call it creates the row; on subsequent calls
    it updates the fingerprint if the admin key has rotated. Called
    from the supervised services' lifespan hooks (gateway, dashboard,
    MCP server) so single-operator installs keep working without any
    manual setup.

    Returns the current admin record. If ``settings.api_key`` is empty
    (e.g. tests that disable auth) the admin row is still created with
    a ``None`` fingerprint so foreign keys on ``AgentSession`` can
    resolve.
    """
    from brains.config import settings

    init_db()
    fingerprint = _fingerprint(settings.api_key) if settings.api_key else None
    with _db_module.SessionLocal() as session:
        row = session.query(Operator).filter(Operator.slug == ADMIN_SLUG).one_or_none()
        if row is None:
            row = Operator(
                slug=ADMIN_SLUG,
                display_name="admin",
                key_fingerprint=fingerprint,
            )
            session.add(row)
            session.flush()
        elif row.key_fingerprint != fingerprint:
            row.key_fingerprint = fingerprint
        from brains.control.durable_mailbox import _ensure_operator_mailbox_row

        _ensure_operator_mailbox_row(session, row.id, row.slug)
        session.commit()
        session.refresh(row)
        return _to_record(row)


def list_operators() -> list[OperatorRecord]:
    """Return every operator ordered by creation time, admin first."""
    init_db()
    with _db_module.SessionLocal() as session:
        rows = session.query(Operator).order_by(Operator.id.asc()).all()
        return [_to_record(r) for r in rows]


def add_operator(
    slug: str,
    *,
    display_name: str | None = None,
    api_key: str | None = None,
) -> tuple[OperatorRecord, str]:
    """Mint a new operator + persist its API key on disk.

    Returns ``(record, api_key)``. The raw key is returned *once* so the
    CLI can print it; subsequent reads of the operator's record only
    expose the fingerprint. The key file lives at
    ``operator_keys_dir() / f"{slug}.key"`` with mode 0600 on POSIX.

    Raises :class:`OperatorSlugError` for invalid / reserved slugs and
    :class:`OperatorExistsError` if the slug is already taken or the
    on-disk key file already exists.
    """
    clean_slug = _validate_slug(slug)
    init_db()
    with _db_module.SessionLocal() as session:
        existing = session.query(Operator).filter(Operator.slug == clean_slug).one_or_none()
        if existing is not None:
            raise OperatorExistsError(f"operator {clean_slug!r} already exists")
        path = _operator_key_path(clean_slug)
        if path.exists():
            raise OperatorExistsError(
                f"operator key file already present at {path}; refusing to "
                "overwrite. Remove the file manually if this is intentional."
            )
        key = api_key or secrets.token_urlsafe(32)
        row = Operator(
            slug=clean_slug,
            display_name=display_name or clean_slug,
            key_fingerprint=_fingerprint(key),
        )
        session.add(row)
        session.flush()
        from brains.control.durable_mailbox import _ensure_operator_mailbox_row

        _ensure_operator_mailbox_row(session, row.id, row.slug)
        session.commit()
        session.refresh(row)
        record = _to_record(row)
    file_created = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            file_created = True
            handle.write(key + "\n")
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        if file_created:
            with contextlib.suppress(OSError):
                path.unlink()
        with contextlib.suppress(Exception), _db_module.SessionLocal() as session:
            from brains.storage.models import Mailbox

            session.query(Mailbox).filter(Mailbox.owner_operator_id == record["id"]).delete(
                synchronize_session=False
            )
            persisted = session.get(Operator, record["id"])
            if persisted is not None:
                session.delete(persisted)
            session.commit()
        raise
    _register_operator_credential(record, key)
    _invalidate_credential_sources()
    return record, key


def _register_operator_credential(record: OperatorRecord, key: str) -> None:
    """Bind the new key to its operator explicitly, rather than by adoption.

    Issuing a key is an explicit act, so it is also the act that may re-enable
    a hash an operator previously revoked - a passive resync never can. It also
    binds ``operator_id`` directly instead of leaving the store to match a
    fingerprint after the fact.
    """
    try:
        from brains.authz import credentials as creds

        creds.register_credential(
            key,
            kind=creds.KIND_OPERATOR,
            operator_id=record["id"],
            label=f"operator {record['slug']}",
            source=creds.SOURCE_OPERATOR_KEY,
            reinstate=True,
        )
    except Exception:  # pragma: no cover - defensive
        pass


def remove_operator_key(slug: str) -> bool:
    """Delete an operator's on-disk key file and revoke the credential it fed.

    Returns ``True`` when a key file was removed.

    The revocation is explicit and exact: the file is read *before* it is
    unlinked and that value names the credential to retire, so the store is
    never asked to infer a supersede from a key that has merely gone missing
    from one process's view. A filesystem error propagates rather than being
    swallowed - a caller that asked for a key to stop working must not be told
    it did when the file is still there.
    """
    path = _operator_key_path((slug or "").strip())
    if not path.exists():
        _invalidate_credential_sources()
        return False
    superseded = path.read_text(encoding="utf-8").strip()
    path.unlink()
    if superseded:
        from brains.authz import credentials as creds

        creds.revoke_local_secret(superseded)
    _invalidate_credential_sources()
    return True


def _invalidate_credential_sources() -> None:
    """Best-effort re-adoption of the on-disk keys into the credential store."""
    try:
        from brains.authz import credentials as creds

        creds.invalidate_source_cache()
        creds.sync_local_credentials()
    except Exception:  # pragma: no cover - defensive
        pass


def load_operator_api_keys() -> tuple[str, ...]:
    """Read every persisted operator key from disk.

    Used by :func:`brains.api.auth._valid_keys` to grow the accepted
    credential set without forcing the operator to plumb every key
    through ``BRAINS_API_KEYS``. Returns an empty tuple when the
    directory does not exist (no operators have been added yet).
    """
    directory = operator_keys_dir()
    if not directory.exists():
        return ()
    keys: list[str] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".key":
            continue
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            keys.append(value)
    return tuple(keys)


def resolve_operator_for_key(api_key: str | None) -> OperatorRecord | None:
    """Return the operator owning ``api_key`` (by fingerprint), or ``None``.

    Looks up the key fingerprint in the ``operators`` table. Does *not*
    fall back to admin — callers that want the fallback should use
    :func:`resolve_current_operator` instead. Returns ``None`` if the
    fingerprint matches no operator row (e.g. key rotated, but the
    admin row hasn't been re-synced yet).
    """
    if not api_key:
        return None
    fingerprint = _fingerprint(api_key)
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(Operator).filter(Operator.key_fingerprint == fingerprint).one_or_none()
        return _to_record(row) if row is not None else None


def _resolve_by_slug(slug: str) -> OperatorRecord | None:
    init_db()
    with _db_module.SessionLocal() as session:
        row = session.query(Operator).filter(Operator.slug == slug).one_or_none()
        return _to_record(row) if row is not None else None


def resolve_current_operator(*, operator: str | None = None) -> OperatorRecord:
    """Resolve the operator that should own a newly-started session.

    Resolution order (first hit wins):

    1. explicit ``operator`` argument (slug),
    2. the principal the current request authenticated as
       (:func:`brains.authz.resolver.get_current_principal`),
    3. :data:`current_operator` ContextVar (set by SSE auth middleware),
    4. ``BRAINS_OPERATOR`` environment variable,
    5. fingerprint match against ``BRAINS_API_KEY`` env value,
    6. the auto-provisioned ``admin`` operator.

    Always returns *some* operator — if every prior step misses, this
    function calls :func:`ensure_admin_operator` and returns it so the
    caller can always stamp a valid foreign key. That fallback is an
    *attribution* default for local processes, not an authorization
    decision: HTTP authorization is decided by the resolved principal in
    :mod:`brains.authz.policy` before any control function is reached.
    """
    # 1. explicit argument.
    if operator:
        record = _resolve_by_slug(operator.strip().lower())
        if record is not None:
            return record

    # 2. the principal the current request authenticated as (BL-P0-01). This is
    #    the authoritative actor for an HTTP/MCP request; the older
    #    ``current_operator`` ContextVar below stays for callers that set only
    #    a slug.
    try:
        from brains.authz.resolver import get_current_principal

        principal = get_current_principal()
    except Exception:
        principal = None
    if principal is not None and principal.operator_slug:
        record = _resolve_by_slug(principal.operator_slug)
        if record is not None:
            return record

    # 3. ContextVar set by middleware.
    ctx_slug = current_operator.get()
    if ctx_slug:
        record = _resolve_by_slug(ctx_slug)
        if record is not None:
            return record

    # 4. env override (CLI / stdio launch).
    env_slug = (os.environ.get("BRAINS_OPERATOR") or "").strip().lower()
    if env_slug:
        record = _resolve_by_slug(env_slug)
        if record is not None:
            return record

    # 5. fingerprint match against BRAINS_API_KEY.
    env_key = os.environ.get("BRAINS_API_KEY")
    if env_key:
        match = resolve_operator_for_key(env_key)
        if match is not None:
            return match

    # 6. fall back to admin (auto-provision if missing).
    return ensure_admin_operator()


__all__ = [
    "ADMIN_SLUG",
    "OperatorExistsError",
    "OperatorRecord",
    "OperatorSlugError",
    "add_operator",
    "current_operator",
    "ensure_admin_operator",
    "list_operators",
    "load_operator_api_keys",
    "operator_keys_dir",
    "remove_operator_key",
    "resolve_current_operator",
    "resolve_operator_for_key",
]

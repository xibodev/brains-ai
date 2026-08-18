"""Enrolment tokens - single-use, expiring Connect-a-machine credentials (F1).

A new machine connects to the hub without an operator API key by presenting an
*enrolment token*. The operator mints one (``mint_token``) from an authed
surface; the machine redeems it (``redeem_token``) on an UNAUTHENTICATED route,
because the token itself is the credential.

Security invariants (do not weaken):

* The raw token is high-entropy (``secrets.token_urlsafe``) and is returned to
  the operator exactly ONCE at mint time. We persist ONLY its sha256 hash -
  the raw token is never stored or logged.
* Tokens are single-use *under concurrency*: redemption is one conditional
  ``UPDATE ... WHERE redeemed_at IS NULL``, so two machines racing on the same
  token produce exactly one winner and one ``already redeemed`` refusal. A
  read-then-write check would let both pass.
* Tokens expire: a redeem after ``expires_at`` is rejected.
* Redemption mints a **Runtime-narrow, Org-bound** credential
  (:mod:`brains.authz.credentials`), not an operator key. That credential
  authorizes only the Runtime operations of the machine it was minted for -
  register, heartbeat, status, claim, execute - and no operator or admin API.
  Only its hash is stored, so the hub cannot hand the secret back out later.
* A machine belongs to exactly one Org, and the claim is taken **before** any
  credential is minted, in the same transaction as the token claim, whether or
  not the redemption registers a single Runtime. A redemption whose token
  names a different Org than the machine's existing owner mints nothing and is
  refused with the same wording as an unknown token, so a redeemer cannot use
  the unauthenticated route to probe which Org owns which machine id. The
  refused token stays unredeemed and can be retried against a machine its Org
  is allowed to claim.

Pure control logic - no FastAPI. Uses ``init_db()`` + ``SessionLocal()`` like
the other control modules.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, timedelta

from sqlalchemy import update

from brains.control import runtimes as runtimes_ctl
from brains.control.common import utc_now
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import EnrolmentToken, Org

#: What a refused redemption says, whatever the reason. An unknown token and a
#: token for a machine another Org already owns are deliberately
#: indistinguishable: the redeem route is unauthenticated, so every
#: distinguishable answer is a probe.
_REFUSAL = "invalid enrolment token"


class MachineClaimError(ValueError):
    """A redemption would claim a machine that belongs to another Org.

    Carries the same message as an unknown token so the unauthenticated redeem
    route cannot be used to enumerate which machine ids exist or which Org owns
    them.
    """


def _hash_token(token: str) -> str:
    """Return the sha256 hex digest of a raw token (what we persist)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _default_org_id(session) -> int | None:
    """The ``default`` Org's id, or ``None`` when the install has none."""
    row = session.query(Org).filter(Org.slug == "default").one_or_none()
    return row.id if row is not None else None


def mint_token(
    *,
    label: str | None = None,
    org_id: int | None = None,
    ttl_seconds: int = 900,
    operator=None,
) -> dict:
    """Mint a single-use connect token.

    Generates a high-entropy raw token, persists only its sha256 hash, and sets
    ``expires_at = now + ttl_seconds`` (a negative ``ttl_seconds`` mints an
    already-expired token, used for tests/revocation). Returns the raw token
    ONCE - it is never stored or logged in raw form.

    ``operator`` records who minted the token, so a Runtime credential minted
    by its redemption is attributable to the operator that authorized the
    connection.

    The token carries the Org it is minted for. When the caller names none the
    ``default`` Org is stamped on it at mint time rather than resolved at
    redeem time, so the *intended* Org is part of the credential the machine
    presents and redemption always has a concrete Org to compare a machine's
    existing claim against.
    """
    init_db()
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    now = utc_now()
    expires_at = now + timedelta(seconds=ttl_seconds)
    operator_id = None
    if operator is not None:
        operator_id = (
            operator.get("id") if isinstance(operator, dict) else getattr(operator, "id", None)
        )
    with SessionLocal() as session:
        if org_id is None:
            org_id = _default_org_id(session)
        row = EnrolmentToken(
            token_hash=token_hash,
            label=label,
            org_id=org_id,
            created_by_operator_id=operator_id,
            created_at=now,
            expires_at=expires_at,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        token_id = row.id
    return {
        "id": token_id,
        "token": raw,
        "label": label,
        "org_id": org_id,
        "expires_at": expires_at.isoformat(),
    }


def _claim_token(session, token_hash: str, machine_id: str, now) -> EnrolmentToken:
    """Atomically claim one unredeemed, unexpired token. Raises on refusal.

    The claim is a conditional update, so the single-use rule is a state
    transition the database enforces rather than a check the application hopes
    nobody races. It is deliberately *not* committed here: the caller keeps the
    transaction open so the machine-Org claim and the Runtime credential the
    redemption mints land with it or not at all. The update is also what takes
    the write lock, so every claim check the caller makes afterwards runs
    inside a transaction no competing claimer can commit into.
    """
    row = (
        session.query(EnrolmentToken).filter(EnrolmentToken.token_hash == token_hash).one_or_none()
    )
    if row is None:
        raise ValueError(_REFUSAL)
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        # SQLite hands back tz-naive datetimes; values are stored UTC.
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at is not None and now > expires_at:
        raise ValueError("enrolment token expired")
    result = session.execute(
        update(EnrolmentToken)
        .where(
            EnrolmentToken.token_hash == token_hash,
            EnrolmentToken.redeemed_at.is_(None),
        )
        .values(redeemed_at=now, redeemed_machine_id=machine_id)
    )
    if result.rowcount != 1:
        session.rollback()
        raise ValueError("enrolment token already redeemed")
    return row


def redeem_token(
    token: str,
    *,
    machine_id: str,
    clis: list[dict] | None = None,
    org_id: int | None = None,
) -> dict:
    """Redeem a connect token for ``machine_id``.

    Raises ``ValueError`` if the token is unknown, already redeemed, expired,
    or names an Org that may not claim ``machine_id``. On success stamps
    ``redeemed_at`` + ``redeemed_machine_id``, claims the machine and mints the
    machine's credential in ONE transaction, then - when ``clis`` is given -
    registers one runtime per CLI (version-stamped).

    Returns ``{"machine_id", "runtimes", "daemon_key", "credential_id",
    "org_id", "expires_at"}``. ``daemon_key`` is the raw Runtime-narrow
    credential, returned exactly once to the machine that redeemed the token.

    The Org the credential is bound to is the one the *token* names. A caller
    supplied ``org_id`` is honoured only when the token itself carries none, so
    a redeemer cannot widen its own scope by naming a different Org.

    The machine's Org is claimed *before* anything is minted, and the claim
    counts the machine's live Runtime credential as well as its Runtime rows.
    A redemption with an empty ``clis`` list registers no Runtime at all, so a
    claim derived only from ``runtimes`` would read an enrolled machine as
    unclaimed and hand a second Org a credential for a box it does not own -
    with which it could then read, heartbeat, open Sessions on and inject
    events into that machine's work. A refused redemption rolls the token claim
    back with it: the token stays unredeemed and retry-safe, and no credential
    exists for it.
    """
    init_db()
    token_hash = _hash_token(token)
    now = utc_now()
    with SessionLocal() as session:
        row = _claim_token(session, token_hash, machine_id, now)
        token_org_id = row.org_id if row.org_id is not None else org_id
        created_by_operator_id = row.created_by_operator_id
        token_label = row.label

        # Still inside the write transaction the token claim opened. On
        # PostgreSQL take the machine's advisory lock too, so a competing
        # redemption blocks here instead of racing the claim below.
        runtimes_ctl.serialize_machine_claim(session, machine_id)
        claimed_org_id = runtimes_ctl.machine_claim_org_id(session, machine_id)
        if (
            claimed_org_id is not None
            and token_org_id is not None
            and claimed_org_id != token_org_id
        ):
            session.rollback()
            raise MachineClaimError(_REFUSAL)
        effective_org_id = token_org_id if token_org_id is not None else claimed_org_id

        # Mint the machine's ONGOING credential: Runtime-narrow and Org-bound,
        # so a daemon can register, heartbeat, report status, claim work and
        # stream execution for its own machine and nothing else. It is
        # revocable on its own (``brains-ai credentials revoke``) without
        # touching the admin key, and it expires, so a machine that is never
        # re-enrolled stops being trusted. Minting it inside the claim is what
        # makes it the machine's standing Org claim even when the redemption
        # registers no Runtime at all.
        from brains.authz import credentials as creds

        record, daemon_key = creds.mint_runtime_credential(
            org_id=effective_org_id,
            machine_id=machine_id,
            label=token_label or f"runtime {machine_id}",
            created_by_operator_id=created_by_operator_id,
            session=session,
        )
        session.commit()

    registered: list[dict] = []
    for spec in clis or []:
        tool = spec.get("tool")
        if not tool:
            continue
        version = spec.get("version")
        caps = json.dumps({"version": version}) if version is not None else None
        rt = runtimes_ctl.register_runtime(
            machine_id,
            tool,
            org_id=effective_org_id,
            daemon_version=version,
            capabilities=caps,
            status="online",
            health="healthy",
        )
        # Surface the version on the returned dict (the runtime dict carries it
        # in capabilities JSON; expose it explicitly for the connect contract).
        rt["version"] = version
        registered.append(rt)

    return {
        "machine_id": machine_id,
        "runtimes": registered,
        "daemon_key": daemon_key,
        "credential_id": record["credential_id"],
        "org_id": effective_org_id,
        "expires_at": record["expires_at"],
    }

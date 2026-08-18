"""Runtimes — tool × machine registration + liveness (native-battalion WS2).

A *runtime* is a registered CLI on a daemon-managed machine. The WS1 daemon
registers it and heartbeats it; a sweeper flips stale rows to ``offline``.

``register_runtime`` upserts by ``(machine_id, tool)`` and first upserts the
``registered_tools`` row (via :mod:`brains.control.tool_registry`) so the hard
``runtimes.tool → registered_tools.name`` FK always holds (WS2-RATIFIED fork #5).

Pure control logic — no FastAPI.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC
from typing import Any, cast

from sqlalchemy import case, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.tool_registry import register_tool
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import ApiCredential, RegisteredTool, Runtime

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
RUNTIME_STATUSES = {"online", "offline", "draining"}
RUNTIME_HEALTHS = {"healthy", "degraded", "unhealthy", "unknown"}


class RuntimeOrgConflictError(ValueError):
    """A registration would move an already-claimed machine into another Org.

    A machine belongs to exactly one Org. Registration is an upsert, so without
    this an ``admin`` of any Org could re-register another Org's machine and
    take its Runtimes - and the work assigned to them - with it. The HTTP layer
    answers this with a non-disclosing ``404``.
    """


def _parse_capabilities(raw) -> dict:
    """Capabilities are persisted as a JSON string; return a structured dict so
    the API/UI can read ``capabilities.models[]`` directly (F2 cascade)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _runtime_to_dict(rt: Runtime) -> dict:
    return {
        "id": rt.id,
        "slug": rt.slug,
        "org_id": rt.org_id,
        "machine_id": rt.machine_id,
        "machine_label": rt.machine_label,
        "tool": rt.tool,
        "display_name": rt.display_name,
        "daemon_version": rt.daemon_version,
        "os": rt.os,
        "working_root": rt.working_root,
        "capabilities": _parse_capabilities(rt.capabilities),
        "status": rt.status,
        "health": rt.health,
        "last_heartbeat_at": rt.last_heartbeat_at.isoformat() if rt.last_heartbeat_at else None,
        "registered_at": rt.registered_at.isoformat() if rt.registered_at else None,
        "created_at": rt.created_at.isoformat() if rt.created_at else None,
        "updated_at": rt.updated_at.isoformat() if rt.updated_at else None,
        "metadata_json": rt.metadata_json,
    }


def _ensure_tool_registered(tool: str) -> None:
    """Guarantee a ``registered_tools`` row exists so the hard FK holds.

    The daemon upserts the tool registry first (fork #5). We reuse the existing
    :func:`register_tool` control path, which is itself an idempotent upsert,
    and we do it *before* the claim transaction opens: ``register_tool``
    commits on its own session, which a transaction already holding the
    machine's claim would deadlock against.
    """
    init_db()
    with SessionLocal() as session:
        row = session.query(RegisteredTool).filter(RegisteredTool.name == tool).one_or_none()
        if row is not None:
            return
    register_tool(
        name=tool,
        display_name=tool,
        cli_command=tool,
        notes="auto-registered by runtime registration",
        verify=False,
    )


def register_runtime(
    machine_id: str,
    tool: str,
    *,
    slug: str | None = None,
    org_id: int | None = None,
    machine_label: str | None = None,
    display_name: str | None = None,
    daemon_version: str | None = None,
    os: str | None = None,
    working_root: str | None = None,
    capabilities: str | None = None,
    status: str = "online",
    health: str = "unknown",
    metadata_json: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Upsert a runtime keyed by ``(machine_id, tool)``.

    Also upserts the ``registered_tools`` row so the hard FK holds. Idempotent:
    a second call with the same ``(machine_id, tool)`` updates the existing row
    rather than minting a duplicate.

    Concurrency-safe on the Org claim, and *atomic* on it: the row and the
    claim it carries are one transaction, so a registration that loses the
    claim leaves nothing behind. A refused registration used to commit the bare
    row first and apply the Org second, which left an Org-less Runtime on a
    machine owned by somebody else - a row no Org owns, that no Org-scoped
    listing accounts for, and that the next caller to register could claim.

    Two registrations racing to claim the *same* machine from *different* Orgs
    are serialized (see :func:`_serialize_machine_claim`); exactly one commits
    a claim and the other is refused with :class:`RuntimeOrgConflictError`
    rather than splitting one machine's Runtimes across two Orgs. Two *tools*
    racing on the same machine for the same Org both succeed.
    """
    if status not in RUNTIME_STATUSES:
        raise ValueError(f"status must be one of {sorted(RUNTIME_STATUSES)}")
    if health not in RUNTIME_HEALTHS:
        raise ValueError(f"health must be one of {sorted(RUNTIME_HEALTHS)}")
    runtime_slug = slug or f"{machine_id}-{tool}"
    if not SLUG_PATTERN.match(runtime_slug):
        raise ValueError("runtime slug must be lowercase alphanumeric with - or _ (max 63 chars)")
    init_db()
    # Outside the claim transaction: ``register_tool`` commits on its own
    # session, and a nested write would contend with the lock the claim holds.
    _ensure_tool_registered(tool)
    attempts = 0
    while True:
        attempts += 1
        try:
            result, created = _register_runtime_once(
                machine_id,
                tool,
                runtime_slug=runtime_slug,
                explicit_slug=slug,
                org_id=org_id,
                machine_label=machine_label,
                display_name=display_name,
                daemon_version=daemon_version,
                os=os,
                working_root=working_root,
                capabilities=capabilities,
                status=status,
                health=health,
                metadata_json=metadata_json,
            )
            break
        except IntegrityError:
            # Another writer inserted this runtime between our read and our
            # write. Retry once against the row it committed.
            if attempts >= 3:
                raise
    append_event(
        "runtime_registered" if created else "runtime_updated",
        f"{result['slug']} ({tool}@{machine_id}) -> {status}",
        session_id=session_id,
        metadata={"slug": result["slug"], "tool": tool, "machine_id": machine_id},
    )
    return result


def serialize_machine_claim(session, machine_id: str) -> None:
    """Serialize concurrent writers that could claim the same machine.

    The one-Org-per-machine rule is a constraint across *rows* - "no sibling
    Runtime and no live Runtime credential on this machine names a different
    Org" - and no single-column unique index can express it. It therefore has
    to be checked, and the check has to be safe against a concurrent claimer.

    SQLite gives that for free: it takes a write lock for the duration of a
    write transaction, so the losing claimer cannot commit between our write
    and our check - which is why every caller writes first and verifies the
    claim afterwards, from inside the transaction that holds the write.
    PostgreSQL at ``READ COMMITTED`` does not order the two - two transactions
    can each see an unclaimed machine and each write a different Org - so we
    take a transaction-scoped advisory lock keyed by the machine id, which the
    second claimer blocks on until the first has committed or rolled back. The
    key is derived from the machine id rather than from a sequence so both
    processes compute the same one.
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"brains.runtime.machine:{machine_id}".encode()).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def machine_claim_org_id(session, machine_id: str, *, exclude_id: int | None = None) -> int | None:
    """The Org that owns ``machine_id``, read inside the caller's transaction.

    A machine is claimed by two kinds of evidence, and both count:

    * a ``runtimes`` row on the machine that declares an Org, and
    * a live (un-revoked) Runtime credential minted for the machine.

    The credential matters on its own. An enrolment redeemed with an empty
    ``clis`` list registers no Runtime at all, so a claim derived only from
    ``runtimes`` rows would read that machine as unclaimed and let a second Org
    mint a Runtime credential for a box it does not own. The credential *is*
    the standing claim, which is also why revoking it releases the machine.
    """
    query = session.query(Runtime.org_id).filter(
        Runtime.machine_id == machine_id, Runtime.org_id.is_not(None)
    )
    if exclude_id is not None:
        query = query.filter(Runtime.id != exclude_id)
    for (org_id,) in query.distinct().all():
        return org_id
    credential = (
        session.query(ApiCredential.org_id)
        .filter(
            ApiCredential.kind == "runtime",
            ApiCredential.machine_id == machine_id,
            ApiCredential.org_id.is_not(None),
            ApiCredential.revoked_at.is_(None),
        )
        .first()
    )
    return credential[0] if credential is not None else None


def machine_org_claim(machine_id: str) -> int | None:
    """The Org that owns ``machine_id``, on its own short-lived session."""
    if not machine_id:
        return None
    init_db()
    with SessionLocal() as session:
        return machine_claim_org_id(session, machine_id)


def _register_runtime_once(
    machine_id: str,
    tool: str,
    *,
    runtime_slug: str,
    explicit_slug: str | None,
    org_id: int | None,
    machine_label: str | None,
    display_name: str | None,
    daemon_version: str | None,
    os: str | None,
    working_root: str | None,
    capabilities: str | None,
    status: str,
    health: str,
    metadata_json: str | None,
) -> tuple[dict, bool]:
    """One upsert attempt. Raises ``IntegrityError`` when it lost an insert race.

    The insert and the Org claim it carries are the same transaction. The row
    is written with its Org already on it and the claim is verified *after* the
    insert, from inside the transaction that holds it, so a conflicting claim
    rolls the insert back with it: a refused registration leaves no row at all,
    let alone an Org-less one on somebody else's machine.

    For an existing row the claim is enforced by the ``WHERE`` clause of the
    update itself rather than by a preceding read, because SQLite's driver runs
    a ``SELECT`` outside any transaction and a read-then-write check can
    observe "unclaimed" after another Org has committed.

    A registration that names no Org inherits the machine's existing claim
    rather than writing ``NULL`` next to it. "Belongs to nobody" is a fact
    about a pre-Org install, not something a new row on a claimed machine gets
    to assert.
    """
    now = utc_now()
    with SessionLocal() as session:
        serialize_machine_claim(session, machine_id)
        existing = (
            session.query(Runtime)
            .filter(Runtime.machine_id == machine_id, Runtime.tool == tool)
            .one_or_none()
        )
        if existing is None:
            claimed = machine_claim_org_id(session, machine_id)
            if org_id is not None and claimed is not None and claimed != org_id:
                session.rollback()
                raise RuntimeOrgConflictError(
                    f"machine {machine_id!r} is already registered to another Org"
                )
            row = Runtime(
                slug=runtime_slug,
                machine_id=machine_id,
                tool=tool,
                org_id=org_id if org_id is not None else claimed,
                machine_label=machine_label,
                display_name=display_name,
                daemon_version=daemon_version,
                os=os,
                working_root=working_root,
                capabilities=capabilities,
                status=status,
                health=health,
                last_heartbeat_at=now,
                registered_at=now,
                created_at=now,
                updated_at=now,
                metadata_json=metadata_json,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                # Another writer inserted this ``(machine_id, tool)`` or slug
                # first. Nothing of ours is committed; the caller retries.
                session.rollback()
                raise
            # Re-read the siblings from inside the write transaction: any claim
            # committed before it is visible, and none can be committed during
            # it. A conflict here rolls the insert back with the transaction.
            claim_now = machine_claim_org_id(session, machine_id, exclude_id=row.id)
            if row.org_id is None:
                # The machine was claimed while we were inserting. Join the
                # claim rather than leave an Org-less row beside it.
                if claim_now is not None:
                    row.org_id = claim_now
                    session.flush()
            elif claim_now is not None and claim_now != row.org_id:
                session.rollback()
                raise RuntimeOrgConflictError(
                    f"machine {machine_id!r} is already registered to another Org"
                )
            session.commit()
            row = (
                session.query(Runtime)
                .filter(Runtime.machine_id == machine_id, Runtime.tool == tool)
                .one()
            )
            return _runtime_to_dict(row), True

        values: dict = {
            "status": status,
            "health": health,
            "last_heartbeat_at": now,
            "updated_at": now,
        }
        if explicit_slug is not None:
            values["slug"] = runtime_slug
        for column, value in (
            ("org_id", org_id),
            ("machine_label", machine_label),
            ("display_name", display_name),
            ("daemon_version", daemon_version),
            ("os", os),
            ("working_root", working_root),
            ("capabilities", capabilities),
            ("metadata_json", metadata_json),
        ):
            if value is not None:
                values[column] = value

        conditions = [Runtime.machine_id == machine_id, Runtime.tool == tool]
        if org_id is not None:
            sibling = aliased(Runtime)
            conditions.append(
                ~select(sibling.id)
                .where(
                    sibling.machine_id == machine_id,
                    sibling.org_id.is_not(None),
                    sibling.org_id != org_id,
                )
                .exists()
            )
        result = cast(
            "CursorResult[Any]",
            session.execute(update(Runtime).where(*conditions).values(**values)),
        )
        if result.rowcount != 1:
            session.rollback()
            raise RuntimeOrgConflictError(
                f"machine {machine_id!r} is already registered to another Org"
            )
        # Re-read the claim from inside the write transaction, for the same
        # reason the insert path does: the ``WHERE`` clause only sees sibling
        # Runtimes, and a machine can also be claimed by a live Runtime
        # credential minted for it by an enrolment that registered no tool.
        claim_now = machine_claim_org_id(session, machine_id, exclude_id=existing.id)
        if claim_now is not None and org_id is not None and claim_now != org_id:
            session.rollback()
            raise RuntimeOrgConflictError(
                f"machine {machine_id!r} is already registered to another Org"
            )
        session.commit()
        row = (
            session.query(Runtime)
            .filter(Runtime.machine_id == machine_id, Runtime.tool == tool)
            .one()
        )
        return _runtime_to_dict(row), False


def heartbeat(
    runtime_ref: str | int,
    *,
    status: str | None = None,
    health: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Stamp ``last_heartbeat_at`` and optionally update status/health."""
    if status is not None and status not in RUNTIME_STATUSES:
        raise ValueError(f"status must be one of {sorted(RUNTIME_STATUSES)}")
    if health is not None and health not in RUNTIME_HEALTHS:
        raise ValueError(f"health must be one of {sorted(RUNTIME_HEALTHS)}")
    init_db()
    now = utc_now()
    with SessionLocal() as session:
        rt = _get_runtime_row(session, runtime_ref)
        if rt is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        values: dict[str, Any] = {
            "last_heartbeat_at": now,
            "updated_at": now,
            "status": (
                status
                if status is not None
                else case(
                    (Runtime.status == "offline", "online"),
                    else_=Runtime.status,
                )
            ),
        }
        if health is not None:
            values["health"] = health
        session.execute(update(Runtime).where(Runtime.id == rt.id).values(**values))
        session.commit()
        session.expire(rt)
        session.refresh(rt)
        return _runtime_to_dict(rt)


def get_runtime(runtime_ref: str | int) -> dict | None:
    init_db()
    with SessionLocal() as session:
        rt = _get_runtime_row(session, runtime_ref)
        return _runtime_to_dict(rt) if rt is not None else None


def _get_runtime_row(session, ref: str | int) -> Runtime | None:
    if isinstance(ref, int):
        return session.get(Runtime, ref)
    if isinstance(ref, str) and ref.isdigit():
        return session.get(Runtime, int(ref))
    return session.query(Runtime).filter(Runtime.slug == ref).one_or_none()


def list_runtimes(
    *,
    org_id: int | None = None,
    machine_id: str | None = None,
    tool: str | None = None,
    status: str | None = None,
) -> list[dict]:
    init_db()
    with SessionLocal() as session:
        query = session.query(Runtime)
        if org_id is not None:
            query = query.filter(Runtime.org_id == org_id)
        if machine_id is not None:
            query = query.filter(Runtime.machine_id == machine_id)
        if tool is not None:
            query = query.filter(Runtime.tool == tool)
        if status is not None:
            query = query.filter(Runtime.status == status)
        return [_runtime_to_dict(rt) for rt in query.order_by(Runtime.slug).all()]


def set_status(
    runtime_ref: str | int,
    *,
    status: str | None = None,
    health: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Operator-driven status/health update (e.g. drain → ``draining``).

    Unlike :func:`heartbeat`, this does not stamp ``last_heartbeat_at`` — it is a
    control-plane mutation, not a liveness signal.
    """
    if status is not None and status not in RUNTIME_STATUSES:
        raise ValueError(f"status must be one of {sorted(RUNTIME_STATUSES)}")
    init_db()
    with SessionLocal() as session:
        rt = _get_runtime_row(session, runtime_ref)
        if rt is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        if status is not None:
            rt.status = status
        if health is not None:
            rt.health = health
        rt.updated_at = utc_now()
        session.commit()
        session.refresh(rt)
        result = _runtime_to_dict(rt)
    append_event(
        "runtime_status",
        f"{result['slug']} -> {result['status']}",
        session_id=session_id,
        metadata={"slug": result["slug"], "status": result["status"]},
    )
    return result


def mark_offline(runtime_ref: str | int, session_id: str | None = None) -> dict:
    init_db()
    with SessionLocal() as session:
        rt = _get_runtime_row(session, runtime_ref)
        if rt is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        rt.status = "offline"
        rt.updated_at = utc_now()
        session.commit()
        session.refresh(rt)
        result = _runtime_to_dict(rt)
    append_event(
        "runtime_offline",
        f"{result['slug']} marked offline",
        session_id=session_id,
        metadata={"slug": result["slug"]},
    )
    return result


def count_stale(ttl_seconds: int) -> int:
    """Read-only count of online Runtimes silent past ``ttl_seconds``.

    Mirrors :func:`sweep_stale`'s own candidate selection without mutating
    anything, so a readiness/health read can report "how many Runtimes are
    about to be swept offline" without itself causing that sweep as a side
    effect of a GET request.
    """
    init_db()
    now = utc_now()
    count = 0
    with SessionLocal() as session:
        candidates = session.query(Runtime).filter(Runtime.status == "online").all()
        for rt in candidates:
            last = rt.last_heartbeat_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if last is None or (now - last).total_seconds() > ttl_seconds:
                count += 1
    return count


def sweep_stale(ttl_seconds: int, session_id: str | None = None) -> list[dict]:
    """Flip online runtimes whose last heartbeat is older than ``ttl_seconds``
    to ``offline``. Returns the runtimes that were flipped."""
    init_db()
    now = utc_now()
    flipped: list[dict] = []
    with SessionLocal() as session:
        candidates = session.query(Runtime).filter(Runtime.status == "online").all()
        for rt in candidates:
            last = rt.last_heartbeat_at
            if last is not None and last.tzinfo is None:
                # SQLite hands back tz-naive datetimes; values are stored UTC.
                last = last.replace(tzinfo=UTC)
            if last is None or (now - last).total_seconds() > ttl_seconds:
                conditions = [
                    Runtime.id == rt.id,
                    Runtime.status == "online",
                ]
                if rt.last_heartbeat_at is None:
                    conditions.append(Runtime.last_heartbeat_at.is_(None))
                else:
                    conditions.append(Runtime.last_heartbeat_at == rt.last_heartbeat_at)
                changed = cast(
                    CursorResult[Any],
                    session.execute(
                        update(Runtime)
                        .where(*conditions)
                        .values(
                            status="offline",
                            updated_at=now,
                        )
                    ),
                ).rowcount
                if changed == 1:
                    payload = _runtime_to_dict(rt)
                    payload["status"] = "offline"
                    payload["updated_at"] = now.isoformat()
                    flipped.append(payload)
        session.commit()
    for payload in flipped:
        append_event(
            "runtime_swept_offline",
            f"{payload['slug']} swept offline (stale > {ttl_seconds}s)",
            session_id=session_id,
            metadata={"slug": payload["slug"], "ttl": ttl_seconds},
        )
    return flipped


def deregister(runtime_ref: str | int, session_id: str | None = None) -> dict:
    """Remove a runtime row entirely. Returns the removed runtime's snapshot."""
    init_db()
    with SessionLocal() as session:
        rt = _get_runtime_row(session, runtime_ref)
        if rt is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        result = _runtime_to_dict(rt)
        session.delete(rt)
        session.commit()
    append_event(
        "runtime_deregistered",
        f"{result['slug']} deregistered",
        session_id=session_id,
        metadata={"slug": result["slug"]},
    )
    return result

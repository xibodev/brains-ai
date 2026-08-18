"""The durable realtime event log: persist before publish, replay by cursor.

Why this exists
---------------

The :class:`~brains.events.bus.EventBus` is an in-process notifier. On its own
it makes three promises it cannot keep: that a client which was disconnected
for two seconds did not miss anything, that a client reconnecting to a second
gateway process sees the same stream, and that an event announced before its
row committed is not a lie if the write then fails.

This module fixes the ordering and the memory, not the fan-out:

* **Persist before publish.** :func:`publish_durable` commits the event row
  and only then hands the envelope to the in-process bus. A publish that
  cannot be recorded is not announced, and the commit and the announcement are
  one critical section, so subscribers see events in the order the store
  assigned their ids rather than in whichever order two threads happened to
  reach the bus.
* **Monotonic cursor.** ``realtime_events.id`` is assigned by the store, so
  ``event_id`` is comparable across processes and across restarts. Clients
  hold the highest ``event_id`` they have applied and send it back as their
  cursor.
* **Bounded replay with an explicit reset.** :func:`replay` returns at most
  ``limit`` events and reports a *gap* whenever it cannot prove the client's
  cursor is still covered - because the rows it needed were pruned, because
  the cursor is ahead of the store, or because the backlog exceeded the bound.
  A gap is signalled, never papered over with a short replay.
* **Idempotent publish.** ``dedupe_key`` is unique, so the same logical event
  published twice - a retry, two processes reacting to one mutation - yields
  one row, one ``event_id`` and one delivery.

What it deliberately does **not** do: cross-process fan-out. A publish in the
MCP or dashboard process is durable and will be picked up by any gateway
client that reconnects or polls its cursor, but it is not pushed to a socket
attached to another process. That limit is stated in ``docs/ARCHITECTURE.md``
rather than hidden behind an implied broker.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError

from brains.events.bus import bus
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import RealtimeEvent

#: How many rows the log keeps. A cursor older than the oldest retained row is
#: answered with a reset, so this bounds catch-up rather than truncating it
#: silently. ``0`` disables pruning.
RETENTION_ROWS_ENV = "BRAINS_REALTIME_RETENTION_ROWS"
DEFAULT_RETENTION_ROWS = 5_000

#: How many events one replay may deliver before it reports a gap instead.
REPLAY_LIMIT_ENV = "BRAINS_REALTIME_REPLAY_LIMIT"
DEFAULT_REPLAY_LIMIT = 500

#: Reasons a client is told to reset rather than to resume.
RESET_CURSOR_EXPIRED = "cursor_expired"
RESET_CURSOR_AHEAD = "cursor_ahead"
RESET_REPLAY_TRUNCATED = "replay_truncated"

#: SQLite serialises writers, so a busy store answers a concurrent publisher
#: with "database is locked" rather than with a queue. Persist-before-publish
#: means giving up there loses the event, so the insert is retried briefly.
_WRITE_ATTEMPTS = 6
_WRITE_BACKOFF_SECONDS = 0.05

#: Pruning is amortised rather than run on every insert: trimming inside every
#: publish would make the log its own write-contention source.
_PRUNE_EVERY = 128

#: Announcement order is part of the cursor contract, not a detail. ``event_id``
#: is assigned by the store, and a client holds the highest id it applied - so a
#: publisher that committed *later* must not announce *earlier*. Two concurrent
#: publishers can otherwise commit 96 then 97 and announce 97 then 96, which
#: carries every subscriber's cursor past 96 before 96 is delivered: a client
#: that drops in between resumes past an event it never saw. The commit and its
#: announcement are therefore one critical section per process.
_ANNOUNCE_LOCK = threading.Lock()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def retention_rows() -> int:
    return _env_int(RETENTION_ROWS_ENV, DEFAULT_RETENTION_ROWS)


def replay_limit() -> int:
    return max(1, _env_int(REPLAY_LIMIT_ENV, DEFAULT_REPLAY_LIMIT))


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _scalar(row: Any) -> int:
    """The first column of a one-column row, or ``0`` for an empty table."""
    return int(row[0]) if row else 0


def envelope_for(row: RealtimeEvent) -> dict[str, Any]:
    """The WS3 envelope for one stored event.

    ``event_id`` is the durable cursor and ``durable`` marks the frame as
    replayable, which is how a client knows whether advancing its cursor on
    this frame is meaningful.
    """
    created = row.created_at or datetime.now(UTC)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {
        "v": 1,
        "type": row.event_type,
        "entity": row.entity,
        "id": row.entity_id,
        "topic": row.topic,
        "ts": created.isoformat(),
        "payload": _loads(row.payload_json),
        "event_id": row.id,
        "durable": True,
        "org_id": row.org_id,
        "workspace_id": row.workspace_id,
    }


def record_event(
    topic: str,
    event_type: str,
    *,
    entity: str | None = None,
    entity_id: Any = None,
    org_id: int | None = None,
    workspace_id: int | None = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Commit one event and return ``(envelope, created)``.

    ``created`` is ``False`` when ``dedupe_key`` already existed: the caller
    then holds the *original* envelope, which is what makes a duplicate
    publish a no-op instead of a second delivery.

    A busy SQLite writer answers a concurrent publisher with "database is
    locked" rather than with a queue, so the insert is retried briefly -
    persist-before-publish means giving up there would lose the event.
    Retention is trimmed on an amortised schedule rather than inside every
    write, which would make the log its own source of write contention.
    """
    init_db()
    payload_json = json.dumps(payload or {}, default=str)
    entity_ref = None if entity_id is None else str(entity_id)[:64]
    last_error: Exception | None = None
    for attempt in range(_WRITE_ATTEMPTS):
        row = RealtimeEvent(
            topic=topic,
            event_type=event_type,
            entity=entity,
            entity_id=entity_ref,
            org_id=org_id,
            workspace_id=workspace_id,
            dedupe_key=dedupe_key,
            payload_json=payload_json,
            created_at=datetime.now(UTC),
        )
        with SessionLocal() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if not dedupe_key:
                    raise
                existing = (
                    session.query(RealtimeEvent)
                    .filter(RealtimeEvent.dedupe_key == dedupe_key)
                    .one_or_none()
                )
                if existing is None:  # pragma: no cover - the constraint said otherwise
                    raise
                return envelope_for(existing), False
            except OperationalError as exc:  # pragma: no cover - timing dependent
                session.rollback()
                last_error = exc
                time.sleep(_WRITE_BACKOFF_SECONDS * (attempt + 1))
                continue
            session.refresh(row)
            envelope = envelope_for(row)
            event_id = row.id
        if event_id % _PRUNE_EVERY == 0:
            prune()
        return envelope, True
    raise last_error if last_error else RuntimeError("realtime event write did not complete")


def publish_durable(
    topic: str,
    event_type: str,
    *,
    entity: str | None = None,
    entity_id: Any = None,
    org_id: int | None = None,
    workspace_id: int | None = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> dict[str, Any] | None:
    """Persist ``event`` and then announce it on the in-process bus.

    Best-effort at the *announcement*, never at the record: a store failure
    returns ``None`` and publishes nothing, so nobody is told about a state
    change that did not commit. A duplicate (same ``dedupe_key``) commits
    nothing and announces nothing, and returns the original envelope.

    The commit and the announcement are one critical section, so subscribers
    see events in the same order the store assigned their ids. Announcing out
    of order would be indistinguishable from a lost event to a client whose
    cursor is the highest id it applied.
    """
    with _ANNOUNCE_LOCK:
        try:
            envelope, created = record_event(
                topic,
                event_type,
                entity=entity,
                entity_id=entity_id,
                org_id=org_id,
                workspace_id=workspace_id,
                payload=payload,
                dedupe_key=dedupe_key,
            )
        except Exception:
            return None
        if created:
            # Best-effort at the announcement, and inside the lock: a delivery
            # that raised still committed, so the caller is told what its
            # event_id is rather than that nothing happened.
            with contextlib.suppress(Exception):
                bus.deliver(envelope)
    return envelope


@dataclass(frozen=True)
class ReplayResult:
    """The outcome of a catch-up read.

    ``gap`` means the client must resynchronise from the REST entity reads
    before trusting the stream again; ``cursor`` is where it should resume
    from either way.
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    cursor: int = 0
    gap: bool = False
    reason: str | None = None


def latest_event_id() -> int:
    init_db()
    with SessionLocal() as session:
        return _scalar(session.query(RealtimeEvent.id).order_by(RealtimeEvent.id.desc()).first())


def earliest_event_id() -> int:
    init_db()
    with SessionLocal() as session:
        return _scalar(session.query(RealtimeEvent.id).order_by(RealtimeEvent.id.asc()).first())


def replay(
    topics: list[str] | set[str],
    after_id: int | None,
    *,
    limit: int | None = None,
    org_ids: set[int] | None = None,
    workspace_ids: set[int] | None = None,
) -> ReplayResult:
    """Events after ``after_id`` on ``topics``, bounded, with gap detection.

    ``org_ids``/``workspace_ids`` are the *subscriber's* authorized scope and
    are applied as defence in depth: an event whose recorded scope is outside
    it is dropped even if it was published on a topic the subscriber holds.
    ``None`` means "no filter" and is only ever passed for a principal that may
    read every Org (the bootstrap admin).
    """
    wanted = sorted({t for t in topics if t})
    bound = replay_limit() if limit is None else max(1, limit)
    init_db()
    # One session for the bounds *and* the rows: a cursor computed from a
    # different snapshot than the replay would either skip an event or replay
    # one twice on the next resume.
    with SessionLocal() as session:
        newest = _scalar(session.query(RealtimeEvent.id).order_by(RealtimeEvent.id.desc()).first())
        if after_id is None:
            # No cursor: the caller asked to start live, so the store's newest
            # id is exactly where it resumes from.
            return ReplayResult(events=[], cursor=newest)
        if after_id < 0 or after_id > newest:
            # Negative, or ahead of the store: a restored or rebuilt database,
            # or a cursor minted by a different install. Resynchronise.
            return ReplayResult(events=[], cursor=newest, gap=True, reason=RESET_CURSOR_AHEAD)
        earliest = _scalar(session.query(RealtimeEvent.id).order_by(RealtimeEvent.id.asc()).first())
        gap = False
        reason: str | None = None
        if earliest and after_id + 1 < earliest:
            gap = True
            reason = RESET_CURSOR_EXPIRED
        if not wanted:
            return ReplayResult(
                events=[], cursor=newest if gap else after_id, gap=gap, reason=reason
            )
        rows = list(
            session.query(RealtimeEvent)
            .filter(RealtimeEvent.topic.in_(wanted))
            .filter(RealtimeEvent.id > after_id)
            .order_by(RealtimeEvent.id.asc())
            .limit(bound + 1)
        )
        truncated = len(rows) > bound
        rows = rows[:bound]
        events = [
            envelope_for(row)
            for row in rows
            if _in_scope(row.org_id, row.workspace_id, org_ids, workspace_ids)
        ]
    # The cursor never runs ahead of what this caller was actually handed. The
    # store's newest id may name an event on a topic the caller does not hold,
    # and reporting that would silently retire events it never received.
    cursor = rows[-1].id if rows else after_id
    if truncated:
        return ReplayResult(events=events, cursor=cursor, gap=True, reason=RESET_REPLAY_TRUNCATED)
    if gap and not rows:
        # The cursor is older than anything retained and nothing on these
        # topics survived it. Handing the same dead cursor back would only
        # expire again on the next resume, so the client resynchronises from
        # REST and continues from where the store actually is.
        cursor = newest
    return ReplayResult(events=events, cursor=cursor, gap=gap, reason=reason)


def _in_scope(
    org_id: int | None,
    workspace_id: int | None,
    org_ids: set[int] | None,
    workspace_ids: set[int] | None,
) -> bool:
    if org_ids is not None and org_id is not None and org_id not in org_ids:
        return False
    return not (
        workspace_ids is not None and workspace_id is not None and workspace_id not in workspace_ids
    )


def prune() -> int:
    """Trim the log to :func:`retention_rows`, oldest first; return the count.

    Called on an amortised schedule from :func:`record_event` and directly by
    operators/tests. Never raises: a retention failure must not break a write.
    """
    keep = retention_rows()
    if keep <= 0:
        return 0
    try:
        with SessionLocal() as session:
            newest = _scalar(
                session.query(RealtimeEvent.id).order_by(RealtimeEvent.id.desc()).first()
            )
            floor = newest - keep
            if floor <= 0:
                return 0
            deleted = (
                session.query(RealtimeEvent)
                .filter(RealtimeEvent.id <= floor)
                .delete(synchronize_session=False)
            )
            if deleted:
                session.commit()
            else:
                session.rollback()
            return int(deleted or 0)
    except Exception:  # pragma: no cover - pruning must never break a write
        return 0


__all__ = [
    "DEFAULT_REPLAY_LIMIT",
    "DEFAULT_RETENTION_ROWS",
    "RESET_CURSOR_AHEAD",
    "RESET_CURSOR_EXPIRED",
    "RESET_REPLAY_TRUNCATED",
    "ReplayResult",
    "earliest_event_id",
    "envelope_for",
    "latest_event_id",
    "prune",
    "publish_durable",
    "record_event",
    "replay",
    "replay_limit",
    "retention_rows",
]

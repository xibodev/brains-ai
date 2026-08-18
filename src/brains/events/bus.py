"""The in-process realtime notifier (WS3 §3.4), scoped at delivery.

Design notes
------------
* **Topic-addressed.** Producers ``publish(topic, type, ...)``; subscribers hold
  a :class:`Subscription` bound to a set of topics and ``await`` envelopes.
* **Scoped at delivery.** A subscription carries the Org/Workspace scope its
  principal was authorized for, and an envelope whose recorded scope falls
  outside it is dropped even though its topic matched. Topic authorization is
  the gate; this is the defence in depth behind it, so a producer that put an
  Org A payload on an Org B topic still cannot reach an Org B subscriber.
* **Thread-safe across the sync/async boundary.** FastAPI runs ``def`` route
  handlers in a worker thread, but the WebSocket handler's :class:`asyncio.Queue`
  lives on the event-loop thread. A subscription captures the loop it was created
  on and delivers via :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`, so a
  synchronous ``publish`` from a route thread safely wakes the WS coroutine.
* **Best-effort.** A delivery failure to one subscriber never breaks ``publish``
  for the others, and ``publish`` never raises into a write path.
* **Notification, not record.** This bus is process-local and holds nothing.
  Events whose loss a user would notice are committed by
  :mod:`brains.events.store` *before* they are announced here, and are replayed
  from there by cursor on reconnect. What this module provides is the low
  latency, not the durability.
* **WS3 envelope.** Every published message is the canonical frame
  ``{v, type, entity, id, topic, ts, payload, seq}``, extended with
  ``event_id``/``durable`` (the durable cursor, ``None``/``False`` for a
  notification-only event) and the ``org_id``/``workspace_id`` the publisher
  resolved.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SubscriptionScope:
    """The Org/Workspace scope a subscription's principal may receive.

    ``None`` means "no filter" and is only ever built for a principal that may
    read every Org (the bootstrap admin). An empty set means "nothing", which
    is the correct answer for a principal with no memberships.
    """

    org_ids: frozenset[int] | None = None
    workspace_ids: frozenset[int] | None = None

    def allows(self, envelope: dict[str, Any]) -> bool:
        org_id = envelope.get("org_id")
        workspace_id = envelope.get("workspace_id")
        if self.org_ids is not None and org_id is not None and org_id not in self.org_ids:
            return False
        return not (
            self.workspace_ids is not None
            and workspace_id is not None
            and workspace_id not in self.workspace_ids
        )


#: The scope of a subscription that has not been authorized against a
#: principal at all (bus unit tests, internal fan-out): no filter.
UNSCOPED = SubscriptionScope()


class Subscription:
    """A live subscription to a set of topics, draining via :meth:`get`."""

    def __init__(
        self,
        bus: EventBus,
        topics: Iterable[str],
        scope: SubscriptionScope = UNSCOPED,
    ):
        self._bus = bus
        self.topics: set[str] = set(topics)
        self.scope = scope
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # -- delivery (called by the bus, possibly from another thread) --------- #
    def _deliver(self, envelope: dict[str, Any]) -> None:
        if not self.scope.allows(envelope):
            return
        loop = self._loop
        if loop is not None and loop.is_running():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self.queue.put_nowait, envelope)
        else:
            self.queue.put_nowait(envelope)

    # -- consumer API ------------------------------------------------------- #
    async def get(self) -> dict[str, Any]:
        return await self.queue.get()

    def add(self, topics: Iterable[str]) -> None:
        self._bus._mutate(self, add=set(topics))

    def remove(self, topics: Iterable[str]) -> None:
        self._bus._mutate(self, remove=set(topics))

    def set_scope(self, scope: SubscriptionScope) -> None:
        """Replace the delivery scope, e.g. after a re-authorization."""
        self._bus._mutate(self, scope=scope)

    def replace_topics(self, topics: Iterable[str]) -> None:
        """Set the topic set outright, e.g. after a re-authorization."""
        self._bus._mutate(self, replace=set(topics))

    def close(self) -> None:
        self._bus._remove(self)

    # Context-manager sugar so handlers can ``with bus.subscribe(...) as sub:``.
    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class EventBus:
    """A single-process topic pub/sub fan-out."""

    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._lock = threading.Lock()
        self._seq = count(1)

    def subscribe(
        self,
        topics: Iterable[str] = (),
        scope: SubscriptionScope = UNSCOPED,
    ) -> Subscription:
        sub = Subscription(self, topics, scope)
        with self._lock:
            self._subs.add(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)

    def _mutate(
        self,
        sub: Subscription,
        *,
        add: set[str] | None = None,
        remove: set[str] | None = None,
        replace: set[str] | None = None,
        scope: SubscriptionScope | None = None,
    ) -> None:
        with self._lock:
            if replace is not None:
                sub.topics = set(replace)
            if add:
                sub.topics |= add
            if remove:
                sub.topics -= remove
            if scope is not None:
                sub.scope = scope

    def deliver(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Fan out an already-built envelope (e.g. one read back from the log).

        Never raises: a misbehaving subscriber cannot break a producer.
        """
        topic = envelope.get("topic")
        if "seq" not in envelope:
            # ``setdefault`` would consume a sequence number even when the
            # envelope already carries one, leaving gaps in the counter.
            envelope["seq"] = next(self._seq)
        with self._lock:
            targets = [s for s in self._subs if topic in s.topics]
        for sub in targets:
            with contextlib.suppress(Exception):
                sub._deliver(envelope)
        return envelope

    def publish(
        self,
        topic: str,
        type: str,
        *,
        entity: str | None = None,
        id: Any = None,
        payload: dict[str, Any] | None = None,
        ref: str | None = None,
        org_id: int | None = None,
        workspace_id: int | None = None,
    ) -> dict[str, Any]:
        """Announce one notification-only envelope to ``topic``'s subscribers.

        Returns the envelope (handy for tests). Never raises — a misbehaving
        subscriber can't break a producer's write path. Events that must
        survive a disconnect go through
        :func:`brains.events.store.publish_durable` instead, which commits
        first and then calls :meth:`deliver`.
        """
        envelope: dict[str, Any] = {
            "v": 1,
            "type": type,
            "entity": entity,
            "id": id,
            "topic": topic,
            "ts": _now_iso(),
            "payload": payload or {},
            "seq": next(self._seq),
            "event_id": None,
            "durable": False,
            "org_id": org_id,
            "workspace_id": workspace_id,
        }
        if ref is not None:
            envelope["ref"] = ref
        return self.deliver(envelope)

    def subscriber_count(self, topic: str | None = None) -> int:
        """Diagnostics: total subscriptions, or those listening on ``topic``."""
        with self._lock:
            if topic is None:
                return len(self._subs)
            return sum(1 for s in self._subs if topic in s.topics)


# The process-wide singleton every producer + the WS/SSE handlers share.
bus = EventBus()


def publish(topic: str, type: str, **kwargs: Any) -> dict[str, Any] | None:
    """Module-level best-effort publish helper.

    Wraps :meth:`EventBus.publish` so producers can fire-and-forget without ever
    risking an exception leaking into a write path.
    """
    try:
        return bus.publish(topic, type, **kwargs)
    except Exception:
        return None

"""WebSocket + SSE realtime transports (WS3 §3), scoped and resumable.

``GET /v1/ws`` upgrades to a WebSocket; ``GET /v1/events`` is the SSE fallback
(WS3 §3.5). FastAPI's ``Depends`` HTTP gate can't run on a WS upgrade, so auth
is explicit here but **reuses the same principal resolver** as every other
surface — no separate bypass. On auth failure the socket closes with policy
code ``4401``.

Three properties this module is responsible for (BL-P0-02):

**Subscriptions are server-derived, not client-chosen.** A client names a
topic; :func:`brains.authz.policy.resolve_topic` parses it against the closed
grammar in :mod:`brains.events.topics`, resolves the entity it names against
the store, and returns the *canonical* topic plus the Org/Workspace scope it
carries — or nothing. Malformed, wildcard, unknown-family, unknown-entity and
cross-Org topics are refused identically, so a subscription can never be used
to discover that something exists. The ack reports the derived topic, and the
connection's delivery scope is applied to every frame as defence in depth.

**Both transports resume by cursor, and the cursor follows delivery.** Durable
events carry a monotonic ``event_id`` (:mod:`brains.events.store`). A client
reconnects with the highest id it applied — ``cursor`` on the WS ``subscribe``
message, ``cursor``/``Last-Event-ID`` on SSE — and receives a bounded replay of
what it missed. The ack is written *before* those frames, so it never reports a
cursor past them: the catch-up frames advance the client's cursor themselves,
one applied event at a time, and a trailing ``replay_complete`` receipt — sent
only after every frame in the batch was written — confirms the rest. A replay
interrupted halfway therefore leaves the client holding the id of the last
event it actually applied, and the reconnect delivers the remainder instead of
skipping it. If the cursor can no longer be honoured (pruned, ahead of the
store, or a backlog larger than the replay bound) the client is told to reset
rather than handed a silently short stream.

**A catch-up batch is delivered whole, before any live frame that follows it.**
The cursor rule above only holds if the frames arrive in cursor order, and they
do not by themselves: the subscription is registered on the bus *before* the
replay snapshot is read (otherwise an event published in between would be lost
by both paths), so live fan-out and the batch are in flight at the same time. A
live event slipped between two replay frames carries a *higher* ``event_id``
than the frames still to come, so a client that applied it and then dropped
would reconnect past the remainder — the exact hole ``replay_complete`` exists
to close. Delivery is therefore serialised per connection (:class:`_Delivery`):
a replay phase opens before the subscription is registered and closes after the
receipt is written, and live frames queue behind it instead of interleaving.
Nothing is lost, because the queue holds them, and nothing is duplicated,
because a live frame whose ``event_id`` the batch already carried is dropped.
Replay and live delivery still overlap *across* batches — a ``resync`` re-sends
what a client was already handed — so a client applies each event at most once
by ``event_id``.

That suppression is scoped to the topic the batch actually carried, and to the
ids it actually sent. A connection subscribes incrementally: a console that is
already live on ``org/7/issues`` adds ``session/AS-9/stdout`` later, and the
catch-up for the new topic routinely carries ids far above the events queued
for the old one. A single watermark would read those as "already delivered" and
drop them — an event lost on a topic whose subscription never changed, which no
reconnect recovers because the client's cursor has moved past it. The same
reason bounds the receipt: a batch that did not cover every topic this
connection holds hands over no cursor at all. The client keeps one cursor for
the whole connection, so any number a partial batch offered would retire the
live frames still queued for the topics it never read - frames whose ids are
routinely *below* everything the batch carried. Such a receipt reports its
high-water mark as ``batch_cursor``, which is reporting and not permission; the
handed-over ``cursor`` is ``null``. The ack says which kind of batch is coming
(``covers_connection``), because the same bound applies to the frames
themselves: their ids sit above the queued ones, so a client may apply them and
still not resume from them. The first full-coverage batch — any reconnect, any
``resync``, every SSE stream — settles the difference.

**Authorization is re-checked, not assumed for the life of the socket, and it
fails closed.** Every client message re-resolves the credential, and a
background revalidation loop does the same on a timer, so a revoked or expired
credential loses the stream promptly instead of at its next reconnect. Topics
are re-authorized at the same time: a membership removed mid-connection drops
the topics it granted. A check that cannot be *completed* — the store is
unavailable, a policy read raises — is not evidence that the credential still
holds, so it closes the connection (``4401``) rather than leaving a stream
running behind a check nobody is performing.

**Every database read happens off the event loop.** Credential resolution,
topic derivation, the Org/Workspace visibility set and replay reads are all
blocking store work, on connection setup as much as on revalidation. One
connection's slow read must not stall every other socket the process serves.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect

from brains.api.auth import BROWSER_AUTH_COOKIE, resolve_browser_cookie
from brains.authz import policy
from brains.authz.principal import Principal
from brains.authz.resolver import bootstrap_principal, principal_for_secret
from brains.config import settings
from brains.events import store as event_store
from brains.events import topics as topic_grammar
from brains.events.bus import bus

router = APIRouter(prefix="/v1")

WS_CLOSE_POLICY_VIOLATION = 4401

#: The credential itself stopped holding: reconnecting with it changes nothing.
REVOKED_CREDENTIAL = "credential_revoked"
#: Some topics went away with a membership; the connection is still usable.
REVOKED_SCOPE = "scope_revoked"
#: The re-authorization could not be *performed*. The credential may well still
#: be good, but nobody proved it, so the stream stops and the client reconnects
#: into a fresh check rather than streaming behind an unanswered question.
REVOKED_REVALIDATION_FAILED = "revalidation_failed"

#: Sent after the last frame of a catch-up batch: "everything up to this cursor
#: has been handed over". A replay that dies mid-batch never emits one.
REPLAY_COMPLETE_TYPE = "replay_complete"

#: How many delivered ``event_id``s one connection remembers per topic, to drop
#: the live copy of an event its catch-up batch already carried. A single batch
#: is bounded by the replay limit; this bounds a connection that resyncs
#: indefinitely.
MAX_SUPPRESSED_IDS_PER_TOPIC = 4096

#: How often a live connection re-resolves its credential and re-authorizes
#: its topics. Bounds how long a revoked credential keeps streaming.
REVALIDATE_ENV = "BRAINS_REALTIME_REVALIDATE_SECONDS"
DEFAULT_REVALIDATE_SECONDS = 10.0

#: SSE keep-alive cadence. A tick is also when a stream re-checks its
#: credential, so the effective idle wait is the smaller of this and the
#: revalidation interval.
SSE_TICK_SECONDS = 5.0


def revalidate_seconds() -> float:
    raw = os.environ.get(REVALIDATE_ENV, "").strip()
    if not raw:
        return DEFAULT_REVALIDATE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REVALIDATE_SECONDS
    return value if value > 0 else DEFAULT_REVALIDATE_SECONDS


def _ws_token(websocket: WebSocket) -> str | None:
    params = websocket.query_params
    token = params.get("access_token") or params.get("key")
    if token:
        return token
    authz = websocket.headers.get("authorization")
    if authz:
        scheme, _, value = authz.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value
    return None


def _ws_principal(websocket: WebSocket) -> Principal | None:
    """Resolve a WS upgrade to the same principal the HTTP gate would.

    Called on the upgrade *and* on every revalidation, so a credential that is
    revoked, expired or unbound mid-connection resolves to ``None`` and the
    socket is closed.
    """
    if settings.allow_unauthenticated_api:
        return bootstrap_principal()
    token = _ws_token(websocket)
    if token:
        return principal_for_secret(token)
    cookie = websocket.cookies.get(BROWSER_AUTH_COOKIE)
    if cookie:
        raw_key = resolve_browser_cookie(cookie)
        if raw_key:
            return principal_for_secret(raw_key)
    return None


def _resolve_ws_principal(websocket: WebSocket) -> Principal | None:
    """The operator principal for this socket, or ``None`` if it has none.

    Folds the two refusals a transport must not distinguish - no valid
    credential, and a Runtime credential, which is refused the operator
    realtime surface outright - into one answer.

    A failure of the credential *store* is deliberately not folded in: it is
    not an answer at all, and reporting it as "revoked" would tell a console to
    stop reconnecting over a transient database error. It is raised, and the
    caller decides - which in every case means closing the connection, but for
    the honest reason.
    """
    principal = _ws_principal(websocket)
    return None if principal is None or principal.is_runtime else principal


def _as_cursor(raw: object) -> int | None:
    """Parse a client cursor; ``None`` means "start live, no replay"."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _reset_frame(result: event_store.ReplayResult, topics: list[str]) -> dict[str, Any]:
    return {
        "type": "realtime.reset",
        "payload": {
            "reason": result.reason,
            "cursor": result.cursor,
            "topics": topics,
        },
    }


def _revoked_frame(reason: str, *, topics: list[str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"reason": reason}
    if topics is not None:
        payload["topics"] = topics
    return {"type": "realtime.revoked", "payload": payload}


def _pre_replay_cursor(requested: int | None, result: event_store.ReplayResult) -> int:
    """What a client may resume from *before* one replay frame has been sent.

    The ack goes out ahead of the catch-up batch, so the cursor it carries has
    to be true at that moment. Two cases can move it: a client that sent no
    cursor asked to start live and is missing nothing by definition, and a gap
    is answered with ``realtime.reset``, which carries its own cursor and tells
    the client to re-read over REST anyway. Otherwise the only honest answer is
    the cursor the client already had - the replay frames advance it themselves.
    """
    if requested is None or result.gap:
        return result.cursor
    return requested


def _replay_complete_frame(
    result: event_store.ReplayResult,
    topics: list[str],
    *,
    cursor: int | None,
    covers_connection: bool = True,
    with_event_id: bool = False,
) -> dict[str, Any]:
    """The receipt for a fully delivered catch-up batch.

    ``cursor`` is the resume point the batch *hands over*, and it is a number
    only where the batch spoke for the whole connection (see
    :func:`_receipt_cursor`). It then also carries the client past ids it will
    never be sent - rows dropped by its own delivery scope - which would
    otherwise stall its cursor and be re-read on every reconnect. A partial
    batch reports ``cursor: null`` and puts what it wrote in ``batch_cursor``,
    which is informational: a client that adopted it would retire live frames
    still queued for the topics the batch did not read. ``covers_connection``
    is on the wire so the client can tell the two receipts apart rather than
    infer it. ``with_event_id`` puts the handed-over cursor in the SSE ``id:``
    field, which is what a browser ``EventSource`` resumes from.
    """
    payload: dict[str, Any] = {
        "cursor": cursor,
        "topics": topics,
        "count": len(result.events),
        "covers_connection": covers_connection,
    }
    if cursor is None:
        payload["batch_cursor"] = _replay_high_water(result)
    frame: dict[str, Any] = {"type": REPLAY_COMPLETE_TYPE, "payload": payload}
    if with_event_id and cursor is not None:
        frame["event_id"] = cursor
    return frame


async def _offload(fn: Callable[[], Any]) -> Any:
    """Run one synchronous authorization/replay read off the event loop.

    Resolving a credential, resolving a topic, rebuilding Workspace visibility
    and reading the replay log are all blocking database work. A connection
    does them when it is set up, on every client message and on a timer, so
    leaving any of them inline would let one socket stall the loop every other
    socket shares.
    """
    return await run_in_threadpool(fn)


def _replay_delivered(result: event_store.ReplayResult) -> dict[str, set[int]]:
    """The ``event_id``s a catch-up batch actually handed over, per topic.

    Deliberately *not* ``result.cursor``, and deliberately not one number for
    the whole connection. The reported cursor runs ahead of the frames over ids
    this connection's own delivery scope dropped, and those events are not
    deliverable live either - but a batch for one topic is no evidence at all
    about another. Subscriptions are added one at a time, so a catch-up for a
    newly held topic normally carries ids well above the live events queued for
    a topic that was already being delivered; a shared watermark would read
    those as duplicates and drop them for good.
    """
    delivered: dict[str, set[int]] = {}
    for envelope in result.events:
        topic = envelope.get("topic")
        event_id = envelope.get("event_id")
        if isinstance(topic, str) and isinstance(event_id, int):
            delivered.setdefault(topic, set()).add(event_id)
    return delivered


def _replay_high_water(result: event_store.ReplayResult) -> int:
    """The highest ``event_id`` the batch actually wrote, or ``0`` for none."""
    highest = 0
    for envelope in result.events:
        event_id = envelope.get("event_id")
        if isinstance(event_id, int) and event_id > highest:
            highest = event_id
    return highest


def _receipt_cursor(result: event_store.ReplayResult, *, covers_connection: bool) -> int | None:
    """The cursor a completed batch may hand the client, or ``None`` for none.

    A client holds one cursor for the whole connection, so a receipt retires
    every id below it on *every* topic it holds. ``result.cursor`` is only safe
    to report when the batch covered all of them: then an id below it was
    either in the batch or dropped by this connection's scope, and neither is
    owed to the client.

    A partial batch - the usual shape of an incremental ``subscribe`` - hands
    over nothing at all. It is racing live events on the topics it did not
    read, and those events are queued *behind* it: their ids are routinely
    below everything the batch carried, so even the batch's own high-water mark
    would retire frames that have not been written yet. A client that adopted
    it and then dropped would reconnect past them for good. The high-water mark
    still travels on the receipt as ``batch_cursor``, as reporting rather than
    permission, and the client's cursor stays where its delivered frames left
    it. The cost is that ids the scope dropped are re-read on the next resume,
    which the first full-coverage batch (any reconnect, any ``resync``) settles
    for good; the alternative is losing them.
    """
    return result.cursor if covers_connection else None


def _superseded(
    frame: dict[str, Any],
    *,
    topics: set[str] | frozenset[str],
    delivered: dict[str, set[int]],
) -> bool:
    """True when a queued live frame must not be written to this connection.

    Two reasons, both of which only arise because delivery is queued rather
    than immediate. A frame whose topic was unsubscribed or lost with a
    membership while it sat in the queue is no longer this connection's to see.
    A durable frame a replay batch already carried *on that same topic* is the
    overlap between the snapshot and the live fan-out - the store commits
    *then* announces, so an event can be both in the batch and in the queue -
    and sending it again would hand the client the same ``event_id`` twice.

    The second test is by exact id and by topic, never by a watermark: an event
    on another topic, or one below the batch's ids that the batch did not
    contain, has not been delivered by anybody and is not this connection's to
    drop.
    """
    topic = frame.get("topic")
    if topic is not None and topic not in topics:
        return True
    if not frame.get("durable"):
        return False
    event_id = frame.get("event_id")
    if not isinstance(event_id, int) or not isinstance(topic, str):
        # Unattributable: sending it again is a duplicate the client drops by
        # ``event_id``; suppressing it would be a loss nobody resends.
        return False
    return event_id in delivered.get(topic, frozenset())


class _Delivery:
    """One socket's writer: replay batches and live frames, never interleaved.

    A connection has two writers - the live pump and whatever the client just
    asked for - and ordering between them is a correctness property, not a
    cosmetic one. ``send`` is used for control frames and for the frames of a
    catch-up batch; ``send_live`` is used by the pump and additionally waits
    for any open replay phase, so a batch is written whole and the live frames
    that arrived while it was being read and sent follow it in cursor order.

    The gate is only ever *held* by the task writing a batch and only ever
    *awaited* by the pump, and it is awaited before the send lock rather than
    behind it, so a replay can never be waiting on a pump that is waiting on
    the replay. The phase is released in a ``finally``, so a cancelled or
    failed replay reopens live delivery instead of stranding it.
    """

    def __init__(self, websocket: WebSocket, connection: _Connection) -> None:
        self._websocket = websocket
        self._connection = connection
        self._lock = asyncio.Lock()
        self._live_open = asyncio.Event()
        self._live_open.set()
        self._replays = 0
        self._delivered: dict[str, set[int]] = {}
        self._seq = 0

    async def send(self, frame: dict[str, Any]) -> None:
        """Write one control or replay frame, whole."""
        async with self._lock:
            await self._websocket.send_json(frame)

    @contextlib.asynccontextmanager
    async def replay_phase(self):
        """Hold live delivery for the duration of one catch-up batch."""
        self._replays += 1
        self._live_open.clear()
        try:
            yield self
        finally:
            self._replays = max(0, self._replays - 1)
            if self._replays == 0:
                self._live_open.set()

    def covered(self, result: event_store.ReplayResult, topics: list[str] | None = None) -> None:
        """Record what the batch handed over, per topic, so it is not resent.

        Only the ids that were actually written, and only under the topic they
        were written for: a batch read for one topic must never suppress a
        queued live event on another.

        A batch that reported a gap starts its topics' records over. The client
        is being told to resynchronise, and ids minted before a prune - or by a
        different install of the store - are no evidence about what this
        connection has been handed since.
        """
        delivered = _replay_delivered(result)
        if result.gap:
            for topic in delivered if topics is None else topics:
                self._delivered.pop(topic, None)
        for topic, event_ids in delivered.items():
            seen = self._delivered.setdefault(topic, set())
            seen |= event_ids
            if len(seen) > MAX_SUPPRESSED_IDS_PER_TOPIC:
                # Bounded per topic, for a connection that resyncs all day.
                # The *newest* ids are kept: they are the ones a live copy can
                # still be queued for. Forgetting one only risks a duplicate,
                # which a client drops by ``event_id``; keeping the wrong ones
                # would risk a loss, which nothing recovers.
                self._delivered[topic] = set(sorted(seen)[-MAX_SUPPRESSED_IDS_PER_TOPIC:])

    def forget(self, topics: list[str]) -> None:
        """Drop the record for topics this connection no longer holds.

        A topic that is unsubscribed, or lost with a membership, may come back
        - and when it does its catch-up is read fresh. Keeping the old ids
        would let a re-subscription suppress live frames on the strength of a
        batch sent to a subscription that no longer exists.
        """
        for topic in topics:
            self._delivered.pop(topic, None)

    async def send_live(self, frame: dict[str, Any]) -> bool:
        """Write one live frame once no replay is in flight; ``False`` if dropped.

        The gate is re-checked under the send lock: it is cleared by another
        task, so a phase can begin between the wait returning and the lock
        being taken, and writing then would put a live frame inside a batch.
        """
        while True:
            await self._live_open.wait()
            async with self._lock:
                if not self._live_open.is_set():
                    continue
                if _superseded(
                    frame,
                    topics=set(self._connection.grants),
                    delivered=self._delivered,
                ):
                    return False
                self._seq += 1
                frame["seq"] = self._seq
                await self._websocket.send_json(frame)
                return True


class _Connection:
    """One authorized realtime connection: topics, scope, cursor, principal.

    ``cursor`` is the delivery watermark: the highest ``event_id`` this
    connection has actually finished handing to its client, never the highest
    the store holds.
    """

    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self.grants: dict[str, policy.TopicGrant] = {}
        self.scope = policy.subscription_scope(principal)
        self.cursor = 0

    @property
    def topics(self) -> list[str]:
        return sorted(self.grants)

    def add(self, grants: list[policy.TopicGrant]) -> list[policy.TopicGrant]:
        """Hold ``grants``, up to the per-connection bound; return what was held."""
        held: list[policy.TopicGrant] = []
        for grant in grants:
            if (
                grant.topic not in self.grants
                and len(self.grants) >= topic_grammar.MAX_TOPICS_PER_CONNECTION
            ):
                continue
            self.grants[grant.topic] = grant
            held.append(grant)
        return held

    def remove(self, topics: list[str]) -> list[str]:
        removed = []
        for topic in topics:
            if topic in self.grants:
                del self.grants[topic]
                removed.append(topic)
        return removed

    def adopt(self, principal: Principal) -> None:
        """Take the freshly resolved credential without re-deriving anything.

        Called on every client message. Resolving the credential is one lookup
        and must happen that often; re-deriving every held topic and rebuilding
        the Workspace visibility set is not, so that is the timer's job and the
        job of an explicit ``subscribe``.
        """
        self.principal = principal

    def refresh_scope(self) -> None:
        self.scope = policy.subscription_scope(self.principal)

    def reauthorize(self, principal: Principal) -> list[str]:
        """Re-derive every held topic for ``principal``; return what was lost."""
        self.principal = principal
        self.refresh_scope()
        lost: list[str] = []
        for topic, grant in list(self.grants.items()):
            regrant = policy.resolve_topic(principal, grant.requested)
            if regrant is None or regrant.topic != topic:
                del self.grants[topic]
                lost.append(topic)
        return lost

    def replay(self, topics: list[str], cursor: int | None) -> event_store.ReplayResult:
        return event_store.replay(
            topics,
            cursor,
            org_ids=None if self.scope.org_ids is None else set(self.scope.org_ids),
            workspace_ids=(
                None if self.scope.workspace_ids is None else set(self.scope.workspace_ids)
            ),
        )


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    try:
        principal = await _offload(lambda: _resolve_ws_principal(websocket))
    except Exception:
        # The store could not answer. An unproven credential is refused the
        # same way an absent one is; only the client's retry differs.
        principal = None
    if principal is None:
        # A Runtime credential streams its own execution over the REST
        # ingest route; the operator realtime surface is refused to it.
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return
    await websocket.accept()
    # Building the connection resolves the principal's Org/Workspace visibility
    # against the store, so it is offloaded like every other read here rather
    # than run on the loop just because it happens during setup.
    try:
        connection: _Connection = await _offload(lambda: _Connection(principal))
    except Exception:
        # Scope this connection could not be established is scope it does not
        # have. Refuse now rather than accept a socket with an unknown one.
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return
    sub = bus.subscribe([], connection.scope)
    # Live fan-out and the request handler both write to this socket, and a
    # replay batch has to reach the client before the live frames that were
    # published while it was being read. One writer owns both orderings.
    delivery = _Delivery(websocket, connection)
    # Re-authorization and message handling both read and rewrite the
    # connection's grants, so they take turns rather than interleave.
    auth_lock = asyncio.Lock()

    async def _send(frame: dict[str, Any]) -> None:
        await delivery.send(frame)

    async def _close_policy(reason: str | None) -> None:
        """End this connection: say why if it can still be said, then close.

        Returning from any of the three tasks tears the whole connection down,
        so this is the one place a refusal has to write to the socket.
        """
        if reason is not None:
            with contextlib.suppress(Exception):
                await _send(_revoked_frame(reason))
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)

    async def _pump() -> None:
        """Fan live envelopes onto the socket until it stops accepting them."""
        try:
            while True:
                envelope = await sub.get()
                await delivery.send_live(dict(envelope))
        except asyncio.CancelledError:
            raise
        except Exception:
            # The socket is gone, or a send raised. Returning ends the whole
            # connection; a pump that died quietly would leave a live socket
            # that no longer receives anything and never says so.
            return

    async def _revalidate() -> None:
        """Drop the socket when its credential or its scope stops holding."""
        interval = revalidate_seconds()
        while True:
            await asyncio.sleep(interval)
            try:
                async with auth_lock:
                    current = await _offload(lambda: _resolve_ws_principal(websocket))
                    if current is None:
                        await _close_policy(REVOKED_CREDENTIAL)
                        return
                    lost = await _offload(functools.partial(connection.reauthorize, current))
                    delivery.forget(lost)
                    sub.set_scope(connection.scope)
                    sub.replace_topics(connection.topics)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The check itself failed - the store is unavailable, a policy
                # read raised. That is not evidence the credential still holds,
                # and a revoked or unknown scope must not keep streaming
                # because the question could not be asked. Fail closed.
                await _close_policy(REVOKED_REVALIDATION_FAILED)
                return
            if lost:
                with contextlib.suppress(Exception):
                    await _send(_revoked_frame(REVOKED_SCOPE, topics=lost))

    async def _serve() -> None:
        """Handle client messages until the socket or its authorization ends."""
        while True:
            try:
                message = await websocket.receive_json()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A closed socket or a frame that is not JSON: either way this
                # connection has nothing more to say.
                return
            try:
                async with auth_lock:
                    # One credential lookup per message: a revoked key stops
                    # being obeyed at its next word, not at its next reconnect.
                    current = await _offload(lambda: _resolve_ws_principal(websocket))
                    if current is None:
                        await _close_policy(None)
                        return
                    connection.adopt(current)
                    await _handle_client_message(delivery, sub, message, connection)
            except asyncio.CancelledError:
                raise
            except WebSocketDisconnect:
                return
            except Exception:
                # A handler that could not complete leaves this connection's
                # authorization unproven for the same reason the timer's
                # failure does. Close rather than carry on.
                await _close_policy(REVOKED_REVALIDATION_FAILED)
                return

    tasks = [asyncio.create_task(coro) for coro in (_pump(), _revalidate(), _serve())]
    try:
        # Whichever ends first ends the connection: a closed socket, a failed
        # check and a dead pump are all reasons to stop, not to keep two of
        # three halves running.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        # Awaiting the cancellations is what makes "the stream stopped" true. A
        # revalidation task left pending would go on re-authorizing - and
        # sending on - a socket this endpoint has already given up. If this
        # endpoint is itself being torn down mid-wait, the cancellations have
        # already been requested and stand on their own.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        sub.close()


async def _handle_client_message(
    delivery: _Delivery,
    sub: Any,
    message: Any,
    connection: _Connection,
) -> None:
    send = delivery.send
    # A client can send valid JSON that is not an object; it is answered like
    # any other unknown message rather than dropping the connection.
    msg_type = message.get("type") if isinstance(message, dict) else None
    ref = message.get("ref") if isinstance(message, dict) else None
    if not isinstance(message, dict):
        message = {}
    if msg_type == "subscribe":
        # A deliberate subscribe is where the Org/Workspace visibility set is
        # rebuilt, so a topic is never granted against a stale scope.
        await _offload(connection.refresh_scope)
        grants, denied = await _offload(
            lambda: policy.authorize_topics(connection.principal, message.get("topics"))
        )
        held = connection.add(grants)
        added = [grant.topic for grant in held]
        cursor = _as_cursor(message.get("cursor"))
        # Whether this batch speaks for the whole connection. An incremental
        # subscribe - the console adding one Session stream to the Org topics
        # it is already live on - does not, and its receipt therefore hands
        # over no cursor: the topics it did not read have live frames queued
        # below the ids it carried.
        covers_connection = set(added) >= set(connection.topics)
        # The phase opens *before* the subscription is registered, so the
        # window between registering (which is what keeps a concurrent publish
        # from being lost by both paths) and writing the batch cannot produce a
        # live frame that jumps the queue and carries the client past frames it
        # has not been handed.
        async with delivery.replay_phase() as phase:
            sub.replace_topics(connection.topics)
            sub.set_scope(connection.scope)
            result = await _offload(lambda: connection.replay(added, cursor))
            # The ack precedes the replay frames, so it reports only what the
            # client can already resume from. Advancing it here would retire
            # events it has not been handed: a disconnect halfway through the
            # batch would then reconnect past the remainder and lose it
            # silently.
            reported = _pre_replay_cursor(cursor, result)
            await send(
                {
                    "type": "ack",
                    "ref": ref,
                    "ok": not denied and len(held) == len(grants),
                    "subscribed": added,
                    "denied": denied,
                    "aliases": {
                        grant.requested: grant.topic
                        for grant in held
                        if grant.requested != grant.topic
                    },
                    "cursor": reported,
                    # The client holds one cursor for the whole socket, so it
                    # has to know whether the batch this ack announces speaks
                    # for all of it before deciding what its frames retire.
                    "covers_connection": covers_connection,
                }
            )
            connection.cursor = max(
                connection.cursor,
                await _send_replay(
                    send, result, added, reported, covers_connection=covers_connection
                ),
            )
            # Recorded while live delivery is still held: an event that was in
            # both the snapshot and the queue is now a duplicate, not news -
            # on the topics the batch carried, and on no others.
            phase.covered(result, added)
    elif msg_type == "unsubscribe":
        raw = message.get("topics")
        items = raw if isinstance(raw, list | tuple) else []
        requested = [t for t in items if isinstance(t, str)][: topic_grammar.MAX_TOPICS_PER_REQUEST]
        derived = await _offload(lambda: _derive(connection, requested))
        removed = connection.remove(derived)
        # A topic that comes back is replayed fresh, so what an earlier
        # subscription was handed must not suppress the new one's live frames.
        delivery.forget(removed)
        sub.replace_topics(connection.topics)
        await send({"type": "ack", "ref": ref, "ok": True})
    elif msg_type == "resync":
        cursor = _as_cursor(message.get("cursor"))
        async with delivery.replay_phase() as phase:
            topics = connection.topics
            result = await _offload(lambda: connection.replay(topics, cursor))
            reported = _pre_replay_cursor(cursor, result)
            await send(
                {
                    "type": "ack",
                    "ref": ref,
                    "ok": True,
                    "cursor": reported,
                    # A resync reads every topic the connection holds, so both
                    # its frames and its receipt speak for the whole cursor.
                    "covers_connection": True,
                }
            )
            connection.cursor = max(
                connection.cursor, await _send_replay(send, result, topics, reported)
            )
            phase.covered(result, topics)
    elif msg_type == "chat.send":
        # Mirror the chat message back onto the session chat topic so other
        # subscribers (and the sender) see it. This echo is notification-only
        # and is *not* the record: the durable, replayable operator message is
        # a ``session_commands`` row written by
        # ``POST /v1/sessions/{id}/message``, which the console renders and
        # backfills (BL-P0-05). A frame accepted here changes no state.
        grant = await _offload(
            lambda: policy.resolve_topic(connection.principal, message.get("topic"))
        )
        # Read access to a topic is not permission to *write* to it: only a
        # Session's own chat stream carries operator chat, so an authorized
        # Org channel cannot be used to inject a forged frame into every
        # other subscriber's console.
        if grant is None or grant.family != "session" or grant.channel != "chat":
            await send(
                {
                    "type": "error",
                    "ref": ref,
                    "payload": {
                        "error": {
                            # Deliberately uniform: an unknown Session and one
                            # in another Org must be indistinguishable here.
                            "message": "not authorized for the requested topic",
                            "type": "invalid_request_error",
                            "code": "topic_forbidden",
                        }
                    },
                }
            )
            return
        bus.publish(
            grant.topic,
            "chat.message",
            entity="agent_session",
            id=grant.reference,
            payload=message.get("payload") or {},
            ref=ref,
            org_id=grant.org_id,
            workspace_id=grant.workspace_id,
        )
        await send({"type": "ack", "ref": ref, "ok": True})
    elif msg_type == "ping":
        await send({"type": "pong", "ref": ref})
    else:
        await send(
            {
                "type": "error",
                "ref": ref,
                "payload": {
                    "error": {
                        "message": f"unknown message type: {msg_type!r}",
                        "type": "invalid_request_error",
                        "code": "unknown_ws_message",
                    }
                },
            }
        )


async def _send_replay(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    result: event_store.ReplayResult,
    topics: list[str],
    reported: int,
    *,
    covers_connection: bool = True,
) -> int:
    """Emit the reset signal (if any), the catch-up frames, then the receipt.

    Returns the cursor the client may hold once all of that was written. The
    trailing ``replay_complete`` comes *after* every frame in the batch, so a
    connection that dies mid-replay never produces one: the client keeps the id
    of the last event it actually applied and its reconnect delivers the
    remainder rather than skipping it.

    ``covers_connection`` says whether the batch read every topic this
    connection holds. When it did not, the receipt hands over no cursor at all:
    the client's cursor is one number for the whole connection, and the topics
    that were not read have live frames queued below the batch's ids. The
    client then stays where its delivered frames left it, and the returned
    watermark is only what this connection actually wrote.
    """
    if result.gap:
        await send(_reset_frame(result, topics))
    for envelope in result.events:
        frame = dict(envelope)
        frame["replayed"] = True
        await send(frame)
    cursor = _receipt_cursor(result, covers_connection=covers_connection)
    if cursor is not None:
        if cursor > reported:
            await send(_replay_complete_frame(result, topics, cursor=cursor))
        return max(cursor, reported)
    high_water = _replay_high_water(result)
    if high_water > reported:
        # The batch closed, and says so; what it wrote is reported, not handed
        # over. Nothing here retires an id on a topic this batch never read.
        await send(_replay_complete_frame(result, topics, cursor=None, covers_connection=False))
    return max(high_water, reported)


def _derive(connection: _Connection, requested: list[str]) -> list[str]:
    """The canonical name for each requested topic, or the request itself.

    An unresolvable name is kept as-is so an unsubscribe still clears a topic
    whose entity has since disappeared.
    """
    derived: list[str] = []
    for topic in requested:
        grant = policy.resolve_topic(connection.principal, topic)
        derived.append(grant.topic if grant is not None else topic)
    return derived


# --------------------------------------------------------------------------- #
# SSE fallback (WS3 §3.5) — read-only; normal HTTP auth gate applies.
# --------------------------------------------------------------------------- #


def _sse_principal(request: Request) -> Principal | None:
    """Re-resolve the streaming request's credential, or ``None`` if it died.

    ``None`` means the credential was answered for and no longer holds. A
    failure of the store itself is raised, not folded in: the stream stops
    either way, but a transient database error must not be reported to a
    console as a revocation.
    """
    from brains.authz.deps import resolve_request_principal

    try:
        principal = resolve_request_principal(
            request,
            authorization=request.headers.get("authorization"),
            x_api_key_header=request.headers.get("x-api-key"),
            allow_cookie=True,
        )
    except HTTPException:
        return None
    return None if principal.is_runtime else principal


def _sse_data(payload: dict[str, Any]) -> str:
    """One SSE frame; durable events carry their cursor as the SSE ``id``."""
    event_id = payload.get("event_id")
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}data: {json.dumps(payload)}\n\n"


@router.get("/events")
async def sse_events(request: Request, topics: str = "", cursor: str = "") -> StreamingResponse:
    requested = [t for t in (topics.split(",") if topics else []) if t]

    def _authorize() -> tuple[_Connection, list[str]]:
        """Resolve the credential, the operator check and the topics — off-loop.

        Credential resolution, topic derivation and the Org/Workspace
        visibility set are all store reads. Opening a stream is not an excuse
        to run them on the event loop: a slow store here would stall every
        other connection this process is serving.
        """
        from brains.authz.deps import resolve_request_principal

        # Apply the console gate explicitly (this is a streaming GET, not a WS)
        # so the SPA (cookie) and daemon/scripts (Bearer) can both open it.
        principal = resolve_request_principal(
            request,
            authorization=request.headers.get("authorization"),
            x_api_key_header=request.headers.get("x-api-key"),
            allow_cookie=True,
        )
        policy.require_operator(principal, operation="the realtime event stream")
        grants, denied = policy.authorize_topics(principal, requested)
        connection = _Connection(principal)
        connection.add(grants)
        return connection, denied

    connection, denied = await _offload(_authorize)
    if denied:
        # Uniform refusal: naming which topic failed, or why, would make this
        # endpoint an existence oracle for other Orgs' entities.
        raise policy.forbidden("not authorized for the requested topics")
    # ``Last-Event-ID`` is what a browser ``EventSource`` resends by itself;
    # ``cursor`` is the explicit form for a script. Same meaning as the WS
    # ``subscribe`` cursor, so the two transports resume identically.
    resume = _as_cursor(cursor if cursor else request.headers.get("last-event-id"))
    derived = connection.topics

    async def _stream():
        seq = 0
        last_check = time.monotonic()
        # Registered here rather than in the endpoint so the subscription and
        # its ``close`` share one scope: a response whose body is never
        # consumed then leaks nothing on the bus.
        sub = bus.subscribe(derived, connection.scope)
        try:
            yield _sse_data({"type": "realtime.ready", "payload": {"topics": derived}})
            try:
                result = await _offload(lambda: connection.replay(derived, resume))
            except Exception:
                # A catch-up that could not be read is a gap of unknown size.
                # Ending the stream sends the client back through a fresh
                # authorization and a fresh replay; continuing would hand it a
                # live tail it would mistake for a complete one.
                yield _sse_data(_revoked_frame(REVOKED_REVALIDATION_FAILED))
                return
            reported = _pre_replay_cursor(resume, result)
            if result.gap:
                yield _sse_data(_reset_frame(result, derived))
            for envelope in result.events:
                seq += 1
                frame = dict(envelope)
                frame["seq"] = seq
                frame["replayed"] = True
                yield _sse_data(frame)
            if result.cursor > reported:
                # Written after the batch and carrying the cursor as the SSE
                # ``id:``, so an interrupted replay leaves the browser resuming
                # from the last event it was actually handed. One stream reads
                # every topic it holds, so this receipt always speaks for the
                # whole cursor.
                yield _sse_data(
                    _replay_complete_frame(
                        result, derived, cursor=result.cursor, with_event_id=True
                    )
                )
            connection.cursor = max(connection.cursor, result.cursor)
            # One coroutine writes both the batch and the live tail here, so
            # ordering is free - but the subscription was registered before the
            # snapshot was read, so an event can sit in both. It is handed over
            # once, and only the topic that carried it is suppressed: this
            # stream holds several at once, and one topic's batch says nothing
            # about another topic's queue.
            delivered = _replay_delivered(result)
            while True:
                if await request.is_disconnected():
                    break
                now = time.monotonic()
                if now - last_check >= revalidate_seconds():
                    last_check = now
                    try:
                        current = await _offload(lambda: _sse_principal(request))
                        lost = (
                            []
                            if current is None
                            else await _offload(functools.partial(connection.reauthorize, current))
                        )
                    except Exception:
                        # Same rule as the socket: a check that could not run
                        # is not evidence the credential still holds, so the
                        # stream stops instead of outliving its authorization.
                        yield _sse_data(_revoked_frame(REVOKED_REVALIDATION_FAILED))
                        break
                    if current is None:
                        yield _sse_data(_revoked_frame(REVOKED_CREDENTIAL))
                        break
                    sub.set_scope(connection.scope)
                    sub.replace_topics(connection.topics)
                    if lost:
                        yield _sse_data(_revoked_frame(REVOKED_SCOPE, topics=lost))
                        if not connection.topics:
                            # Nothing left this principal may read: closing is
                            # honest, where holding an empty stream open is not.
                            break
                try:
                    envelope = await asyncio.wait_for(
                        sub.get(), timeout=min(SSE_TICK_SECONDS, revalidate_seconds())
                    )
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                frame = dict(envelope)
                if _superseded(frame, topics=set(connection.grants), delivered=delivered):
                    continue
                seq += 1
                frame["seq"] = seq
                yield _sse_data(frame)
        finally:
            sub.close()

    return StreamingResponse(_stream(), media_type="text/event-stream")

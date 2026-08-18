"""Realtime events: a durable log plus an in-process notifier (WS3 §3.4).

Two layers, deliberately separated:

* :mod:`brains.events.store` is the record. Session, Issue, approval and
  Runtime state events commit there *before* anything is announced, carry a
  monotonic ``event_id`` clients hold as a cursor, and are replayed on
  reconnect with an explicit reset when the cursor can no longer be honoured.
* :mod:`brains.events.bus` is the notifier. It is process-local and holds
  nothing; it turns a committed event into a low-latency frame for the sockets
  attached to *this* process, and drops any envelope whose recorded Org or
  Workspace scope falls outside the subscription's authorized scope.
* :mod:`brains.events.topics` is the closed topic grammar both sides agree on,
  so a subscriber can only name something the server can resolve to a scope.

Cross-process fan-out is not implemented: a publish in the MCP or dashboard
process is durable and is caught up by cursor, not pushed to a socket attached
to the gateway process.
"""

from brains.events.bus import EventBus, Subscription, SubscriptionScope, bus

__all__ = ["EventBus", "Subscription", "SubscriptionScope", "bus"]

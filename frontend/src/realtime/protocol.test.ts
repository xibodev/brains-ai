// Unit tests for the realtime wire contract (BL-P0-02).
//
// Run with Node's built-in test runner and its built-in TypeScript stripping,
// so the console gets real assertions without adding a test dependency:
//
//     node --test src/realtime/protocol.test.ts
//
// `tests/test_frontend_realtime.py` runs exactly that from the Python gate and
// skips when Node is absent or too old to strip types.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CREDENTIAL_REVOKED,
  CursorTracker,
  DEDUPE_WINDOW,
  REVALIDATION_FAILED,
  SCOPE_REVOKED,
  TopicMap,
  isControlFrame,
  isFatalRevocation,
  subscribeMessage,
} from "./protocol.ts";

test("a renamed topic still reaches the handler that asked for it", () => {
  const map = new TopicMap();
  map.applyAck({
    type: "ack",
    subscribed: ["org/7/issues"],
    aliases: { "org/acme/issues": "org/7/issues" },
  });
  assert.deepEqual(map.listeners("org/7/issues"), ["org/acme/issues"]);
  assert.equal(map.derived("org/acme/issues"), "org/7/issues");
});

test("two spellings of one Org both receive its frames", () => {
  const map = new TopicMap();
  map.applyAck({
    type: "ack",
    subscribed: ["org/7/sessions"],
    aliases: { "org/default/sessions": "org/7/sessions", "org/acme/sessions": "org/7/sessions" },
  });
  assert.deepEqual(map.listeners("org/7/sessions").sort(), [
    "org/acme/sessions",
    "org/default/sessions",
  ]);
});

test("an un-aliased topic routes to itself", () => {
  const map = new TopicMap();
  map.applyAck({ type: "ack", subscribed: ["org/7/issues"] });
  assert.deepEqual(map.listeners("org/7/issues"), ["org/7/issues"]);
  // A frame that arrives before any ack is delivered, not dropped.
  assert.deepEqual(map.listeners("session/AS-1/stdout"), ["session/AS-1/stdout"]);
  assert.deepEqual(map.listeners(undefined), []);
});

test("a denied topic is never routed", () => {
  const map = new TopicMap();
  map.applyAck({ type: "ack", ok: false, subscribed: [], denied: ["org/9/issues"] });
  // Nothing was granted, so nothing is aliased; a stray frame would only ever
  // route to its own name, and no handler is registered under the derived one.
  assert.deepEqual(map.derived("org/9/issues"), "org/9/issues");
});

test("forgetting a topic stops routing it", () => {
  const map = new TopicMap();
  map.applyAck({
    type: "ack",
    subscribed: ["org/7/issues"],
    aliases: { "org/acme/issues": "org/7/issues" },
  });
  map.forget("org/acme/issues");
  assert.deepEqual(map.listeners("org/7/issues"), ["org/7/issues"]);
});

test("a durable event is applied once even when replay and live overlap", () => {
  const cursor = new CursorTracker();
  assert.equal(cursor.accept({ type: "issue.created", event_id: 10 }), true);
  assert.equal(cursor.accept({ type: "issue.created", event_id: 10, replayed: true }), false);
  assert.equal(cursor.cursor, 10);
});

test("a replay that predates a live frame is still applied", () => {
  const cursor = new CursorTracker();
  // The live frame arrives first; the catch-up for 8 and 9 must not be dropped
  // just because a higher id was already seen.
  assert.equal(cursor.accept({ type: "issue.created", event_id: 10 }), true);
  assert.equal(cursor.accept({ type: "issue.created", event_id: 8, replayed: true }), true);
  assert.equal(cursor.accept({ type: "issue.created", event_id: 9, replayed: true }), true);
  assert.equal(cursor.cursor, 10);
});

test("a notification-only frame is never suppressed", () => {
  const cursor = new CursorTracker();
  const chunk = { type: "session.event", event_id: null };
  assert.equal(cursor.accept(chunk), true);
  assert.equal(cursor.accept(chunk), true);
  assert.equal(cursor.cursor, 0);
});

test("the ack cursor is adopted so a reconnect resumes", () => {
  const cursor = new CursorTracker();
  cursor.applyAck({ type: "ack", cursor: 42, covers_connection: true });
  assert.equal(cursor.cursor, 42);
  // Never moves backwards.
  cursor.applyAck({ type: "ack", cursor: 7, covers_connection: true });
  assert.equal(cursor.cursor, 42);
});

test("a fresh console does not start live off a partial batch's ack", () => {
  // Only a batch that read every topic this socket holds can say where the
  // console may start: an incremental subscribe's ack runs ahead of live
  // frames already queued for the topics it did not read.
  const cursor = new CursorTracker();
  cursor.applyAck({ type: "ack", cursor: 42, covers_connection: false });
  assert.equal(cursor.cursor, 0);
});

test("an ack never retires events the client has not been handed", () => {
  // The server writes the ack *before* the catch-up frames it announces. A
  // client that adopted the ack's cursor would skip the whole batch if the
  // socket died before it arrived.
  const cursor = new CursorTracker();
  assert.equal(cursor.accept({ type: "issue.created", event_id: 40 }), true);
  cursor.applyAck({ type: "ack", cursor: 900, covers_connection: true });
  assert.equal(cursor.cursor, 40);
});

test("a disconnect mid-replay resumes from the last applied event", () => {
  const cursor = new CursorTracker();
  cursor.accept({ type: "issue.created", event_id: 40 });
  // Reconnect: the ack echoes the cursor we sent, then two of the four
  // catch-up frames arrive and the socket drops. No `replay_complete` is sent.
  cursor.applyAck({ type: "ack", cursor: 40, covers_connection: true });
  cursor.accept({ type: "issue.created", event_id: 41, replayed: true });
  cursor.accept({ type: "issue.created", event_id: 42, replayed: true });
  assert.equal(cursor.cursor, 42);
  // The next subscribe asks for the remainder rather than skipping 43 and 44.
  assert.deepEqual(subscribeMessage(["org/7/issues"], cursor.cursor), {
    type: "subscribe",
    topics: ["org/7/issues"],
    cursor: 42,
  });
});

test("a completed replay carries the cursor past events it will never send", () => {
  const cursor = new CursorTracker();
  cursor.accept({ type: "issue.created", event_id: 41, replayed: true });
  cursor.accept({ type: "issue.created", event_id: 42, replayed: true });
  // 43-60 exist but fall outside this connection's scope, so they are never
  // delivered. Without the receipt the cursor would stall at 42 forever. The
  // batch read every topic the socket holds, so its receipt hands one over.
  cursor.applyReplayComplete({
    type: "replay_complete",
    payload: { cursor: 60, topics: ["org/7/issues"], count: 2, covers_connection: true },
  });
  assert.equal(cursor.cursor, 60);
  // It is a watermark, not an instruction: it never moves the cursor back.
  cursor.applyReplayComplete({
    type: "replay_complete",
    payload: { cursor: 5, covers_connection: true },
  });
  assert.equal(cursor.cursor, 60);
  cursor.applyReplayComplete({ type: "replay_complete" });
  assert.equal(cursor.cursor, 60);
});

test("a partial receipt reports what it wrote and retires nothing", () => {
  // The batch covered one newly added topic, not the whole connection, so its
  // high-water mark travels as `batch_cursor` and the cursor stays put.
  const cursor = new CursorTracker();
  cursor.accept({ type: "issue.created", topic: "org/7/issues", event_id: 12 });
  cursor.applyReplayComplete({
    type: "replay_complete",
    payload: {
      cursor: null,
      batch_cursor: 40,
      topics: ["session/AS-9/stdout"],
      count: 0,
      covers_connection: false,
    },
  });
  assert.equal(cursor.cursor, 12);
  // A receipt whose shape is not understood - no flag, an older server - is
  // read the same way: re-reading rows is recoverable, skipping is not.
  cursor.applyReplayComplete({ type: "replay_complete", payload: { cursor: 40 } });
  assert.equal(cursor.cursor, 12);
});

test("an empty batch advances only where it spoke for the whole connection", () => {
  // Nothing was deliverable: every id up to the store's is out of scope. A
  // full-coverage receipt is what retires them.
  const full = new CursorTracker();
  full.accept({ type: "issue.created", event_id: 9 });
  full.applyReplayComplete({
    type: "replay_complete",
    payload: { cursor: 50, topics: ["org/7/issues"], count: 0, covers_connection: true },
  });
  assert.equal(full.cursor, 50);

  const partial = new CursorTracker();
  partial.accept({ type: "issue.created", event_id: 9 });
  partial.applyReplayComplete({
    type: "replay_complete",
    payload: {
      cursor: null,
      batch_cursor: 0,
      topics: ["session/AS-9/stdout"],
      count: 0,
      covers_connection: false,
    },
  });
  assert.equal(partial.cursor, 9);
});

test("a reset is not undone by the receipt of the batch that followed it", () => {
  // The server could not honour the cursor: it resets, replays from where it
  // actually is, and closes the batch. The reset's own cursor is the floor.
  const cursor = new CursorTracker();
  cursor.accept({ type: "issue.created", event_id: 5 });
  cursor.applyReset({ type: "realtime.reset", payload: { reason: "cursor_expired", cursor: 900 } });
  assert.equal(cursor.accept({ type: "issue.created", event_id: 901, replayed: true }), true);
  // A partial batch's receipt cannot push past the frames it handed over.
  cursor.applyReplayComplete({
    type: "replay_complete",
    payload: { cursor: null, batch_cursor: 950, count: 1, covers_connection: false },
  });
  assert.equal(cursor.cursor, 901);
  cursor.applyReplayComplete({
    type: "replay_complete",
    payload: { cursor: 950, count: 1, covers_connection: true },
  });
  assert.equal(cursor.cursor, 950);
});

test("a reset adopts the server's cursor and forgets what was applied", () => {
  const cursor = new CursorTracker();
  cursor.accept({ type: "issue.created", event_id: 5 });
  cursor.applyReset({ type: "realtime.reset", payload: { reason: "cursor_expired", cursor: 900 } });
  assert.equal(cursor.cursor, 900);
  // The window was cleared, so the same id is applied again after a reset.
  assert.equal(cursor.accept({ type: "issue.created", event_id: 5 }), true);
});

test("a reset with no cursor restarts from the beginning", () => {
  const cursor = new CursorTracker();
  cursor.accept({ type: "issue.created", event_id: 5 });
  cursor.applyReset({ type: "realtime.reset", payload: { reason: "cursor_ahead" } });
  assert.equal(cursor.cursor, 0);
});

test("the dedupe window is bounded", () => {
  const cursor = new CursorTracker();
  for (let id = 1; id <= DEDUPE_WINDOW + 1; id += 1) {
    assert.equal(cursor.accept({ type: "issue.created", event_id: id }), true);
  }
  // The oldest id fell out of the window; the cursor is what still bounds it.
  assert.equal(cursor.accept({ type: "issue.created", event_id: 1 }), true);
  assert.equal(cursor.cursor, DEDUPE_WINDOW + 1);
});

test("control frames are not product events", () => {
  for (const type of [
    "ack",
    "pong",
    "error",
    "realtime.reset",
    "realtime.revoked",
    "realtime.ready",
    "replay_complete",
  ]) {
    assert.equal(isControlFrame({ type }), true, type);
  }
  assert.equal(isControlFrame({ type: "issue.created" }), false);
});

test("a lost membership is not a lost connection", () => {
  // The credential still holds; only some topics went away.
  assert.equal(
    isFatalRevocation({
      type: "realtime.revoked",
      payload: { reason: SCOPE_REVOKED, topics: ["org/7/issues"] },
    }),
    false,
  );
  // The server could not run the check and closed rather than stream behind
  // it. The console's answer is to reconnect and be checked again.
  assert.equal(
    isFatalRevocation({ type: "realtime.revoked", payload: { reason: REVALIDATION_FAILED } }),
    false,
  );
  assert.equal(
    isFatalRevocation({ type: "realtime.revoked", payload: { reason: CREDENTIAL_REVOKED } }),
    true,
  );
  // An unknown or missing reason is treated as fatal: guessing that a
  // revocation is survivable is the dangerous direction.
  assert.equal(isFatalRevocation({ type: "realtime.revoked", payload: {} }), true);
  assert.equal(isFatalRevocation({ type: "realtime.revoked" }), true);
});

test("a fresh client starts live instead of replaying the whole log", () => {
  // The server reads cursor 0 as "from the beginning of the retained log".
  assert.deepEqual(subscribeMessage(["org/7/issues"], 0), {
    type: "subscribe",
    topics: ["org/7/issues"],
  });
  assert.deepEqual(subscribeMessage(["org/7/issues"], 12), {
    type: "subscribe",
    topics: ["org/7/issues"],
    cursor: 12,
  });
});

test("adding a topic never drops or repeats the topic already being delivered", () => {
  // The end-to-end shape of the incremental subscribe: a console live on the
  // Org's issues opens a Session and subscribes to its stream. The Session's
  // catch-up carries ids above what the issues topic has queued, and an issue
  // event published while that batch was being read arrives *after* it, with a
  // lower id. It has to be applied - exactly once - and routed under the name
  // the screen registered, and until it lands the console may not resume from
  // the batch that overtook it.
  const map = new TopicMap();
  const cursor = new CursorTracker();
  const issues = "org/7/issues";
  const stdout = "session/AS-9/stdout";

  map.applyAck({ type: "ack", subscribed: [issues], aliases: { "org/acme/issues": issues } });
  cursor.applyAck({ type: "ack", cursor: 11, covers_connection: true });
  assert.equal(cursor.accept({ type: "issue.created", topic: issues, event_id: 12 }), true);

  // Add the Session stream, resuming from the highest id applied so far.
  assert.deepEqual(subscribeMessage([stdout], cursor.cursor), {
    type: "subscribe",
    topics: [stdout],
    cursor: 12,
  });
  map.applyAck({ type: "ack", subscribed: [stdout], cursor: 12 });
  // The ack precedes the batch and may not retire it, and it says the batch
  // reads one topic of the two this socket holds.
  cursor.applyAck({ type: "ack", cursor: 99, covers_connection: false });
  assert.equal(cursor.cursor, 12);
  for (const event_id of [14, 15, 16]) {
    assert.equal(
      cursor.accept({ type: "session.output", topic: stdout, event_id, replayed: true }),
      true,
    );
  }
  // Applied, but not resumable: id 13 is still in the queue below them.
  assert.equal(cursor.cursor, 12);
  cursor.applyReplayComplete({
    type: "replay_complete",
    payload: { cursor: null, batch_cursor: 16, topics: [stdout], count: 3, covers_connection: false },
  });
  assert.equal(cursor.cursor, 12);

  // The issue event that was queued while the Session's backlog was read. Its
  // id is below the batch's, and it is news, not a duplicate.
  const queued = { type: "issue.created", topic: issues, event_id: 13 };
  assert.equal(cursor.accept(queued), true);
  assert.deepEqual(map.listeners(issues), ["org/acme/issues"]);
  // The durable log's own copy of it, announced a second time, is not applied
  // twice.
  assert.equal(cursor.accept({ ...queued, replayed: true }), false);
  // Live delivery has caught up with the batch, so the cursor moves with it.
  assert.equal(cursor.accept({ type: "session.output", topic: stdout, event_id: 17 }), true);
  assert.equal(cursor.cursor, 17);

  // Reconnect: both topics, resuming from 17. The server replays what is past
  // it; anything it re-sends from before is still applied only once.
  assert.deepEqual(subscribeMessage([issues, stdout], cursor.cursor), {
    type: "subscribe",
    topics: [issues, stdout],
    cursor: 17,
  });
  cursor.applyAck({ type: "ack", cursor: 17, covers_connection: true });
  assert.equal(cursor.accept({ ...queued, replayed: true }), false);
  assert.equal(
    cursor.accept({ type: "session.output", topic: stdout, event_id: 16, replayed: true }),
    false,
  );
  assert.equal(cursor.accept({ type: "issue.created", topic: issues, event_id: 18 }), true);
  assert.equal(cursor.cursor, 18);
});

test("a disconnect between a partial receipt and the queue behind it loses nothing", () => {
  // The regression, on the production tracker. A console live on the Org's
  // issues adds a Session stream; the catch-up for the new topic is written
  // whole, its receipt closes it, and the issue event that was published while
  // that batch was being read is still queued *behind* the receipt with a lower
  // id. The socket dies in exactly that window.
  const cursor = new CursorTracker();
  const issues = "org/7/issues";
  const stdout = "session/AS-9/stdout";

  cursor.applyAck({ type: "ack", subscribed: [issues], cursor: 11, covers_connection: true });
  cursor.accept({ type: "issue.created", topic: issues, event_id: 12 });

  cursor.applyAck({ type: "ack", subscribed: [stdout], cursor: 12, covers_connection: false });
  for (const event_id of [14, 15, 16]) {
    cursor.accept({ type: "session.output", topic: stdout, event_id, replayed: true });
  }
  cursor.applyReplayComplete({
    type: "replay_complete",
    payload: { cursor: null, batch_cursor: 16, topics: [stdout], count: 3, covers_connection: false },
  });
  // The socket drops here: event 13 was never written. The console resumes
  // from 12, not from 16, or nothing would ever deliver it again.
  assert.equal(cursor.cursor, 12);
  assert.deepEqual(subscribeMessage([issues, stdout], cursor.cursor), {
    type: "subscribe",
    topics: [issues, stdout],
    cursor: 12,
  });

  // Reconnect. This batch reads both topics, so it speaks for the cursor.
  cursor.applyAck({
    type: "ack",
    subscribed: [issues, stdout],
    cursor: 12,
    covers_connection: true,
  });
  const recovered: number[] = [];
  for (const frame of [
    { type: "issue.created", topic: issues, event_id: 13 },
    { type: "session.output", topic: stdout, event_id: 14 },
    { type: "session.output", topic: stdout, event_id: 15 },
    { type: "session.output", topic: stdout, event_id: 16 },
  ]) {
    if (cursor.accept({ ...frame, replayed: true })) recovered.push(frame.event_id);
  }
  // The event the partial batch overtook comes back, and only that one: the
  // frames the console already applied are still suppressed by id.
  assert.deepEqual(recovered, [13]);
  cursor.applyReplayComplete({
    type: "replay_complete",
    payload: { cursor: 16, topics: [issues, stdout], count: 4, covers_connection: true },
  });
  assert.equal(cursor.cursor, 16);
});

test("a receipt for one topic does not suppress another topic's frames", () => {
  // The console holds one cursor, but it is a resume point, never a filter: a
  // frame below it is applied if it has not been applied before.
  const cursor = new CursorTracker();
  cursor.applyReplayComplete({
    type: "replay_complete",
    payload: {
      cursor: 50,
      topics: ["session/AS-9/stdout"],
      count: 0,
      covers_connection: true,
    },
  });
  assert.equal(cursor.cursor, 50);
  assert.equal(cursor.accept({ type: "issue.created", topic: "org/7/issues", event_id: 30 }), true);
  assert.equal(
    cursor.accept({ type: "issue.created", topic: "org/7/issues", event_id: 30 }),
    false,
  );
});

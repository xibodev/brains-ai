// Unit tests for the console's per-Session async scoping (BL-P0-05).
//
// Run with Node's built-in test runner and its built-in TypeScript stripping,
// so the console gets real assertions without adding a test dependency:
//
//     node --test src/components/sessionScope.test.ts
//
// `tests/test_frontend_session_scope.py` runs exactly that from the Python
// gate and skips when Node is absent or too old to strip types.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  type ScopedState,
  isCurrent,
  patchById,
  removeById,
  sameSession,
  scopedClear,
  scopedGet,
  scopedSet,
  scopedUpdate,
  sessionKey,
  upsertById,
} from "./sessionScope.ts";

interface Msg {
  id: string;
  text: string;
  status?: string;
}

test("a Session id compares the same however it arrived", () => {
  assert.equal(sessionKey(7), "7");
  assert.equal(sessionKey("7"), "7");
  assert.ok(sameSession(7, "7"));
  assert.ok(!sameSession("s1", "s2"));
});

test("no Session is never the same Session", () => {
  assert.equal(sessionKey(null), null);
  assert.equal(sessionKey(undefined), null);
  assert.equal(sessionKey(""), null);
  assert.ok(!sameSession(null, null));
  assert.ok(!sameSession(undefined, "s1"));
});

test("a response for the Session still on screen is applied", () => {
  const ref = { current: sessionKey("s1") };
  assert.ok(isCurrent(ref, sessionKey("s1")));
});

test("a response that arrives after the operator switched Session is ignored", () => {
  const ref = { current: sessionKey("s1") };
  const captured = sessionKey("s1");
  ref.current = sessionKey("s2"); // the operator clicked another Session
  assert.ok(!isCurrent(ref, captured));
  // ...and the late answer for s2 is not applied to s1 either.
  assert.ok(!isCurrent({ current: sessionKey("s1") }, sessionKey("s2")));
});

test("a response captured with nothing selected can never be applied", () => {
  assert.ok(!isCurrent({ current: null }, null));
});

test("a response that arrives after the thread closed is ignored", () => {
  const ref = { current: sessionKey("s1") };
  const captured = ref.current;
  ref.current = null; // the dock deselected
  assert.ok(!isCurrent(ref, captured));
});

test("pending state is held per Session, so one thread cannot write into another", () => {
  let state: ScopedState<Msg[]> = {};
  state = scopedSet(state, "s1", [{ id: "op-1", text: "hello", status: "sending" }]);
  state = scopedSet(state, "s2", [{ id: "op-2", text: "other", status: "sending" }]);
  assert.deepEqual(
    scopedGet(state, "s1", []).map((m) => m.id),
    ["op-1"],
  );
  assert.deepEqual(
    scopedGet(state, "s2", []).map((m) => m.id),
    ["op-2"],
  );
  assert.deepEqual(scopedGet(state, "s3", []), []);
});

test("a late failure marks the Session it was sent for, not the visible one", () => {
  let state: ScopedState<Msg[]> = {};
  state = scopedUpdate(state, "s1", [] as Msg[], (items) =>
    upsertById(items, { id: "op-1", text: "hello", status: "sending" }),
  );
  state = scopedUpdate(state, "s2", [] as Msg[], (items) =>
    upsertById(items, { id: "op-2", text: "other", status: "sending" }),
  );
  // The s1 request rejects while s2 is on screen.
  state = scopedUpdate(state, "s1", [] as Msg[], (items) =>
    patchById(items, "op-1", (m) => ({ ...m, status: "failed" })),
  );
  assert.equal(scopedGet(state, "s1", [])[0].status, "failed");
  assert.equal(scopedGet(state, "s2", [])[0].status, "sending");
});

test("a value with no Session to attribute it to changes nothing", () => {
  const state: ScopedState<Msg[]> = { s1: [{ id: "op-1", text: "hello" }] };
  assert.equal(scopedSet(state, null, []), state);
  assert.equal(
    scopedUpdate(state, undefined, [] as Msg[], () => []),
    state,
  );
  assert.equal(scopedClear(state, null), state);
});

test("selecting a Session clears only that Session's pending slice", () => {
  let state: ScopedState<Msg[]> = {
    s1: [{ id: "op-1", text: "hello" }],
    s2: [{ id: "op-2", text: "other" }],
  };
  state = scopedClear(state, "s1");
  assert.deepEqual(Object.keys(state), ["s2"]);
});

test("a retry replaces the pending row it retries rather than adding one", () => {
  let items: Msg[] = [{ id: "op-1", text: "hello", status: "failed" }];
  items = upsertById(items, { id: "op-1", text: "hello", status: "sending" });
  assert.equal(items.length, 1);
  assert.equal(items[0].status, "sending");
  items = upsertById(items, { id: "op-2", text: "second", status: "sending" });
  assert.deepEqual(
    items.map((m) => m.id),
    ["op-1", "op-2"],
  );
});

test("a settled send drops only its own pending row", () => {
  const items: Msg[] = [
    { id: "op-1", text: "hello" },
    { id: "op-2", text: "second" },
  ];
  assert.deepEqual(
    removeById(items, "op-1").map((m) => m.id),
    ["op-2"],
  );
  assert.deepEqual(removeById(items, "missing").length, 2);
});

test("patching an unknown row is a no-op rather than an insertion", () => {
  const items: Msg[] = [{ id: "op-1", text: "hello" }];
  const patched = patchById(items, "op-9", (m) => ({ ...m, status: "failed" }));
  assert.equal(patched.length, 1);
  assert.equal(patched[0].status, undefined);
});

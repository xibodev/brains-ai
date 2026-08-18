// Async results belong to the Session they were asked for (BL-P0-05).
//
// Every mutation the dock performs is a request over the network: send a
// message, stop a Session, re-read its detail. The operator does not wait for
// them - they click the next Session in the list while the first one's request
// is still in flight. When it resolves, the component that receives it is
// showing something else, and a naive `setState` writes one Session's outcome
// into another Session's thread: a message bubble in a thread it was never
// typed into, a "Session stopped" toast against a Session nobody stopped, a
// detail object that disables the composer for the wrong agent.
//
// The rule is one line long - *capture the Session at request time and ignore
// the result if the selection moved on* - but it has to hold at every await,
// so it lives here as data rather than as a habit. Pending state is keyed by
// Session for the same reason: a per-component `pending` array is a single
// slot that the wrong Session can write into, while a map keyed by Session id
// simply has nowhere to put a stale answer.
//
// No React and no fetch: this is the part that can be reasoned about, and
// tested, on its own.

/** However a Session id arrives from the router, the store or the API. */
export type SessionRef = string | number | null | undefined;

/** The minimal shape of a React ref, so this module needs no React import. */
export interface CurrentRef<T> {
  current: T;
}

/** One comparable identity for a Session, or `null` for "no Session". */
export function sessionKey(ref: SessionRef): string | null {
  if (ref === null || ref === undefined) return null;
  const key = String(ref);
  return key === "" ? null : key;
}

/** Whether two refs name the same Session. `null` never matches anything. */
export function sameSession(a: SessionRef, b: SessionRef): boolean {
  const left = sessionKey(a);
  return left !== null && left === sessionKey(b);
}

/**
 * Whether a result captured for `captured` may still be applied.
 *
 * `ref` holds the Session the view is showing *now*; `captured` is the one the
 * request was issued for. A request issued with no Session selected can never
 * be applied, which is why `null` is not current even against a `null` ref.
 */
export function isCurrent(ref: CurrentRef<string | null>, captured: string | null): boolean {
  return captured !== null && ref.current === captured;
}

/** Per-Session state, so an answer for one Session cannot land in another. */
export type ScopedState<T> = Readonly<Record<string, T>>;

export function scopedGet<T>(state: ScopedState<T>, ref: SessionRef, fallback: T): T {
  const key = sessionKey(ref);
  if (key === null) return fallback;
  return Object.prototype.hasOwnProperty.call(state, key) ? state[key] : fallback;
}

/**
 * Replace one Session's slice. A `ref` that names no Session changes nothing,
 * because there is no Session to attribute the value to.
 */
export function scopedSet<T>(state: ScopedState<T>, ref: SessionRef, value: T): ScopedState<T> {
  const key = sessionKey(ref);
  if (key === null) return state;
  return { ...state, [key]: value };
}

/** Derive one Session's slice from its current value. */
export function scopedUpdate<T>(
  state: ScopedState<T>,
  ref: SessionRef,
  fallback: T,
  update: (current: T) => T,
): ScopedState<T> {
  const key = sessionKey(ref);
  if (key === null) return state;
  return { ...state, [key]: update(scopedGet(state, key, fallback)) };
}

/** Drop one Session's slice entirely (its thread was reloaded from the server). */
export function scopedClear<T>(state: ScopedState<T>, ref: SessionRef): ScopedState<T> {
  const key = sessionKey(ref);
  if (key === null || !Object.prototype.hasOwnProperty.call(state, key)) return state;
  const next = { ...state };
  delete next[key];
  return next;
}

/** The identified item a pending list holds. */
export interface Identified {
  id: string;
}

/** Insert or replace a pending item by id, preserving its position. */
export function upsertById<T extends Identified>(items: readonly T[], item: T): T[] {
  const index = items.findIndex((existing) => existing.id === item.id);
  if (index < 0) return [...items, item];
  const next = [...items];
  next[index] = item;
  return next;
}

/** Remove a pending item by id. */
export function removeById<T extends Identified>(items: readonly T[], id: string): T[] {
  return items.filter((existing) => existing.id !== id);
}

/** Apply a change to one pending item by id, leaving the rest untouched. */
export function patchById<T extends Identified>(
  items: readonly T[],
  id: string,
  patch: (item: T) => T,
): T[] {
  return items.map((existing) => (existing.id === id ? patch(existing) : existing));
}

// The realtime wire contract, as pure state (BL-P0-02).
//
// The server derives every topic name from its own state: a console asks for
// `org/acme/issues` or `org/default/sessions` and the connection is granted
// `org/7/issues`. Frames then arrive under the *derived* name, so a client that
// dispatched on the string it sent would silently receive nothing. The ack
// carries the mapping, and `TopicMap` is where it is kept.
//
// Durable frames also carry `event_id`, a monotonic cursor. The console holds
// the highest id it has applied, sends it back on (re)subscribe, and receives a
// bounded replay of what it missed. Replay and live delivery deliberately
// overlap, so the same event can arrive twice and must be applied once;
// `CursorTracker` is that filter. The cursor follows *delivery*, not the ack:
// the server writes the ack before the catch-up frames, so the frames advance
// the cursor themselves and a trailing `replay_complete` confirms the batch -
// but only where that receipt speaks for the whole connection. A batch read for
// one newly added topic does not: live frames for the topics it did not read
// are queued behind it with lower ids, so its receipt reports what it wrote and
// hands over nothing. A disconnect mid-replay therefore leaves the console
// holding the id of the last event it actually applied, and the reconnect asks
// for the remainder. When the server cannot honour a cursor it says so
// (`realtime.reset`) instead of quietly sending a short stream, and the
// console's answer to a reset is to re-read over REST rather than to pretend.
//
// This module holds no socket and no React: it is the part of the protocol that
// can be reasoned about, and tested, on its own.

export interface Envelope {
  v?: number;
  type: string;
  entity?: string;
  id?: string | number;
  topic?: string;
  ts?: string;
  payload?: unknown;
  seq?: number;
  ref?: string;
  /** The durable cursor; absent or null on a notification-only frame. */
  event_id?: number | null;
  durable?: boolean;
  /** Set by the server on a catch-up frame. */
  replayed?: boolean;
  org_id?: number | null;
  workspace_id?: number | null;
}

export interface SubscribeAck {
  type: string;
  ref?: string;
  ok?: boolean;
  subscribed?: string[];
  denied?: string[];
  /** requested name -> derived name, for every topic the server renamed. */
  aliases?: Record<string, string>;
  cursor?: number;
  /**
   * True when the catch-up batch this ack announces reads every topic the
   * connection holds, which is what makes its frames safe to resume from.
   */
  covers_connection?: boolean;
}

export interface ResetFrame {
  type: string;
  payload?: { reason?: string | null; cursor?: number; topics?: string[] };
}

/**
 * The receipt for a fully delivered catch-up batch.
 *
 * Sent after the last replay frame, so receiving it is the only proof that the
 * whole batch arrived. `cursor` is the resume point the batch *hands over*,
 * and the server only fills it in when the batch read every topic the
 * connection holds (`covers_connection`): it then also carries the console past
 * ids it will never be sent - events the server dropped as outside this
 * connection's scope - which would otherwise stall the cursor and be re-read on
 * every reconnect.
 *
 * A partial batch - the incremental subscribe, where one topic is added to
 * others already being delivered - reports `cursor: null` and puts what it
 * wrote in `batch_cursor`. That number is reporting, not permission: live
 * frames for the topics the batch did not read are queued *behind* it with
 * lower ids, and a console that adopted it and then dropped would reconnect
 * past events it was never handed.
 */
export interface ReplayCompleteFrame {
  type: string;
  payload?: {
    cursor?: number | null;
    topics?: string[];
    count?: number;
    /** True when the batch read every topic this connection holds. */
    covers_connection?: boolean;
    /** The highest id a partial batch wrote. Informational; never adopted. */
    batch_cursor?: number;
  };
}

export const RESET_TYPE = "realtime.reset";
export const REVOKED_TYPE = "realtime.revoked";
export const READY_TYPE = "realtime.ready";
export const REPLAY_COMPLETE_TYPE = "replay_complete";

/** The credential itself stopped holding: reconnecting changes nothing. */
export const CREDENTIAL_REVOKED = "credential_revoked";
/** Only some topics were withdrawn; the connection is still usable. */
export const SCOPE_REVOKED = "scope_revoked";
/**
 * The server could not *perform* the re-authorization and closed the socket.
 *
 * The credential may well still be good - the store was unavailable, a policy
 * read failed - so this is not a reason to stop reconnecting. The server failed
 * closed; the console's part is to come back and be checked again.
 */
export const REVALIDATION_FAILED = "revalidation_failed";

export interface RevokedFrame {
  type: string;
  payload?: { reason?: string | null; topics?: string[] };
}

/**
 * True when a `realtime.revoked` frame ends the connection for good.
 *
 * The server uses one frame type for three different events. `scope_revoked`
 * means a membership or an entity went away and took some topics with it - the
 * credential is still good and the socket stays open. `revalidation_failed`
 * means the check could not be run: the socket closes, but reconnecting is
 * exactly the right response. Treating either as fatal would black out the
 * whole console - for one changed Org membership, or for a database blip - so
 * the distinction is made here rather than assumed.
 */
export function isFatalRevocation(frame: RevokedFrame): boolean {
  const reason = frame.payload?.reason;
  return reason !== SCOPE_REVOKED && reason !== REVALIDATION_FAILED;
}

/**
 * The `subscribe` message for `topics`, resuming from `cursor`.
 *
 * A cursor of `0` is omitted rather than sent: the server reads `0` as "replay
 * the whole retained log", which is right for a script asking for history and
 * wrong for a tab that has just opened and wants to start live.
 */
export function subscribeMessage(topics: string[], cursor: number): Record<string, unknown> {
  const message: Record<string, unknown> = { type: "subscribe", topics };
  if (cursor > 0) message.cursor = cursor;
  return message;
}

/** Control frames are protocol, not product events, and are never dispatched raw. */
export function isControlFrame(frame: { type?: string }): boolean {
  return (
    frame.type === "ack" ||
    frame.type === "pong" ||
    frame.type === "error" ||
    frame.type === RESET_TYPE ||
    frame.type === REVOKED_TYPE ||
    frame.type === READY_TYPE ||
    frame.type === REPLAY_COMPLETE_TYPE
  );
}

// --------------------------------------------------------------------------- //
// Requested name <-> derived name
// --------------------------------------------------------------------------- //

export class TopicMap {
  private derivedOf = new Map<string, string>();
  private requestedOf = new Map<string, Set<string>>();

  /** Record the server's answer for one subscribe. */
  applyAck(ack: SubscribeAck): void {
    const aliases = ack.aliases ?? {};
    for (const [requested, derived] of Object.entries(aliases)) {
      this.link(requested, derived);
    }
    // A topic the server did not rename still has to be routable, and the ack's
    // `subscribed` list is the only place an un-aliased derived name appears.
    for (const derived of ack.subscribed ?? []) {
      if (!this.requestedOf.has(derived)) this.link(derived, derived);
    }
  }

  private link(requested: string, derived: string): void {
    this.derivedOf.set(requested, derived);
    const set = this.requestedOf.get(derived) ?? new Set<string>();
    set.add(requested);
    this.requestedOf.set(derived, set);
  }

  forget(requested: string): void {
    const derived = this.derivedOf.get(requested);
    this.derivedOf.delete(requested);
    if (!derived) return;
    const set = this.requestedOf.get(derived);
    if (!set) return;
    set.delete(requested);
    if (set.size === 0) this.requestedOf.delete(derived);
  }

  /** The derived name for a requested one, or the requested one if unknown. */
  derived(requested: string): string {
    return this.derivedOf.get(requested) ?? requested;
  }

  /**
   * Every locally registered name a frame on `derived` should be dispatched to.
   *
   * Falls back to the topic itself, so a frame that arrives before its ack (or
   * on a name the client never aliased) is still delivered rather than dropped.
   */
  listeners(derived: string | undefined): string[] {
    if (!derived) return [];
    const set = this.requestedOf.get(derived);
    if (!set || set.size === 0) return [derived];
    return [...set];
  }

  clear(): void {
    this.derivedOf.clear();
    this.requestedOf.clear();
  }
}

// --------------------------------------------------------------------------- //
// The cursor and the at-most-once filter
// --------------------------------------------------------------------------- //

/** How many recent event ids are remembered for duplicate suppression. */
export const DEDUPE_WINDOW = 2048;

export class CursorTracker {
  private applied = new Set<number>();
  private order: number[] = [];
  private highest = 0;
  /**
   * Whether the catch-up batch currently in flight may move the resume cursor.
   *
   * A console holds *one* cursor for the whole socket, so a frame only retires
   * an id if everything below it on every other topic has been handed over
   * too. A batch that read every topic the connection holds satisfies that; an
   * incremental subscribe does not. The server serialises delivery, so the live
   * frames the console was owed on the topics it was already watching arrive
   * *after* that batch and its receipt - with ids below everything the batch
   * carried. Letting the batch advance the cursor would retire them while they
   * are still in the queue, and a drop before they landed would resume past
   * them for good.
   *
   * Set from the ack, which the server writes ahead of the batch it announces,
   * and cleared again when the batch's receipt closes it. Live frames are never
   * held back: they are only ever written once no batch is in flight.
   */
  private batchRetires = true;

  /** The cursor to send on the next (re)subscribe. */
  get cursor(): number {
    return this.highest;
  }

  /**
   * True when this frame should be applied.
   *
   * A frame with no `event_id` is notification-only (chat echo, transcript
   * chunk): it is not replayable, so it is never suppressed. A durable frame is
   * applied once; the second copy - the overlap between a replay and the live
   * stream - is dropped.
   *
   * Applying a frame and *resuming* from it are two different things. A frame
   * that belongs to a catch-up batch which covered only some of this socket's
   * topics is applied like any other, but it does not move the cursor: the
   * frames it would retire on the topics that batch never read are still queued
   * behind it (see `batchRetires`).
   */
  accept(frame: Envelope): boolean {
    const id = frame.event_id;
    if (typeof id !== "number" || !Number.isFinite(id)) return true;
    if (this.applied.has(id)) return false;
    this.applied.add(id);
    this.order.push(id);
    if (this.order.length > DEDUPE_WINDOW) {
      const evicted = this.order.shift();
      if (evicted !== undefined) this.applied.delete(evicted);
    }
    const retires = this.batchRetires || frame.replayed !== true;
    if (retires && id > this.highest) this.highest = id;
    return true;
  }

  /**
   * Adopt the cursor an ack reported, and record what its batch may retire.
   *
   * The ack is written *before* the catch-up frames it announces, so its cursor
   * is only ever a starting point - never a receipt. A console that already
   * holds a cursor keeps it and lets the replay frames advance it one applied
   * event at a time; adopting the ack's number instead would retire events that
   * are still in flight, and a disconnect mid-batch would reconnect past them.
   * A console with no cursor has nothing to lose and takes the server's, which
   * is how a fresh tab starts live instead of replaying the retained log - and
   * a fresh tab's first subscribe is by definition the whole connection.
   *
   * The ack is also where the console learns whether the batch behind it speaks
   * for the whole socket. It does not for an incremental subscribe, and until
   * that batch is closed its frames are applied without moving the cursor.
   */
  applyAck(ack: SubscribeAck): void {
    this.batchRetires = ack.covers_connection === true;
    if (!this.batchRetires) return;
    if (this.highest !== 0) return;
    if (typeof ack.cursor === "number" && ack.cursor > this.highest) {
      this.highest = ack.cursor;
    }
  }

  /**
   * Adopt the cursor a completed replay confirmed - if it confirmed one.
   *
   * A receipt that speaks for the whole connection (`covers_connection`) comes
   * after every frame in its batch *and* after every frame the connection had
   * queued below it, so it is the one place a cursor may jump ahead of what was
   * applied: the ids in between are events the server will never send this
   * connection, and waiting for them forever would re-read the same rows on
   * every reconnect.
   *
   * Any other receipt is read as reporting. A batch that covered only some of
   * the console's topics is racing live frames on the ones it did not read, and
   * those frames are queued behind it with *lower* ids; adopting its number
   * would retire them, and a disconnect before the queue drained would resume
   * past them for good. The console's cursor stays where its delivered frames
   * left it, and the next full-coverage batch settles the rest. A receipt whose
   * shape is not understood - an older server, a missing flag - is treated the
   * same way: re-reading a handful of rows is recoverable, skipping an event is
   * not.
   */
  applyReplayComplete(frame: ReplayCompleteFrame): void {
    const covers = frame.payload?.covers_connection === true;
    // Whatever it said, the batch is over: live delivery resumes, and live
    // frames always move the cursor.
    this.batchRetires = true;
    if (!covers) return;
    const cursor = frame.payload?.cursor;
    if (typeof cursor !== "number" || !Number.isFinite(cursor)) return;
    if (cursor > this.highest) this.highest = cursor;
  }

  /**
   * Answer a reset: the server could not honour the cursor, so the console
   * must re-read state over REST and resume from where the server actually is.
   */
  applyReset(frame: ResetFrame): void {
    const cursor = frame.payload?.cursor;
    this.applied.clear();
    this.order = [];
    this.highest = typeof cursor === "number" && cursor > 0 ? cursor : 0;
  }

  /** Forget everything - used when the connection's identity changed. */
  reset(): void {
    this.applied.clear();
    this.order = [];
    this.highest = 0;
    this.batchRetires = true;
  }
}

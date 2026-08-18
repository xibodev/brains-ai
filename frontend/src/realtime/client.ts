// Realtime client — a single multiplexed WebSocket over /v1/ws (WS3 §3).
//
// One socket fans out to many topic subscribers (`org/{slug}/issues`,
// `session/{id}/stdout`, etc). Each frame is the WS3 envelope
// `{v,type,entity,id,topic,ts,payload,seq,ref}`, extended with
// `event_id`/`durable` for anything replayable.
//
// Three properties this client is responsible for (BL-P0-02):
//
// * **Topics are the server's, not ours.** A screen subscribes with the name it
//   knows (`org/acme/issues`, `org/default/sessions`); the server answers with
//   the canonical one (`org/7/issues`) and frames arrive under *that*. The ack's
//   alias map is applied through `TopicMap` so a handler registered on the name
//   it asked for still receives its events, and a denied topic is simply never
//   delivered rather than being retried forever.
// * **Reconnects resume, they do not restart.** The highest applied `event_id`
//   is sent as `cursor` on every (re)subscribe, so a drop replays what was
//   missed instead of leaving a hole the console never notices. The cursor
//   follows delivery: the ack that precedes a catch-up batch never advances it,
//   the replay frames do, and a trailing `replay_complete` closes the batch —
//   handing over a cursor only where it covered every topic this socket holds,
//   so an incremental subscribe never retires the frames queued for the topics
//   it did not read. A disconnect halfway through a replay resumes from the
//   last event the console actually applied.
// * **A gap is handled, not hidden.** When the server cannot honour the cursor
//   it sends `realtime.reset`; the client forwards that to every affected
//   subscriber, whose handlers refetch over REST — the same thing they do for a
//   normal event, which is why a reset is safe to treat as "resynchronise now".
//
// Auth rides the same-origin `brains_admin_key` cookie on upgrade — no
// token plumbing needed for the browser path (WS3 §3.1).

import {
  CursorTracker,
  REPLAY_COMPLETE_TYPE,
  RESET_TYPE,
  REVOKED_TYPE,
  TopicMap,
  isControlFrame,
  isFatalRevocation,
  subscribeMessage,
  type Envelope,
  type ReplayCompleteFrame,
  type ResetFrame,
  type RevokedFrame,
  type SubscribeAck,
} from "./protocol";

export type { Envelope } from "./protocol";

type Handler = (env: Envelope) => void;
export type ConnState = "connecting" | "open" | "closed" | "denied";

class RealtimeClient {
  private ws: WebSocket | null = null;
  private handlers = new Map<string, Set<Handler>>();
  private connListeners = new Set<(s: ConnState) => void>();
  private state: ConnState = "closed";
  private reconnectDelay = 1000;
  private reconnectTimer: number | null = null;
  private intentionalClose = false;
  private topics = new TopicMap();
  private cursor = new CursorTracker();

  get connState(): ConnState {
    return this.state;
  }

  private setState(s: ConnState) {
    this.state = s;
    this.connListeners.forEach((l) => l(s));
  }

  onConn(listener: (s: ConnState) => void): () => void {
    this.connListeners.add(listener);
    listener(this.state);
    return () => this.connListeners.delete(listener);
  }

  private url(): string {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/v1/ws`;
  }

  connect(): void {
    if (this.state === "denied") return;
    if (this.ws && (this.state === "open" || this.state === "connecting")) return;
    this.intentionalClose = false;
    this.setState("connecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.setState("open");
      this.reconnectDelay = 1000;
      // (re)subscribe to every live topic, resuming from the last applied event.
      const topics = [...this.handlers.keys()].filter((t) => t !== "*");
      if (topics.length) this.subscribeRemote(topics);
    };
    ws.onmessage = (ev) => {
      let env: Envelope;
      try {
        env = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      this.receive(env);
    };
    ws.onclose = () => {
      this.ws = null;
      if (this.state === "denied") return;
      this.setState("closed");
      if (!this.intentionalClose) this.scheduleReconnect();
    };
    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    };
  }

  private subscribeRemote(topics: string[]) {
    this.send(subscribeMessage(topics, this.cursor.cursor));
  }

  // Route one inbound frame: protocol frames update connection state, product
  // events are applied at most once and dispatched to whoever asked for them.
  private receive(env: Envelope) {
    if (env.type === "ack") {
      const ack = env as unknown as SubscribeAck;
      this.topics.applyAck(ack);
      this.cursor.applyAck(ack);
      return;
    }
    if (env.type === REPLAY_COMPLETE_TYPE) {
      // The batch this receipt closes was fully written before it was sent. Its
      // cursor is adopted only where the batch covered every topic this socket
      // holds; a partial batch reports what it wrote and retires nothing, since
      // live frames for the topics it did not read are still queued below it. A
      // replay cut short never produces a receipt at all, and the reconnect
      // then resumes from the last frame actually applied.
      this.cursor.applyReplayComplete(env as unknown as ReplayCompleteFrame);
      return;
    }
    if (env.type === RESET_TYPE) {
      const reset = env as unknown as ResetFrame;
      this.cursor.applyReset(reset);
      // The cursor a reset invalidates is one number for the whole socket, so
      // every subscriber resynchronises from REST, not only the ones the frame
      // named: a screen on a topic the batch never read has just had its resume
      // point moved out from under it too.
      const targets = [...this.handlers.keys()].filter((t) => t !== "*");
      new Set(targets).forEach((topic) => this.dispatch(topic, env));
      this.dispatch("*", env);
      return;
    }
    if (env.type === REVOKED_TYPE) {
      const frame = { type: env.type, payload: env.payload } as RevokedFrame;
      if (!isFatalRevocation(frame)) {
        // Either only what the credential may read shrank (`scope_revoked`,
        // socket stays open), or the server could not run the check and closed
        // the socket rather than stream behind it (`revalidation_failed`, which
        // carries no topics). Neither is a reason to stop reconnecting: tell
        // the screens that lost a stream, forget those topics, and let the
        // normal reconnect re-ask for them, so a membership that is restored -
        // or a store that comes back - returns on its own.
        const affected = frame.payload?.topics ?? [];
        const local = new Set(affected.flatMap((topic) => this.topics.listeners(topic)));
        local.forEach((requested) => {
          this.dispatch(requested, env);
          this.topics.forget(requested);
        });
        this.dispatch("*", env);
        return;
      }
      // The credential itself stopped holding. Reconnecting with it would only
      // be refused again, so stop and say so.
      this.intentionalClose = true;
      this.setState("denied");
      this.dispatch("*", env);
      return;
    }
    if (isControlFrame(env)) return;
    if (!this.cursor.accept(env)) return;
    this.topics.listeners(env.topic).forEach((topic) => this.dispatch(topic, env));
    // also dispatch on a wildcard for the whole stream
    this.dispatch("*", env);
  }

  private scheduleReconnect() {
    if (this.reconnectTimer != null) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 1.7, 15000);
  }

  private dispatch(topic: string, env: Envelope) {
    this.handlers.get(topic)?.forEach((h) => {
      try {
        h(env);
      } catch {
        /* subscriber error is non-fatal */
      }
    });
  }

  private send(obj: unknown) {
    if (this.ws && this.state === "open") {
      try {
        this.ws.send(JSON.stringify(obj));
      } catch {
        /* ignore */
      }
    }
  }

  subscribe(topic: string, handler: Handler): () => void {
    let set = this.handlers.get(topic);
    const isNew = !set;
    if (!set) {
      set = new Set();
      this.handlers.set(topic, set);
    }
    set.add(handler);
    if (isNew && topic !== "*") this.subscribeRemote([topic]);
    this.connect();

    return () => {
      const s = this.handlers.get(topic);
      if (!s) return;
      s.delete(handler);
      if (s.size === 0) {
        this.handlers.delete(topic);
        if (topic !== "*") {
          this.send({ type: "unsubscribe", topics: [topic] });
          this.topics.forget(topic);
        }
      }
    };
  }

  // Upstream chat over the socket (WS3 §3.3). Falls back to REST elsewhere.
  // The server accepts this on a Session's own chat stream only, so the topic
  // is built from the Session rather than taken from a caller.
  chatSend(sessionId: string | number, text: string, ref?: string) {
    this.send({
      type: "chat.send",
      topic: `session/${sessionId}/chat`,
      payload: { text },
      ref,
    });
  }

  close() {
    this.intentionalClose = true;
    this.ws?.close();
    this.ws = null;
  }
}

export const realtime = new RealtimeClient();

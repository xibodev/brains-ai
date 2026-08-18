import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Approval, Session, SessionCommand, SessionEvent } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { useDock } from "../store/DockContext";
import { useTopic } from "../realtime/useRealtime";
import { type Envelope } from "../realtime/client";
import { AskCard } from "./AskCard";
import { ApprovalToast } from "./ApprovalToast";
import { useToast } from "./Toast";
import { relativeTime } from "./format";
import {
  type ScopedState,
  isCurrent,
  patchById,
  removeById,
  sameSession,
  scopedClear,
  scopedGet,
  scopedUpdate,
  sessionKey,
  upsertById,
} from "./sessionScope";

interface ThreadMsg {
  id: string;
  who: string;
  mine: boolean;
  text: string;
  ts?: string;
  /** Durable command state for an operator message; absent for transcript. */
  status?: string;
  result?: string | null;
  error?: string | null;
  /** Which durable command this row is, where it is one. */
  kind?: string;
  /** The idempotency handle, so a failed send retries as the same command. */
  operationId?: string;
}

// A durable command's state, phrased for an operator rather than for a log.
const STATUS_LABEL: Record<string, string> = {
  requested: "queued",
  delivered: "delivering",
  acknowledged: "delivered",
  failed: "not delivered",
  cancelled: "cancelled",
  sending: "sending…",
};

function statusLabel(msg: ThreadMsg): string | null {
  if (!msg.status) return null;
  if (msg.status === "acknowledged" && msg.result && msg.result !== "delivered") {
    return msg.result.replace(/_/g, " ");
  }
  if (msg.status === "failed" && msg.result === "unsupported") return "not supported";
  return STATUS_LABEL[msg.status] ?? msg.status;
}

function newOperationId(): string {
  const c = globalThis.crypto as { randomUUID?: () => string } | undefined;
  if (c?.randomUUID) return c.randomUUID();
  return `op-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function commandToMsg(command: SessionCommand): ThreadMsg {
  const label = command.kind === "stop" ? "stop requested" : (command.text ?? "");
  return {
    id: command.command_id,
    who: "you",
    mine: true,
    text: label,
    ts: command.created_at,
    status: command.status,
    result: command.result ?? null,
    error: command.error ?? null,
    kind: command.kind,
  };
}

// Persistent right-hand dock: running sessions grouped, the selected thread
// with interleaved governance cards, and a composer over the durable Session
// command queue (BL-P0-05) — nothing renders as sent before the server has
// recorded it, and a reload replays the history the server holds rather than
// whatever this tab happened to remember.
//
// Every request captures the Session it was issued for and is discarded if the
// operator has moved on by the time it answers (see `./sessionScope`): an
// in-flight send, stop or detail read must never write one Session's outcome
// into the thread of another.
export function ChatDock() {
  const { activeOrg } = useOrg();
  const { selectedSession, openInChat, collapsed, toggleCollapsed, setInboxCount } = useDock();
  const { toast } = useToast();

  const [sessions, setSessions] = useState<Session[]>([]);
  const [detail, setDetail] = useState<Session | null>(null);
  const [transcript, setTranscript] = useState<ThreadMsg[]>([]);
  const [commands, setCommands] = useState<SessionCommand[]>([]);
  const [pending, setPending] = useState<ScopedState<ThreadMsg[]>>({});
  const [stoppingSession, setStoppingSession] = useState<string | null>(null);
  const [govItems, setGovItems] = useState<Approval[]>([]);
  const [draft, setDraft] = useState("");
  const threadRef = useRef<HTMLDivElement>(null);
  // The Session the view is showing *now*, readable from an async callback
  // that closed over an older render.
  const currentSession = useRef<string | null>(sessionKey(selectedSession));
  currentSession.current = sessionKey(selectedSession);

  const selectedKey = sessionKey(selectedSession);
  const stopping = stoppingSession !== null && stoppingSession === selectedKey;
  const pendingHere = scopedGet(pending, selectedKey, [] as ThreadMsg[]);

  // session list
  useEffect(() => {
    let cancelled = false;
    api
      .listSessions({ status: "running" })
      .then((s) => !cancelled && setSessions(s))
      .catch(() => !cancelled && setSessions([]));
    return () => {
      cancelled = true;
    };
  }, [activeOrg?.slug]);

  // live session-list updates
  useTopic("org/default/sessions", () => {
    api.listSessions({ status: "running" }).then(setSessions).catch(() => undefined);
  });

  // Durable backfill: the transcript, the command history, and the Session's
  // own message capability. All three come from the server, so a reload shows
  // exactly what the server holds. Each response is applied only while its
  // Session is still the selected one — `cancelled` covers the unmount, and
  // the captured key covers a second selection that resolved first.
  useEffect(() => {
    setTranscript([]);
    setCommands([]);
    setGovItems([]);
    setDetail(null);
    const scope = sessionKey(selectedSession);
    setPending((cur) => scopedClear(cur, scope));
    if (!selectedSession || scope === null) return;
    let cancelled = false;
    const applies = () => !cancelled && isCurrent(currentSession, scope);
    api
      .getSession(selectedSession)
      .then((s) => applies() && setDetail(s))
      .catch(() => undefined);
    api
      .sessionEvents(selectedSession, { limit: "50" })
      .then((events: SessionEvent[]) => {
        if (!applies()) return;
        setTranscript(
          events
            .filter((e) => e.message || e.chunk)
            .map((e, i) => ({
              id: `e${i}`,
              who: "session",
              mine: false,
              text: e.message ?? e.chunk ?? "",
              ts: e.created_at,
            })),
        );
      })
      .catch(() => undefined);
    api
      .sessionCommands(selectedSession, { limit: "100" })
      .then((rows) => applies() && setCommands(rows))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [selectedSession]);

  // Command state is only ever applied to the thread it belongs to: a frame or
  // a response for another Session is dropped rather than merged into the
  // visible one.
  const upsertCommand = (command: SessionCommand, scope: string | null) => {
    if (!isCurrent(currentSession, scope)) return;
    setCommands((cur) => {
      const next = cur.filter((c) => c.command_id !== command.command_id);
      next.push(command);
      next.sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
      return next;
    });
  };

  // Live transcript + durable command state for the selected session. A
  // command frame is applied by command_id, so a duplicate delivery (or a
  // replayed catch-up frame) updates the same row instead of adding one.
  useTopic(
    selectedSession
      ? [`session/${selectedSession}/stdout`, `session/${selectedSession}/chat`]
      : null,
    (env: Envelope) => {
      const p = (env.payload ?? {}) as Partial<SessionCommand> & {
        chunk?: string;
        text?: string;
      };
      if (env.type === "session.command" && p.command_id) {
        if (p.session_id && !sameSession(p.session_id, currentSession.current)) return;
        upsertCommand(p as SessionCommand, currentSession.current);
        return;
      }
      const text = p.chunk ?? p.text ?? "";
      if (!text) return;
      setTranscript((cur) => [
        ...cur,
        {
          id: `${env.event_id ?? env.seq ?? Date.now()}`,
          who: "session",
          mine: false,
          text,
          ts: env.ts,
        },
      ]);
    },
  );

  // inbox topic injects ask/approval cards into the dock + drives bell count
  useTopic("org/default/inbox", (env) => {
    if (
      env.type === "approval.resolved" ||
      env.type === "ask_human.answered" ||
      env.type === "approval.created" ||
      env.type === "ask_human.created"
    ) {
      refreshInbox();
    }
  });

  const refreshInbox = () => {
    if (!activeOrg) return;
    api
      .listApprovals()
      .then((items) => {
        setInboxCount(items.length);
        // surface items tied to the open session in the thread
        if (selectedSession) {
          setGovItems(
            items.filter(
              (a) =>
                String(a.session_id ?? a.from_session_id ?? "") === String(selectedSession),
            ),
          );
        }
      })
      .catch(() => undefined);
  };

  useEffect(() => {
    refreshInbox();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeOrg?.slug, selectedSession]);

  const messages: ThreadMsg[] = [...transcript, ...commands.map(commandToMsg), ...pendingHere];

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [commands, transcript, pending, govItems]);

  const capability = detail?.message_capability;
  const blockedReason = capability && capability.supported === false ? capability.reason : null;
  const ended = detail ? detail.status === "ended" || Boolean(detail.ended_at) : false;
  const composerDisabled = !selectedSession || Boolean(blockedReason) || ended;

  const submit = async (text: string, operationId: string) => {
    const scope = sessionKey(selectedSession);
    if (!selectedSession || scope === null) return;
    const target = selectedSession;
    setPending((cur) =>
      scopedUpdate(cur, scope, [] as ThreadMsg[], (items) =>
        upsertById(items, {
          id: operationId,
          who: "you",
          mine: true,
          text,
          status: "sending",
          operationId,
        }),
      ),
    );
    try {
      const command = await api.messageSession(target, text, operationId);
      setPending((cur) =>
        scopedUpdate(cur, scope, [] as ThreadMsg[], (items) => removeById(items, operationId)),
      );
      upsertCommand(command, scope);
      // A failure the operator is no longer looking at is still recorded and
      // still shown by that thread's own history; announcing it over another
      // Session would attribute it to work that never failed.
      if (command.status === "failed" && isCurrent(currentSession, scope)) {
        toast(command.error ?? "The message could not be delivered");
      }
    } catch (e) {
      // The send is retryable with the SAME operation id, so a retry can never
      // queue a second prompt even if the first request did reach the server.
      setPending((cur) =>
        scopedUpdate(cur, scope, [] as ThreadMsg[], (items) =>
          patchById(items, operationId, (p) => ({
            ...p,
            status: "failed",
            error: e instanceof Error ? e.message : "the message was not accepted",
          })),
        ),
      );
      if (isCurrent(currentSession, scope)) toast("Message failed to send — press retry");
    }
  };

  const send = async () => {
    if (!draft.trim() || !selectedSession) return;
    const text = draft.trim();
    setDraft("");
    await submit(text, newOperationId());
  };

  const retry = async (msg: ThreadMsg) => {
    if (!msg.operationId) return;
    await submit(msg.text, msg.operationId);
  };

  const stop = async () => {
    const scope = sessionKey(selectedSession);
    if (!selectedSession || scope === null) return;
    const target = selectedSession;
    setStoppingSession(scope);
    try {
      const command = await api.stopSession(target);
      upsertCommand(command, scope);
      const outcome =
        command.status === "acknowledged"
          ? command.result === "already_terminal"
            ? "Session had already finished"
            : "Session stopped"
          : command.status === "failed"
            ? (command.error ?? "Stop failed")
            : "Stop requested";
      if (isCurrent(currentSession, scope)) toast(outcome);
      api
        .getSession(target)
        .then((s) => isCurrent(currentSession, scope) && setDetail(s))
        .catch(() => undefined);
    } catch (e) {
      if (isCurrent(currentSession, scope)) {
        toast(e instanceof Error ? e.message : "Stop failed");
      }
    } finally {
      setStoppingSession((cur) => (cur === scope ? null : cur));
    }
  };

  const answerAsk = async (code: string, answer: string) => {
    await api.answerAsk(code, answer);
    toast("Answer sent");
    refreshInbox();
  };
  const resolveApproval = async (
    code: string,
    chosen: string,
    reasoning: string | undefined,
    status: "resolved" | "rejected",
  ) => {
    await api.resolveApproval(code, chosen, reasoning, status);
    toast(status === "resolved" ? "Approved" : "Rejected");
    refreshInbox();
  };

  if (collapsed) {
    return (
      <aside className="dock collapsed">
        <button className="icon-btn" onClick={toggleCollapsed} aria-label="Expand chat" title="Chat">
          ◌
        </button>
      </aside>
    );
  }

  return (
    <aside className="dock">
      <div className="dock-head">
        <span className="eyebrow">
          <span>Sessions</span>
        </span>
        <div className="row">
          {selectedSession && !ended && (
            <button
              className="btn small danger"
              onClick={() => void stop()}
              disabled={stopping}
              aria-label="Stop session"
            >
              Stop
            </button>
          )}
          <button className="icon-btn" onClick={toggleCollapsed} aria-label="Collapse chat">
            ⟩
          </button>
        </div>
      </div>

      <div className="session-list">
        {sessions.length === 0 && (
          <div className="meta" style={{ padding: 8 }}>
            No running sessions.
          </div>
        )}
        {sessions.map((s) => (
          <button
            key={String(s.id)}
            className={`session-row ${String(selectedSession) === String(s.id) ? "active" : ""}`}
            onClick={() => openInChat(s.id)}
          >
            <span className="dot live" />
            <span>{s.persona_name ?? s.tool ?? `session ${s.id}`}</span>
            <span className="code">{s.issue_id ? `#${s.issue_id}` : ""}</span>
          </button>
        ))}
      </div>

      <div className="thread" ref={threadRef}>
        {!selectedSession && (
          <div className="meta" style={{ padding: 8 }}>
            Select a session to open its thread.
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`bubble ${m.mine ? "you" : ""}`}>
            <div className="who">
              <span>{m.mine ? "you" : m.who}</span>
              {m.ts && <span>{relativeTime(m.ts)}</span>}
            </div>
            {m.text}
            {statusLabel(m) && (
              <div className="meta" style={{ marginTop: 4 }}>
                {statusLabel(m)}
                {m.error ? ` — ${m.error}` : ""}
                {m.status === "failed" && m.operationId && (
                  <button
                    className="btn small"
                    style={{ marginLeft: 8 }}
                    onClick={() => void retry(m)}
                  >
                    Retry
                  </button>
                )}
                {/* A stop that failed without ending the Session is retryable
                    by pressing stop again: the server mints a new durable
                    attempt rather than handing back the dead one. */}
                {m.status === "failed" && !m.operationId && m.kind === "stop" && !ended && (
                  <button
                    className="btn small"
                    style={{ marginLeft: 8 }}
                    disabled={stopping}
                    onClick={() => void stop()}
                  >
                    Retry stop
                  </button>
                )}
              </div>
            )}
          </div>
        ))}
        {govItems.map((g) =>
          g.kind === "ask_human" ? (
            <AskCard key={g.code} ask={g} onAnswer={answerAsk} />
          ) : (
            <ApprovalToast key={g.code} approval={g} onResolve={resolveApproval} />
          ),
        )}
      </div>

      {blockedReason && (
        <div className="meta" style={{ padding: "6px 8px" }} role="note">
          Messaging is unavailable for this session: {blockedReason}
        </div>
      )}

      <div className="composer">
        <textarea
          value={draft}
          placeholder={
            !selectedSession
              ? "select a session"
              : blockedReason
                ? "this agent cannot receive messages"
                : ended
                  ? "this session has ended"
                  : "message session…"
          }
          disabled={composerDisabled}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <button className="btn primary" disabled={composerDisabled} onClick={() => void send()}>
          ▷
        </button>
      </div>
    </aside>
  );
}

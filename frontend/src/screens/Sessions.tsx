import { useState } from "react";
import { api } from "../api/client";
import type { Session, SessionEvent } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { useAsync } from "../store/useAsync";
import { useTopic } from "../realtime/useRealtime";
import { ScreenHead } from "./ScreenHead";
import { SoftCard } from "../components/SoftCard";
import { StatusPill } from "../components/StatusPill";
import { AsyncBoundary } from "../components/EmptyState";
import { useToast } from "../components/Toast";
import { relativeTime } from "../components/format";

// Labs Session view: read-centric detail plus truthful, capability-gated messaging.
export function Sessions() {
  const { activeOrg } = useOrg();
  const { toast } = useToast();
  const [selected, setSelected] = useState<Session | null>(null);

  const state = useAsync<Session[]>(() => api.listSessions(), [activeOrg?.slug]);
  useTopic("org/default/sessions", () => state.refetch());

  const stop = async (s: Session) => {
    try {
      // The stop is a durable command: its recorded outcome is what the
      // operator is told, so a Runtime that could not reach the process
      // reports that rather than a stop that never happened (BL-P0-05).
      const command = await api.stopSession(s.id);
      if (command.status === "acknowledged") {
        toast(
          command.result === "already_terminal"
            ? "Session had already finished"
            : "Session stopped",
        );
      } else if (command.status === "failed") {
        toast(command.error ?? "Stop failed");
      } else {
        toast("Stop requested — waiting for the runtime");
      }
      state.refetch();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Stop failed");
    }
  };

  return (
    <div>
      <ScreenHead eyebrow="Sessions" title="Live & recent executions" />
      <AsyncBoundary
        state={state}
        emptyTitle="No sessions yet"
        emptyBody="Spawn a persona on an issue and its execution shows up here."
      >
        {(sessions) => (
          <div style={{ display: "grid", gridTemplateColumns: "minmax(260px, 360px) 1fr", gap: 24 }}>
            <div className="card-list">
              {sessions.map((s) => (
                <SoftCard
                  key={String(s.id)}
                  interactive
                  onClick={() => setSelected(s)}
                  style={selected?.id === s.id ? { background: "var(--fill-active)" } : undefined}
                >
                  <div className="row spread">
                    <strong>{s.persona_name ?? s.tool ?? `session ${s.id}`}</strong>
                    <StatusPill label={s.state ?? s.status ?? "—"} dot />
                  </div>
                  <div className="meta" style={{ marginTop: 6 }}>
                    {s.issue_id ? `issue #${s.issue_id} · ` : ""}
                    {relativeTime(s.last_activity_at ?? s.started_at)}
                  </div>
                </SoftCard>
              ))}
            </div>
            <div>
              {selected ? (
                <SessionDetail
                  key={String(selected.id)}
                  session={selected}
                  onStop={() => void stop(selected)}
                />
              ) : (
                <div className="meta">Select a session to inspect its event stream.</div>
              )}
            </div>
          </div>
        )}
      </AsyncBoundary>
    </div>
  );
}

function SessionDetail({
  session,
  onStop,
}: {
  session: Session;
  onStop: () => void;
}) {
  const { toast } = useToast();
  const [messagingOpen, setMessagingOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const events = useAsync<SessionEvent[]>(
    () => api.sessionEvents(session.id, { limit: "100" }),
    [session.id],
  );
  useTopic([`session/${session.id}/stdout`], () => events.refetch());

  const capability = session.message_capability;
  const ended = session.status === "ended" || Boolean(session.ended_at);
  const blockedReason = capability?.supported === true
    ? null
    : capability?.reason || "the runtime did not report an interactive input channel";
  const composerDisabled = ended || Boolean(blockedReason) || sending;

  const send = async () => {
    const text = draft.trim();
    if (!text || composerDisabled) return;
    setSending(true);
    try {
      const command = await api.messageSession(session.id, text, crypto.randomUUID());
      if (command.status === "failed") {
        toast(command.error ?? "The message could not be delivered");
      } else {
        setDraft("");
        toast(command.status === "acknowledged" ? "Message delivered" : "Message queued");
      }
    } catch (error) {
      toast(error instanceof Error ? error.message : "Message failed to send");
    } finally {
      setSending(false);
    }
  };

  return (
    <div>
      <div className="row spread" style={{ marginBottom: 12 }}>
        <h2>{session.persona_name ?? `Session ${session.id}`}</h2>
        <div className="row">
          <button className="btn small" onClick={() => setMessagingOpen((open) => !open)}>
            {messagingOpen ? "Hide messaging" : "Message session"}
          </button>
          {(session.state === "running" || session.status === "running") && (
            <button className="btn small danger" onClick={onStop}>
              Stop
            </button>
          )}
        </div>
      </div>
      <div className="row wrap" style={{ marginBottom: 16 }}>
        <StatusPill label={session.state ?? session.status ?? "—"} dot />
        {session.tool && <StatusPill label={session.tool} tone="accent" />}
        {session.issue_id && <span className="meta">issue #{session.issue_id}</span>}
        {session.duration_seconds != null && (
          <span className="meta">{Math.round(session.duration_seconds)}s</span>
        )}
      </div>
      {messagingOpen && (
        <SoftCard style={{ marginBottom: 16 }}>
          {blockedReason && (
            <div className="meta" role="note" style={{ marginBottom: 8 }}>
              Messaging is unavailable for this session: {blockedReason}
            </div>
          )}
          <div className="composer" style={{ padding: 0, borderTop: 0 }}>
            <textarea
              value={draft}
              placeholder={
                blockedReason
                  ? "this agent cannot receive messages"
                  : ended
                    ? "this session has ended"
                    : "message session..."
              }
              disabled={composerDisabled}
              onChange={(event) => setDraft(event.target.value)}
            />
            <button
              className="btn primary"
              disabled={composerDisabled || !draft.trim()}
              onClick={() => void send()}
            >
              {sending ? "Sending..." : "Send"}
            </button>
          </div>
        </SoftCard>
      )}
      <div className="eyebrow" style={{ marginBottom: 8 }}><span>Event stream</span></div>
      <SoftCard>
        {(events.data ?? []).length === 0 ? (
          <div className="meta">No events captured.</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {(events.data ?? []).map((e, i) => (
              <div key={i} className="row" style={{ gap: 8, alignItems: "baseline" }}>
                <span className="meta" style={{ minWidth: 64 }}>
                  {e.kind ?? e.stream ?? "evt"}
                </span>
                <span className="mono" style={{ whiteSpace: "pre-wrap" }}>
                  {e.message ?? e.chunk ?? ""}
                </span>
              </div>
            ))}
          </div>
        )}
      </SoftCard>
    </div>
  );
}

import { useState } from "react";
import { api } from "../api/client";
import type { Session, SessionEvent } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { useDock } from "../store/DockContext";
import { useAsync } from "../store/useAsync";
import { useTopic } from "../realtime/useRealtime";
import { ScreenHead } from "./ScreenHead";
import { SoftCard } from "../components/SoftCard";
import { StatusPill } from "../components/StatusPill";
import { AsyncBoundary } from "../components/EmptyState";
import { useToast } from "../components/Toast";
import { relativeTime } from "../components/format";

// Sessions — list → read-centric detail (event stream). Steering is the dock.
export function Sessions() {
  const { activeOrg } = useOrg();
  const { openInChat } = useDock();
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
                  session={selected}
                  onOpenChat={() => openInChat(selected.id)}
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
  onOpenChat,
  onStop,
}: {
  session: Session;
  onOpenChat: () => void;
  onStop: () => void;
}) {
  const events = useAsync<SessionEvent[]>(
    () => api.sessionEvents(session.id, { limit: "100" }),
    [session.id],
  );
  useTopic([`session/${session.id}/stdout`], () => events.refetch());

  return (
    <div>
      <div className="row spread" style={{ marginBottom: 12 }}>
        <h2>{session.persona_name ?? `Session ${session.id}`}</h2>
        <div className="row">
          <button className="btn small" onClick={onOpenChat}>
            Open in chat
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

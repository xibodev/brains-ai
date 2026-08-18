import { useState } from "react";
import { api } from "../api/client";
import type { Approval, Handoff } from "../api/types";
import { useOrg } from "../store/OrgContext";
import { useDock } from "../store/DockContext";
import { useAsync } from "../store/useAsync";
import { useTopic } from "../realtime/useRealtime";
import { ScreenHead } from "./ScreenHead";
import { SoftCard } from "../components/SoftCard";
import { AskCard } from "../components/AskCard";
import { ApprovalToast } from "../components/ApprovalToast";
import { EmptyState, Loading } from "../components/EmptyState";
import { useToast } from "../components/Toast";
import { relativeTime, truncate } from "../components/format";

type Tab = "approvals" | "asks" | "handoffs";

// Inbox / Approvals — the single governance triage queue (WS4 §3.1). Shares
// org/{slug}/inbox with the chat dock: an item answered here vanishes there.
export function Inbox() {
  const { activeOrg } = useOrg();
  const { openInChat } = useDock();
  const { toast } = useToast();
  const [tab, setTab] = useState<Tab>("approvals");

  const approvals = useAsync<Approval[]>(
    () => (activeOrg ? api.listApprovals() : Promise.resolve([])),
    [activeOrg?.slug],
  );
  const handoffs = useAsync<Handoff[]>(
    () => api.listHandoffs({ status: "active" }).catch(() => []),
    [activeOrg?.slug],
  );

  useTopic("org/default/inbox", () => {
    approvals.refetch();
    handoffs.refetch();
  });

  const asksList = (approvals.data ?? []).filter((a) => a.kind === "ask_human");
  const approvalsList = (approvals.data ?? []).filter((a) => a.kind !== "ask_human");

  const onAnswer = async (code: string, answer: string) => {
    await api.answerAsk(code, answer);
    toast("Answer sent");
    approvals.refetch();
  };
  const onResolve = async (
    code: string,
    chosen: string,
    reasoning: string | undefined,
    status: "resolved" | "rejected",
  ) => {
    await api.resolveApproval(code, chosen, reasoning, status);
    toast(status === "resolved" ? "Approved" : "Rejected");
    approvals.refetch();
  };

  const counts = {
    approvals: approvalsList.length,
    asks: asksList.length,
    handoffs: handoffs.data?.length ?? 0,
  };

  return (
    <div>
      <ScreenHead eyebrow="Inbox / Approvals" title="Things waiting on you" />

      <div className="tabs">
        <button className={`tab ${tab === "approvals" ? "active" : ""}`} onClick={() => setTab("approvals")}>
          Approvals {counts.approvals > 0 && `⬤${counts.approvals}`}
        </button>
        <button className={`tab ${tab === "asks" ? "active" : ""}`} onClick={() => setTab("asks")}>
          Asks {counts.asks > 0 && `⬤${counts.asks}`}
        </button>
        <button className={`tab ${tab === "handoffs" ? "active" : ""}`} onClick={() => setTab("handoffs")}>
          Handoffs {counts.handoffs > 0 && `⬤${counts.handoffs}`}
        </button>
      </div>

      {approvals.loading && approvals.data === undefined && <Loading />}

      {tab === "approvals" &&
        (approvalsList.length === 0 ? (
          <EmptyState title="No open approvals" body="Gated actions awaiting your decision will appear here." />
        ) : (
          <div className="card-list">
            {approvalsList.map((a) => (
              <SoftCard key={a.code}>
                <div className="row spread" style={{ marginBottom: 8 }}>
                  <div className="row">
                    <span className="mono">{a.code}</span>
                    <strong>{a.title ?? a.subject ?? "Approval"}</strong>
                  </div>
                  <span className="meta">
                    {a.persona_name ?? ""} · {relativeTime(a.created_at)}
                  </span>
                </div>
                <div className="meta" style={{ marginBottom: 8 }}>
                  {truncate(a.body, 180)}
                </div>
                <ApprovalToast approval={a} onResolve={onResolve} />
                {a.session_id && (
                  <button
                    className="btn ghost small"
                    style={{ marginTop: 8 }}
                    onClick={() => openInChat(a.session_id!)}
                  >
                    Open in chat
                  </button>
                )}
              </SoftCard>
            ))}
          </div>
        ))}

      {tab === "asks" &&
        (asksList.length === 0 ? (
          <EmptyState title="No open asks" body="ask_human tickets routed to you will appear here." />
        ) : (
          <div className="card-list">
            {asksList.map((a) => (
              <AskCard key={a.code} ask={a} onAnswer={onAnswer} />
            ))}
          </div>
        ))}

      {tab === "handoffs" &&
        (counts.handoffs === 0 ? (
          <EmptyState title="No active handoffs" body="Work needing a human picker will surface here." />
        ) : (
          <div className="card-list">
            {(handoffs.data ?? []).map((h) => (
              <SoftCard key={String(h.id)}>
                <div className="row spread">
                  <strong>{h.title ?? h.code ?? `Handoff ${h.id}`}</strong>
                  <span className="meta">{relativeTime(h.created_at)}</span>
                </div>
              </SoftCard>
            ))}
          </div>
        ))}
    </div>
  );
}

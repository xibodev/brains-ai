import { useState } from "react";
import type { Approval } from "../api/types";
import { relativeTime, truncate } from "./format";

// Inline / gate approval prompt with Approve / Reject / Edit (WS4 §4).
// Approve => resolve_decision(status=resolved); Reject => status=rejected.
export function ApprovalToast({
  approval,
  onResolve,
}: {
  approval: Approval;
  onResolve: (
    code: string,
    chosen: string,
    reasoning: string | undefined,
    status: "resolved" | "rejected",
  ) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [chosen, setChosen] = useState(approval.proposed_answer ?? "yes");
  const [busy, setBusy] = useState(false);

  const act = async (status: "resolved" | "rejected") => {
    setBusy(true);
    try {
      await onResolve(approval.code, status === "resolved" ? chosen : "no", undefined, status);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gov-card approval">
      <div className="gov-head">
        <span className="eyebrow">
          <span>APPROVAL · {approval.code}</span>
        </span>
        <span className="meta" style={{ marginLeft: "auto" }}>
          {approval.persona_name ?? "gate"}
          {approval.created_at ? ` · ${relativeTime(approval.created_at)}` : ""}
        </span>
      </div>
      <strong>{approval.title ?? approval.subject ?? "Approval requested"}</strong>
      <div className="meta" style={{ margin: "6px 0" }}>
        {truncate(approval.body, 200)}
      </div>
      {editing ? (
        <input value={chosen} onChange={(e) => setChosen(e.target.value)} />
      ) : (
        approval.proposed_answer && (
          <div className="meta">proposed: {approval.proposed_answer}</div>
        )
      )}
      <div className="gov-actions">
        <button className="btn small danger" disabled={busy} onClick={() => void act("rejected")}>
          Reject
        </button>
        <button className="btn small" disabled={busy} onClick={() => setEditing((e) => !e)}>
          {editing ? "Done" : "Edit answer"}
        </button>
        <button className="btn primary small" disabled={busy} onClick={() => void act("resolved")}>
          Approve ▷
        </button>
      </div>
    </div>
  );
}

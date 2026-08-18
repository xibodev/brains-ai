import { useState } from "react";
import type { Approval } from "../api/types";
import { relativeTime, truncate } from "./format";

// Inline answerable ask_human ticket (WS4 §4). Resolves help_requests.answer.
export function AskCard({
  ask,
  onAnswer,
}: {
  ask: Approval;
  onAnswer: (code: string, answer: string) => Promise<void>;
}) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!answer.trim()) return;
    setBusy(true);
    try {
      await onAnswer(ask.code, answer.trim());
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gov-card ask">
      <div className="gov-head">
        <span className="eyebrow">
          <span>ASK · {ask.code}</span>
        </span>
        <span className="meta" style={{ marginLeft: "auto" }}>
          {ask.persona_name ? `from ${ask.persona_name}` : ""}
          {ask.expires_at ? ` · expires ${relativeTime(ask.expires_at)}` : ""}
        </span>
      </div>
      {ask.subject && <strong>{ask.subject}</strong>}
      <div className="meta" style={{ margin: "6px 0" }}>
        {truncate(ask.question ?? ask.body, 200)}
      </div>
      <input
        value={answer}
        placeholder="answer…"
        onChange={(e) => setAnswer(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void submit();
        }}
      />
      <div className="gov-actions">
        <button className="btn primary small" disabled={busy} onClick={() => void submit()}>
          Send answer
        </button>
      </div>
    </div>
  );
}

import { useState } from "react";
import { api } from "../api/client";
import type { OperatorDecision } from "../api/types";
import { relativeTime } from "../components/format";
import {
  OperatorCard,
  OperatorPageHead,
  OperatorState,
  OperatorStatus,
  displayValue,
} from "../components/OperatorPrimitives";
import { useToast } from "../components/Toast";
import { useAsync } from "../store/useAsync";

export function Governance() {
  const state = useAsync(() => api.operatorGovernance(), []);
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const { toast } = useToast();
  const selected = state.data?.decisions.find((row) => row.code === selectedCode) ?? state.data?.decisions[0];
  const empty = Boolean(state.data && !state.data.decisions.length && !state.data.actions.length && !state.data.audit.length);

  const resolve = async (decision: OperatorDecision, chosen: string, status = "resolved") => {
    setSaving(true);
    try {
      await api.resolveApproval(decision.code, chosen, note, status);
      toast(`${decision.code} ${status}`);
      setNote("");
      state.refetch();
    } catch {
      toast("The decision could not be resolved. Retry after checking authorization and local service status.");
    } finally {
      setSaving(false);
    }
  };

  const answer = async (decision: OperatorDecision) => {
    if (!note.trim()) return;
    setSaving(true);
    try {
      await api.answerAsk(decision.code, note.trim());
      toast(`${decision.code} answered`);
      setNote("");
      state.refetch();
    } catch {
      toast("The ask could not be answered. Retry after checking authorization and local service status.");
    } finally {
      setSaving(false);
    }
  };

  const verify = async () => {
    try {
      const result = await api.operatorAuditVerify();
      toast(result.ok === false ? "Audit verification reported a divergence" : "Audit chain verified");
      state.refetch();
    } catch {
      toast("The audit chain could not be verified. Retry after checking authorization and local service status.");
    }
  };

  return (
    <div className="operator-page" data-testid="governance">
      <OperatorPageHead
        eyebrow="Human authority and evidence"
        title="Governance"
        lede="Resolve what is waiting, follow every outward action through its decision spine, and verify the signed record."
        actions={<button className="operator-button" onClick={() => void verify()}>Verify audit chain</button>}
      />
      <OperatorState loading={state.loading} error={state.error} kind={state.errorKind} empty={empty} boundary="governance" emptyTitle="No governance state" emptyBody="No decisions, governed actions, or audit entries are currently visible." />
      {state.data && !empty && (
        <div className="operator-governance-layout">
          <OperatorCard kicker="Decision queue" title={`${state.data.decisions.length} open`}>
            <div className="operator-decision-queue">
              {state.data.decisions.map((decision) => (
                <button key={decision.code} className={selected?.code === decision.code ? "selected" : ""} onClick={() => setSelectedCode(decision.code)}>
                  <div><code>{decision.code}</code><OperatorStatus tone="warning">{decision.kind === "ask_human" ? "ask" : "open"}</OperatorStatus></div>
                  <strong>{decision.title}</strong><small>{decision.workspace} / {relativeTime(decision.created_at)}</small>
                </button>
              ))}
              {!state.data.decisions.length && <OperatorState loading={false} empty emptyTitle="No open decisions" emptyBody="Governed effects will appear here before they may run." />}
            </div>
          </OperatorCard>

          {selected ? (
            <article className="operator-decision-card">
              <header><code>{selected.code} / OPEN</code><h2>{selected.title}</h2><p>Requested in <strong>{selected.workspace}</strong>{selected.session_id ? ` by session ${selected.session_id.slice(0, 12)}` : ""}.</p></header>
              <div className="operator-decision-body">
                <dl>
                  <dt>Kind</dt><dd>{selected.kind || "approval"}</dd>
                  <dt>Workspace</dt><dd>{selected.workspace}</dd>
                  <dt>Proposed answer</dt><dd>{selected.proposed_answer || "None"}</dd>
                  <dt>Created</dt><dd>{relativeTime(selected.created_at)}</dd>
                  <dt>Request detail</dt><dd>{selected.body || "No additional detail supplied."}</dd>
                </dl>
                <div className="operator-effect"><strong>{selected.kind === "ask_human" ? "Human answer" : "Human authority boundary"}</strong>{selected.kind === "ask_human" ? "The answer resolves this durable question and remains attributable to the browser operator." : "Approval resolves this durable request. It does not grant a generic browser terminal or bypass the governed-action argument scope."}</div>
                <label className="operator-field"><span>{selected.kind === "ask_human" ? "Answer" : "Decision note"}</span><textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder={selected.kind === "ask_human" ? "Answer the agent's question" : "Record why this decision is appropriate"} /></label>
              </div>
              <footer>
                {selected.kind === "ask_human" ? (
                  <button className="operator-button primary" disabled={saving || !note.trim()} onClick={() => void answer(selected)}>Send answer</button>
                ) : (
                  <>
                    <button className="operator-button" disabled={saving} onClick={() => void resolve(selected, "defer", "deferred")}>Defer</button>
                    <button className="operator-button danger" disabled={saving} onClick={() => void resolve(selected, "reject", "rejected")}>Reject</button>
                    <button className="operator-button primary" disabled={saving} onClick={() => void resolve(selected, "approve", "resolved")}>Approve once</button>
                  </>
                )}
              </footer>
            </article>
          ) : (
            <OperatorCard kicker="Decision" title="Nothing awaiting resolution"><p className="operator-muted">The decision detail appears when a request is open.</p></OperatorCard>
          )}

          <OperatorCard kicker="Governed action spine" title="Recent effects">
            <div className="operator-spine">
              {state.data.actions.slice(0, 8).map((action, index) => (
                <div className={`operator-spine-row ${action.status === "pending" ? "pending" : ""}`} key={String(action.action_id || index)}>
                  <strong>{displayValue(action.action, "Governed action")}</strong>
                  <small>{displayValue(action.status)} / {relativeTime(typeof action.created_at === "string" ? action.created_at : undefined)}</small>
                  <code>{displayValue(action.tool, "control")}</code>
                </div>
              ))}
              {!state.data.actions.length && <span className="operator-muted">No governed actions recorded.</span>}
            </div>
            <div className="operator-audit-summary">
              <span>Audit entries <strong>{state.data.audit.length}</strong></span>
              <span>Chain <strong>{state.data.chain ? displayValue(state.data.chain.ok, "reported") : "install admin only"}</strong></span>
            </div>
          </OperatorCard>
        </div>
      )}
    </div>
  );
}

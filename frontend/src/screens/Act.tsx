import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, formatApiError } from "../api/client";
import type { OperatorCapability, OperatorTransport, OperatorWorkspace } from "../api/types";
import {
  OperatorCard,
  OperatorPageHead,
  OperatorState,
  OperatorStatus,
} from "../components/OperatorPrimitives";
import { useToast } from "../components/Toast";
import { useOperator } from "../store/OperatorContext";
import { useAsync } from "../store/useAsync";

const CATEGORY_LABELS: Record<string, string> = {
  coordination: "Coordination",
  governance: "Governance",
  operations: "Operations",
};

export function Act() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const { catalog, loading, error } = useOperator();
  const workspaces = useAsync(() => api.operatorWorkspaces(), []);
  const categories = Array.from(new Set((catalog?.data ?? []).map((row) => row.category)));
  const category = params.get("category") || catalog?.data.find((row) => row.key === params.get("capability"))?.category || categories[0];
  const available = catalog?.data.filter((row) => row.category === category) ?? [];
  const selected = available.find((row) => row.key === params.get("capability")) || available[0];

  const selectCategory = (next: string) => setParams({ category: next });
  const selectCapability = (next: OperatorCapability) => {
    const nextParams = new URLSearchParams(params);
    nextParams.set("category", next.category);
    nextParams.set("capability", next.key);
    setParams(nextParams);
  };

  return (
    <div className="operator-page" data-testid="act">
      <OperatorPageHead
        eyebrow="Typed capability launcher"
        title="Act"
        lede="Choose a control operation, target its scope, preview its durable effect, and execute through a typed HTTP contract. This is not a terminal."
      />
      <OperatorState loading={loading || workspaces.loading} error={error || workspaces.error} />
      {catalog && workspaces.data && (
        <>
          <section className="operator-parity-banner">
            <strong>Browser parity is explicit</strong>
            <div>{["native_http", "thin_adapter", "host_contract"].map((transport) => <span key={transport}><b>{catalog.data.filter((row) => row.transport === transport).length}</b> {transport.replace("_", " ")}</span>)}</div>
          </section>
          <div className="operator-act-layout">
            <OperatorCard kicker="Categories" title="Operator jobs">
              <div className="operator-category-list">
                {categories.map((name) => <button key={name} className={category === name ? "active" : ""} onClick={() => selectCategory(name)}><span>{CATEGORY_LABELS[name] || name}</span><b>{catalog.data.filter((row) => row.category === name).length}</b></button>)}
              </div>
            </OperatorCard>

            <div className="operator-capability-grid">
              {available.map((capability) => <button key={capability.key} className={`operator-capability ${selected?.key === capability.key ? "selected" : ""}`} onClick={() => selectCapability(capability)}><div><strong>{capability.label}</strong><Transport transport={capability.transport} /></div><p>{capability.reason || capabilityDescription(capability.key)}</p><code>{capability.scope} scope</code></button>)}
            </div>

            {selected && <ActionSheet capability={selected} workspaces={workspaces.data} initialWorkspace={params.get("workspace") || undefined} navigate={navigate} />}
          </div>
        </>
      )}
    </div>
  );
}

function Transport({ transport }: { transport: OperatorTransport }) {
  const tone = transport === "native_http" ? "native" : transport === "thin_adapter" ? "adapter" : "host";
  return <OperatorStatus tone={tone}>{transport.replace("_", " ")}</OperatorStatus>;
}

function capabilityDescription(key: string): string {
  const descriptions: Record<string, string> = {
    "task.create": "Create durable work in a visible workspace.",
    "workspace.claim": "Take time-bounded ownership using a live workspace session.",
    "handoff.set": "Leave durable continuity context for the next operator or agent.",
    "message.send": "Send a workspace-scoped coordination message.",
    "topic.post": "Post a scoped update to an agent topic board.",
    "knowledge.add": "Record a reusable blocker, workaround, resolution, or caveat.",
    "pattern.decide": "Approve or reject a global reusable pattern.",
    "decision.resolve": "Resolve a queued human decision from Governance.",
    "queue.repair.preview": "Preview bounded coordination queue repairs without mutation.",
  };
  return descriptions[key] || "Use the named typed contract for this operator job.";
}

function ActionSheet({ capability, workspaces, initialWorkspace, navigate }: { capability: OperatorCapability; workspaces: OperatorWorkspace[]; initialWorkspace?: string; navigate: (to: string) => void }) {
  const visibleInitialWorkspace = workspaces.some((row) => row.slug === initialWorkspace)
    ? initialWorkspace!
    : workspaces[0]?.slug || "";
  const [workspace, setWorkspace] = useState(visibleInitialWorkspace);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [kind, setKind] = useState(() => defaultKind(capability.key));
  const [saving, setSaving] = useState(false);
  const { toast } = useToast();

  useEffect(() => {
    setTitle("");
    setBody("");
    setSessionId("");
    setKind(defaultKind(capability.key));
  }, [capability.key]);

  const execute = async () => {
    setSaving(true);
    try {
      if (capability.key === "task.create") await api.operatorCreateTask(workspace, { title, body, priority: kind });
      else if (capability.key === "task.claim") await api.operatorClaimTask(title, sessionId);
      else if (capability.key === "task.complete") await api.operatorCompleteTask(title, sessionId, body);
      else if (capability.key === "task.release") await api.operatorReleaseTask(title, sessionId, body);
      else if (capability.key === "workspace.claim") await api.operatorClaimWorkspace(workspace, { session_id: sessionId, scope: title || "code", duration_minutes: 30 });
      else if (capability.key === "workspace.release") await api.operatorReleaseWorkspace(workspace, sessionId);
      else if (capability.key === "handoff.set") await api.operatorSetHandoff(workspace, { title, body, session_id: sessionId || undefined });
      else if (capability.key === "handoff.pick") await api.operatorPickHandoff(workspace, sessionId || undefined);
      else if (capability.key === "handoff.clear") await api.operatorClearHandoff(workspace, sessionId || undefined, body);
      else if (capability.key === "message.send") await api.operatorSendMessage(workspace, { subject: title, body, kind: "info" });
      else if (capability.key === "topic.post") await api.operatorPostTopic({ workspace, topic: kind, subject: title, body });
      else if (capability.key === "knowledge.add") await api.operatorAddKnowledge(workspace, { type: kind, title, body, scope: "workspace" });
      else if (capability.key === "knowledge.resolve") await api.operatorResolveKnowledge(title, kind);
      else if (capability.key === "pattern.decide") await api.operatorDecidePattern(title, kind === "approved");
      else if (capability.key === "decision.resolve") await api.resolveApproval(title, kind, body, kind === "rejected" ? "rejected" : kind === "deferred" ? "deferred" : "resolved");
      else if (capability.key === "audit.verify") await api.operatorAuditVerify();
      else if (capability.key === "tool.verify") await api.operatorVerifyTool(title);
      else if (capability.key === "queue.repair.preview") await api.repairQueueHealth(false);
      else throw new Error("Open the contextual screen to complete this action safely.");
      toast(`${capability.label} recorded`);
      setTitle(""); setBody(""); setSessionId("");
    } catch (error) {
      toast(formatApiError(capability.label, error));
    } finally {
      setSaving(false);
    }
  };

  const contextualRoute = capability.key === "decision.resolve" ? "/governance" : null;
  const runnable = capability.enabled && ["task.create", "task.claim", "task.complete", "task.release", "workspace.claim", "workspace.release", "handoff.set", "handoff.pick", "handoff.clear", "message.send", "topic.post", "knowledge.add", "knowledge.resolve", "pattern.decide", "decision.resolve", "audit.verify", "tool.verify", "queue.repair.preview"].includes(capability.key);
  const needsTitle = !["handoff.pick", "handoff.clear", "workspace.release", "audit.verify", "queue.repair.preview"].includes(capability.key);
  const needsSession = ["task.claim", "task.complete", "task.release", "workspace.claim", "workspace.release"].includes(capability.key);
  const needsWorkspace = ["task.create", "workspace.claim", "workspace.release", "handoff.set", "handoff.pick", "handoff.clear", "message.send", "topic.post", "knowledge.add"].includes(capability.key);
  const valid = runnable && (!needsTitle || title.trim()) && (!needsSession || sessionId.trim());
  const titleLabel = capability.key === "workspace.claim" ? "Scope" : ["task.claim", "task.complete", "task.release"].includes(capability.key) ? "Task code" : capability.key === "knowledge.resolve" ? "Knowledge code" : capability.key === "pattern.decide" ? "Pattern name" : capability.key === "decision.resolve" ? "Decision code" : capability.key === "tool.verify" ? "Tool name" : capability.key === "message.send" || capability.key === "topic.post" ? "Subject" : "Title";

  return (
    <aside className="operator-action-sheet">
      <header><Transport transport={capability.transport} /><h2>{capability.label}</h2><p>Named action: <code>{capability.key}</code></p></header>
      <div className="operator-sheet-body">
        {needsWorkspace ? <label className="operator-field"><span>Workspace</span><select value={workspace} onChange={(event) => setWorkspace(event.target.value)}>{workspaces.map((row) => <option key={row.slug} value={row.slug}>{row.name || row.slug}</option>)}</select></label> : null}
        {needsTitle && <label className="operator-field"><span>{titleLabel}</span><input value={title} onChange={(event) => setTitle(event.target.value)} /></label>}
        {capability.key === "task.create" && <label className="operator-field"><span>Priority</span><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="p0">p0</option><option value="p1">p1</option><option value="p2">p2</option><option value="p3">p3</option></select></label>}
        {capability.key === "topic.post" && <label className="operator-field"><span>Topic</span><input value={kind} onChange={(event) => setKind(event.target.value.toLowerCase().replace(/[^a-z0-9._-]/g, "-"))} /></label>}
        {capability.key === "knowledge.add" && <label className="operator-field"><span>Knowledge type</span><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="blocker">blocker</option><option value="workaround">workaround</option><option value="resolution">resolution</option><option value="caveat">caveat</option><option value="environment_note">environment note</option><option value="dependency_note">dependency note</option></select></label>}
        {capability.key === "knowledge.resolve" && <label className="operator-field"><span>Status</span><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="resolved">resolved</option><option value="confirmed">confirmed</option><option value="rejected">rejected</option><option value="stale">stale</option></select></label>}
        {capability.key === "pattern.decide" && <label className="operator-field"><span>Decision</span><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="approved">approve</option><option value="rejected">reject</option></select></label>}
        {capability.key === "decision.resolve" && <label className="operator-field"><span>Decision</span><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="approve">approve</option><option value="reject">reject</option><option value="defer">defer</option></select></label>}
        {["task.claim", "task.complete", "task.release", "workspace.claim", "workspace.release", "handoff.set", "handoff.pick", "handoff.clear"].includes(capability.key) && <label className="operator-field"><span>Live session ID {needsSession ? "" : "(optional)"}</span><input value={sessionId} onChange={(event) => setSessionId(event.target.value)} /></label>}
        {["task.create", "task.complete", "task.release", "handoff.set", "handoff.clear", "message.send", "topic.post", "knowledge.add", "decision.resolve"].includes(capability.key) && <label className="operator-field"><span>{["task.complete"].includes(capability.key) ? "Completion summary" : ["task.release", "handoff.clear", "decision.resolve"].includes(capability.key) ? "Reason" : "Description"}</span><textarea value={body} onChange={(event) => setBody(event.target.value)} /></label>}
        <div className="operator-effect-preview"><strong>Durable effect preview</strong>{runnable ? effectPreview(capability.key, workspace, title) : capability.reason || "This native action requires its contextual screen and specific identity inputs."}</div>
        {!capability.enabled && <div className="operator-route-gap">Activation requirement: {capability.reason || "authorized typed HTTP support"}. No shell execution is involved.</div>}
        {capability.enabled && !runnable && <div className="operator-route-gap">Open the contextual screen to supply the identity and evidence this action requires.</div>}
      </div>
      <footer>{contextualRoute && capability.enabled ? <button className="operator-button" onClick={() => navigate(contextualRoute)}>Open contextual view</button> : null}<button className="operator-button primary" disabled={!valid || saving} onClick={() => void execute()}>{saving ? "Recording..." : runnable ? capability.label : capability.transport === "native_http" ? "Context required" : "HTTP adapter required"}</button></footer>
    </aside>
  );
}

function effectPreview(key: string, workspace: string, title: string): string {
  if (key === "task.create") return `Creates one available task in ${workspace}, attributed to the browser operator.`;
  if (["task.claim", "task.complete", "task.release"].includes(key)) return `Transitions the named task using the supplied live Session and records the browser operator in audit.`;
  if (key === "workspace.claim") return `Creates or renews a 30-minute claim in ${workspace} for the named live session.`;
  if (key === "workspace.release") return `Releases the supplied Session's claim in ${workspace}.`;
  if (key === "handoff.set") return `Replaces the active handoff in ${workspace} with ${title || "the supplied continuity note"}.`;
  if (key === "handoff.pick") return `Picks the active handoff in ${workspace}, optionally attributing a live Session.`;
  if (key === "handoff.clear") return `Clears the active handoff in ${workspace} with an attributable reason.`;
  if (key === "message.send") return `Queues one workspace-scoped message in ${workspace}.`;
  if (key === "topic.post") return `Posts one scoped topic update from ${workspace} without an install-wide notification blast.`;
  if (key === "knowledge.add") return `Adds one workspace-scoped knowledge entry in ${workspace}.`;
  if (key === "knowledge.resolve") return `Transitions one named knowledge entry after Workspace authorization.`;
  if (key === "pattern.decide") return `Approves or rejects one global reusable pattern as install admin.`;
  if (key === "decision.resolve") return `Resolves one Workspace-scoped decision through the human separation-of-duty check.`;
  if (key === "audit.verify") return "Recomputes the signed audit chain without mutation.";
  if (key === "tool.verify") return "Checks one registered executable against PATH and records the result.";
  return "Runs a dry-read preview and does not apply repair mutations.";
}

function defaultKind(key: string): string {
  if (key === "task.create") return "p2";
  if (key === "topic.post") return "coordination";
  if (key === "knowledge.resolve") return "resolved";
  if (key === "pattern.decide") return "approved";
  if (key === "decision.resolve") return "approve";
  return "blocker";
}

import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { OperatorWorkspaceDetail, WorkspaceLookupEnvelope } from "../api/types";
import { relativeTime } from "../components/format";
import {
  OperatorCard,
  OperatorMiniList,
  OperatorPageHead,
  OperatorState,
  OperatorStatus,
  countByStatus,
} from "../components/OperatorPrimitives";
import { isCurrent } from "../components/sessionScope";
import { useAsync } from "../store/useAsync";

type WorkspaceTab = "overview" | "work" | "communication" | "knowledge" | "activity" | "access";
const WORKSPACE_TABS: WorkspaceTab[] = ["overview", "work", "communication", "knowledge", "activity", "access"];

export function Workspaces() {
  const { slug } = useParams<{ slug: string }>();
  const navigate = useNavigate();
  const [tab, setTab] = useState<WorkspaceTab>("overview");
  const list = useAsync(() => api.operatorWorkspaces(), []);
  const selectedSlug = slug || list.data?.[0]?.slug;
  const detail = useAsync<OperatorWorkspaceDetail>(
    () => selectedSlug ? api.operatorWorkspace(selectedSlug) : Promise.reject(new Error("No workspace selected")),
    [selectedSlug],
  );
  const workspace = detail.data;
  // Workspace existence is itself scoped information. A denied deep link is
  // deliberately indistinguishable from an unknown one in this view.
  const detailKind = detail.errorKind === "unauthorized" ? "not_found" : detail.errorKind;

  useEffect(() => {
    setTab("overview");
  }, [selectedSlug]);

  const openAct = (capability: string) => {
    const query = new URLSearchParams({ capability });
    if (selectedSlug) query.set("workspace", selectedSlug);
    navigate(`/act?${query.toString()}`);
  };

  return (
    <div className="operator-page" data-testid="workspaces">
      <OperatorPageHead
        eyebrow="Portfolio and control room"
        title="Workspaces"
        lede="Move from the whole brain into one repository without losing tasks, ownership, communication, continuity, or evidence."
        actions={<><button className="operator-button" disabled title="A typed workspace-import HTTP contract is not available">Import workspace</button><button className="operator-button primary" onClick={() => openAct("task.create")}>Workspace action</button></>}
      />
      <OperatorState loading={list.loading} error={list.error} kind={list.errorKind} empty={Boolean(list.data && !list.data.length)} emptyTitle="No visible workspaces" emptyBody="Workspaces appear after an authorized session registers them." />
      {list.data && list.data.length > 0 && (
        <div className="operator-workspace-layout">
          <OperatorCard kicker="Visible portfolio" title={`${list.data.length} workspaces`} className="operator-workspace-list-card">
            <div className="operator-workspace-choices">
              {list.data.map((item) => (
                <button
                  key={item.slug}
                  className={`operator-workspace-choice ${item.slug === selectedSlug ? "selected" : ""}`}
                  onClick={() => navigate(`/workspaces/${item.slug}`)}
                >
                  <strong>{item.name || item.slug}</strong><small>{item.path}</small>
                  <span><OperatorStatus tone={item.live_agents ? "ready" : "neutral"}>{item.live_agents ? `${item.live_agents} live` : "quiet"}</OperatorStatus>{item.open_decisions > 0 && <OperatorStatus tone="warning">{item.open_decisions} decisions</OperatorStatus>}</span>
                </button>
              ))}
            </div>
          </OperatorCard>

          <section className="operator-control-room">
            <OperatorState loading={detail.loading} error={detail.error} kind={detailKind} />
            {workspace && workspace.workspace.slug === selectedSlug && (
              <>
                <section className="operator-workspace-banner">
                  <div className="operator-workspace-banner-top">
                    <div><div className="operator-card-kicker">Workspace control room</div><h2>{workspace.workspace.name || workspace.workspace.slug}</h2><code>{workspace.workspace.path}</code><p>{workspace.workspace.last_summary || "No current summary has been recorded for this workspace."}</p></div>
                    <div className="operator-action-row"><OperatorStatus tone="ready">{workspace.workspace.status}</OperatorStatus><OperatorStatus>{workspace.workspace.visibility}</OperatorStatus></div>
                  </div>
                  <div className="operator-workspace-actionbar">
                    <button className="operator-button primary" onClick={() => openAct("task.create")}>Create task</button>
                    <button className="operator-button" onClick={() => openAct("message.send")}>Message workspace</button>
                    <button className="operator-button" onClick={() => openAct("handoff.set")}>Set handoff</button>
                    <button className="operator-button" onClick={() => openAct("workspace.claim")}>Claim workspace</button>
                  </div>
                </section>
                <div className="operator-tabs" role="tablist" aria-label="Workspace views">
                  {WORKSPACE_TABS.map((name, index) => (
                    <button
                      key={name}
                      id={`workspace-tab-${name}`}
                      role="tab"
                      aria-selected={tab === name}
                      aria-controls={`workspace-panel-${name}`}
                      tabIndex={tab === name ? 0 : -1}
                      className={tab === name ? "active" : ""}
                      onClick={() => setTab(name)}
                      onKeyDown={(event) => {
                        const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
                        const requested = event.key === "Home"
                          ? 0
                          : event.key === "End"
                            ? WORKSPACE_TABS.length - 1
                            : direction
                              ? (index + direction + WORKSPACE_TABS.length) % WORKSPACE_TABS.length
                              : -1;
                        if (requested < 0) return;
                        event.preventDefault();
                        const next = WORKSPACE_TABS[requested];
                        setTab(next);
                        event.currentTarget.parentElement
                          ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[requested]
                          ?.focus();
                      }}
                    >{name}</button>
                  ))}
                </div>
                <div
                  id={`workspace-panel-${tab}`}
                  role="tabpanel"
                  aria-labelledby={`workspace-tab-${tab}`}
                  tabIndex={0}
                >
                  <WorkspaceTabContent tab={tab} detail={workspace} />
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function WorkspaceTabContent({ tab, detail }: { tab: WorkspaceTab; detail: OperatorWorkspaceDetail }) {
  const navigate = useNavigate();
  const activeHandoff = detail.handoffs.find((row) => row.status === "active");
  if (tab === "work") {
    return <div className="operator-room-grid"><OperatorCard kicker="Tasks" title={`${detail.tasks.length} durable tasks`}><div className="operator-work-list">{detail.tasks.map((task) => <div className="operator-work-item" key={task.code}><div><code>{task.code}</code><OperatorStatus tone={task.status === "blocked" ? "warning" : task.status === "done" ? "ready" : "neutral"}>{task.status}</OperatorStatus></div><strong>{task.title}</strong><small>{task.priority} / {task.claimed_by_session_id ? `claimed by ${task.claimed_by_session_id.slice(0, 8)}` : "unclaimed"}</small></div>)}</div></OperatorCard><OperatorCard kicker="Human authority" title="Open decisions"><div className="operator-work-list">{detail.decisions.map((row) => <div className="operator-work-item" key={row.code}><code>{row.code}</code><strong>{row.title}</strong><small>{relativeTime(row.created_at)}</small></div>)}{!detail.decisions.length && <span className="operator-muted">No open decisions.</span>}</div></OperatorCard></div>;
  }
  if (tab === "communication") {
    return <div className="operator-room-grid"><OperatorCard kicker="Continuity" title="Handoffs"><div className="operator-work-list">{detail.handoffs.map((row) => <div className="operator-work-item" key={String(row.handoff_id || row.id)}><OperatorStatus tone={row.status === "active" ? "ready" : "neutral"}>{row.status || "unknown"}</OperatorStatus><strong>{row.title || "Untitled handoff"}</strong><small>{relativeTime(row.set_at || row.created_at)}</small></div>)}{!detail.handoffs.length && <span className="operator-muted">No handoffs recorded.</span>}</div></OperatorCard><OperatorCard kicker="Presence" title="Live agents"><div className="operator-agent-list">{detail.live_agents.map((agent) => <div className="operator-agent-row" key={agent.session_id}><span><i>{(agent.tool || "A").slice(0, 1).toUpperCase()}</i><b>{agent.tool || "agent"}<small>{agent.session_id.slice(0, 12)}</small></b></span><span className="operator-agent-actions"><code>{relativeTime(agent.last_activity_at)}</code>{agent.mailbox_deep_link && <button className="operator-button quiet" onClick={() => navigate(agent.mailbox_deep_link!.replace(/^\/app/, ""))}>Open mailbox</button>}</span></div>)}{!detail.live_agents.length && <span className="operator-muted">No live agents.</span>}</div></OperatorCard></div>;
  }
  if (tab === "knowledge") {
    return <div className="operator-room-grid"><LookupPanel key={detail.workspace.slug} slug={detail.workspace.slug} /><OperatorCard kicker="Knowledge ledger" title={`${detail.knowledge.length} entries`}><div className="operator-work-list">{detail.knowledge.map((row) => <div className="operator-work-item" key={row.code}><div><code>{row.code}</code><OperatorStatus tone={row.severity === "critical" ? "danger" : row.type === "blocker" ? "warning" : "neutral"}>{row.type}</OperatorStatus></div><strong>{row.title}</strong><small>{row.scope} / {row.status}</small></div>)}</div></OperatorCard><OperatorCard kicker="Advisory signals" title="What agents should know"><OperatorMiniList rows={detail.signals.map((signal) => ({ label: signal.type.replaceAll("_", " "), value: signal.count }))} /></OperatorCard></div>;
  }
  if (tab === "activity") {
    return <OperatorCard kicker="Durable activity" title="Workspace timeline"><div className="operator-event-list">{detail.events.map((event) => <div className="operator-event-row" key={event.id}><time>{relativeTime(event.created_at)}</time><i>+</i><div><strong>{event.kind.replaceAll("_", " ")}</strong><p>{event.message}</p></div><code>{event.session_id?.slice(0, 8) || "system"}</code></div>)}</div></OperatorCard>;
  }
  if (tab === "access") {
    return <div className="operator-room-grid"><OperatorCard kicker="Workspace boundary" title="Visibility"><OperatorMiniList rows={[{ label: "Visibility", value: detail.workspace.visibility }, { label: "Org ID", value: detail.workspace.org_id || "Unassigned" }, { label: "Status", value: detail.workspace.status }, { label: "Workspace ID", value: detail.workspace.id }]} /></OperatorCard><OperatorCard kicker="Contract" title="Access changes"><p className="operator-muted">Workspace membership and visibility mutations are not enabled here until a typed, auditable HTTP contract is available.</p><button className="operator-button" disabled>Adapter required</button></OperatorCard></div>;
  }
  return <div className="operator-room-grid">{detail.claims.length > 0 && <div className="operator-claim-banner"><div><strong>Workspace claimed for {detail.claims[0].scope}</strong><small>{detail.claims[0].session_id} / expires {relativeTime(detail.claims[0].expires_at)}</small></div><OperatorStatus tone="warning">exclusive ownership</OperatorStatus></div>}<OperatorCard kicker="Work" title="Current task load"><OperatorMiniList rows={[{ label: "Available", value: countByStatus(detail.tasks, "available") }, { label: "In progress", value: countByStatus(detail.tasks, "in_progress") }, { label: "Blocked", value: countByStatus(detail.tasks, "blocked") }, { label: "Completed", value: countByStatus(detail.tasks, "done") }]} /></OperatorCard><OperatorCard kicker="Agents" title="Active execution context"><div className="operator-agent-list">{detail.live_agents.map((agent) => <div className="operator-agent-row" key={agent.session_id}><span><i>{(agent.tool || "A")[0].toUpperCase()}</i><b>{agent.tool || "agent"}<small>{agent.session_id.slice(0, 12)}</small></b></span><code>{relativeTime(agent.last_activity_at)}</code></div>)}{!detail.live_agents.length && <span className="operator-muted">No live agents in this workspace.</span>}</div></OperatorCard><OperatorCard kicker="Continuity" title="Current handoff">{activeHandoff ? <div className="operator-handoff-box"><strong>{activeHandoff.title}</strong><p>{activeHandoff.body || "No handoff detail supplied."}</p></div> : <span className="operator-muted">No active handoff.</span>}</OperatorCard><OperatorCard kicker="Signals" title="Knowledge and coordination"><OperatorMiniList rows={[{ label: "Active blockers", value: detail.knowledge.filter((row) => row.type === "blocker" && row.status === "active").length }, { label: "Reusable workarounds", value: detail.knowledge.filter((row) => row.type === "workaround").length }, { label: "Open decisions", value: detail.decisions.length }, { label: "Recorded sessions", value: detail.sessions.length }]} /></OperatorCard></div>;
}

function LookupPanel({ slug }: { slug: string }) {
  const [query, setQuery] = useState("");
  const [lookup, setLookup] = useState<WorkspaceLookupEnvelope>();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const currentWorkspace = useRef<string | null>(slug);
  const request = useRef<AbortController | null>(null);
  currentWorkspace.current = slug;

  useEffect(() => {
    request.current?.abort();
    setQuery("");
    setLookup(undefined);
    setError(null);
    setLoading(false);
    return () => request.current?.abort();
  }, [slug]);

  const run = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    setLookup(undefined);
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const requestedWorkspace = slug;
    try {
      const result = await api.operatorWorkspaceLookup(
        requestedWorkspace,
        query.trim(),
        10,
        controller.signal,
      );
      if (!controller.signal.aborted && isCurrent(currentWorkspace, requestedWorkspace)) {
        setLookup(result);
      }
    } catch (reason) {
      if (!controller.signal.aborted && isCurrent(currentWorkspace, requestedWorkspace)) {
        setError(reason instanceof Error ? reason.message : "Lookup failed");
      }
    } finally {
      if (request.current === controller && isCurrent(currentWorkspace, requestedWorkspace)) {
        request.current = null;
        setLoading(false);
      }
    }
  };

  return <OperatorCard kicker="Source lookup" title="Substring and symbol search">
    <p className="operator-muted">Read-only lookup works immediately on the Workspace source.</p>
    <div className="operator-action-row">
      <input aria-label="Source query" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void run(); }} />
      <button className="operator-button primary" disabled={loading || !query.trim()} onClick={() => void run()}>{loading ? "Looking…" : "Lookup"}</button>
    </div>
    {error && <p role="alert">{error}</p>}
    {lookup?.status === "unavailable" && <p role="status">Workspace source is unavailable ({lookup.reason.replaceAll("_", " ")}).</p>}
    {lookup?.status === "empty" && <p role="status">No source matches.</p>}
    {lookup?.status === "limited" && <p role="status">Lookup is incomplete ({lookup.incomplete_reasons.map((reason) => reason.replaceAll("_", " ")).join(", ")}). Results may be partial.</p>}
    {(lookup?.status === "ok" || lookup?.status === "limited") && <div className="operator-work-list">{lookup.results.map((row) => <div className="operator-work-item" key={`${row.path}:${row.line}`}><div><code>{row.path}:{row.line}</code><OperatorStatus>{row.match}</OperatorStatus></div>{row.symbol && <strong>{row.symbol}</strong>}<pre>{row.snippet}</pre></div>)}</div>}
  </OperatorCard>;
}

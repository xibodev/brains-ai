import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { relativeTime } from "../components/format";
import {
  OperatorCard,
  OperatorMiniList,
  OperatorPageHead,
  OperatorState,
  OperatorStatus,
} from "../components/OperatorPrimitives";
import { useAsync } from "../store/useAsync";

export function CommandCenter() {
  const navigate = useNavigate();
  const state = useAsync(() => api.operatorOverview(), []);
  const data = state.data;

  return (
    <div className="operator-page" data-testid="command-center">
      <OperatorPageHead
        eyebrow="Across the brain"
        title="Command Center"
        lede="What is moving in every visible workspace, who owns it, where continuity is at risk, and what needs a human next."
        meta={data ? <><strong>Live state current</strong><br />updated {relativeTime(data.generated_at)}</> : undefined}
      />
      <OperatorState loading={state.loading} error={state.error} kind={state.errorKind} />
      {data && (
        <>
          <section className="operator-situation-strip" aria-label="Current situation">
            {[
              [data.situation.workspaces, "Visible workspaces"],
              [data.situation.live_agents, "Live agents"],
              [data.situation.active_claims, "Active claims"],
              [data.situation.open_decisions, "Open decisions"],
              [data.situation.active_handoffs, "Active handoffs"],
            ].map(([value, label]) => (
              <div className="operator-situation" key={String(label)}>
                <div>{value}</div><span>{label}</span>
              </div>
            ))}
          </section>

          <div className="operator-command-grid">
            <OperatorCard
              kicker="Workspace pulse"
              title="Where work stands now"
              action={<div className="operator-action-row"><button className="operator-button" onClick={() => navigate("/workspaces")}>Portfolio</button><button className="operator-button primary" onClick={() => navigate("/act")}>New action</button></div>}
              className="operator-workspace-table-card"
            >
              <div className="operator-workspace-header" aria-hidden>
                <span>Workspace</span><span>Now</span><span>Ownership</span><span>Work</span><span>Attention</span><span />
              </div>
              <div className="operator-workspace-table">
                {data.workspaces.map((workspace) => {
                  const openTasks = Object.entries(workspace.tasks)
                    .filter(([status]) => !["done", "archived"].includes(status))
                    .reduce((total, [, count]) => total + count, 0);
                  const attention = workspace.open_decisions + workspace.active_handoffs + workspace.unread_messages;
                  return (
                    <button
                      className="operator-workspace-row"
                      key={workspace.slug}
                      onClick={() => navigate(`/workspaces/${workspace.slug}`)}
                    >
                      <div><strong>{workspace.name || workspace.slug}</strong><code>{workspace.path}</code></div>
                      <div><b>{workspace.live_agents ? `${workspace.live_agents} live` : "Quiet"}</b><small>{relativeTime(workspace.last_touched_at)}</small></div>
                      <div><b>{workspace.claim ? `Claimed: ${workspace.claim.scope}` : "Unclaimed"}</b><small>{workspace.claim ? relativeTime(workspace.claim.expires_at) : workspace.visibility}</small></div>
                      <div><b>{openTasks ? `${openTasks} open tasks` : "No open tasks"}</b><small>{workspace.tasks.blocked ? `${workspace.tasks.blocked} blocked` : "no blockers recorded"}</small></div>
                      <div><em className={attention ? "attention" : ""}>{attention}</em><small>items</small></div>
                      <span aria-hidden>&gt;</span>
                    </button>
                  );
                })}
                {!data.workspaces.length && <OperatorState loading={false} empty emptyTitle="No visible workspaces" emptyBody="Register or join a workspace before coordinating work." />}
              </div>
            </OperatorCard>

            <aside className="operator-right-rail">
              <section className="operator-attention-card">
                <div className="operator-card-kicker">Needs you</div>
                <h2>{data.situation.open_decisions + data.situation.active_handoffs} human actions</h2>
                {[...data.attention.decisions.map((row) => ({ kind: row.kind === "ask_human" ? "Ask" : "Approval", title: row.title, scope: `${row.workspace} / ${row.code}` })), ...data.attention.handoffs.map((row) => ({ kind: "Handoff", title: row.title || "Untitled handoff", scope: `${row.workspace || "workspace"} / ${relativeTime(row.set_at)}` }))].slice(0, 5).map((item, index) => (
                  <div className="operator-attention-item" key={`${item.kind}-${index}`}>
                    <span>{item.kind}</span><strong>{item.title}</strong><small>{item.scope}</small>
                  </div>
                ))}
                {!data.attention.decisions.length && !data.attention.handoffs.length && <p>No open decisions or handoffs.</p>}
                <button className="operator-button inverse" onClick={() => navigate("/governance")}>Open governance</button>
              </section>

              <OperatorCard kicker="Presence" title="Agents working now">
                <div className="operator-presence-list">
                  {data.live_agents.slice(0, 6).map((agent) => (
                    <div className="operator-presence-row" key={agent.session_id}>
                      <span><i /> <b>{agent.tool || "agent"}</b><small>{agent.workspace || "unscoped"}</small></span>
                      <code>{relativeTime(agent.last_activity_at)}</code>
                    </div>
                  ))}
                  {!data.live_agents.length && <span className="operator-muted">No recently active agents.</span>}
                </div>
              </OperatorCard>
            </aside>
          </div>

          <div className="operator-below-grid">
            <OperatorCard kicker="Durable activity" title="What changed">
              <div className="operator-event-list">
                {data.recent_events.slice(0, 8).map((event) => (
                  <div className="operator-event-row" key={event.id}>
                    <time>{relativeTime(event.created_at)}</time><i>+</i><div><strong>{event.kind.replaceAll("_", " ")}</strong><p>{event.message}</p></div><code>{event.session_id?.slice(0, 8) || "system"}</code>
                  </div>
                ))}
                {!data.recent_events.length && <span className="operator-muted">No recent durable events.</span>}
              </div>
            </OperatorCard>
            <OperatorCard kicker="Brain posture" title="Operational truth" action={<button className="operator-button quiet" onClick={() => navigate("/operations")}>Inspect</button>}>
              <OperatorMiniList rows={[
                { label: "Protected readiness", value: data.readiness ? <OperatorStatus tone={data.readiness.status === "ready" ? "ready" : "warning"}>{data.readiness.status}</OperatorStatus> : "Install admin only" },
                { label: "Blocked work", value: `${data.situation.blocked_tasks} tasks` },
                { label: "Audit chain", value: data.audit ? "Reported" : "Install admin only" },
                { label: "Workspace continuity", value: `${data.situation.active_handoffs} handoffs` },
              ]} />
            </OperatorCard>
          </div>
        </>
      )}
    </div>
  );
}

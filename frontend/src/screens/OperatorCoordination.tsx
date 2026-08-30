import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { OperatorTask } from "../api/types";
import { relativeTime } from "../components/format";
import { MailboxWorkspace } from "../components/MailboxWorkspace";
import {
  OperatorCard,
  OperatorMiniList,
  OperatorPageHead,
  OperatorState,
  OperatorStatus,
} from "../components/OperatorPrimitives";
import { useAsync } from "../store/useAsync";

const FILTERS = ["all", "available", "in_progress", "blocked"] as const;

export function OperatorCoordination() {
  const navigate = useNavigate();
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("all");
  const state = useAsync(() => api.operatorCoordination(), []);
  const data = state.data;
  const tasks = data?.tasks.filter((task) => filter === "all" || task.status === filter) ?? [];

  const openAct = (capability: string) =>
    navigate(`/act?capability=${encodeURIComponent(capability)}`);

  return (
    <div className="operator-page" data-testid="coordination">
      <OperatorPageHead
        eyebrow="Shared work plane"
        title="Coordination"
        lede="Global queues for tasks, ownership, handoffs, messages, topics, and shared learning, always traceable back to a workspace."
        actions={
          <>
            <button className="operator-button" onClick={() => openAct("topic.post")}>Post topic</button>
            <button className="operator-button primary" onClick={() => openAct("task.create")}>Create task</button>
          </>
        }
      />
      <OperatorState loading={state.loading} error={state.error} />
      {data && (
        <>
          <MailboxWorkspace />
          <div className="operator-filterbar">
            <div className="operator-filter-chips">
              {FILTERS.map((name) => (
                <button key={name} className={filter === name ? "active" : ""} onClick={() => setFilter(name)}>
                  {name.replace("_", " ")}
                </button>
              ))}
            </div>
            <span>{tasks.length} matching tasks across {new Set(tasks.map((task) => task.workspace)).size} workspaces</span>
          </div>

          <div className="operator-claim-strip">
            {data.claims.slice(0, 4).map((claim) => (
              <button key={`${claim.workspace}-${claim.session_id}`} onClick={() => navigate(`/workspaces/${claim.workspace}`)}>
                <strong>{claim.workspace} / {claim.scope}</strong>
                <small>{claim.session_id.slice(0, 12)} / expires {relativeTime(claim.expires_at)}</small>
              </button>
            ))}
            {!data.claims.length && <div className="operator-muted">No active workspace claims.</div>}
          </div>

          <div className="operator-coordination-grid">
            <OperatorCard kicker="Durable task queue" title="Work moving across the brain">
              <div className="operator-task-board">
                <TaskColumn title="Available" tasks={tasks.filter((task) => task.status === "available")} />
                <TaskColumn title="In progress" tasks={tasks.filter((task) => task.status === "in_progress")} />
                <TaskColumn title="Blocked" tasks={tasks.filter((task) => task.status === "blocked")} />
              </div>
              {!tasks.length && <OperatorState loading={false} empty emptyTitle="No matching tasks" emptyBody="Adjust the filter or create a workspace task through Act." />}
            </OperatorCard>

            <aside className="operator-intel-stack">
              <OperatorCard kicker="Handoffs" title="Ready for pickup" action={<OperatorStatus tone={data.handoffs.some((row) => row.status === "active") ? "warning" : "ready"}>{data.handoffs.filter((row) => row.status === "active").length}</OperatorStatus>}>
                <div className="operator-work-list">
                  {data.handoffs.filter((row) => row.status === "active").slice(0, 4).map((handoff) => (
                    <button className="operator-work-item" key={String(handoff.handoff_id || handoff.id)} onClick={() => navigate(`/workspaces/${handoff.workspace}`)}>
                      <strong>{handoff.title || "Untitled handoff"}</strong>
                      <small>{handoff.workspace} / {relativeTime(handoff.set_at || handoff.created_at)}</small>
                    </button>
                  ))}
                  {!data.handoffs.some((row) => row.status === "active") && <span className="operator-muted">No active handoffs.</span>}
                </div>
              </OperatorCard>

              <OperatorCard kicker="Comms" title="Boards and presence">
                <OperatorMiniList rows={[
                  { label: "Active topics", value: data.topics.length },
                  { label: "Recent topic posts", value: data.topic_posts.length },
                  { label: "Live agents", value: data.live_agents.length },
                  { label: "Workspace claims", value: data.claims.length },
                ]} />
              </OperatorCard>

              <OperatorCard kicker="Learning" title="Review queue">
                <OperatorMiniList rows={[
                  { label: "Pattern proposals", value: data.patterns.filter((row) => row.status === "proposed").length },
                  { label: "Knowledge blockers", value: data.knowledge.filter((row) => row.type === "blocker" && row.status === "active").length },
                  { label: "Workarounds", value: data.knowledge.filter((row) => row.type === "workaround").length },
                  { label: "Advisory signals", value: data.signals.reduce((total, row) => total + row.count, 0) },
                ]} />
              </OperatorCard>
            </aside>
          </div>
        </>
      )}
    </div>
  );
}

function TaskColumn({ title, tasks }: { title: string; tasks: OperatorTask[] }) {
  return (
    <section className="operator-task-column">
      <header><span>{title}</span><b>{tasks.length}</b></header>
      {tasks.map((task) => (
        <article className="operator-task-card" key={task.code}>
          <div><code>{task.code}</code><OperatorStatus tone={task.priority === "p0" ? "danger" : task.priority === "p1" ? "warning" : "neutral"}>{task.priority}</OperatorStatus></div>
          <strong>{task.title}</strong>
          <footer><span>{task.workspace}</span><span>{relativeTime(task.created_at)}</span></footer>
        </article>
      ))}
    </section>
  );
}

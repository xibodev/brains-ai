import { useNavigate } from "react-router-dom";
import { api, formatApiError } from "../api/client";
import { relativeTime } from "../components/format";
import {
  OperatorCard,
  OperatorMiniList,
  OperatorPageHead,
  OperatorState,
  OperatorStatus,
} from "../components/OperatorPrimitives";
import { useToast } from "../components/Toast";
import { useAsync } from "../store/useAsync";

export function Operations() {
  const navigate = useNavigate();
  const state = useAsync(() => api.operatorOperations(), []);
  const { toast } = useToast();
  const data = state.data;

  const verifyTool = async (name: string) => {
    try {
      await api.operatorVerifyTool(name);
      toast(`${name} verification recorded`);
      state.refetch();
    } catch (error) {
      toast(formatApiError("Verify tool", error));
    }
  };

  return (
    <div className="operator-page" data-testid="operations">
      <OperatorPageHead
        eyebrow="Install and continuity"
        title="Operations"
        lede="The service tree, tools, wiring, storage, recovery, access, and configuration, with typed safeguards for every host-level effect."
        actions={<><button className="operator-button" disabled title="Service logs need a typed host contract">View service logs</button><button className="operator-button primary" onClick={() => navigate("/act?category=operations")}>Operational action</button></>}
      />
      <OperatorState loading={state.loading} error={state.error} />
      {data && (
        <>
          <section className="operator-topology" aria-label="Brains topology">
            {[
              [
                "API / MCP",
                `${Number(Boolean(data.service.listeners?.gateway)) + Number(Boolean(data.service.listeners?.mcp))} / 2 listeners`,
              ],
              ["Storage", data.readiness.components.storage.state],
              ["Queues", data.readiness.components.queue.state],
              ["Runtimes", `${data.runtimes.length} known`],
              ["Tools", `${data.tools.length} registered`],
              ["Recovery", data.recovery.ready ? "ready" : "incomplete"],
            ].map(([name, value]) => <div key={name}><span>+</span><strong>{name}</strong><small>{value}</small></div>)}
          </section>

          <div className="operator-operations-grid">
            <OperatorCard kicker="Protected readiness" title="Dependencies" action={<OperatorStatus tone={data.readiness.status === "ready" ? "ready" : "warning"}>{data.readiness.status}</OperatorStatus>} className="operator-operation-card">
              <div className="operator-op-number">{Object.values(data.readiness.components).filter((row) => row.state === "ready").length} / {Object.keys(data.readiness.components).length}</div>
              <p>Bounded storage, queue, Runtime-lifecycle, and recovery-policy checks.</p>
              <OperatorMiniList rows={Object.entries(data.readiness.components).map(([name, row]) => ({ label: name.replaceAll("_", " "), value: <OperatorStatus tone={row.state === "ready" ? "ready" : "warning"}>{row.state}</OperatorStatus> }))} />
            </OperatorCard>

            <OperatorCard kicker="Coordination queues" title="Continuity" action={<OperatorStatus tone={data.queue.diagnosis.issue_count ? "warning" : "ready"}>{data.queue.diagnosis.issue_count ? "attention" : "ready"}</OperatorStatus>} className="operator-operation-card">
              <div className="operator-op-number">{data.queue.diagnosis.issue_count} issues</div>
              <p>Durable coordination families are diagnosed before any repair is applied.</p>
              <OperatorMiniList rows={[
                { label: "Families", value: Object.keys(data.queue.summary.families).length },
                { label: "Stale or expired", value: Object.values(data.queue.summary.families).reduce((total, row) => total + row.stale_or_expired, 0) },
                { label: "Repair mode", value: "Dry-run first" },
              ]} />
              <button className="operator-button" onClick={() => void api.repairQueueHealth(false).then(() => toast("Queue repair preview complete")).catch((error) => toast(formatApiError("Preview queue repair", error)))}>Preview repair</button>
            </OperatorCard>

            <OperatorCard kicker="Tools" title="Registered capabilities" action={<OperatorStatus tone="native">Native HTTP</OperatorStatus>} className="operator-operation-card">
              <div className="operator-op-number">{data.tools.filter((tool) => tool.is_available).length} available</div>
              <p>Verification is bounded to registered executable discovery and records an audit event.</p>
              <div className="operator-tool-list">
                {data.tools.slice(0, 5).map((tool) => <button key={tool.name} onClick={() => void verifyTool(tool.name)}><span><strong>{tool.display_name}</strong><small>{relativeTime(tool.last_verified_at)}</small></span><OperatorStatus tone={tool.is_available ? "ready" : "warning"}>{tool.is_available ? "available" : "missing"}</OperatorStatus></button>)}
                {!data.tools.length && <span className="operator-muted">No tools registered.</span>}
              </div>
            </OperatorCard>

            <OperatorCard kicker="Storage and recovery" title="Durability policy" action={<OperatorStatus tone={data.recovery.ready ? "ready" : "warning"}>{data.recovery.ready ? "ready" : "incomplete"}</OperatorStatus>} className="operator-operation-card">
              <div className="operator-op-number">{data.recovery.policy.missing_fields.length} gaps</div>
              <p>Backup and restore stay disabled in the browser until typed preview and confirmation routes exist.</p>
              <OperatorMiniList rows={[
                { label: "Retention", value: data.recovery.policy.retention_days == null ? "Not set" : `${data.recovery.policy.retention_days} days` },
                { label: "Restore drill", value: data.recovery.policy.last_restore_drill_at ? relativeTime(data.recovery.policy.last_restore_drill_at) : "Not recorded" },
                { label: "Schema compatibility", value: data.recovery.compatibility.migration_healthy ? "Healthy" : "Degraded" },
              ]} />
              <button className="operator-button" disabled>Backup adapter required</button>
            </OperatorCard>

            <OperatorCard kicker="Access and configuration" title="Operators and settings" action={<OperatorStatus tone="adapter">Mixed HTTP</OperatorStatus>} className="operator-operation-card">
              <div className="operator-op-number">{data.operators.length} operators</div>
              <p>Org membership and encrypted settings are native; remaining workspace-access operations stay explicit gaps.</p>
              <div className="operator-action-row"><button className="operator-button" onClick={() => navigate("/operations/access")}>Manage access</button><button className="operator-button" onClick={() => navigate("/operations/config")}>Configuration</button></div>
            </OperatorCard>

            <OperatorCard kicker="Host contracts" title="Service and wiring" action={<OperatorStatus tone="host">Host contract</OperatorStatus>} className="operator-operation-card">
              <div className="operator-op-number">Read only</div>
              <p>Start, stop, restart, logs, wire, and unwire need install-admin preview, confirmation, audit, and bounded result contracts.</p>
              <div className="operator-action-row"><button className="operator-button" disabled>Restart unavailable</button><button className="operator-button" disabled>Wire unavailable</button></div>
            </OperatorCard>
          </div>
        </>
      )}
    </div>
  );
}

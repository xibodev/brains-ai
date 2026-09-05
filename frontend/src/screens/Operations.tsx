import { api } from "../api/client";
import { actHref, useCoreNavigation } from "../coreRoutes";
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
  const navigation = useCoreNavigation();
  const state = useAsync(() => api.operatorOperations(), []);
  const { toast } = useToast();
  const data = state.data;
  const empty = Boolean(data && (!data.service || !data.readiness || !data.queue || !data.recovery));

  return (
    <div className="operator-page" data-testid="operations">
      <OperatorPageHead
        eyebrow="Install and continuity"
        title="Operations"
        lede="The service tree, tools, wiring, storage, recovery, access, and configuration, with typed safeguards for every host-level effect."
        actions={<><button className="operator-button" disabled title="Service logs need a typed host contract">View service logs</button><button className="operator-button primary" onClick={() => navigation.open(actHref({ category: "operations" }))}>Operational action</button></>}
      />
      <OperatorState loading={state.loading} error={state.error} kind={state.errorKind} empty={empty} boundary="operations" emptyTitle="No operational state" emptyBody="Required service, readiness, queue, or recovery state is unavailable." />
      {data && !empty && (
        <>
          <section className="operator-topology" aria-label="Brains topology">
            {[
              [
                "API / MCP",
                `${Number(Boolean(data.service.listeners?.gateway)) + Number(Boolean(data.service.listeners?.mcp))} / 2 listeners`,
              ],
              ["Storage", data.readiness.components.storage.state],
              ["SQLite integrity", data.readiness.components.sqlite_integrity.state],
              ["HTTP gateway", data.readiness.components.gateway_protocol.state],
              ["MCP protocol", data.readiness.components.mcp_protocol.state],
              ["Queues", data.readiness.components.queue.state],
              ["Durable mail", data.readiness.components.durable_mail.state],
              ["Recovery", data.recovery.ready ? "ready" : "incomplete"],
            ].map(([name, value]) => <div key={name}><span>+</span><strong>{name}</strong><small>{value}</small></div>)}
          </section>

          <div className="operator-operations-grid">
            <OperatorCard kicker="Protected readiness" title="Dependencies" action={<OperatorStatus tone={data.readiness.status === "ready" ? "ready" : "warning"}>{data.readiness.status}</OperatorStatus>} className="operator-operation-card">
              <div className="operator-op-number">{Object.values(data.readiness.components).filter((row) => row.state === "ready").length} / {Object.keys(data.readiness.components).length}</div>
              <p>Bounded SQLite, core HTTP gateway, authenticated MCP, queue, durable-mail, and verified recovery checks.</p>
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
              <button className="operator-button" onClick={() => void api.repairQueueHealth(false).then(() => toast("Queue repair preview complete")).catch(() => toast("The queue repair preview could not be completed. Retry after checking authorization and local service status."))}>Preview repair</button>
            </OperatorCard>

            <OperatorCard kicker="Storage and recovery" title="Durability policy" action={<OperatorStatus tone={data.recovery.ready ? "ready" : "warning"}>{data.recovery.ready ? "ready" : "incomplete"}</OperatorStatus>} className="operator-operation-card">
              <div className="operator-op-number">{data.recovery.policy.missing_fields.length} gaps</div>
              <p>Backup and restore stay disabled in the browser until typed preview and confirmation routes exist.</p>
              <OperatorMiniList rows={[
                { label: "Retention", value: data.recovery.policy.retention_days == null ? "Not set" : `${data.recovery.policy.retention_days} days` },
                { label: "Restore candidate", value: data.recovery.candidate.ready ? "Verified" : "Not verified" },
                { label: "Restore drill", value: data.recovery.last_drill.verified ? "Verified" : "Not verified" },
                { label: "Schema compatibility", value: data.recovery.compatibility.migration_healthy ? "Healthy" : "Degraded" },
              ]} />
              <button className="operator-button" disabled>Backup adapter required</button>
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

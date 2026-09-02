import { useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { api, formatApiError } from "../api/client";
import type {
  QueueHealthReport,
  ReadinessReport,
  RecoveryPolicyReport,
} from "../api/types";
import { AsyncBoundary } from "../components/EmptyState";
import { MasterDetail, type RailItem } from "../components/MasterDetail";
import { SoftCard } from "../components/SoftCard";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/Toast";
import { useAsync } from "../store/useAsync";
import { ScreenHead } from "./ScreenHead";

const SECTIONS: RailItem[] = [
  { key: "mcp", label: "MCP servers", section: "Config" },
  { key: "health", label: "Health & recovery", section: "Config" },
];

export function Config() {
  const { section = "mcp" } = useParams();
  const navigate = useNavigate();
  if (!SECTIONS.some((item) => item.key === section)) {
    return <Navigate to="/operations/config/mcp" replace />;
  }
  return (
    <div style={{ height: "100%" }}>
      <ScreenHead eyebrow={`Config ▸ ${labelFor(section)}`} title="Configure" />
      <MasterDetail items={SECTIONS} activeKey={section} onSelect={(key) => navigate(`/operations/config/${key}`)} railOnLeft>
        {section === "health" ? <Health /> : <McpConfig />}
      </MasterDetail>
    </div>
  );
}

function labelFor(key: string): string {
  return SECTIONS.find((section) => section.key === key)?.label ?? key;
}

function McpConfig() {
  return (
    <SoftCard>
      <div className="eyebrow"><span>MCP servers</span></div>
      <h2 style={{ margin: "8px 0 12px" }}>Agent connections</h2>
      <p className="meta">
        Use <code>brains-ai wire</code> to configure a supported harness. Client configuration changes take effect when that client reconnects.
      </p>
    </SoftCard>
  );
}

interface HealthData {
  readiness: ReadinessReport;
  queue: QueueHealthReport;
  recovery: RecoveryPolicyReport;
}

function Health() {
  const state = useAsync<HealthData>(
    () => Promise.all([api.readiness(), api.queueHealth(), api.recoveryPolicy()]).then(([readiness, queue, recovery]) => ({ readiness, queue, recovery })),
    [],
  );
  const { toast } = useToast();
  const [repairing, setRepairing] = useState(false);
  const runRepair = async (apply: boolean) => {
    setRepairing(true);
    try {
      const result = await api.repairQueueHealth(apply);
      const summary = result.actions.map((action) => `${action.code}=${apply ? (action.applied_rows ?? 0) : (action.would_affect_rows ?? 0)}`).join(", ");
      toast(apply ? `Repair applied: ${summary}` : `Dry-run: ${summary}`);
      state.refetch();
    } catch (error) {
      toast(formatApiError("Repair", error));
    } finally {
      setRepairing(false);
    }
  };
  return (
    <AsyncBoundary state={state} emptyTitle="No health data" emptyBody="Operational health is unavailable.">
      {(data) => (
        <div>
          <div className="eyebrow"><span>Operational health</span></div>
          <h2 style={{ margin: "8px 0 16px" }}>Readiness, queues & recovery</h2>
          <SoftCard>
            <div className="row spread">
              <strong>Overall readiness</strong>
              <StatusPill label={data.readiness.status} tone={data.readiness.status === "ready" ? "positive" : undefined} />
            </div>
            <div className="card-list" style={{ marginTop: 12 }}>
              {Object.entries(data.readiness.components).map(([name, component]) => (
                <div className="row spread" key={name}>
                  <span className="meta">{name.replace(/_/g, " ")}</span>
                  <StatusPill label={component.state} tone={component.state === "ready" ? "positive" : undefined} />
                </div>
              ))}
            </div>
          </SoftCard>
          <SoftCard style={{ marginTop: 16 }}>
            <div className="row spread"><strong>Coordination queues</strong><span className="meta">{data.queue.diagnosis.issue_count} issue(s) detected</span></div>
            <div className="row" style={{ marginTop: 12, gap: 8 }}>
              <button className="btn small" disabled={repairing} onClick={() => void runRepair(false)}>Preview repair</button>
              <button className="btn small" disabled={repairing} onClick={() => void runRepair(true)}>Apply safe repair</button>
            </div>
          </SoftCard>
          <SoftCard style={{ marginTop: 16 }}>
            <div className="row spread">
              <strong>Recovery policy</strong>
              <StatusPill label={data.recovery.policy.complete ? "complete" : "incomplete"} tone={data.recovery.policy.complete ? "positive" : undefined} />
            </div>
            {!data.recovery.policy.complete && <p className="meta" style={{ marginTop: 8 }}>Missing: {data.recovery.policy.missing_fields.join(", ")}</p>}
            <p className="meta" style={{ marginTop: 12 }}>Brains reports the declared external backup policy; it does not fabricate a schedule or drill result.</p>
          </SoftCard>
        </div>
      )}
    </AsyncBoundary>
  );
}

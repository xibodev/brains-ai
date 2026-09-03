import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, formatApiError } from "../api/client";
import type {
  QueueHealthReport,
  ReadinessReport,
  RecoveryPolicyReport,
  CoreConfiguration,
  CoreConfigurationField,
} from "../api/types";
import { AsyncBoundary } from "../components/EmptyState";
import { MasterDetail, type RailItem } from "../components/MasterDetail";
import { SoftCard } from "../components/SoftCard";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/Toast";
import { useAsync } from "../store/useAsync";
import { ScreenHead } from "./ScreenHead";
import { NotFound } from "./NotFound";

const SECTIONS: RailItem[] = [
  { key: "local", label: "Local service", section: "Config" },
  { key: "mcp", label: "MCP servers", section: "Config" },
  { key: "health", label: "Health & recovery", section: "Config" },
];

export function Config() {
  const { section = "local" } = useParams();
  const navigate = useNavigate();
  if (!SECTIONS.some((item) => item.key === section)) {
    return <NotFound resource="Configuration section" />;
  }
  return (
    <div style={{ height: "100%" }}>
      <ScreenHead eyebrow={`Config ▸ ${labelFor(section)}`} title="Configure" />
      <MasterDetail items={SECTIONS} activeKey={section} onSelect={(key) => navigate(`/operations/config/${key}`)} railOnLeft>
        {section === "health" ? <Health /> : section === "local" ? <LocalConfig /> : <McpConfig />}
      </MasterDetail>
    </div>
  );
}

function labelFor(key: string): string {
  return SECTIONS.find((section) => section.key === key)?.label ?? key;
}

function LocalConfig() {
  const state = useAsync<CoreConfiguration>(() => api.operatorConfiguration(), []);
  const { toast } = useToast();
  const [draft, setDraft] = useState<Record<string, string | number | boolean>>({});
  const [saving, setSaving] = useState(false);
  const [outcome, setOutcome] = useState<"live_reload" | "restart_required" | null>(null);
  useEffect(() => {
    if (!state.data) return;
    setDraft(Object.fromEntries(state.data.fields.filter((field) => field.editable).map((field) => [field.key, field.value])));
  }, [state.data]);

  const save = async (data: CoreConfiguration) => {
    const changes = Object.fromEntries(
      data.fields
        .filter((field) => field.editable && draft[field.key] !== field.value)
        .map((field) => [field.key, draft[field.key]]),
    );
    if (Object.keys(changes).length === 0) return;
    setOutcome(null);
    setSaving(true);
    try {
      const result = await api.operatorUpdateConfiguration(data.revision, changes);
      setOutcome(result.apply_mode);
      toast(result.restart_required ? "Saved. Restart required to converge." : "Saved and reloaded.");
      state.refetch();
    } catch (error) {
      toast(formatApiError("Configuration update", error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <AsyncBoundary state={state} emptyTitle="Configuration unavailable" emptyBody="The supported configuration summary could not be loaded.">
      {(data) => (
        <div>
          <div className="eyebrow"><span>Supported local configuration</span></div>
          <h2 style={{ margin: "8px 0 8px" }}>Effective settings</h2>
          <p className="meta">Only local service, Streamable HTTP MCP, SQLite, and supported harness posture is exposed. Secret values and filesystem locations are omitted.</p>
          <div className="card-list" style={{ marginTop: 16 }}>
            {data.fields.map((field) => (
              <ConfigurationField key={field.key} field={field} value={draft[field.key] ?? field.value} onChange={(value) => setDraft((current) => ({ ...current, [field.key]: value }))} />
            ))}
          </div>
          <div className="row" style={{ marginTop: 16, gap: 8 }}>
            <button className="btn small" disabled={saving} onClick={() => void save(data)}>{saving ? "Saving…" : "Save supported changes"}</button>
            {outcome && <StatusPill label={outcome === "restart_required" ? "restart required" : "reloaded"} tone={outcome === "restart_required" ? "warning" : "positive"} />}
          </div>
          <SoftCard style={{ marginTop: 16 }}>
            <strong>Harness wiring</strong>
            <div className="card-list" style={{ marginTop: 10 }}>
              {data.harnesses.map((harness) => (
                <div className="row spread" key={harness.tool}>
                  <span>{harness.tool}</span>
                  <span className="meta">{harness.mcp_wired ? harness.mcp_transport ?? "wired" : harness.detected ? "detected, not wired" : "not detected"}</span>
                </div>
              ))}
            </div>
          </SoftCard>
        </div>
      )}
    </AsyncBoundary>
  );
}

function ConfigurationField({ field, value, onChange }: { field: CoreConfigurationField; value: string | number | boolean; onChange: (value: string | number | boolean) => void }) {
  return (
    <div className="row spread">
      <div><strong>{field.key}</strong><div className="meta">{field.source} · {field.apply_mode.replace("_", " ")}</div></div>
      {!field.editable ? <code>{String(field.value)}</code> : typeof field.value === "boolean" ? (
        <input aria-label={field.key} type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} />
      ) : (
        <input aria-label={field.key} type="number" value={Number(value)} min={0} onChange={(event) => onChange(Number(event.target.value))} style={{ width: 130 }} />
      )}
    </div>
  );
}

function McpConfig() {
  return (
    <div data-async-state="success"><SoftCard>
      <div className="eyebrow"><span>MCP servers</span></div>
      <h2 style={{ margin: "8px 0 12px" }}>Agent connections</h2>
      <p className="meta">
        Use <code>brains-ai wire</code> to configure a supported harness. Client configuration changes take effect when that client reconnects.
      </p>
    </SoftCard></div>
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

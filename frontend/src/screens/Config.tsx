import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, formatApiError } from "../api/client";
import type { ConfigSummary, ReadinessReport, QueueHealthReport, RecoveryPolicyReport } from "../api/types";
import { useAsync } from "../store/useAsync";
import { ScreenHead } from "./ScreenHead";
import { MasterDetail, type RailItem } from "../components/MasterDetail";
import { SoftCard } from "../components/SoftCard";
import { StatusPill } from "../components/StatusPill";
import { AsyncBoundary } from "../components/EmptyState";
import { useToast } from "../components/Toast";

// Config — real, read-mostly view of the gateway config (F7). Providers + gateway
// posture come from GET /v1/config/summary; a provider's connectivity is probed
// live via POST /v1/config/providers/{name}/test. Secrets stay in the secure
// env/admin surface and are never edited from the console.
//
// The "Health" section (B8, BL-P1-09, BL-P1-12) is a separate bootstrap-admin
// surface: overall readiness, coordination-queue family health with a
// preview/apply repair action, and recovery-policy completeness. It never
// shows a secret and never claims readiness the backend did not report.

const SECTIONS: RailItem[] = [
  { key: "providers", label: "Providers", section: "Config" },
  { key: "models", label: "Models / Gateway", section: "Config" },
  { key: "mcp", label: "MCP servers", section: "Config" },
  { key: "integrations", label: "Integrations", section: "Config" },
  { key: "health", label: "Health & recovery", section: "Config" },
  { key: "secrets", label: "Secrets / Keys", section: "Config" },
];

export function Config() {
  const { section = "providers" } = useParams();
  const navigate = useNavigate();
  const state = useAsync<ConfigSummary>(() => api.configSummary(), []);

  return (
    <div style={{ height: "100%" }}>
      <ScreenHead eyebrow={`Config \u25b8 ${labelFor(section)}`} title="Configure" />
      <MasterDetail
        items={SECTIONS}
        activeKey={section}
        onSelect={(k) => navigate(`/config/${k}`)}
        railOnLeft
      >
        {section === "health" ? (
          <Health />
        ) : (
          <AsyncBoundary state={state} emptyTitle="No config" emptyBody="Config unavailable.">
            {(cfg) =>
              section === "providers" ? (
                <Providers cfg={cfg} />
              ) : section === "models" ? (
                <ModelsGateway cfg={cfg} />
              ) : section === "secrets" ? (
                <Secrets cfg={cfg} />
              ) : (
                <SectionInfo section={section} cfg={cfg} />
              )
            }
          </AsyncBoundary>
        )}
      </MasterDetail>
    </div>
  );
}

function labelFor(key: string): string {
  return SECTIONS.find((s) => s.key === key)?.label ?? key;
}

function Providers({ cfg }: { cfg: ConfigSummary }) {
  const { toast } = useToast();
  const [results, setResults] = useState<Record<string, { ok: boolean; detail?: string }>>({});
  const [testing, setTesting] = useState<string | null>(null);

  const test = async (name: string) => {
    setTesting(name);
    try {
      const r = await api.testProvider(name);
      setResults((cur) => ({ ...cur, [name]: r }));
      toast(r.ok ? `${name}: reachable` : `${name}: ${r.detail ?? "unreachable"}`);
    } catch {
      setResults((cur) => ({ ...cur, [name]: { ok: false, detail: "error" } }));
    } finally {
      setTesting(null);
    }
  };

  return (
    <div>
      <div className="eyebrow"><span>OpenAI-compatible gateway</span></div>
      <h2 style={{ margin: "8px 0 16px" }}>Providers</h2>
      <p className="meta" style={{ marginBottom: 16 }}>
        This console is read-only. Inspect effective provider state and run a bounded
        connectivity probe here.
      </p>
      <div className="card-list" data-testid="config-providers">
        {cfg.providers.length === 0 ? (
          <div className="meta">No providers registered.</div>
        ) : (
          cfg.providers.map((p) => {
            const r = results[p.name];
            return (
              <SoftCard key={p.name}>
                <div className="row spread">
                  <strong>{p.name}</strong>
                  <StatusPill
                    label={p.status}
                    tone={p.configured ? "positive" : undefined}
                  />
                </div>
                {p.reason && <p className="meta" style={{ marginTop: 8 }}>{p.reason}</p>}
                <div className="row" style={{ marginTop: 8, gap: 8 }}>
                  <button
                    className="btn small"
                    disabled={testing === p.name}
                    onClick={() => void test(p.name)}
                  >
                    {testing === p.name ? "Testing\u2026" : "Test connection"}
                  </button>
                  {r && (
                    <StatusPill
                      label={r.ok ? `\u2713 ${r.detail ?? "ok"}` : `\u2717 ${r.detail ?? "fail"}`}
                      tone={r.ok ? "positive" : undefined}
                    />
                  )}
                </div>
              </SoftCard>
            );
          })
        )}
      </div>
    </div>
  );
}

function ModelsGateway({ cfg }: { cfg: ConfigSummary }) {
  return (
    <SoftCard>
      <div className="eyebrow"><span>Models / Gateway</span></div>
      <h2 style={{ margin: "8px 0 12px" }}>Gateway</h2>
      <div className="row spread" style={{ marginBottom: 6 }}>
        <span className="meta">Router</span>
        <StatusPill
          label={cfg.gateway.router_enabled ? "enabled" : "off"}
          tone={cfg.gateway.router_enabled ? "positive" : undefined}
        />
      </div>
      <div className="row spread" style={{ marginBottom: 6 }}>
        <span className="meta">Base URL</span>
        <code>{cfg.gateway.base_url ?? "/v1"}</code>
      </div>
      <div className="row spread">
        <span className="meta">Models catalog</span>
        <code>{cfg.models_endpoint ?? "/v1/models"}</code>
      </div>
      <p className="meta" style={{ marginTop: 14 }}>
        {cfg.write_contract?.detail ?? "The modern Config console is read-only."}
      </p>
      {cfg.write_contract?.reload && (
        <p className="meta" style={{ marginTop: 8 }}>{cfg.write_contract.reload}</p>
      )}
      <div className="card-list" style={{ marginTop: 14 }}>
        {(cfg.models ?? []).map((route) => (
          <div className="row spread" key={route.tier}>
            <strong>{route.tier}</strong>
            <code>
              {route.provider}/{route.model}
              {route.simulated ? " (simulated)" : ""}
            </code>
          </div>
        ))}
      </div>
    </SoftCard>
  );
}

function Secrets({ cfg }: { cfg: ConfigSummary }) {
  return (
    <SoftCard>
      <div className="eyebrow"><span>Secrets / Keys</span></div>
      <h2 style={{ margin: "8px 0 12px" }}>Secrets</h2>
      <p className="meta">
        {cfg.secrets_managed ??
          "Secrets are managed in the secure environment and are not editable from the console for safety."}
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
    () =>
      Promise.all([api.readiness(), api.queueHealth(), api.recoveryPolicy()]).then(
        ([readiness, queue, recovery]) => ({ readiness, queue, recovery }),
      ),
    [],
  );
  const { toast } = useToast();
  const [repairing, setRepairing] = useState(false);

  const runRepair = async (apply: boolean) => {
    setRepairing(true);
    try {
      const result = await api.repairQueueHealth(apply);
      const summary = result.actions
        .map((a) => `${a.code}=${apply ? (a.applied_rows ?? 0) : (a.would_affect_rows ?? 0)}`)
        .join(", ");
      toast(apply ? `Repair applied: ${summary}` : `Dry-run: ${summary}`);
      state.refetch();
    } catch (e) {
      toast(formatApiError("Repair", e));
    } finally {
      setRepairing(false);
    }
  };

  return (
    <AsyncBoundary
      state={state}
      emptyTitle="No health data"
      emptyBody="Operational health is unavailable — the bootstrap admin surface may be unreachable."
    >
      {(data) => (
        <div>
          <div className="eyebrow"><span>Operational health</span></div>
          <h2 style={{ margin: "8px 0 16px" }}>Readiness, queues &amp; recovery</h2>

          <SoftCard>
            <div className="row spread">
              <strong>Overall readiness</strong>
              <StatusPill
                label={data.readiness.status}
                tone={data.readiness.status === "ready" ? "positive" : undefined}
              />
            </div>
            <div className="card-list" style={{ marginTop: 12 }}>
              {Object.entries(data.readiness.components).map(([name, component]) => (
                <div className="row spread" key={name}>
                  <span className="meta">{name.replace(/_/g, " ")}</span>
                  <StatusPill
                    label={component.state}
                    tone={component.state === "ready" ? "positive" : undefined}
                  />
                </div>
              ))}
            </div>
          </SoftCard>

          <SoftCard style={{ marginTop: 16 }}>
            <div className="row spread">
              <strong>Coordination queues</strong>
              <span className="meta">{data.queue.diagnosis.issue_count} issue(s) detected</span>
            </div>
            <div className="card-list" style={{ marginTop: 12 }}>
              {Object.entries(data.queue.summary.families).map(([name, family]) => (
                <div className="row spread" key={name}>
                  <span className="meta">{name.replace(/_/g, " ")}</span>
                  <code>
                    {family.open}/{family.total} open
                    {family.stale_or_expired > 0
                      ? `, ${family.stale_or_expired} stale/expired`
                      : ""}
                  </code>
                </div>
              ))}
            </div>
            <div className="row" style={{ marginTop: 12, gap: 8 }}>
              <button
                className="btn small"
                disabled={repairing}
                onClick={() => void runRepair(false)}
              >
                Preview repair
              </button>
              <button
                className="btn small"
                disabled={repairing}
                onClick={() => void runRepair(true)}
              >
                Apply safe repair
              </button>
            </div>
            <p className="meta" style={{ marginTop: 8 }}>
              Repair only ever performs a status transition or an expired-claim
              release already used opportunistically by each queue family; it
              never deletes an open approval, unread mail, or unresolved work.
            </p>
          </SoftCard>

          <SoftCard style={{ marginTop: 16 }}>
            <div className="row spread">
              <strong>Recovery policy</strong>
              <StatusPill
                label={data.recovery.policy.complete ? "complete" : "incomplete"}
                tone={data.recovery.policy.complete ? "positive" : undefined}
              />
            </div>
            {!data.recovery.policy.complete && (
              <p className="meta" style={{ marginTop: 8 }}>
                Missing: {data.recovery.policy.missing_fields.join(", ")}
              </p>
            )}
            <div className="row spread" style={{ marginTop: 8 }}>
              <span className="meta">Schedule</span>
              <code>{data.recovery.policy.schedule ?? "not configured"}</code>
            </div>
            <div className="row spread" style={{ marginTop: 8 }}>
              <span className="meta">Retention</span>
              <code>
                {data.recovery.policy.retention_days
                  ? `${data.recovery.policy.retention_days}d`
                  : "not configured"}
              </code>
            </div>
            <div className="row spread" style={{ marginTop: 8 }}>
              <span className="meta">RTO / RPO</span>
              <code>
                {data.recovery.policy.rto_minutes ?? "?"}m / {data.recovery.policy.rpo_minutes ?? "?"}m
              </code>
            </div>
            <div className="row spread" style={{ marginTop: 8 }}>
              <span className="meta">Offsite owner</span>
              <code>{data.recovery.policy.offsite_owner ?? "not configured"}</code>
            </div>
            <p className="meta" style={{ marginTop: 12 }}>
              Brains does not run a backup scheduler itself — this reflects the
              declared policy an external scheduler is expected to honour, and
              never fabricates a schedule or drill date.
            </p>
          </SoftCard>
        </div>
      )}
    </AsyncBoundary>
  );
}

function SectionInfo({ section, cfg }: { section: string; cfg: ConfigSummary }) {
  if (section === "integrations") {
    const github = cfg.integrations?.github;
    const bridges = cfg.integrations?.bridges ?? [];
    return (
      <SoftCard>
        <div className="eyebrow"><span>Integrations</span></div>
        <h2 style={{ margin: "8px 0 12px" }}>Integration readiness</h2>
        <p className="meta">
          Webhook secrets and repository names remain hidden. Delivery failures are retained
          as degraded bridge state.
        </p>
        <div className="row spread" style={{ marginTop: 12 }}>
          <span>GitHub webhook</span>
          <code>
            {github
              ? `${github.configured ? "configured" : "unconfigured"} (${github.allowed_repository_count} repositories)`
              : "status unavailable"}
          </code>
        </div>
        {bridges.map((bridge) => (
          <div className="row spread" style={{ marginTop: 8 }} key={bridge.name}>
            <span>{bridge.name}</span>
            <code>{bridge.status}</code>
          </div>
        ))}
      </SoftCard>
    );
  }
  return (
    <SoftCard>
      <div className="eyebrow"><span>{labelFor(section)}</span></div>
      <h2 style={{ margin: "8px 0 12px" }}>{labelFor(section)}</h2>
      <p className="meta">
        {section === "mcp"
          ? "MCP servers are registered with the hub and surfaced to agents via the coordination layer."
          : "Configured via the gateway environment."}
      </p>
      <div className="row spread" style={{ marginTop: 12 }}>
        <span className="meta">Gateway base</span>
        <code>{cfg.gateway.base_url ?? "/v1"}</code>
      </div>
    </SoftCard>
  );
}

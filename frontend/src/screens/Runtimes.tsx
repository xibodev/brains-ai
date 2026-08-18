import { useState } from "react";
import { api } from "../api/client";
import type { Runtime } from "../api/types";
import { useAsync } from "../store/useAsync";
import { useTopic } from "../realtime/useRealtime";
import { ScreenHead } from "./ScreenHead";
import { RuntimeTile } from "../components/RuntimeTile";
import { SoftCard } from "../components/SoftCard";
import { StatusPill } from "../components/StatusPill";
import { AsyncBoundary } from "../components/EmptyState";
import { Drawer } from "../components/Drawer";
import { ConnectMachineModal } from "../components/ConnectMachineModal";
import { useToast } from "../components/Toast";
import { relativeTime } from "../components/format";

// Runtimes — live status grid driven by org/{slug}/runtimes (WS4 §3.7).
//
// `capabilities` arrives as a JSON *string* on the wire; parse it before
// pretty-printing so the drawer shows readable JSON, not an escaped blob.
function formatCapabilities(caps: unknown): string {
  if (typeof caps === "string") {
    try {
      return JSON.stringify(JSON.parse(caps), null, 2);
    } catch {
      return caps;
    }
  }
  return JSON.stringify(caps, null, 2);
}

export function Runtimes() {
  const { toast } = useToast();
  const [selected, setSelected] = useState<Runtime | null>(null);
  const [connectOpen, setConnectOpen] = useState(false);

  // Runtimes are GLOBAL infra (CONFIGURE group), not org-scoped: the daemon
  // registers them org-less, so list ALL of them regardless of the active org.
  const state = useAsync<Runtime[]>(() => api.listRuntimes(), []);

  // heartbeat / status / registered events animate tiles live. Runtimes
  // publish to the org-less `org/default/runtimes` topic.
  useTopic("org/default/runtimes", () => state.refetch());

  const drain = async (rt: Runtime) => {
    try {
      await api.patchRuntime(rt.slug, { status: "draining" });
      toast(`Draining ${rt.machine_label ?? rt.slug}`);
      state.refetch();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Drain failed");
    }
  };

  const connectButton = (
    <button
      className="btn primary"
      onClick={() => setConnectOpen(true)}
    >
      + Connect a machine
    </button>
  );

  return (
    <div>
      <ScreenHead
        eyebrow="Runtimes"
        title="Where work can run"
        actions={connectButton}
      />
      <AsyncBoundary
        state={state}
        emptyTitle="No runtimes registered"
        emptyBody="Connect a machine that can run CLIs. Run the daemon command on a target host and it appears here on first heartbeat."
        emptyAction={
          <button
            className="btn primary"
            onClick={() => setConnectOpen(true)}
          >
            + Connect a machine
          </button>
        }
      >
        {(runtimes) => (
          <div className="grid">
            {runtimes.map((rt) => (
              <RuntimeTile
                key={String(rt.id)}
                rt={rt}
                selected={selected?.id === rt.id}
                onClick={() => setSelected(rt)}
              />
            ))}
          </div>
        )}
      </AsyncBoundary>

      <ConnectMachineModal
        open={connectOpen}
        onClose={() => setConnectOpen(false)}
        onConnected={() => {
          // Refresh the grid behind the modal but KEEP the modal open so the
          // operator sees the "connected — N CLIs detected" confirmation. They
          // dismiss it themselves.
          state.refetch();
        }}
      />

      <Drawer open={!!selected} onClose={() => setSelected(null)}>
        {selected && (
          <div>
            <div className="eyebrow">
              <span>Runtime</span>
            </div>
            <h2 style={{ margin: "8px 0 16px" }}>
              {selected.machine_label ?? selected.slug}
            </h2>
            <div className="row wrap" style={{ marginBottom: 16 }}>
              <StatusPill label={selected.status ?? "unknown"} dot />
              {selected.health && <StatusPill label={selected.health} />}
              {selected.tool && <StatusPill label={selected.tool} tone="accent" />}
            </div>
            <SoftCard>
              <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "auto 1fr", gap: "8px 16px" }}>
                <span className="meta">machine_id</span>
                <span className="mono">{selected.machine_id ?? "—"}</span>
                <span className="meta">os</span>
                <span>{selected.os ?? "—"}</span>
                <span className="meta">working root</span>
                <span className="mono">{selected.working_root ?? "—"}</span>
                <span className="meta">daemon</span>
                <span>{selected.daemon_version ?? "—"}</span>
                <span className="meta">last heartbeat</span>
                <span>{relativeTime(selected.last_heartbeat_at)}</span>
                <span className="meta">sessions here</span>
                <span>{selected.active_sessions ?? 0}</span>
              </dl>
            </SoftCard>
            {selected.capabilities && (
              <SoftCard style={{ marginTop: 12 }}>
                <div className="eyebrow"><span>Capabilities</span></div>
                <pre className="mono" style={{ whiteSpace: "pre-wrap", margin: "8px 0 0" }}>
                  {formatCapabilities(selected.capabilities)}
                </pre>
              </SoftCard>
            )}
            {selected.status === "online" && (
              <div className="row" style={{ marginTop: 16 }}>
                <button className="btn danger" onClick={() => void drain(selected)}>
                  Drain
                </button>
              </div>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}

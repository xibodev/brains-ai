// ConnectMachineModal — F1 "Connect a machine" flow.
//
// On open: snapshots existing machine_ids, calls POST /v1/runtimes/enrol,
// shows the real one-line command, then polls GET /v1/runtimes every 2 s
// (and subscribes to the org/default/runtimes realtime topic) until a new
// machine_id appears — then flips to the "connected" state.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { EnrolResponse, Runtime } from "../api/types";
import { useTopic } from "../realtime/useRealtime";
import { useToast } from "./Toast";
import { Drawer } from "./Drawer";
import { SoftCard } from "./SoftCard";

type Status = "enrolling" | "waiting" | "connected" | "error";

export function ConnectMachineModal({
  open,
  onClose,
  onConnected,
}: {
  open: boolean;
  onClose: () => void;
  onConnected: () => void;
}) {
  const { toast } = useToast();
  const [enrol, setEnrol] = useState<EnrolResponse | null>(null);
  const [status, setStatus] = useState<Status>("enrolling");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [newRuntime, setNewRuntime] = useState<Runtime | null>(null);
  const [newCliCount, setNewCliCount] = useState<number>(0);

  // Stable refs so polling/realtime callbacks always see fresh values.
  const baselineIds = useRef<Set<string>>(new Set());
  const detectedRef = useRef(false);
  const onConnectedRef = useRef(onConnected);
  onConnectedRef.current = onConnected;

  // Reset + kick off enrol each time the modal opens.
  useEffect(() => {
    if (!open) {
      setEnrol(null);
      setStatus("enrolling");
      setErrorMsg(null);
      setNewRuntime(null);
      setNewCliCount(0);
      baselineIds.current = new Set();
      detectedRef.current = false;
      return;
    }

    let cancelled = false;
    setStatus("enrolling");
    detectedRef.current = false;

    async function run() {
      // Snapshot current runtimes before enrol so we can diff for new arrivals.
      const rts = await api.listRuntimes();
      if (cancelled) return;
      baselineIds.current = new Set(rts.map((r) => String(r.machine_id ?? r.id)));

      const result = await api.enrolRuntime({});
      if (cancelled) return;
      setEnrol(result);
      setStatus("waiting");
    }

    run().catch((e: unknown) => {
      if (!cancelled) {
        setErrorMsg(e instanceof Error ? e.message : "Could not generate enrol command.");
        setStatus("error");
      }
    });

    return () => {
      cancelled = true;
    };
  }, [open]);

  // Check the current runtime list for a new machine_id.
  const checkForArrival = useCallback(() => {
    if (detectedRef.current) return;
    api
      .listRuntimes()
      .then((rts) => {
        if (detectedRef.current) return;
        const arrived = rts.find(
          (r) => !baselineIds.current.has(String(r.machine_id ?? r.id)),
        );
        if (arrived) {
          detectedRef.current = true;
          // One runtime is registered per detected CLI; count the siblings that
          // share this machine to report "N CLIs detected".
          const arrivedMachine = String(arrived.machine_id ?? arrived.id);
          const count = rts.filter(
            (r) => String(r.machine_id ?? r.id) === arrivedMachine,
          ).length;
          setNewRuntime(arrived);
          setNewCliCount(count);
          setStatus("connected");
          onConnectedRef.current();
        }
      })
      .catch(() => {
        // ignore transient poll errors
      });
  }, []);

  // Poll every 2 s while waiting.
  useEffect(() => {
    if (status !== "waiting") return;
    const id = window.setInterval(checkForArrival, 2000);
    return () => window.clearInterval(id);
  }, [status, checkForArrival]);

  // Also react to realtime pushes for faster detection.
  useTopic(status === "waiting" ? "org/default/runtimes" : null, checkForArrival);

  const copy = () => {
    if (!enrol) return;
    navigator.clipboard?.writeText(enrol.command);
    toast("Copied");
  };

  const cliCount = newRuntime ? newCliCount : undefined;

  return (
    <Drawer open={open} onClose={onClose}>
      <div className="eyebrow">
        <span>Connect a machine</span>
      </div>
      <h2 style={{ margin: "8px 0 20px" }}>Run the daemon on a target host</h2>

      {status === "enrolling" && (
        <p className="meta">Generating connect command…</p>
      )}

      {status === "error" && (
        <p style={{ color: "var(--danger)", margin: "0 0 16px" }}>
          {errorMsg ?? "Failed to generate an enrol command."}
        </p>
      )}

      {(status === "waiting" || status === "connected") && enrol && (
        <>
          <p className="meta" style={{ marginBottom: 8 }}>
            Copy and run this command on the machine you want to connect:
          </p>

          <SoftCard style={{ marginBottom: 4 }}>
            <code
              data-testid="connect-command"
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 12,
                whiteSpace: "pre-wrap",
                wordBreak: "break-all",
                display: "block",
              }}
            >
              {enrol.command}
            </code>
          </SoftCard>

          <div className="row" style={{ marginBottom: 20, gap: 12 }}>
            <button className="btn small primary" onClick={copy}>
              Copy
            </button>
            <span className="meta">
              Expires {new Date(enrol.expires_at).toLocaleString()}
            </span>
          </div>

          {status === "waiting" && (
            <div className="row" style={{ gap: 8 }}>
              <span className="dot live" />
              <span className="meta">Waiting for this machine…</span>
            </div>
          )}

          {status === "connected" && newRuntime && (
            <div className="row" style={{ gap: 8 }}>
              <span aria-hidden="true" style={{ fontSize: 15 }}>&#x2705;</span>
              <span>
                <strong>
                  {newRuntime.machine_label ??
                    newRuntime.machine_id ??
                    newRuntime.slug}
                </strong>
                {" connected"}
                {cliCount !== undefined
                  ? ` \u2014 ${cliCount} CLI${cliCount !== 1 ? "s" : ""} detected`
                  : ""}
              </span>
            </div>
          )}
        </>
      )}
    </Drawer>
  );
}

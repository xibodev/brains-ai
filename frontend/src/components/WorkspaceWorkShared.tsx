import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError, formatApiError } from "../api/client";
import { useAsync } from "../store/useAsync";
import { OperatorStatus } from "./OperatorPrimitives";

// Established console / Operate: paper surfaces, green ink, blue actions and
// serif headings. Lists lead to inspectable records; forms expand in place.
// Manual observations and human actions never imply an agent process is running.
export function useWorkRead<T>(read: (signal: AbortSignal) => Promise<T>, deps: readonly unknown[]) {
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  return useAsync(async () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    const data = await read(next.signal);
    return { value: data, refreshedAt: new Date().toISOString() };
  }, deps);
}

export function useWorkMutation() {
  const controller = useRef<AbortController | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [feedback, setFeedback] = useState("");
  useEffect(() => () => controller.current?.abort(), []);
  async function run<T>(action: string, work: (signal: AbortSignal) => Promise<T>, done: (value: T) => void, stale?: () => void) {
    if (controller.current) return;
    const next = new AbortController();
    controller.current = next;
    setPending(true);
    setError(null);
    setFeedback("");
    try {
      const result = await work(next.signal);
      if (!next.signal.aborted) done(result);
    } catch (reason) {
      if (!next.signal.aborted) {
        setError(formatApiError(action, reason));
        if (reason instanceof ApiError && reason.status === 409 && stale) {
          setConflict(true);
          stale();
        }
      }
    } finally {
      if (!next.signal.aborted) {
        controller.current = null;
        setPending(false);
      }
    }
  }
  return { run, pending, error, setError, conflict, feedback, setFeedback,
    reviewed: () => { setConflict(false); setError(null); } };
}

export function useWorkCreationKey() {
  const key = useRef<string | null>(null);
  return {
    get: () => key.current ?? (key.current = crypto.randomUUID()),
    isRetry: () => key.current !== null,
    edited: () => { key.current = null; },
  };
}

export function workTextError(value: string, label: string, required = false, maximum = 32768): string | null {
  if (required && !value.trim()) return `${label} is required.`;
  if (value.includes("\0") || new TextEncoder().encode(value).length > maximum) {
    return `${label} must be non-NUL text, at most ${maximum.toLocaleString()} UTF-8 bytes.`;
  }
  return null;
}

export function workDeadline(value: string, bounded = false): string | undefined {
  if (!value) return undefined;
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) throw new Error("Enter a valid local deadline.");
  if (bounded && (timestamp <= Date.now() || timestamp > Date.now() + 30 * 86400000)) {
    throw new Error("Deadline must be in the future and within 30 days.");
  }
  return new Date(timestamp).toISOString();
}

export function WorkField({ label, required, hint, children }: {
  label: string; required?: boolean; hint?: string; children: ReactNode;
}) {
  return <label className="operator-field workspace-work-field"><span>{label}{required && " (required)"}</span>{children}{hint && <small>{hint}</small>}</label>;
}

export function WorkText({ label, value }: { label: string; value?: string | null }) {
  return <div className="workspace-work-text"><h4>{label}</h4><pre tabIndex={0} aria-label={label}>{value || "Not recorded."}</pre></div>;
}

export function workTime(value?: string | null) {
  if (!value) return "Not recorded";
  // Core legacy timestamps without an offset are UTC, not browser local time.
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function WorkRefresh({ at, pending, refresh }: { at?: string; pending: boolean; refresh: () => void }) {
  return <div className="workspace-work-refresh"><span>Manual refresh · {at ? `Last refreshed ${workTime(at)}` : "Not yet refreshed"}</span><button type="button" className="operator-button" disabled={pending} onClick={refresh}>Refresh</button></div>;
}

export function WorkStatus({ status, assignment = false }: { status: string; assignment?: boolean }) {
  const labels: Record<string, string> = {
    ready: "Ready for acceptance", accepted: "Accepted", planned: "Awaiting acknowledgements",
    collecting: "Collecting initial reports", discussing: "Discussion", cancel_requested: "Cancellation requested",
    cancelled: "Cancelled", uncertain: "Uncertain", failed: "Reported failed",
    completed: assignment ? "Reported completed" : "Synthesis recorded",
  };
  const tone = status === "uncertain" || status === "cancel_requested" ? "warning"
    : status === "failed" ? "danger" : status === "completed" ? "ready" : "neutral";
  return <OperatorStatus tone={tone}>{labels[status] || status.replaceAll("_", " ")}</OperatorStatus>;
}

export function WorkAuthor({ kind, session }: { kind: string; session: string | null }) {
  return <p className="workspace-work-meta">{kind === "operator" ? "Recorded as you · human operator" : <>Recorded by agent Session <code>{session || "not recorded"}</code></>}</p>;
}

export function WorkMutationNotice({ mutation, canReview }: { mutation: ReturnType<typeof useWorkMutation>; canReview: boolean }) {
  return <>{mutation.error && <p className="workspace-work-error" role="alert">{mutation.error}</p>}
    {mutation.conflict && <div className="workspace-work-notice" role="alert"><p>The record changed. Refreshing the latest state; review it before another action. Nothing was automatically retried.</p><button type="button" className="operator-button" disabled={!canReview} onClick={mutation.reviewed}>I reviewed the refreshed record</button></div>}
    {mutation.feedback && <p className="workspace-work-notice" role="status">{mutation.feedback}</p>}</>;
}

export function WorkCancel({ disabled, pending, reasonRequired, confirm, label = "Request cancellation" }: {
  disabled: boolean; pending: boolean; reasonRequired?: boolean; confirm: (reason: string) => void; label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const close = () => { setOpen(false); setError(null); trigger.current?.focus(); };
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  return <div className="workspace-work-cancel">
    <button type="button" ref={trigger} className="operator-button danger" disabled={disabled || pending} aria-expanded={open} aria-controls={id} onClick={() => setOpen(true)}>{label}</button>
    {open && <form id={id} className="workspace-work-confirm" onSubmit={(event) => {
      event.preventDefault();
      if (pending || disabled) return;
      const problem = reasonRequired ? workTextError(reason, "Cancellation reason", true) : null;
      if (problem) { setError(problem); return; }
      confirm(reason);
      close();
    }}><p>Cancel this recorded work? Cancellation is cooperative; it does not stop an agent process.</p>
      {reasonRequired && <WorkField label="Cancellation reason" required><textarea autoFocus required value={reason} onChange={(event) => setReason(event.target.value)} disabled={pending} /></WorkField>}
      {error && <p role="alert" className="workspace-work-error">{error}</p>}
      <div className="operator-action-row"><button className="operator-button danger" disabled={pending || disabled}>Confirm cancellation</button><button type="button" className="operator-button" disabled={pending} onClick={close}>Keep work open</button></div>
    </form>}
  </div>;
}

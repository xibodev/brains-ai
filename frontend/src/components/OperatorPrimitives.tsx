import type { ReactNode } from "react";

export function OperatorPageHead({
  eyebrow,
  title,
  lede,
  actions,
  meta,
}: {
  eyebrow: string;
  title: string;
  lede: string;
  actions?: ReactNode;
  meta?: ReactNode;
}) {
  return (
    <header className="operator-page-head">
      <div>
        <div className="operator-eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{lede}</p>
      </div>
      {actions ? <div className="operator-action-row">{actions}</div> : meta ? <div className="operator-page-meta">{meta}</div> : null}
    </header>
  );
}

export function OperatorCard({
  kicker,
  title,
  action,
  children,
  className = "",
}: {
  kicker?: string;
  title?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`operator-card ${className}`.trim()}>
      {(kicker || title || action) && (
        <header className="operator-card-head">
          <div>
            {kicker && <div className="operator-card-kicker">{kicker}</div>}
            {title && <h2>{title}</h2>}
          </div>
          {action}
        </header>
      )}
      <div className="operator-card-body">{children}</div>
    </section>
  );
}

export function OperatorStatus({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "ready" | "warning" | "danger" | "native" | "adapter" | "host" | "neutral";
}) {
  return <span className={`operator-status ${tone}`}>{children}</span>;
}

export function OperatorState({
  loading,
  error,
  kind,
  empty,
  emptyTitle = "Nothing here yet",
  emptyBody = "This view will fill as durable work is recorded.",
  boundary,
}: {
  loading: boolean;
  error?: string | null;
  kind?: "unauthorized" | "not_found" | "error" | null;
  empty?: boolean;
  emptyTitle?: string;
  emptyBody?: string;
  boundary?: string;
}) {
  if (loading) {
    return (
      <div className="operator-state" role="status" data-async-state="loading" data-boundary={boundary}>
        <span className="operator-loader" />
        <strong>Reading durable state</strong>
      </div>
    );
  }
  if (error) {
    const title = kind === "unauthorized"
      ? "Authorization required"
      : kind === "not_found"
        ? "Requested resource not found"
        : "Could not load this control view";
    const detail = kind === "unauthorized"
      ? "Sign in with an authorized local operator before using this view."
      : kind === "not_found"
        ? "The requested resource is unavailable or outside your visible scope."
        : "This view could not be loaded. Retry after checking the local service status.";
    return (
      <div className="operator-state error" role="alert" data-async-state={kind ?? "error"} data-boundary={boundary}>
        <strong>{title}</strong>
        <span>{detail}</span>
      </div>
    );
  }
  if (empty) {
    return (
      <div className="operator-state" data-async-state="empty" data-boundary={boundary}>
        <strong>{emptyTitle}</strong>
        <span>{emptyBody}</span>
      </div>
    );
  }
  return <span className="visually-hidden" role="status" data-async-state="success" data-boundary={boundary}>Ready</span>;
}

export function OperatorMiniList({
  rows,
}: {
  rows: Array<{ label: ReactNode; value: ReactNode }>;
}) {
  return (
    <div className="operator-mini-list">
      {rows.map((row, index) => (
        <div className="operator-mini-row" key={index}>
          <span>{row.label}</span>
          <strong>{row.value}</strong>
        </div>
      ))}
    </div>
  );
}

export function countByStatus(rows: Array<{ status?: string }>, status: string): number {
  return rows.filter((row) => row.status === status).length;
}

export function displayValue(value: unknown, fallback = "Not reported"): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

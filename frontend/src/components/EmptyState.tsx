import type { ReactNode } from "react";

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {body && <p>{body}</p>}
      {action && <div style={{ marginTop: 16 }}>{action}</div>}
    </div>
  );
}

export function Loading({ what = "Loading…" }: { what?: string }) {
  return <div className="loading" role="status">{what}</div>;
}

// Uniform wrapper that renders loading / error / empty / content states from a
// useAsync result so every screen tells the same story (WS4 brief: graceful
// empty/loading states).
export function AsyncBoundary<T>({
  state,
  emptyTitle,
  emptyBody,
  emptyAction,
  isEmpty,
  children,
}: {
  state: { data: T | undefined; loading: boolean; error: string | null; errorKind?: "unauthorized" | "not_found" | "error" | null };
  emptyTitle: string;
  emptyBody?: string;
  emptyAction?: ReactNode;
  isEmpty?: (data: T) => boolean;
  children: (data: T) => ReactNode;
}) {
  if (state.loading) return <div data-async-state="loading"><Loading /></div>;
  if (state.error) {
    const detail = state.errorKind === "unauthorized"
      ? "Sign in with an authorized local operator before using this view."
      : state.errorKind === "not_found"
        ? "The requested resource is unavailable or outside your visible scope."
        : state.error;
    return (
      <div data-async-state={state.errorKind ?? "error"} role="alert">
      <EmptyState
        title={state.errorKind === "unauthorized" ? "Authorization required" : state.errorKind === "not_found" ? "Requested resource not found" : "Couldn't load this"}
        body={detail}
      />
      </div>
    );
  }
  const data = state.data as T;
  const empty =
    data === undefined ||
    (Array.isArray(data) && data.length === 0) ||
    (isEmpty ? isEmpty(data) : false);
  if (empty) {
    return <div data-async-state="empty"><EmptyState title={emptyTitle} body={emptyBody} action={emptyAction} /></div>;
  }
  return <div data-async-state="success">{children(data)}</div>;
}

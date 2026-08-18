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
  return <div className="loading">{what}</div>;
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
  state: { data: T | undefined; loading: boolean; error: string | null };
  emptyTitle: string;
  emptyBody?: string;
  emptyAction?: ReactNode;
  isEmpty?: (data: T) => boolean;
  children: (data: T) => ReactNode;
}) {
  if (state.loading && state.data === undefined) return <Loading />;
  if (state.error) {
    return (
      <EmptyState
        title="Couldn't load this"
        body={state.error}
      />
    );
  }
  const data = state.data as T;
  const empty =
    data === undefined ||
    (Array.isArray(data) && data.length === 0) ||
    (isEmpty ? isEmpty(data) : false);
  if (empty) {
    return <EmptyState title={emptyTitle} body={emptyBody} action={emptyAction} />;
  }
  return <>{children(data)}</>;
}

// Tiny async-data hook — a graceful loading/error/data state machine over a
// promise factory, with manual refetch. Avoids pulling in TanStack Query while
// still giving every screen a uniform empty/loading/error story (WS4 brief).

import { useCallback, useEffect, useState } from "react";

type AsyncSnapshot<T> = Pick<AsyncState<T>, "data" | "loading" | "error" | "errorKind">;

export interface AsyncState<T> {
  data: T | undefined;
  loading: boolean;
  error: string | null;
  errorKind: "unauthorized" | "not_found" | "error" | null;
  refetch: () => void;
  setData: (updater: (prev: T | undefined) => T) => void;
}

export function useAsync<T>(
  factory: () => Promise<T>,
  deps: ReadonlyArray<unknown>,
): AsyncState<T> {
  const [state, setState] = useState<AsyncSnapshot<T>>({
    data: undefined,
    loading: true,
    error: null,
    errorKind: null,
  });
  const [nonce, setNonce] = useState(0);

  // factory is intentionally excluded — callers pass an inline closure and
  // gate re-runs through `deps` + the refetch nonce.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(factory, deps);

  useEffect(() => {
    let cancelled = false;
    // A refresh is a new observation, not permission to keep presenting the
    // previous one as current while the request is pending or has failed.
    setState({ data: undefined, loading: true, error: null, errorKind: null });
    run()
      .then((d) => {
        if (!cancelled) setState({ data: d, loading: false, error: null, errorKind: null });
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          const status = typeof e === "object" && e !== null && "status" in e
            ? Number((e as { status?: unknown }).status)
            : undefined;
          setState({
            data: undefined,
            loading: false,
            errorKind: status === 401 || status === 403 ? "unauthorized" : status === 404 ? "not_found" : "error",
            error: e instanceof Error ? e.message : String(e),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [run, nonce]);

  const refetch = useCallback(() => {
    setState({ data: undefined, loading: true, error: null, errorKind: null });
    setNonce((n) => n + 1);
  }, []);
  const patch = useCallback(
    (updater: (prev: T | undefined) => T) => setState((current) => ({
      ...current,
      data: updater(current.data),
    })),
    [],
  );

  return { ...state, refetch, setData: patch };
}

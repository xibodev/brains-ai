// Tiny async-data hook — a graceful loading/error/data state machine over a
// promise factory, with manual refetch. Avoids pulling in TanStack Query while
// still giving every screen a uniform empty/loading/error story (WS4 brief).

import { useCallback, useEffect, useState } from "react";

export interface AsyncState<T> {
  data: T | undefined;
  loading: boolean;
  error: string | null;
  refetch: () => void;
  setData: (updater: (prev: T | undefined) => T) => void;
}

export function useAsync<T>(
  factory: () => Promise<T>,
  deps: ReadonlyArray<unknown>,
): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  // factory is intentionally excluded — callers pass an inline closure and
  // gate re-runs through `deps` + the refetch nonce.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(factory, deps);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    run()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [run, nonce]);

  const refetch = useCallback(() => setNonce((n) => n + 1), []);
  const patch = useCallback(
    (updater: (prev: T | undefined) => T) => setData((prev) => updater(prev)),
    [],
  );

  return { data, loading, error, refetch, setData: patch };
}

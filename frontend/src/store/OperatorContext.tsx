import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api } from "../api/client";
import type { OperatorCapabilityCatalog } from "../api/types";

interface OperatorContextValue {
  catalog: OperatorCapabilityCatalog | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

const OperatorContext = createContext<OperatorContextValue | null>(null);

export function OperatorProvider({ children }: { children: ReactNode }) {
  const [catalog, setCatalog] = useState<OperatorCapabilityCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .operatorCapabilities()
      .then((next) => {
        if (!cancelled) setCatalog(next);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  return (
    <OperatorContext.Provider
      value={{ catalog, loading, error, refresh: () => setNonce((value) => value + 1) }}
    >
      {children}
    </OperatorContext.Provider>
  );
}

export function useOperator(): OperatorContextValue {
  const context = useContext(OperatorContext);
  if (!context) throw new Error("useOperator must be used within OperatorProvider");
  return context;
}

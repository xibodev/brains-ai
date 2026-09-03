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
  errorKind: "unauthorized" | "error" | null;
  refresh: () => void;
}

const OperatorContext = createContext<OperatorContextValue | null>(null);

export function OperatorProvider({ children }: { children: ReactNode }) {
  const [catalog, setCatalog] = useState<OperatorCapabilityCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorKind, setErrorKind] = useState<OperatorContextValue["errorKind"]>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setErrorKind(null);
    api
      .operatorCapabilities()
      .then((next) => {
        if (!cancelled) setCatalog(next);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          const status = typeof reason === "object" && reason !== null && "status" in reason
            ? Number((reason as { status?: unknown }).status)
            : undefined;
          setErrorKind(status === 401 || status === 403 ? "unauthorized" : "error");
          setError(reason instanceof Error ? reason.message : String(reason));
        }
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
      value={{ catalog, loading, error, errorKind, refresh: () => setNonce((value) => value + 1) }}
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

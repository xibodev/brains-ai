import {
  createContext,
  useCallback,
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
  const [state, setState] = useState<Omit<OperatorContextValue, "refresh">>({
    catalog: null,
    loading: true,
    error: null,
    errorKind: null,
  });
  const [nonce, setNonce] = useState(0);
  const refresh = useCallback(() => {
    setState({ catalog: null, loading: true, error: null, errorKind: null });
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Capabilities authorize and shape actions. Never retain an earlier
    // catalog across a refresh boundary whose result is not yet known.
    setState({ catalog: null, loading: true, error: null, errorKind: null });
    api
      .operatorCapabilities()
      .then((next) => {
        if (!cancelled) setState({ catalog: next, loading: false, error: null, errorKind: null });
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          const status = typeof reason === "object" && reason !== null && "status" in reason
            ? Number((reason as { status?: unknown }).status)
            : undefined;
          setState({
            catalog: null,
            loading: false,
            errorKind: status === 401 || status === 403 ? "unauthorized" : "error",
            error: reason instanceof Error ? reason.message : String(reason),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  return (
    <OperatorContext.Provider
      value={{ ...state, refresh }}
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

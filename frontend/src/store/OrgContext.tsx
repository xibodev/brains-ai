// Org context — the active org scopes every workspace screen. Persists the
// active slug in localStorage and exposes the org list + switch action.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api } from "../api/client";
import type { Org } from "../api/types";

interface OrgContextValue {
  orgs: Org[];
  activeOrg: Org | null;
  activeSlug: string | null;
  setActiveOrg: (slug: string) => void;
  loading: boolean;
  refresh: () => void;
}

const OrgContext = createContext<OrgContextValue | null>(null);
const STORE_KEY = "brains.activeOrg";

export function OrgProvider({ children }: { children: ReactNode }) {
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [activeSlug, setSlug] = useState<string | null>(
    () => localStorage.getItem(STORE_KEY),
  );
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listOrgs()
      .then((list) => {
        if (cancelled) return;
        setOrgs(list);
        setSlug((cur) => {
          if (cur && list.some((o) => o.slug === cur)) return cur;
          return list[0]?.slug ?? null;
        });
      })
      .catch(() => {
        if (!cancelled) setOrgs([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  const setActiveOrg = useCallback((slug: string) => {
    setSlug(slug);
    localStorage.setItem(STORE_KEY, slug);
  }, []);

  const value = useMemo<OrgContextValue>(() => {
    const activeOrg = orgs.find((o) => o.slug === activeSlug) ?? null;
    return {
      orgs,
      activeOrg,
      activeSlug,
      setActiveOrg,
      loading,
      refresh: () => setNonce((n) => n + 1),
    };
  }, [orgs, activeSlug, setActiveOrg, loading]);

  return <OrgContext.Provider value={value}>{children}</OrgContext.Provider>;
}

export function useOrg(): OrgContextValue {
  const ctx = useContext(OrgContext);
  if (!ctx) throw new Error("useOrg must be used within OrgProvider");
  return ctx;
}

import { useEffect, useState } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { api } from "../api/client";
import { TopBar } from "./TopBar";
import { Sidebar } from "./Sidebar";
import { ChatDock } from "./ChatDock";
import { CommandPalette } from "./CommandPalette";
import { useDock } from "../store/DockContext";
import { useConnState } from "../realtime/useRealtime";

// 3-column shell + topbar. In /config|/settings the sidebar narrows to icons so
// the section rail reads as a continuous left edge (founder's want).
//
// Fresh-state guard (F6 / AC-F6-01): before rendering any app screen we ask the
// server whether this operator is still owed onboarding. The decision is a
// server fact about the store - an install that has never produced a Session
// for an Issue - so it survives a reload and a new browser, and it is asked
// once per mount rather than trusted from local state. A failed check renders
// the app rather than trapping the operator in a redirect.
export function AppShell() {
  const { collapsed } = useDock();
  const location = useLocation();
  const conn = useConnState();
  const [theme, setTheme] = useState(() => localStorage.getItem("brains.theme") ?? "dark");
  const [onboardingRequired, setOnboardingRequired] = useState<boolean | null>(null);

  const configMode =
    location.pathname.startsWith("/config") || location.pathname.startsWith("/settings");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("brains.theme", theme);
  }, [theme]);

  useEffect(() => {
    let cancelled = false;
    api
      .onboardingState()
      .then((state) => {
        if (!cancelled) setOnboardingRequired(state.required);
      })
      .catch(() => {
        if (!cancelled) setOnboardingRequired(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const shellCls = ["shell"];
  if (collapsed) shellCls.push("dock-collapsed");
  if (configMode) shellCls.push("config-mode");

  if (onboardingRequired === null) {
    return <div className="shell-loading" data-testid="shell-loading" />;
  }
  if (onboardingRequired) {
    return <Navigate to="/onboarding" replace />;
  }

  return (
    <div className={shellCls.join(" ")}>
      <TopBar onTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))} />
      {conn === "closed" && (
        <div className="conn-banner" style={{ gridColumn: "1 / -1" }}>
          Realtime disconnected — reconnecting…
        </div>
      )}
      {conn === "denied" && (
        <div className="conn-banner" style={{ gridColumn: "1 / -1" }}>
          Realtime stopped — your access changed. Reload after signing in again.
        </div>
      )}
      <Sidebar mini={configMode} />
      <main className="main">
        <Outlet />
      </main>
      <ChatDock />
      <CommandPalette />
    </div>
  );
}

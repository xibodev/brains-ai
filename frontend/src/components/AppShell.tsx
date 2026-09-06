import { Outlet } from "react-router-dom";
import { TopBar } from "./TopBar";
import { Sidebar } from "./Sidebar";
import { CommandPalette } from "./CommandPalette";
import { useConnState } from "../realtime/useRealtime";

export function AppShell() {
  const conn = useConnState();

  return (
    <div className="control-shell">
      <Sidebar />
      <div className="control-content">
        <TopBar connection={conn} />
        {conn === "closed" && (
          <div className="control-connection" role="status" aria-live="polite" data-connection-state="degraded">Realtime disconnected. Reconnecting. Durable HTTP state remains available.</div>
        )}
        {conn === "denied" && (
          <div className="control-connection danger" role="alert" data-connection-state="unauthorized">
            Realtime access changed. Sign in again before relying on live state.
          </div>
        )}
        <main className="control-main">
          <Outlet />
        </main>
      </div>
      <CommandPalette />
    </div>
  );
}

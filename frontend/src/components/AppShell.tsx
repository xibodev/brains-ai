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
          <div className="control-connection">Realtime disconnected. Reconnecting.</div>
        )}
        {conn === "denied" && (
          <div className="control-connection danger">
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

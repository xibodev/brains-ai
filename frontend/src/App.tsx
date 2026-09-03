import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { OperatorProvider } from "./store/OperatorContext";
import { ToastProvider } from "./components/Toast";
import { AppShell } from "./components/AppShell";
import { CommandCenter } from "./screens/CommandCenter";
import { Workspaces } from "./screens/Workspaces";
import { OperatorCoordination } from "./screens/OperatorCoordination";
import { Governance } from "./screens/Governance";
import { Operations } from "./screens/Operations";
import { Act } from "./screens/Act";
import { Config } from "./screens/Config";

// Router resolves all deep links to index.html (FastAPI SPA history fallback).
// `basename` matches the /app StaticFiles mount prefix (vite base).
export function App() {
  return (
    <ToastProvider>
      <OperatorProvider>
            <BrowserRouter basename="/app">
              <Routes>
                <Route element={<AppShell />}>
                  <Route index element={<Navigate to="/command-center" replace />} />
                  <Route path="/command-center" element={<CommandCenter />} />
                  <Route path="/workspaces" element={<Workspaces />} />
                  <Route path="/workspaces/:slug" element={<Workspaces />} />
                  <Route path="/coordination" element={<OperatorCoordination />} />
                  <Route path="/governance" element={<Governance />} />
                  <Route path="/operations" element={<Operations />} />
                  <Route path="/operations/config" element={<Navigate to="/operations/config/mcp" replace />} />
                  <Route path="/operations/config/:section" element={<Config />} />
                  <Route path="/act" element={<Act />} />

                  <Route path="/inbox" element={<Navigate to="/governance" replace />} />
                  <Route path="/config" element={<Navigate to="/operations/config/mcp" replace />} />
                  <Route path="*" element={<Navigate to="/command-center" replace />} />
                </Route>
              </Routes>
            </BrowserRouter>
      </OperatorProvider>
    </ToastProvider>
  );
}

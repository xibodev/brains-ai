import { BrowserRouter, Route, Routes } from "react-router-dom";
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
import { NotFound } from "./screens/NotFound";

// Router resolves all deep links to index.html (FastAPI SPA history fallback).
// `basename` matches the /app StaticFiles mount prefix (vite base).
export function App() {
  return (
    <ToastProvider>
      <OperatorProvider>
            <BrowserRouter basename="/app">
              <Routes>
                <Route element={<AppShell />}>
                  <Route index element={<CommandCenter />} />
                  <Route path="/command-center" element={<CommandCenter />} />
                  <Route path="/workspaces" element={<Workspaces />} />
                  <Route path="/workspaces/:slug" element={<Workspaces />} />
                  <Route path="/coordination" element={<OperatorCoordination />} />
                  <Route path="/governance" element={<Governance />} />
                  <Route path="/operations" element={<Operations />} />
                  <Route path="/operations/config" element={<Config />} />
                  <Route path="/operations/config/:section" element={<Config />} />
                  <Route path="/act" element={<Act />} />

                  <Route path="/inbox" element={<NotFound />} />
                  <Route path="/config" element={<NotFound />} />
                  <Route path="*" element={<NotFound />} />
                </Route>
              </Routes>
            </BrowserRouter>
      </OperatorProvider>
    </ToastProvider>
  );
}

import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { OrgProvider } from "./store/OrgContext";
import { DockProvider } from "./store/DockContext";
import { OperatorProvider } from "./store/OperatorContext";
import { ToastProvider } from "./components/Toast";
import { AppShell } from "./components/AppShell";
import { CommandCenter } from "./screens/CommandCenter";
import { Workspaces } from "./screens/Workspaces";
import { OperatorCoordination } from "./screens/OperatorCoordination";
import { Governance } from "./screens/Governance";
import { Operations } from "./screens/Operations";
import { Act } from "./screens/Act";
import { LabsGate, LabsHome } from "./screens/Labs";
import { LegacyLabsRedirect } from "./screens/LegacyLabsRedirect";
import { Sessions } from "./screens/Sessions";
import { Personas } from "./screens/Personas";
import { Pods } from "./screens/Pods";
import { Projects } from "./screens/Projects";
import { Issues } from "./screens/Issues";
import { Runtimes } from "./screens/Runtimes";
import { Automation } from "./screens/Automation";
import { Config } from "./screens/Config";
import { Settings } from "./screens/Settings";
import { Onboarding } from "./screens/Onboarding";

// Router resolves all deep links to index.html (FastAPI SPA history fallback).
// `basename` matches the /app StaticFiles mount prefix (vite base).
export function App() {
  return (
    <ToastProvider>
      <OrgProvider>
        <DockProvider>
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
                  <Route path="/operations/config" element={<Navigate to="/operations/config/general" replace />} />
                  <Route path="/operations/config/:section" element={<Config />} />
                  <Route path="/operations/access" element={<Navigate to="/operations/access/org" replace />} />
                  <Route path="/operations/access/:section" element={<Settings />} />
                  <Route path="/act" element={<Act />} />

                  <Route element={<LabsGate />}>
                    <Route path="/labs" element={<LabsHome />} />
                    <Route path="/labs/onboarding" element={<Onboarding />} />
                    <Route path="/labs/sessions" element={<Sessions />} />
                    <Route path="/labs/sessions/:id" element={<Sessions />} />
                    <Route path="/labs/personas" element={<Personas />} />
                    <Route path="/labs/personas/:slug" element={<Personas />} />
                    <Route path="/labs/pods" element={<Pods />} />
                    <Route path="/labs/pods/:slug" element={<Pods />} />
                    <Route path="/labs/projects" element={<Projects />} />
                    <Route path="/labs/projects/:code" element={<Projects />} />
                    <Route path="/labs/issues" element={<Issues />} />
                    <Route path="/labs/issues/:code" element={<Issues />} />
                    <Route path="/labs/automation" element={<Automation />} />
                    <Route path="/labs/runtimes" element={<Runtimes />} />
                    <Route path="/labs/runtimes/:slug" element={<Runtimes />} />
                  </Route>

                  <Route path="/inbox" element={<Navigate to="/governance" replace />} />
                  <Route path="/sessions" element={<Navigate to="/labs/sessions" replace />} />
                  <Route path="/sessions/:id" element={<LegacyLabsRedirect to="/labs/sessions" parameter="id" />} />
                  <Route path="/personas" element={<Navigate to="/labs/personas" replace />} />
                  <Route path="/personas/:slug" element={<LegacyLabsRedirect to="/labs/personas" parameter="slug" />} />
                  <Route path="/pods" element={<Navigate to="/labs/pods" replace />} />
                  <Route path="/pods/:slug" element={<LegacyLabsRedirect to="/labs/pods" parameter="slug" />} />
                  <Route path="/projects" element={<Navigate to="/labs/projects" replace />} />
                  <Route path="/projects/:code" element={<LegacyLabsRedirect to="/labs/projects" parameter="code" />} />
                  <Route path="/issues" element={<Navigate to="/labs/issues" replace />} />
                  <Route path="/issues/:code" element={<LegacyLabsRedirect to="/labs/issues" parameter="code" />} />
                  <Route path="/automation" element={<Navigate to="/labs/automation" replace />} />
                  <Route path="/runtimes" element={<Navigate to="/labs/runtimes" replace />} />
                  <Route path="/runtimes/:slug" element={<LegacyLabsRedirect to="/labs/runtimes" parameter="slug" />} />
                  <Route path="/onboarding" element={<Navigate to="/labs/onboarding" replace />} />
                  <Route path="/config" element={<Navigate to="/operations/config/general" replace />} />
                  <Route path="/config/:section" element={<LegacyLabsRedirect to="/operations/config" parameter="section" />} />
                  <Route path="/settings" element={<Navigate to="/operations/access/org" replace />} />
                  <Route path="/settings/:section" element={<LegacyLabsRedirect to="/operations/access" parameter="section" />} />
                  <Route path="*" element={<Navigate to="/command-center" replace />} />
                </Route>
              </Routes>
            </BrowserRouter>
          </OperatorProvider>
        </DockProvider>
      </OrgProvider>
    </ToastProvider>
  );
}

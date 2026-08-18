import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { OrgProvider } from "./store/OrgContext";
import { DockProvider } from "./store/DockContext";
import { ToastProvider } from "./components/Toast";
import { AppShell } from "./components/AppShell";
import { Inbox } from "./screens/Inbox";
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
          <BrowserRouter basename="/app">
            <Routes>
              <Route path="/onboarding" element={<Onboarding />} />
              <Route element={<AppShell />}>
                <Route index element={<Navigate to="/inbox" replace />} />
                <Route path="/inbox" element={<Inbox />} />
                <Route path="/sessions" element={<Sessions />} />
                <Route path="/sessions/:id" element={<Sessions />} />
                <Route path="/personas" element={<Personas />} />
                <Route path="/personas/:slug" element={<Personas />} />
                <Route path="/pods" element={<Pods />} />
                <Route path="/pods/:slug" element={<Pods />} />
                <Route path="/projects" element={<Projects />} />
                <Route path="/projects/:code" element={<Projects />} />
                <Route path="/issues" element={<Issues />} />
                <Route path="/issues/:code" element={<Issues />} />
                <Route path="/automation" element={<Automation />} />
                <Route path="/runtimes" element={<Runtimes />} />
                <Route path="/runtimes/:slug" element={<Runtimes />} />
                <Route path="/config" element={<Navigate to="/config/providers" replace />} />
                <Route path="/config/:section" element={<Config />} />
                <Route path="/settings" element={<Navigate to="/settings/org" replace />} />
                <Route path="/settings/:section" element={<Settings />} />
                <Route path="*" element={<Navigate to="/inbox" replace />} />
              </Route>
            </Routes>
          </BrowserRouter>
        </DockProvider>
      </OrgProvider>
    </ToastProvider>
  );
}

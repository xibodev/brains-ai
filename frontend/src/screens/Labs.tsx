import { Navigate, NavLink, Outlet } from "react-router-dom";
import { OperatorPageHead, OperatorState } from "../components/OperatorPrimitives";
import { useOperator } from "../store/OperatorContext";

export function LabsGate() {
  const { catalog, loading, error } = useOperator();
  if (loading) return <OperatorState loading />;
  if (error || !catalog?.labs_enabled) return <Navigate to="/command-center" replace />;
  return <Outlet />;
}

export function LabsHome() {
  const links = [
    ["Sessions", "/labs/sessions"],
    ["Personas", "/labs/personas"],
    ["Pods", "/labs/pods"],
    ["Projects", "/labs/projects"],
    ["Issues", "/labs/issues"],
    ["Runtimes", "/labs/runtimes"],
    ["Automation", "/labs/automation"],
    ["Onboarding", "/labs/onboarding"],
  ];
  return <div className="operator-page"><OperatorPageHead eyebrow="Explicit product gate" title="Labs" lede="Unfinished execution-model screens are available because BRAINS_UI_LABS is enabled for this process. They are not part of the normal operator contract." /><div className="operator-labs-grid">{links.map(([label, to]) => <NavLink key={to} to={to}><strong>{label}</strong><span>Experimental screen</span></NavLink>)}</div></div>;
}

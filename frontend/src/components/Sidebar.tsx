import { NavLink } from "react-router-dom";
import { EyebrowLabel } from "./EyebrowLabel";
import { useDock } from "../store/DockContext";

interface Item {
  to: string;
  label: string;
  glyph: string;
  countKey?: "inbox";
}

const WORKSPACE: Item[] = [
  { to: "/inbox", label: "Inbox", glyph: "◍", countKey: "inbox" },
  { to: "/sessions", label: "Sessions", glyph: "▸" },
  { to: "/personas", label: "Personas", glyph: "▸" },
  { to: "/pods", label: "Pods", glyph: "▸" },
  { to: "/projects", label: "Projects", glyph: "▸" },
  { to: "/issues", label: "Issues", glyph: "▸" },
  { to: "/automation", label: "Automation", glyph: "▸" },
];
const CONFIGURE: Item[] = [
  { to: "/runtimes", label: "Runtimes", glyph: "▸" },
  { to: "/config/providers", label: "Config", glyph: "▸" },
  { to: "/settings/org", label: "Settings", glyph: "▸" },
  { to: "/onboarding", label: "Setup", glyph: "▸" },
];

// In /config/* the app sidebar narrows to icons so the providers rail can be
// promoted into the left column (DESIGN-SYNTHESIS lock). `mini` drives that.
export function Sidebar({ mini }: { mini?: boolean }) {
  const { inboxCount } = useDock();

  const renderItem = (it: Item) => (
    <NavLink
      key={it.to}
      to={it.to}
      className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
      title={it.label}
    >
      <span aria-hidden>{it.glyph}</span>
      <span className="label">{it.label}</span>
      {it.countKey === "inbox" && inboxCount > 0 && (
        <span className="count">{inboxCount}</span>
      )}
    </NavLink>
  );

  return (
    <nav className={`sidebar ${mini ? "mini" : ""}`}>
      <div className="nav-group">
        <EyebrowLabel>Workspace</EyebrowLabel>
        {WORKSPACE.map(renderItem)}
      </div>
      <div className="nav-group">
        <EyebrowLabel>Configure</EyebrowLabel>
        {CONFIGURE.map(renderItem)}
      </div>
    </nav>
  );
}

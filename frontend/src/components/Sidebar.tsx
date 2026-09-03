import { NavLink, useNavigate } from "react-router-dom";

interface Item {
  to: string;
  label: string;
  glyph: string;
}

const NAVIGATION: Item[] = [
  { to: "/command-center", label: "Command Center", glyph: "CC" },
  { to: "/workspaces", label: "Workspaces", glyph: "WS" },
  { to: "/coordination", label: "Coordination", glyph: "CO" },
  { to: "/governance", label: "Governance", glyph: "GV" },
  { to: "/operations", label: "Operations", glyph: "OP" },
];

export function Sidebar() {
  const navigate = useNavigate();

  const renderItem = (it: Item) => (
    <NavLink
      key={it.to}
      to={it.to}
      className={({ isActive }) => `control-nav-item ${isActive ? "active" : ""}`}
      title={it.label}
    >
      <span className="control-nav-glyph" aria-hidden>{it.glyph}</span>
      <span>{it.label}</span>
    </NavLink>
  );

  return (
    <>
      <aside className="control-sidebar">
        <div className="control-brand"><span className="control-brain-mark" />Brains</div>
        <div className="control-scope">
          <span>Viewing</span>
          <strong>All visible workspaces</strong>
          <small>Local coordination brain</small>
        </div>
        <div className="control-section-label">Operate</div>
        <nav className="control-nav">{NAVIGATION.map(renderItem)}</nav>
        <div className="control-sidebar-bottom">
          <button className="control-act-button" onClick={() => navigate("/act")}>
            <span>Act</span><kbd>Ctrl K</kbd>
          </button>
        </div>
      </aside>
      <nav className="control-mobile-nav">
        {NAVIGATION.map(renderItem)}
      </nav>
      <button className="control-mobile-act" onClick={() => navigate("/act")}>Act</button>
    </>
  );
}

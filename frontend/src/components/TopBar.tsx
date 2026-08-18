import { useNavigate } from "react-router-dom";
import { useOrg } from "../store/OrgContext";
import { useDock } from "../store/DockContext";
import { OrgSwitcher } from "./OrgSwitcher";

export function TopBar({ onTheme }: { onTheme: () => void }) {
  const navigate = useNavigate();
  const { activeOrg } = useOrg();
  const { inboxCount } = useDock();

  return (
    <header className="topbar">
      <div className="mark">◇ brains</div>
      <OrgSwitcher />
      <button
        className="kbar"
        onClick={() => {
          // dispatch a synthetic ⌘K
          window.dispatchEvent(
            new KeyboardEvent("keydown", { key: "k", metaKey: true }),
          );
        }}
      >
        ⌘K  search…
      </button>
      <div className="spacer" />
      <button
        className="bell"
        aria-label="Approvals and asks"
        title="Inbox"
        onClick={() => navigate("/inbox")}
      >
        🔔
        {inboxCount > 0 && <span className="badge">{inboxCount}</span>}
      </button>
      <button className="icon-btn" onClick={onTheme} aria-label="Toggle theme" title="Theme">
        ◐
      </button>
      <div className="dropdown">
        <button title="Operator">
          ( {activeOrg ? "Operator" : "—"} ▾ )
        </button>
      </div>
    </header>
  );
}

import { useNavigate } from "react-router-dom";
import { useOperator } from "../store/OperatorContext";
import type { ConnState } from "../realtime/client";

const CONNECTION_LABEL: Record<ConnState, string> = {
  connecting: "Brain connecting",
  open: "Brain connected",
  closed: "Brain reconnecting",
  denied: "Access changed",
};

export function TopBar({ connection }: { connection: ConnState }) {
  const navigate = useNavigate();
  const { catalog } = useOperator();

  return (
    <header className="control-topbar">
      <div className="control-mobile-brand"><span className="control-brain-mark" />Brains</div>
      <div className={`control-live-state ${connection}`}><span />{CONNECTION_LABEL[connection]}</div>
      <button
        className="control-search"
        onClick={() => {
          window.dispatchEvent(
            new KeyboardEvent("keydown", { key: "k", ctrlKey: true }),
          );
        }}
      >
        Search workspaces and actions <kbd>Ctrl K</kbd>
      </button>
      <div className="control-spacer" />
      <button
        className="control-icon-button"
        aria-label="Open governance queue"
        title="Governance"
        onClick={() => navigate("/governance")}
      >
        !
      </button>
      <div className="control-operator">
        <div className="control-avatar">O</div>
        <span><strong>Operator</strong><small>{catalog?.install_admin ? "Install admin" : "Workspace member"}</small></span>
      </div>
    </header>
  );
}

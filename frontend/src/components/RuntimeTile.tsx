import type { Runtime } from "../api/types";
import { StatusPill } from "./StatusPill";
import { isStaleHeartbeat, relativeTime } from "./format";

// Live machine×tool tile; desaturates when status=offline OR the heartbeat has
// gone stale before GC flips it (WS4 §3.7 effective-online rule).
export function RuntimeTile({
  rt,
  onClick,
  selected,
}: {
  rt: Runtime;
  onClick?: () => void;
  selected?: boolean;
}) {
  const stale = isStaleHeartbeat(rt.last_heartbeat_at);
  const offline = rt.status === "offline";
  const desaturated = offline || (rt.status === "online" && stale);
  const cls = ["runtime-tile"];
  if (desaturated) cls.push("stale");
  if (selected) cls.push("selected");

  const statusTone =
    rt.status === "online" && !stale
      ? "positive"
      : rt.status === "draining"
        ? "warning"
        : "danger";

  return (
    <div
      className={cls.join(" ")}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      style={selected ? { outline: "2px solid var(--accent)", outlineOffset: 2 } : undefined}
    >
      <div className="rt-head">
        <span className={`dot ${statusTone}`} />
        <span className="rt-machine">{rt.machine_label ?? rt.machine_id ?? rt.slug}</span>
      </div>
      <div className="row spread" style={{ marginBottom: 6 }}>
        <span className="meta">{rt.tool ?? "—"}</span>
        <StatusPill label={rt.status ?? "unknown"} />
      </div>
      <div className="row spread">
        <span className="meta">
          {rt.active_sessions ? `${rt.active_sessions} running` : "idle"}
        </span>
        <span className="meta">
          {offline ? `last ${relativeTime(rt.last_heartbeat_at)}` : `♥ ${relativeTime(rt.last_heartbeat_at)}`}
        </span>
      </div>
      {rt.os && <div className="meta" style={{ marginTop: 4 }}>{rt.os}</div>}
    </div>
  );
}

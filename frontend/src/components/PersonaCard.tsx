import type { Persona } from "../api/types";

// Live/idle accent dot — turns the per-persona accent on when the persona has a
// running session (the colour scopes to the dot only, never chrome — WS4 §6).
export function PersonaDot({
  color,
  live,
}: {
  color?: string | null;
  live?: boolean;
}) {
  return (
    <span
      className={`dot ${live ? "live" : ""}`}
      style={color ? ({ ["--persona-accent" as string]: color } as React.CSSProperties) : undefined}
    />
  );
}

export function PersonaCard({
  persona,
  live,
  cascade,
  onClick,
  action,
}: {
  persona: Persona;
  live?: boolean;
  cascade?: boolean;
  onClick?: () => void;
  action?: React.ReactNode;
}) {
  const cls = ["softcard", "interactive"];
  if (cascade) cls.push("cascade");
  return (
    <div
      data-testid="persona-card"
      className={cls.join(" ")}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      style={
        persona.color
          ? ({ ["--persona-accent" as string]: persona.color } as React.CSSProperties)
          : undefined
      }
    >
      <div className="row spread">
        <div className="row">
          <PersonaDot color={persona.color} live={live} />
          <strong>{persona.name}</strong>
        </div>
        {action}
      </div>
      <div className="meta" style={{ marginTop: 6 }}>
        {[persona.tool, persona.model].filter(Boolean).join(" · ") || "no brain set"}
      </div>
      {persona.description && (
        <div className="meta" style={{ marginTop: 6, color: "var(--text-faint)" }}>
          {persona.description}
        </div>
      )}
    </div>
  );
}

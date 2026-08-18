type Tone = "neutral" | "positive" | "warning" | "danger" | "accent";

const STATUS_TONE: Record<string, Tone> = {
  online: "positive",
  healthy: "positive",
  done: "positive",
  active: "positive",
  resolved: "positive",
  in_review: "accent",
  in_progress: "accent",
  spawning: "accent",
  running: "accent",
  open: "neutral",
  draining: "warning",
  degraded: "warning",
  blocked: "warning",
  paused: "warning",
  pending_approval: "warning",
  offline: "danger",
  unhealthy: "danger",
  cancelled: "danger",
  rejected: "danger",
  p0: "danger",
  p1: "warning",
  p2: "neutral",
  p3: "neutral",
};

export function toneFor(value?: string): Tone {
  if (!value) return "neutral";
  return STATUS_TONE[value.toLowerCase()] ?? "neutral";
}

// Status/priority/health chip. `dot` renders a leading status dot.
export function StatusPill({
  label,
  tone,
  dot,
}: {
  label: string;
  tone?: Tone;
  dot?: boolean;
}) {
  const t = tone ?? toneFor(label);
  return (
    <span className={`pill ${t}`}>
      {dot && <span className={`dot ${t}`} />}
      {label}
    </span>
  );
}

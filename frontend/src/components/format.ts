// Small formatting helpers shared across screens.

// Parse an ISO timestamp to epoch ms, treating a timezone-LESS string as UTC.
// The backend emits naive-UTC timestamps for some rows (e.g. runtime
// last_heartbeat_at) and tz-aware `+00:00` for others; without this, a naive
// string is parsed as browser-local time and lands hours in the past/future
// (e.g. a fresh heartbeat shows "in 6h").
function parseIsoUtc(iso: string): number {
  const hasTz = /([zZ])|([+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(hasTz ? iso : `${iso}Z`).getTime();
}

export function relativeTime(iso?: string | null): string {
  if (!iso) return "—";
  const then = parseIsoUtc(iso);
  if (Number.isNaN(then)) return "—";
  const diff = Date.now() - then;
  const s = Math.round(diff / 1000);
  if (s < 0) {
    // future (e.g. expires_at) — show countdown
    const a = Math.abs(s);
    if (a < 60) return `in ${a}s`;
    if (a < 3600) return `in ${Math.round(a / 60)}m`;
    return `in ${Math.round(a / 3600)}h`;
  }
  if (s < 10) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

// Effective-online: status=online AND heartbeat fresh within ttl (WS4 §3.7).
export function isStaleHeartbeat(iso?: string | null, ttlMs = 30000): boolean {
  if (!iso) return true;
  const then = parseIsoUtc(iso);
  if (Number.isNaN(then)) return true;
  return Date.now() - then > ttlMs;
}

export function truncate(s: string | null | undefined, n = 120): string {
  if (!s) return "";
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

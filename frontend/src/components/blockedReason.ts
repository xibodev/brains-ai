// One place that turns a server ``blocked_reason`` into operator-facing copy.
//
// The server owns the reason vocabulary (F4 dispatch and F5 Pod routing both
// emit stable codes); the console only renders it. An unknown code is shown
// verbatim rather than swallowed, so a new server reason surfaces as text
// instead of disappearing behind "something went wrong".

const BLOCKED_COPY: Record<string, string> = {
  // F4 — Issue dispatch
  issue_closed: "This issue is closed, so it takes no new work.",
  unassigned: "Assign this issue to a persona or a pod before dispatching.",
  assigned_to_operator:
    "This issue is assigned to a person. Assign it to a persona or a pod to dispatch it.",
  persona_unknown: "The assigned persona no longer exists.",
  dispatch_in_flight: "A session for this issue is already running.",
  // F4 + F5 — persona/runtime compatibility
  persona_archived: "The persona is archived.",
  persona_no_runtime: "The persona has no runtime bound.",
  runtime_unknown: "The bound runtime no longer exists.",
  runtime_other_org: "The bound runtime belongs to another org.",
  runtime_offline: "The bound runtime is not online.",
  runtime_tool_mismatch: "The persona's tool does not match its runtime.",
  // F5 — Pod routing
  pod_archived: "This pod is archived and takes no work.",
  pod_empty: "This pod has no persona members yet.",
  pod_no_leader: "This pod has no leader persona yet.",
  pod_no_capable_member:
    "No member persona is bound to an online runtime it can drive.",
  // F6 — onboarding
  runtime_deferred: "Machine setup was deferred, so nothing can execute the first issue yet.",
  runtime_unavailable: "The persona's runtime is not online, so the first issue cannot start.",
  dispatch_refused: "Dispatch was refused — the issue detail names the reason.",
  session_missing: "No session exists for the first issue yet.",
};

export function blockedCopy(reason?: string | null): string | null {
  if (!reason) return null;
  return BLOCKED_COPY[reason] ?? reason;
}

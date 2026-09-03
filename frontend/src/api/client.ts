// Typed fetch client over the WS3 /v1/* surface.
//
// Auth: the SPA relies on the signed `brains_admin_key` cookie (browser
// auth) sent automatically with `credentials: 'include'`. A Bearer token
// fallback is supported for embedded/script contexts via setApiToken().
//
// Lists may come back either bare (tool-parity endpoints) or wrapped in a
// `{data, next_cursor}` envelope (new collection endpoints, WS3 §6). The
// `unwrap` helper normalises both to a plain array.

import type {
  Approval,
  Handoff,
  UsageSummary,
  ReadinessReport,
  QueueHealthReport,
  QueueHealthRepairResult,
  RecoveryPolicyReport,
  CoordinationOverview,
  OperatorCapabilityCatalog,
  OperatorCoordination,
  OperatorGovernance,
  OperatorKnowledge,
  OperatorOperations,
  OperatorOverview,
  OperatorTask,
  OperatorTransitionResult,
  OperatorWorkspace,
  OperatorWorkspaceDetail,
  MailboxAccess,
  MailboxAddress,
  MailboxMessageList,
  MailMessage,
  MailThread,
} from "./types";

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

// Format an ApiError (or any thrown value) into a specific, actionable string.
// Usage: toast(formatApiError("Spawn", e))  =>  "Spawn failed (404): Not Found"
export function formatApiError(action: string, e: unknown): string {
  if (e instanceof ApiError) return `${action} failed (${e.status}): ${e.message}`;
  if (e instanceof Error) return `${action} failed: ${e.message}`;
  return `${action} failed`;
}

let bearerToken: string | null = null;
export function setApiToken(token: string | null): void {
  bearerToken = token;
}

function unwrap<T>(body: unknown): T[] {
  if (Array.isArray(body)) return body as T[];
  if (body && typeof body === "object" && Array.isArray((body as { data?: unknown }).data)) {
    return (body as { data: T[] }).data;
  }
  return [];
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (bearerToken) headers.set("Authorization", `Bearer ${bearerToken}`);

  const res = await fetch(`/v1${path}`, {
    ...options,
    headers,
    credentials: "include",
  });

  if (res.status === 204) return undefined as T;

  let body: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!res.ok) {
    const errObj =
      body && typeof body === "object"
        ? (body as { error?: { message?: string; code?: string } }).error
        : undefined;
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String((body as { detail?: unknown }).detail ?? "")
        : undefined;
    throw new ApiError(
      errObj?.message ?? detail ?? res.statusText ?? "Request failed",
      res.status,
      errObj?.code,
    );
  }
  return body as T;
}

function qs(params?: Record<string, string | number | undefined | null>): string {
  if (!params) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  request,

  // --- approvals + asks (shared store) ---
  listApprovals: (params?: Record<string, string | undefined>) =>
    request<unknown>(`/approvals${qs(params)}`).then((b) => unwrap<Approval>(b)),
  getApproval: (code: string) => request<Approval>(`/approvals/${code}`),
  resolveApproval: (code: string, chosen: string, reasoning?: string, status = "resolved") =>
    request<unknown>(`/approvals/${code}/resolve`, {
      method: "POST",
      body: JSON.stringify({ chosen, reasoning, status }),
    }),
  answerAsk: (code: string, answer: string) =>
    request<unknown>(`/asks/${code}/answer`, {
      method: "POST",
      body: JSON.stringify({ answer }),
    }),

  // --- handoffs (read; surfaced in inbox) ---
  listHandoffs: (params?: Record<string, string | undefined>) =>
    request<unknown>(`/handoffs${qs(params)}`).then((b) => unwrap<Handoff>(b)),

  // --- usage ---
  usageSummary: (days = 30) =>
    request<UsageSummary>(`/usage${qs({ days })}`),
  orgUsageSummary: (org: string | number, days = 30) =>
    request<UsageSummary>(`/orgs/${org}/usage${qs({ days })}`),


  // --- operational health (B8; bootstrap-admin only) ---
  readiness: () => request<ReadinessReport>("/admin/readiness"),
  queueHealth: () => request<QueueHealthReport>("/admin/queue-health"),
  repairQueueHealth: (apply: boolean) =>
    request<QueueHealthRepairResult>("/admin/queue-health/repair", {
      method: "POST",
      body: JSON.stringify({ apply }),
    }),
  recoveryPolicy: () => request<RecoveryPolicyReport>("/admin/recovery-policy"),
  coordinationOverview: () =>
    request<CoordinationOverview>("/admin/coordination/overview"),

  // --- workspace-first operator console ---
  operatorOverview: () => request<OperatorOverview>("/operator/overview"),
  operatorWorkspaces: () =>
    request<{ data: OperatorWorkspace[] }>("/operator/workspaces").then((body) => body.data),
  operatorWorkspace: (slug: string) =>
    request<OperatorWorkspaceDetail>(`/operator/workspaces/${encodeURIComponent(slug)}`),
  operatorCoordination: () => request<OperatorCoordination>("/operator/coordination"),
  operatorMailboxAccess: () =>
    request<{ data: MailboxAccess[] }>("/operator/mailboxes/access").then((body) => body.data),
  operatorMailboxPhonebook: (workspace: string) =>
    request<{ data: MailboxAddress[] }>(
      `/operator/mailboxes${qs({ workspace })}`,
    ).then((body) => body.data),
  operatorMailboxInbox: (address: string, includeRead = false) =>
    request<MailboxMessageList>(
      `/operator/mailboxes/inbox${qs({ address, include_read: includeRead ? "true" : undefined })}`,
    ),
  operatorMailboxSent: (address: string) =>
    request<MailboxMessageList>(`/operator/mailboxes/sent${qs({ address })}`),
  operatorMailboxThread: (threadId: string, address: string) =>
    request<MailThread>(
      `/operator/mailboxes/threads/${encodeURIComponent(threadId)}${qs({ address })}`,
    ),
  operatorMailboxReadInbox: (address: string) =>
    request<MailboxMessageList>("/operator/mailboxes/inbox/read", {
      method: "POST",
      body: JSON.stringify({ address }),
    }),
  operatorMailboxReadThread: (threadId: string, address: string) =>
    request<MailThread>(`/operator/mailboxes/threads/${encodeURIComponent(threadId)}/read`, {
      method: "POST",
      body: JSON.stringify({ address }),
    }),
  operatorMailboxSend: (
    workspace: string,
    body: { recipients: string[]; subject: string; body: string; operation_id: string },
  ) =>
    request<MailMessage>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/mailboxes/messages`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  operatorMailboxReply: (
    workspace: string,
    messageId: string,
    body: { subject?: string; body: string; operation_id: string },
  ) =>
    request<MailMessage>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/mailboxes/messages/${encodeURIComponent(messageId)}/reply`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  operatorMailboxForward: (
    workspace: string,
    messageId: string,
    body: { recipients: string[]; subject?: string; body: string; operation_id: string },
  ) =>
    request<MailMessage>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/mailboxes/messages/${encodeURIComponent(messageId)}/forward`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  operatorGovernance: () => request<OperatorGovernance>("/operator/governance"),
  operatorOperations: () => request<OperatorOperations>("/operator/operations"),
  operatorCapabilities: () =>
    request<OperatorCapabilityCatalog>("/operator/capabilities"),
  operatorCreateTask: (
    workspace: string,
    body: { title: string; body?: string; priority?: string; depends_on?: string; tags?: string },
  ) =>
    request<OperatorTask>(`/operator/workspaces/${encodeURIComponent(workspace)}/tasks`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  operatorClaimTask: (code: string, sessionId: string) =>
    request<OperatorTask>(`/operator/tasks/${encodeURIComponent(code)}/claim`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    }),
  operatorCompleteTask: (code: string, sessionId: string, summary: string) =>
    request<OperatorTask>(`/operator/tasks/${encodeURIComponent(code)}/complete`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, summary }),
    }),
  operatorReleaseTask: (code: string, sessionId: string, reason: string) =>
    request<OperatorTransitionResult>(`/operator/tasks/${encodeURIComponent(code)}/release`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, reason }),
    }),
  operatorClaimWorkspace: (
    workspace: string,
    body: { session_id: string; scope?: string; duration_minutes?: number },
  ) =>
    request<Record<string, unknown>>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/claims`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  operatorReleaseWorkspace: (workspace: string, sessionId: string) =>
    request<Record<string, unknown>>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/claims/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    ),
  operatorSetHandoff: (
    workspace: string,
    body: { title: string; body?: string; session_id?: string },
  ) =>
    request<Record<string, unknown>>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/handoffs`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  operatorPickHandoff: (workspace: string, sessionId?: string) =>
    request<Record<string, unknown>>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/handoffs/pick`,
      { method: "POST", body: JSON.stringify({ session_id: sessionId || null }) },
    ),
  operatorClearHandoff: (workspace: string, sessionId: string | undefined, reason: string) =>
    request<Record<string, unknown>>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/handoffs`,
      {
        method: "DELETE",
        body: JSON.stringify({ session_id: sessionId || null, reason }),
      },
    ),
  operatorSendMessage: (
    workspace: string,
    body: { subject: string; body?: string; kind?: string },
  ) =>
    request<Record<string, unknown>>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/messages`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  operatorPostTopic: (body: {
    workspace: string;
    topic: string;
    subject: string;
    body?: string;
    blast?: boolean;
  }) =>
    request<Record<string, unknown>>("/operator/topics", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  operatorAddKnowledge: (
    workspace: string,
    body: { type: string; title: string; body?: string; scope?: string },
  ) =>
    request<OperatorKnowledge>(
      `/operator/workspaces/${encodeURIComponent(workspace)}/knowledge`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  operatorResolveKnowledge: (code: string, status = "resolved") =>
    request<OperatorTransitionResult>(`/operator/knowledge/${encodeURIComponent(code)}/resolve`, {
      method: "POST",
      body: JSON.stringify({ status }),
    }),
  operatorDecidePattern: (name: string, approved: boolean) =>
    request<Record<string, unknown>>(`/operator/patterns/${encodeURIComponent(name)}/decision`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    }),
  operatorVerifyTool: (name: string) =>
    request<Record<string, unknown>>(`/operator/tools/${encodeURIComponent(name)}/verify`, {
      method: "POST",
    }),
  operatorAuditVerify: () => request<Record<string, unknown>>("/operator/audit/verify"),

};

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
  Autopilot,
  EnrolResponse,
  Handoff,
  Issue,
  IssueComment,
  IssueDispatchPlan,
  IssueDispatchResult,
  IssueEvidence,
  OnboardingState,
  OnboardingAttempt,
  OnboardingStepBody,
  Org,
  OrgMember,
  Persona,
  Pod,
  PodDispatchPlan,
  Project,
  Runtime,
  Session,
  SessionCommand,
  SessionEvent,
  Skill,
  SkillAttachment,
  UsageSummary,
  Workspace,
  ConfigSummary,
  ReadinessReport,
  QueueHealthReport,
  QueueHealthRepairResult,
  RecoveryPolicyReport,
  EmailConfiguration,
  CoordinationOverview,
  GeneralConfiguration,
  SecretConfiguration,
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

  // --- orgs ---
  listOrgs: () => request<unknown>("/orgs").then((b) => unwrap<Org>(b)),
  getOrg: (org: string | number) => request<Org>(`/orgs/${org}`),
  createOrg: (body: { slug: string; name: string; description?: string }) =>
    request<Org>("/orgs", { method: "POST", body: JSON.stringify(body) }),
  patchOrg: (org: string | number, body: Partial<Org>) =>
    request<Org>(`/orgs/${org}`, { method: "PATCH", body: JSON.stringify(body) }),
  listOrgMembers: (org: string | number) =>
    request<unknown>(`/orgs/${org}/members`).then((b) => unwrap<OrgMember>(b)),
  addOrgMember: (org: string | number, body: { operator_id: string; role: string }) =>
    request<OrgMember>(`/orgs/${org}/members`, { method: "POST", body: JSON.stringify(body) }),
  removeOrgMember: (org: string | number, operator: string) =>
    request<void>(`/orgs/${org}/members/${operator}`, { method: "DELETE" }),
  onboard: (body: unknown) =>
    request<unknown>("/orgs/onboard", { method: "POST", body: JSON.stringify(body) }),

  // --- runtimes ---
  // The runtimes endpoint returns a bare `{runtimes: [...]}` envelope (tool
  // parity with the daemon wire protocol), not the `{data}` collection
  // envelope — handle it explicitly before falling back to unwrap.
  listRuntimes: (params?: Record<string, string | undefined>) =>
    request<unknown>(`/runtimes${qs(params)}`).then((b) => {
      if (b && typeof b === "object" && Array.isArray((b as { runtimes?: unknown }).runtimes)) {
        return (b as { runtimes: Runtime[] }).runtimes;
      }
      return unwrap<Runtime>(b);
    }),
  getRuntime: (rt: string | number) => request<Runtime>(`/runtimes/${rt}`),
  patchRuntime: (rt: string | number, body: Partial<Runtime>) =>
    request<Runtime>(`/runtimes/${rt}`, { method: "PATCH", body: JSON.stringify(body) }),
  enrolRuntime: (body: { label?: string; org_id?: number }) =>
    request<EnrolResponse>("/runtimes/enrol", { method: "POST", body: JSON.stringify(body) }),

  // --- personas ---
  listPersonas: (org: string | number, params?: Record<string, string | undefined>) =>
    request<unknown>(`/orgs/${org}/personas${qs(params)}`).then((b) => unwrap<Persona>(b)),
  getPersona: (p: string | number) => request<Persona>(`/personas/${p}`),
  createPersona: (org: string | number, body: Partial<Persona>) =>
    request<Persona>(`/orgs/${org}/personas`, { method: "POST", body: JSON.stringify(body) }),
  patchPersona: (p: string | number, body: Partial<Persona>) =>
    request<Persona>(`/personas/${p}`, { method: "PATCH", body: JSON.stringify(body) }),
  archivePersona: (p: string | number) =>
    request<void>(`/personas/${p}`, { method: "DELETE" }),
  personaSessions: (p: string | number) =>
    request<unknown>(`/personas/${p}/sessions`).then((b) => unwrap<Session>(b)),
  spawnPersona: (p: string | number, body: unknown) =>
    request<unknown>(`/personas/${p}/spawn`, { method: "POST", body: JSON.stringify(body) }),
  listPersonaSkills: (p: string | number) =>
    request<unknown>(`/personas/${p}/skills`).then((b) => unwrap<SkillAttachment>(b)),
  attachPersonaSkill: (p: string | number, skillId: string | number) =>
    request<SkillAttachment>(`/personas/${p}/skills`, {
      method: "POST",
      body: JSON.stringify({ skill_id: skillId }),
    }),
  detachPersonaSkill: (p: string | number, skillId: string | number) =>
    request<void>(`/personas/${p}/skills/${skillId}`, { method: "DELETE" }),

  // --- pods (teams of Personas backed by a compatibility identity) ---
  listPods: (org: string | number, params?: Record<string, string | undefined>) =>
    request<unknown>(`/orgs/${org}/pods${qs(params)}`).then((b) => unwrap<Pod>(b)),
  getPod: (pod: string | number) => request<Pod>(`/pods/${pod}`),
  createPod: (
    org: string | number,
    body: { slug: string; name: string; leader_persona_id?: string; description?: string },
  ) => request<Pod>(`/orgs/${org}/pods`, { method: "POST", body: JSON.stringify(body) }),
  addPodMember: (podId: string | number, body: { persona_id: string; role?: string }) =>
    request<Pod>(`/pods/${podId}/members`, { method: "POST", body: JSON.stringify(body) }),
  removePodMember: (podId: string | number, persona: string | number) =>
    request<Pod>(`/pods/${podId}/members/${persona}`, { method: "DELETE" }),
  setPodLeader: (podId: string | number, body: { leader_persona_id: string }) =>
    request<Pod>(`/pods/${podId}`, { method: "PATCH", body: JSON.stringify(body) }),
  archivePod: (podId: string | number) =>
    request<Pod>(`/pods/${podId}`, { method: "PATCH", body: JSON.stringify({ status: "archived" }) }),
  podDispatchPlan: (podId: string | number) =>
    request<PodDispatchPlan>(`/pods/${podId}/dispatch-plan`),

  // --- projects ---
  listProjects: (org: string | number, params?: Record<string, string | undefined>) =>
    request<unknown>(`/orgs/${org}/projects${qs(params)}`).then((b) => unwrap<Project>(b)),
  listWorkspaces: (org: string | number) =>
    request<unknown>(`/orgs/${org}/workspaces`).then((b) => unwrap<Workspace>(b)),
  getProject: (proj: string | number) => request<Project>(`/projects/${proj}`),
  createProject: (org: string | number, body: Partial<Project>) =>
    request<Project>(`/orgs/${org}/projects`, { method: "POST", body: JSON.stringify(body) }),
  patchProject: (proj: string | number, body: Partial<Project>) =>
    request<Project>(`/projects/${proj}`, { method: "PATCH", body: JSON.stringify(body) }),
  projectIssues: (proj: string | number, params?: Record<string, string | undefined>) =>
    request<unknown>(`/projects/${proj}/issues${qs(params)}`).then((b) => unwrap<Issue>(b)),
  listProjectSkills: (proj: string | number) =>
    request<unknown>(`/projects/${proj}/skills`).then((b) => unwrap<SkillAttachment>(b)),
  attachProjectSkill: (proj: string | number, skillId: string | number) =>
    request<SkillAttachment>(`/projects/${proj}/skills`, {
      method: "POST",
      body: JSON.stringify({ skill_id: skillId }),
    }),
  detachProjectSkill: (proj: string | number, skillId: string | number) =>
    request<void>(`/projects/${proj}/skills/${skillId}`, { method: "DELETE" }),

  // --- issues ---
  listIssues: (params?: Record<string, string | undefined>) =>
    request<unknown>(`/issues${qs(params)}`).then((b) => unwrap<Issue>(b)),
  getIssue: (issue: string | number) => request<Issue>(`/issues/${issue}`),
  createIssue: (proj: string | number, body: Partial<Issue>) =>
    request<Issue>(`/projects/${proj}/issues`, { method: "POST", body: JSON.stringify(body) }),
  patchIssue: (issue: string | number, body: Partial<Issue>) =>
    request<Issue>(`/issues/${issue}`, { method: "PATCH", body: JSON.stringify(body) }),
  transitionIssue: (issue: string | number, status: string, reason?: string) =>
    request<Issue>(`/issues/${issue}/transition`, {
      method: "POST",
      body: JSON.stringify({ status, reason }),
    }),
  assignIssue: (issue: string | number, body: unknown) =>
    request<Issue>(`/issues/${issue}/assign`, { method: "POST", body: JSON.stringify(body) }),
  dispatchIssue: (issue: string | number) =>
    request<IssueDispatchResult>(`/issues/${issue}/dispatch`, { method: "POST" }),
  issueDispatchPlan: (issue: string | number) =>
    request<IssueDispatchPlan>(`/issues/${issue}/dispatch-plan`),
  issueEvidence: (issue: string | number) =>
    request<IssueEvidence>(`/issues/${issue}/evidence`),
  issueSessions: (issue: string | number) =>
    request<unknown>(`/issues/${issue}/sessions`).then((b) => unwrap<Session>(b)),
  listIssueComments: (issue: string | number) =>
    request<unknown>(`/issues/${issue}/comments`).then((b) => unwrap<IssueComment>(b)),
  addIssueComment: (issue: string | number, body: unknown) =>
    request<unknown>(`/issues/${issue}/comments`, { method: "POST", body: JSON.stringify(body) }),

  // --- sessions ---
  listSessions: (params?: Record<string, string | undefined>) =>
    request<unknown>(`/sessions${qs(params)}`).then((b) => unwrap<Session>(b)),
  getSession: (id: string | number) => request<Session>(`/sessions/${id}`),
  sessionEvents: (id: string | number, params?: Record<string, string | undefined>) =>
    request<unknown>(`/sessions/${id}/events${qs(params)}`).then((b) => unwrap<SessionEvent>(b)),
  spawnSession: (body: unknown) =>
    request<unknown>("/sessions/spawn", { method: "POST", body: JSON.stringify(body) }),
  messageSession: (id: string | number, text: string, operationId: string) =>
    request<SessionCommand>(`/sessions/${id}/message`, {
      method: "POST",
      // `operation_id` is the idempotency handle: the same submit replayed
      // after a timeout returns the original command instead of queueing a
      // second prompt (BL-P0-05).
      body: JSON.stringify({ text, operation_id: operationId }),
    }),
  stopSession: (id: string | number, operationId?: string) =>
    request<SessionCommand>(`/sessions/${id}/stop`, {
      method: "POST",
      body: JSON.stringify({ operation_id: operationId }),
    }),
  sessionCommands: (id: string | number, params?: Record<string, string | undefined>) =>
    request<unknown>(`/sessions/${id}/commands${qs(params)}`).then((b) =>
      unwrap<SessionCommand>(b),
    ),

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

  // --- autopilots (recurring agent tasks) ---
  listAutopilots: (org: string | number) =>
    request<unknown>(`/orgs/${org}/autopilots`).then((b) => unwrap<Autopilot>(b)),
  createAutopilot: (
    org: string | number,
    body: { name: string; title_template: string; cron_expr?: string; spawn_tool?: string; spawn_prompt?: string },
  ) => request<Autopilot>(`/orgs/${org}/autopilots`, { method: "POST", body: JSON.stringify(body) }),
  setAutopilotEnabled: (name: string, enabled: boolean) =>
    request<Autopilot>(`/autopilots/${name}/enabled`, { method: "POST", body: JSON.stringify({ enabled }) }),
  fireAutopilot: (name: string) =>
    request<unknown>(`/autopilots/${name}/fire`, { method: "POST" }),

  // --- skills (SKILL.md packs) ---
  listSkills: (org: string | number) =>
    request<unknown>(`/orgs/${org}/skills`).then((b) => unwrap<Skill>(b)),
  createSkill: (
    org: string | number,
    body: { slug: string; name: string; content?: string },
  ) => request<Skill>(`/orgs/${org}/skills`, { method: "POST", body: JSON.stringify(body) }),

  // --- config (F7) ---
  configSummary: () => request<ConfigSummary>("/config/summary"),
  testProvider: (name: string) =>
    request<{ ok: boolean; detail?: string }>(`/config/providers/${name}/test`, { method: "POST" }),

  // --- operational health (B8, BL-P1-09, BL-P1-12; bootstrap-admin only) ---
  readiness: () => request<ReadinessReport>("/admin/readiness"),
  queueHealth: () => request<QueueHealthReport>("/admin/queue-health"),
  repairQueueHealth: (apply: boolean) =>
    request<QueueHealthRepairResult>("/admin/queue-health/repair", {
      method: "POST",
      body: JSON.stringify({ apply }),
    }),
  recoveryPolicy: () => request<RecoveryPolicyReport>("/admin/recovery-policy"),
  emailConfiguration: () => request<EmailConfiguration>("/admin/configuration/email"),
  generalConfiguration: () =>
    request<GeneralConfiguration>("/admin/configuration/general"),
  setGeneralConfiguration: (updates: Record<string, unknown>) =>
    request<GeneralConfiguration>("/admin/configuration/general", {
      method: "PUT",
      body: JSON.stringify({ updates }),
    }),
  secretConfiguration: () =>
    request<SecretConfiguration>("/admin/configuration/secrets"),
  setSecretConfiguration: (name: string, value: string) =>
    request<{ name: string; set: boolean }>(`/admin/configuration/secrets/${name}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  clearSecretConfiguration: (name: string) =>
    request<{ name: string; set: boolean; removed: boolean }>(
      `/admin/configuration/secrets/${name}`,
      { method: "DELETE" },
    ),
  setEmailConfiguration: (name: string, value: string) =>
    request<{ name: string; set: boolean }>(`/admin/configuration/email/${name}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  clearEmailConfiguration: (name: string) =>
    request<{ name: string; set: boolean; removed: boolean }>(
      `/admin/configuration/email/${name}`,
      { method: "DELETE" },
    ),
  testEmailConfiguration: (to: string) =>
    request<{ sent: boolean; to: string; subject: string }>(
      "/admin/configuration/email/test",
      { method: "POST", body: JSON.stringify({ to }) },
    ),
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

  // --- onboarding (F6) ---
  // The attempt is server state: the guard, the resume after a reload, and the
  // blocked outcome all read the same record rather than browser memory.
  onboardingState: () => request<OnboardingState>("/onboarding/state"),
  startOnboarding: () => request<OnboardingState>("/onboarding/attempts", { method: "POST" }),
  recordOnboardingStep: (attemptId: string, step: string, body: OnboardingStepBody) =>
    request<OnboardingAttempt>(`/onboarding/attempts/${attemptId}/steps/${step}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  abandonOnboarding: (attemptId: string) =>
    request<OnboardingAttempt>(`/onboarding/attempts/${attemptId}/abandon`, { method: "POST" }),
};

// Entity types mirror the current Brains REST API and remain permissive so the
// SPA can degrade gracefully when older rows omit optional fields.

export interface Autopilot {
  name: string;
  title_template: string;
  cron_expr?: string | null;
  enabled: boolean;
  spawn_tool?: string | null;
  spawn_prompt?: string | null;
  org_slug?: string;
  [key: string]: unknown;
}

export interface Skill {
  id: number | string;
  slug: string;
  name: string;
  content?: string | null;
  org_id?: number | string | null;
  org_slug?: string;
}

export interface EmailConfiguration {
  mailer: {
    enabled: boolean;
    smtp_host?: string | null;
    smtp_port: number;
    smtp_timeout_seconds: number;
    starttls: boolean;
    from?: string | null;
    has_credentials: boolean;
    operator_notify_email?: string | null;
  };
  secure: {
    encrypted_store: string;
    settings: Record<string, { set: boolean; secret: boolean; source?: string }>;
  };
}

export interface GeneralConfiguration {
  live: Record<string, unknown>;
  overlay: Record<string, unknown>;
  overlay_path: string;
}

export interface SecretConfiguration {
  encrypted_store: string;
  settings: Record<string, { set: boolean; secret: boolean; source?: string }>;
}

export interface CoordinationOverview {
  live_agents: Array<Record<string, unknown>>;
  workspaces: Workspace[];
  claims: Array<Record<string, unknown>>;
  tasks: Array<Record<string, unknown>>;
  handoffs: Handoff[];
  topics: Array<Record<string, unknown>>;
  patterns: Array<Record<string, unknown>>;
  knowledge: Array<Record<string, unknown>>;
  service: Record<string, unknown>;
}

// BL-P1-08 (F10) — a Skill attached to a Persona or Project, with provenance.
export interface SkillAttachment {
  id: number | string;
  skill_id: number | string;
  slug: string;
  name: string;
  source: "persona" | "project";
  entity_id?: number | string;
  position?: number;
  attached_by_operator_id?: number | string | null;
  attached_at?: string | null;
}


export interface Org {
  id: number | string;
  slug: string;
  name: string;
  description?: string | null;
  status?: string;
  created_at?: string;
}

export interface OrgMember {
  operator: string;
  operator_id?: number | string;
  name?: string | null;
  role: string;
  created_at?: string;
}

export type RuntimeStatus = "online" | "offline" | "draining";
export type RuntimeHealth = "healthy" | "degraded" | "unhealthy";

export interface RuntimeCapabilities {
  tool?: string;
  models?: string[];
  [key: string]: unknown;
}

export interface Runtime {
  id: number | string;
  slug: string;
  org_id?: number | string | null;
  machine_id?: string;
  machine_label?: string;
  tool?: string;
  os?: string;
  status?: RuntimeStatus;
  health?: RuntimeHealth;
  last_heartbeat_at?: string | null;
  capabilities?: RuntimeCapabilities | null;
  working_root?: string | null;
  daemon_version?: string | null;
  active_sessions?: number;
}

export interface Persona {
  id: number | string;
  slug: string;
  name: string;
  description?: string | null;
  system_prompt?: string | null;
  model?: string | null;
  tool?: string | null;
  default_runtime_id?: number | string | null;
  operator_id?: number | string | null;
  color?: string | null;
  avatar?: string | null;
  status?: string;
}

export interface Pod {
  id: number | string;
  slug: string;
  name: string;
  description?: string | null;
  status?: string;
  org_id?: number | string | null;
  // Pod leadership is a Persona (BL-P1-03). `leader` names that Persona's slug.
  leader?: string | null;
  leader_persona?: string | null;
  leader_persona_id?: number | string | null;
  // The operator principal that owns the compatibility row. Reported so the UI
  // can show it as provenance, never as the Pod's leader.
  legacy_leader_operator?: string | null;
  members?: PodMember[];
  legacy_operator_members?: PodLegacyMember[];
  archived_at?: string | null;
}

export interface PodMember {
  persona_id: number | string;
  persona_slug?: string;
  persona_name?: string;
  name?: string | null;
  role?: string;
  is_leader?: boolean;
  status?: string;
  model?: string | null;
  tool?: string | null;
  source?: string;
  runtime_id?: number | string | null;
  runtime_slug?: string | null;
  runtime_status?: string | null;
  dispatchable?: boolean;
  blocked_reason?: string | null;
}

// A legacy operator membership that resolved to no single Persona. It
// is shown with its reason and is never dispatchable.
export interface PodLegacyMember {
  operator: string;
  name?: string | null;
  role?: string;
  reason: string;
  dispatchable?: boolean;
}

export interface PodDispatchCandidate {
  persona_id: number | string;
  persona_slug?: string;
  is_leader?: boolean;
  runtime_id?: number | string | null;
  dispatchable: boolean;
  blocked_reason?: string | null;
}

export interface PodDispatchPlan {
  pod_id: number | string;
  pod_slug?: string;
  org_id?: number | string | null;
  leader_persona_id?: number | string | null;
  persona_id?: number | string | null;
  persona_slug?: string;
  runtime_id?: number | string | null;
  tool?: string | null;
  blocked_reason?: string | null;
  candidates: PodDispatchCandidate[];
}

export interface Project {
  id: number | string;
  code: string;
  slug?: string;
  name: string;
  description?: string | null;
  workspace_id?: number | string | null;
  assignee_pod_id?: number | string | null;
  status?: string;
  issue_counts?: Record<string, number>;
}

export interface Workspace {
  id: number | string;
  slug: string;
  name?: string | null;
  path: string;
  status: string;
  visibility: string;
  org_id?: number | string | null;
  last_touched_at?: string | null;
}

export type IssueStatus =
  | "open"
  | "in_progress"
  | "blocked"
  | "in_review"
  | "done"
  | "cancelled";

export type IssuePriority = "p0" | "p1" | "p2" | "p3";

export interface Issue {
  id: number | string;
  code: string;
  title: string;
  body?: string | null;
  status: IssueStatus;
  priority?: IssuePriority;
  project_id?: number | string | null;
  parent_issue_id?: number | string | null;
  workspace_id?: number | string | null;
  assignee_persona_id?: number | string | null;
  assignee_pod_id?: number | string | null;
  assignee_operator_id?: number | string | null;
  assignee_label?: string;
  labels?: string[];
  agent_task_code?: string | null;
  has_live_session?: boolean;
  closed_at?: string | null;
}

export interface Session {
  id: number | string;
  status?: string;
  state?: string;
  duration_seconds?: number | null;
  started_at?: string;
  last_activity_at?: string | null;
  ended_at?: string | null;
  issue_id?: number | string | null;
  persona_id?: number | string | null;
  persona_name?: string;
  runtime_id?: number | string | null;
  tool?: string;
  summary?: string | null;
  // BL-P0-05: whether a console message can reach this Session's agent at all.
  // Declared by the launch shape on the server, so the composer is blocked
  // with a stated reason instead of accepting text that cannot be delivered.
  message_capability?: MessageCapability;
}

export interface MessageCapability {
  supported: boolean;
  reason?: string | null;
}

// BL-P0-05: one durable operator command addressed to a Session. Recorded
// before it is delivered, so a reload renders what was asked for and what
// became of it rather than an optimistic bubble.
export interface SessionCommand {
  command_id: string;
  operation_key?: string;
  session_id: string;
  sequence: number;
  kind: "message" | "stop" | string;
  status: "requested" | "delivered" | "acknowledged" | "failed" | "cancelled" | string;
  result?: string | null;
  error?: string | null;
  text?: string | null;
  reason?: string | null;
  attempt?: number;
  requested_by?: string | null;
  created_at?: string;
  updated_at?: string;
  completed_at?: string | null;
  duplicate?: boolean;
}

export interface SessionEvent {
  id?: number | string;
  kind?: string;
  stream?: string;
  message?: string;
  chunk?: string;
  created_at?: string;
}

export interface IssueComment {
  id: number;
  issue_id: number;
  body: string;
  author_kind?: string;
  author_operator_id?: number | null;
  author_persona_id?: number | null;
  session_id?: string | null;
  created_at?: string;
}

// F4 (BL-P1-02) — what dispatching an Issue would do, or the one stable reason
// it cannot. The console renders `blocked_reason` rather than guessing.
export interface IssueDispatchPlan {
  issue_id: number | string;
  issue_code: string;
  issue_status?: string;
  org_id?: number | string | null;
  assignee_kind?: "persona" | "pod" | "operator" | null;
  assignee_id?: number | string | null;
  assignee_label?: string | null;
  persona_id?: number | string | null;
  runtime_id?: number | string | null;
  tool?: string | null;
  pod_id?: number | string | null;
  pod_leader_persona_id?: number | string | null;
  in_flight_session_id?: string | null;
  dispatchable: boolean;
  blocked_reason?: string | null;
  candidates: PodDispatchCandidate[];
}

export interface IssueDispatchResult {
  status?: string;
  duplicate: boolean;
  session_id: string;
  issue_code?: string;
  persona_id?: number | string | null;
  runtime_id?: number | string | null;
  pod_id?: number | string | null;
}

export interface IssueEvidenceSession extends Session {
  events?: number;
  commands?: number;
  usage?: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    cost_actual_usd: number;
  };
}

// F4 (AC-F4-04/05) — the Issue's reconciled execution evidence, read from
// persisted rows and de-duplicated by primary key.
export interface IssueEvidence {
  issue_id: number | string;
  issue_code: string;
  org_id?: number | string | null;
  assignment: IssueDispatchPlan;
  sessions: IssueEvidenceSession[];
  totals: {
    sessions: number;
    running_sessions: number;
    ended_sessions: number;
    events: number;
    commands: number;
    decisions: number;
    open_decisions: number;
    hidden_sessions?: number;
  };
  events: { total: number; by_kind: Record<string, number> };
  commands: { total: number; by_status: Record<string, number> };
  decisions: Array<{ code: string; status: string; session_id?: string | null; title?: string }>;
  usage: {
    attributed_calls: number;
    input_tokens: number;
    output_tokens: number;
    cost_actual_usd: number;
    priced_calls: number;
    unpriced_calls: number;
    sessions_with_usage: number;
    sessions_without_usage: number;
    attribution: string;
  };
  links: { sessions: string[]; comments: string; session_list: string };
}

// F6 (BL-P1-04) — the durable onboarding attempt the console resumes from.
export type OnboardingStepStatus = "pending" | "done" | "deferred" | "failed";

export interface OnboardingStep {
  step: string;
  status: OnboardingStepStatus;
  entity_ref?: string | null;
  detail?: string | null;
  error?: string | null;
  attempts: number;
  updated_at?: string | null;
}

export interface OnboardingAttempt {
  attempt_id: string;
  status: "in_progress" | "completed" | "blocked" | "abandoned";
  current_step: string;
  blocked_reason?: string | null;
  blocked_detail?: string | null;
  recovery?: { label: string; route: string; detail: string } | null;
  steps: OnboardingStep[];
  entities: {
    org?: { id: number; slug: string } | null;
    runtime?: { id: number; slug: string; status: string } | null;
    persona?: { id: number; slug: string; status: string } | null;
    project?: { id: number; code: string } | null;
    issue?: { id: number; code: string; status: string } | null;
    session?: { id: string; state?: string | null; ended: boolean } | null;
  };
  org_id?: number | null;
  persona_id?: number | null;
  issue_id?: number | null;
  session_id?: string | null;
}

export interface OnboardingState {
  required: boolean;
  fresh_install: boolean;
  attempt: OnboardingAttempt | null;
  steps_order: string[];
}

export interface OnboardingStepBody {
  status?: OnboardingStepStatus;
  entity_ref?: string;
  detail?: string;
  error?: string;
  org_id?: number;
  runtime_id?: number;
  persona_id?: number;
  project_id?: number;
  issue_id?: number;
  session_id?: string;
}

// Approvals + ask_human share one ApprovalRequest store (WS3 §2).
export interface Approval {
  code: string;
  title?: string;
  subject?: string;
  body?: string;
  question?: string;
  proposed_answer?: string | null;
  status?: string;
  kind?: string; // "ask_human" marks the human-question subset
  from_session_id?: number | string | null;
  session_id?: number | string | null;
  persona_name?: string;
  ask_depth?: number;
  expires_at?: string | null;
  created_at?: string;
}

export interface Handoff {
  id: number | string;
  handoff_id?: number | string;
  code?: string;
  title?: string;
  status?: string;
  workspace?: string;
  created_at?: string;
}

export interface Paginated<T> {
  data: T[];
  next_cursor?: string | null;
}

export interface EnrolResponse {
  command: string;
  token: string;
  expires_at: string;
}

export interface UsageModelRow {
  routed_model: string;
  calls: number;
  input_tokens?: number;
  output_tokens?: number;
}

export interface UsageSummary {
  days: number;
  scope?: "gateway" | "org" | string;
  org?: string;
  org_id?: number | string;
  totals: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    cost_actual_usd?: number | null;
    savings_usd?: number | null;
  };
  top_models: UsageModelRow[];
}

export interface ConfigProvider {
  name: string;
  configured: boolean;
  stub: boolean;
  status: "simulated" | "configured" | "unconfigured";
  reason?: string;
}

export interface ConfigModelRoute {
  tier: string;
  provider: string;
  model: string;
  simulated: boolean;
}

export interface ConfigSummary {
  providers: ConfigProvider[];
  gateway: { router_enabled?: boolean; base_url?: string };
  models?: ConfigModelRoute[];
  routes?: Record<string, string>;
  integrations?: {
    github: {
      configured: boolean;
      allowed_repository_count: number;
    };
    bridges: Array<{
      name: string;
      configured: boolean;
      status: "configured" | "unconfigured" | "degraded";
    }>;
  };
  write_contract?: {
    mode: "read_only";
    detail: string;
    reload: string;
  };
  models_endpoint?: string;
  secrets_managed?: string;
}

// --- operational health (B8, BL-P1-09, BL-P1-12) ---
// Bootstrap-admin only. Distinct from liveness `GET /health`: this reports a
// protected ready/degraded verdict for storage/migration, coordination queues,
// durable mailbox state, and recovery-policy configuration.

export type HealthState = "ready" | "degraded";

export interface ReadinessComponent {
  state: HealthState;
  detail: Record<string, unknown>;
}

export interface ReadinessReport {
  status: HealthState;
  components: {
    storage: ReadinessComponent;
    queue: ReadinessComponent;
    durable_mail: ReadinessComponent;
    recovery_policy: ReadinessComponent;
  };
}

export interface QueueFamilyHealth {
  total: number;
  open: number;
  stale_or_expired: number;
  owner: string;
  scope: string;
  lifecycle: string;
  expiry_policy: string;
}

export interface QueueHealthSummary {
  generated_at: string;
  families: Record<string, QueueFamilyHealth>;
}

export interface QueueHealthIssue {
  code: string;
  family: string;
  field?: string;
  count: number;
  detail: string;
  sample: Array<Record<string, unknown>>;
}

export interface QueueHealthDiagnosis {
  generated_at: string;
  issue_count: number;
  issues: QueueHealthIssue[];
}

export interface QueueHealthReport {
  summary: QueueHealthSummary;
  diagnosis: QueueHealthDiagnosis;
}

export interface QueueHealthRepairAction {
  code: string;
  family: string;
  description?: string;
  would_affect_rows?: number;
  applied_rows?: number;
}

export interface QueueHealthRepairResult {
  applied: boolean;
  generated_at?: string;
  applied_at?: string;
  actions: QueueHealthRepairAction[];
  unresolved_work_preserved: boolean;
}

export interface RecoveryPolicySummary {
  scope: string | null;
  schedule: string | null;
  retention_days: number | null;
  encryption_at_rest: boolean;
  encryption_owner: string | null;
  offsite_owner: string | null;
  offsite_location: string | null;
  rto_minutes: number | null;
  rpo_minutes: number | null;
  restore_drill_required: boolean;
  last_restore_drill_at: string | null;
  complete: boolean;
  missing_fields: string[];
}

export interface RecoveryPolicyReport {
  ready: boolean;
  policy: RecoveryPolicySummary;
  compatibility: {
    migration_healthy: boolean;
    known_schema_versions: number;
    applied_schema_versions: number;
    compaction_prerequisite_ok: boolean | null;
    detail: string | null;
  };
  reasons: string[];
}

// --- workspace-first operator console ---

export interface OperatorTask {
  code: string;
  workspace: string;
  title: string;
  body?: string | null;
  priority: string;
  status: string;
  claimed_by_session_id?: string | null;
  created_at?: string;
}

export interface OperatorClaim {
  workspace: string;
  session_id: string;
  scope: string;
  claimed_at?: string;
  expires_at?: string;
}

export interface OperatorDecision {
  code: string;
  workspace: string;
  workspace_id?: number;
  session_id?: string | null;
  title: string;
  body?: string | null;
  proposed_answer?: string | null;
  status: string;
  kind?: string | null;
  metadata?: Record<string, unknown>;
  created_at?: string;
}

export interface OperatorHandoff extends Handoff {
  body?: string | null;
  set_at?: string;
}

export interface OperatorEvent {
  id: number | string;
  kind: string;
  message: string;
  workspace_id?: number | null;
  session_id?: string | null;
  created_at: string;
}

export interface OperatorAgent {
  session_id: string;
  workspace?: string | null;
  tool?: string | null;
  state?: string | null;
  started_at?: string;
  last_activity_at?: string | null;
  interactive_input?: boolean;
  mailbox_address?: string;
  mailbox_deep_link?: string;
}

export interface OperatorKnowledge {
  code: string;
  type: string;
  title: string;
  body?: string | null;
  status: string;
  scope: string;
  workspace?: string | null;
  severity?: string;
  created_at?: string;
}

export interface OperatorSignal {
  type: string;
  scope: string;
  workspace?: string | null;
  count: number;
  last_at?: string | null;
}

export interface OperatorPattern {
  name: string;
  category: string;
  description: string;
  status: string;
  usage_count?: number;
}

export interface OperatorWorkspace extends Workspace {
  last_summary?: string | null;
  live_agents: number;
  claim?: OperatorClaim | null;
  tasks: Record<string, number>;
  open_decisions: number;
  active_handoffs: number;
  unread_messages: number;
}

export interface OperatorOverview {
  generated_at: string;
  situation: {
    workspaces: number;
    live_agents: number;
    active_claims: number;
    open_decisions: number;
    active_handoffs: number;
    blocked_tasks: number;
  };
  workspaces: OperatorWorkspace[];
  attention: {
    decisions: OperatorDecision[];
    handoffs: OperatorHandoff[];
  };
  live_agents: OperatorAgent[];
  recent_events: OperatorEvent[];
  readiness: ReadinessReport | null;
  audit: Record<string, unknown> | null;
}

export interface OperatorWorkspaceDetail {
  workspace: Workspace & { last_summary?: string | null };
  live_agents: OperatorAgent[];
  sessions: Session[];
  claims: OperatorClaim[];
  tasks: OperatorTask[];
  decisions: OperatorDecision[];
  handoffs: OperatorHandoff[];
  knowledge: OperatorKnowledge[];
  signals: OperatorSignal[];
  events: OperatorEvent[];
}

export interface WorkspaceLookupResult {
  path: string;
  line: number;
  end_line: number;
  snippet: string;
  symbol: string | null;
  match: "symbol" | "text";
}

export interface WorkspaceLookupEnvelope {
  status: "ok" | "empty" | "limited" | "unavailable";
  reason: string;
  query: string;
  results: WorkspaceLookupResult[];
  scanned_files: number;
  truncated: boolean;
  incomplete_reasons: string[];
}

export interface OperatorCoordination {
  tasks: OperatorTask[];
  claims: OperatorClaim[];
  handoffs: OperatorHandoff[];
  knowledge: OperatorKnowledge[];
  signals: OperatorSignal[];
}

export interface MailboxAccess {
  address: string;
  kind: "agent" | "operator";
  workspace: string | null;
  tool: string | null;
  owner_operator: string | null;
  unread_count: number;
  can_open: boolean;
  can_send: boolean;
  deep_link: string;
}

export interface MailboxSmtpStatus {
  mailbox: string;
  destination_state: "unconfigured" | "pending" | "verified" | "unavailable";
  superseded?: boolean;
  destination_hint: string | null;
  copy_mode: "disabled" | "notification" | "full_body";
  verified_at: string | null;
  full_body_consented_at: string | null;
  outbox: {
    open: number;
    sent: number;
    failed: number;
    uncertain: number;
    cancelled: number;
  };
}

export interface MailboxAddress {
  address: string;
  kind: "agent" | "operator";
  workspace: string | null;
  tool: string | null;
  owner_operator: string | null;
}

export interface MailDeliveryState {
  cursor: number;
  delivery_id: string;
  recipient: string | null;
  recipient_workspace: string | null;
  state: "accepted" | "read";
  accepted_at: string;
  read_at: string | null;
  read_by_session_id: string | null;
  read_by_operator: string | null;
  read_channel: string | null;
}

export interface MailMessage {
  cursor: number;
  message_id: string;
  thread_id: string;
  sender: string;
  sender_session_id: string | null;
  origin_workspace: string;
  audience: "direct" | "broadcast";
  in_reply_to: string | null;
  forwarded_from: string | null;
  forwarded_message: {
    message_id: string;
    forwarded_from: string | null;
    sender: string | null;
    origin_workspace: string | null;
    kind: string;
    subject: string;
    body: string;
    created_at: string;
  } | null;
  kind: string;
  subject: string;
  body: string;
  created_at: string;
  deliveries: MailDeliveryState[];
  inbox_delivery?: MailDeliveryState | null;
  created?: boolean;
}

export interface MailboxMessageList {
  mailbox: string;
  cursor: number;
  unread_count?: number;
  messages: MailMessage[];
}

export interface MailThread {
  thread_id: string;
  origin_workspace: string;
  started_by: string;
  subject: string;
  created_at: string;
  updated_at: string;
  mailbox: string;
  unread_count: number;
  cursor: number;
  messages: MailMessage[];
}

export interface OperatorGovernance {
  decisions: OperatorDecision[];
  actions: Array<Record<string, unknown>>;
  audit: Array<Record<string, unknown>>;
  chain: Record<string, unknown> | null;
}

export interface OperatorTool {
  name: string;
  display_name: string;
  is_available: boolean;
  last_verified_at?: string | null;
}

export interface OperatorOperations {
  readiness: ReadinessReport;
  queue: {
    summary: QueueHealthSummary;
    diagnosis: QueueHealthDiagnosis;
  };
  recovery: RecoveryPolicyReport;
  service: {
    platform?: string;
    state?: string;
    installed?: boolean;
    serving?: boolean;
    healthy?: boolean;
    listeners?: { gateway?: boolean; mcp?: boolean };
    service_pid?: Record<string, unknown>;
  };
}

export type OperatorTransport = "native_http" | "thin_adapter" | "host_contract";

export interface OperatorCapability {
  key: string;
  label: string;
  category: string;
  scope: string;
  transport: OperatorTransport;
  enabled: boolean;
  reason?: string;
}

export interface OperatorCapabilityCatalog {
  data: OperatorCapability[];
  labs_enabled: boolean;
  install_admin: boolean;
}

export interface OperatorTransitionResult {
  code: string;
  status: string;
}

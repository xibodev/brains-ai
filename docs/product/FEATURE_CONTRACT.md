<!--
last_verified: 2026-08-05T12:22:18.971-06:00
verified_by: GitHub Copilot CLI
verification_basis: working-tree candidate based on HEAD 865794899901b7893759bb5b582f089b856a268f; static inspection and targeted tests for operational readiness (AC-B8-01..04), coordination queue health/continuity repair (AC-F3-02..07, AC-B2-01..03), recovery-policy declaration (AC-B5-04/05), and Runtime/service PID identity and periodic staleness sweep (AC-F1-06, AC-B6-03) - BL-P1-09, BL-P1-12, BL-P1-13; full candidate gate not verified; external integration operation not verified; deployment not verified; UAT not verified
-->

# Brains Feature Contract

## How to read this contract

This document defines stable product promises and records what current HEAD exposes.

Status vocabulary:

- **present (E1/E2 only):** the surface exists in source and a relevant automated contract is present. No pass, UAT, or deployment claim is made.
- **partial (E1/E2 only):** part of the promise exists, but at least one acceptance criterion is absent, contradicted, or only represented by an unmatched client or test contract.
- **missing:** the promised behavior or server contract is absent from current HEAD.
- **not applicable:** deliberately excluded from the feature promise.

Evidence levels are defined in [QUALITY_GATES.md](../QUALITY_GATES.md). All status statements below are static/test-presence observations only.

The user-centered phrasing, minimum paths, implementation surfaces and
structured evidence expectations for these same IDs are maintained in
[USER_OUTCOME_SPEC.md](USER_OUTCOME_SPEC.md).

## Product invariants

1. Brains is the canonical product, repository, package, namespace, CLI, MCP, state, and browser identity.
2. Every protected `/v1/*` operation requires authentication that resolves one explicit principal, and applies Org/Workspace authorization to that principal.
3. Explicit model selection is faithful. Only an explicit `brains/auto` request may invoke classifier routing.
4. Durable state is written before it is presented as recovered or complete.
5. Human-gated actions must not execute before an attributable decision.
6. Health, readiness, test presence, local execution, isolated UAT, and deployment are different claims.
7. A user-facing feature is accepted only through its Persona, Journey, and
   `AC-*` mappings. Supporting capability acceptance criteria additionally require an
   explicit system or operational validation mapping in
   [TRACEABILITY.md](TRACEABILITY.md).
8. The normal `/app` console is Workspace-first: Command Center, Workspaces,
   Coordination, Governance, Operations, and Act. Execution-model screens
   (Runtimes, Personas, Pods, Projects, Issues, Sessions, Automation, and
   onboarding) require the explicit `BRAINS_UI_LABS=1` opt-in and are not a
   normal-install product claim.

## Core Brains features

### F0 - Console foundation and coherent product state

**Promise:** The operator can enter one Brains console, inspect every visible Workspace, navigate stable operational surfaces, receive actionable errors, and launch only typed, truthfully available actions.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F0-01 | `/app` provides a stable authenticated shell and redirects to a valid product start surface. | present (E1/E2 only); `/app` redirects to Command Center. |
| AC-F0-02 | The active scope persists across reload and all scoped screens use it consistently. | present/partial (E1/E2 only); Workspace deep links persist directly, while the active Org remains context for Access and Labs screens. Authorization is enforced independently. |
| AC-F0-03 | API failures remain visible as actionable error states rather than empty data. | partial; typed errors exist, but some screens convert failures to empty arrays. |
| AC-F0-04 | Persona Spawn creates an attributable Session and surfaces it in Sessions. | partial/Labs-only; route and tests exist, but Spawn with no Issue may create no daemon assignment. |
| AC-F0-05 | Deep routes select the named entity or return a clear not-found state. | partial; Workspace, Pod, Project, and Issue parameters are consumed, while gated Sessions, Personas, and Runtimes retain explicit parameter gaps. |

**Exclusions:** The Act palette searches views and typed capabilities, not arbitrary product data, and never executes shell or MCP commands.

**Failure behavior:** Authentication failure must lead to sign-in or a structured API error. Unknown deep links must not silently show a different entity.

### F1 - Connect a machine and register Runtimes

**Promise:** An operator can mint a short-lived connection command, redeem it on a machine, discover supported CLIs, register one Runtime per machine and tool, and see lifecycle state.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F1-01 | Enrollment returns a complete command that identifies the hub and one-time token. | present (E1/E2 only). |
| AC-F1-02 | Redemption registers one Runtime for each detected supported CLI and stores capabilities. | present (E1/E2 only). |
| AC-F1-03 | Enrollment tokens are hash-only, expiring, and single-use under concurrency. | present (E1/E2 only); redemption is one conditional `UPDATE ... WHERE redeemed_at IS NULL`, and a concurrent-redeem test asserts exactly one winner. |
| AC-F1-04 | The console moves through enrolling, waiting, connected, expired, and error states. | present (E1/E2 only); browser evidence simulates redemption. |
| AC-F1-05 | Runtime credentials authorize only Runtime operations in the intended Org. | present (E1/E2 only); redemption mints an Org-bound, machine-bound, expiring, revocable credential limited to register/heartbeat/status/claim/execute, an allow/deny matrix asserts it is refused on every operator, admin and console surface, and registration cannot move an already-claimed machine into another Org nor leave an Org-less Runtime behind when it is refused. |
| AC-F1-06 | Runtime stale/offline state is maintained without requiring an operator read. | present (E1/E2/E3); the recurring-scheduler tick (`brains.mcp.server._scheduler_tick`) now calls `_sweep_stale_runtimes` every tick alongside the governed-action sweep, flipping an online Runtime silent past `BRAINS_RUNTIME_STALE_TTL_SECONDS` (default 90s) to `offline` without an operator read; `count_stale` gives a read-only preview of the same candidates for `GET /v1/admin/readiness`. |

**Exclusions:** Connecting a machine does not install third-party CLIs or grant provider credentials.

**Failure behavior:** Used, expired, unknown, or malformed tokens are rejected. A Runtime without a valid working root or tool capability cannot accept work.

### F2 - Personas and capability binding

**Promise:** An operator can create a reusable Persona, bind it to a Runtime, choose a compatible model/tool combination, maintain instructions, archive it, and spawn work.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F2-01 | Runtime selection constrains the model and tool choices shown to the operator. | present (E1/E2 only). |
| AC-F2-02 | Name, instructions, model, tool, color, and default Runtime persist. | present (E1/E2 only). |
| AC-F2-03 | Invalid capability combinations fail with a visible explanation. | partial; validation exists in several layers, but the complete UI failure contract is not covered. |
| AC-F2-04 | A Persona can be archived without deleting historical Sessions. | present (E1/E2 only). |
| AC-F2-05 | Spawn creates executable work or clearly requires an Issue/runtime input. | partial; the route exists, but empty-body SPA Spawn can lack daemon-pull work. |
| AC-F2-06 | Skills can be attached to a Persona with provenance. | missing. |

**Exclusions:** A Persona is not an authentication identity or security boundary.

**Failure behavior:** Missing or offline Runtime bindings prevent dispatch; the system must not silently route to an unrelated Runtime or model.

### F3 - Sessions, events, steering, and human control

**Promise:** Dispatch produces a durable Session whose lifecycle and events can be followed after reconnect; human asks and approvals are actionable; steering and stop controls have real server and Runtime behavior.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F3-01 | Session events are durably stored and backfilled before realtime continuation. | present (E1/E2 only) for event storage/backfill; Session, Issue, approval and Runtime realtime events also commit to `realtime_events` before they are announced and are replayed by cursor on reconnect, while transcript chunks stay notification-only and are backfilled over REST. |
| AC-F3-02 | Session state follows explicit spawning, running, blocked, completed, and failed transitions. | present (E1/E2 only); the daemon now runs a claimed assignment as the hub's Session rather than a second local one, reports its terminal state, and reconciles on startup what it can no longer prove it owns, so hub and Runtime no longer diverge silently. |
| AC-F3-03 | Session actions can update linked Issue state and comments with attribution. | present (E1/E2 only) for current paths. |
| AC-F3-04 | Asks and approvals appear in Governance and can be resolved once with related Workspace/Session context. | present/partial (E1/E2 only); the Workspace-scoped queue distinguishes human answers from approve/reject/defer decisions, while complete publish coverage remains incomplete. |
| AC-F3-05 | Chat messages are durable, Org-authorized, delivered to the running agent, and recover after reload. | partial (E1/E2); a message is a `session_commands` row written before anything is announced, authorized by Org and Workspace, idempotent per operation key, ordered, claimed by exactly one consumer under a lease, and replayed after reload by `GET /v1/sessions/{id}/commands`. Delivery to the *running agent* is proven only for a tool launched with an open input channel: no shipped CLI is, so a message to `copilot`, `claude` or `codex` is settled `failed`/`unsupported` with the reason and the console blocks its composer instead of echoing a send. |
| AC-F3-06 | Stop requests are authorized, persisted, delivered to the Runtime, and reflected in terminal state. | present (E1/E2 only); `POST /v1/sessions/{id}/stop` is authorized by Org and Workspace, durable, idempotent per Session while an attempt is open or after one stopped it, claimed by exactly the consumer its Session is bound to under a lease - the binding, not the machine recorded on the row, so a Session the hub spawned for a remote Runtime is that Runtime's to stop - and delivered to the exact process handle that consumer launched, never one matched by name. A stop that failed terminally while the Session kept running is retryable: the next press records a new attempt rather than returning the dead one, and the console offers that retry. Terminal state, `ended_at`, Workspace claim release, Task release and the linked Issue move together, and only when the process is proven gone; a stop racing a natural completion is a conditional stamp with one winner. The E4 browser journey is absent. |
| AC-F3-07 | Realtime subscriptions are authorized by operator, Org, and entity. | present (E1/E2 only); topics are a closed grammar resolved by the server to a canonical name and an Org/Workspace scope, refusals are uniform so a subscription discloses no existence, Runtime credentials are refused the operator transports, and authorization is re-checked per message and on a timer. Live fan-out is per gateway process, and the E4 disconnect/reconnect browser journey is absent. |

**Exclusions:** An in-memory WS event is not durable evidence.

**Failure behavior:** Reconnect uses scoped persisted events. Failed stop or steering requests remain visible and do not pretend the agent stopped.

### F4 - Projects, Issues, assignment, and dispatch

**Promise:** Product work is expressed as Projects and Issues, assigned to a human, Persona, or Pod, dispatched to an eligible Runtime, and reconciled with Session and usage evidence.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F4-01 | Operators can create and inspect Projects and Issues with stable codes, status, priority, and comments. | present (E1/E2 only). |
| AC-F4-02 | Exactly one human, Persona, or Pod assignment target is represented and validated. | present (E1/E2 only) at the Issue data/API layer. |
| AC-F4-03 | Dispatch validates assignment and Runtime, creates a linked Session, and moves work into progress. | present (E1/E2 only) for Persona dispatch. |
| AC-F4-04 | Issue detail shows durable Session/event history and attributable comments. | present (E1/E2 only); the evidence view reconciles persisted Sessions, events, commands, decisions, comments, and usage. |
| AC-F4-05 | Token/cost/event rollup is scoped to the Issue and reconciles persisted usage. | present (E1/E2 only); usage is counted only through one-to-one Session attribution and unattributed calls remain explicit. |
| AC-F4-06 | Pod assignment resolves to documented leader/member execution behavior. | present (E1/E2 only); leader-first Persona routing is deterministic and reports every rejected candidate. |
| AC-F4-07 | Natural-language Issue creation is either implemented with confirmation or excluded from the UI contract. | present (E1/E2 only); Issue creation is deliberately structured and deterministic, with no natural-language create promise in the console. |

**Exclusions:** Dragging a card is not proof that execution occurred.

**Failure behavior:** Invalid transitions, multiple assignment targets, missing Runtime binding, and stale optimistic writes return visible errors and preserve the prior durable state.

### F5 - Pods

**Promise:** An operator can create a Pod of Personas, define a leader and members, assign work to it, and understand how dispatch is routed.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F5-01 | Pod create/read/update/archive behavior is available from the modern console. | present (E1/E2 only). |
| AC-F5-02 | Pod membership uses Personas for product execution semantics. | present (E1/E2 only); unresolved legacy operator rows are visible but never treated as dispatchable Personas. |
| AC-F5-03 | Leader change and member add/remove preserve one valid leader. | present (E1/E2 only); removing the leader requires a replacement and the leader remains rostered. |
| AC-F5-04 | Issue-to-Pod dispatch resolves deterministically and is visible in the Session. | present (E1/E2 only); complete browser journey evidence remains absent. |

**Exclusions:** The current `Squad` implementation name is not a separate product concept.

**Failure behavior:** A Pod without an eligible leader/runtime cannot dispatch and must expose a recovery action.

### F6 - First-run onboarding

**Promise:** A fresh operator can move from sign-in to a dispatched Issue through a guided flow without hidden fixtures or prior configuration.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F6-01 | Fresh state automatically offers or routes to onboarding. | missing from the normal console by design at current maturity; the durable flow remains available only at `/app/labs/onboarding` when `BRAINS_UI_LABS=1`. |
| AC-F6-02 | The flow creates/selects an Org, connects or explicitly defers a machine, creates a Persona, creates work, and dispatches. | present (E1/E2 only); deferred Runtime setup leads to a named blocked state rather than false success. |
| AC-F6-03 | Every step has loading, empty, error, retry, back, and safe-exit behavior. | partial (E1/E2 only); durable retry/resume and safe exit exist, while complete browser-state coverage is absent. |
| AC-F6-04 | Completion lands on the attributable Session or a clear blocked state. | present (E1/E2 only); only a Session linked to the attempt's Issue can complete it. |
| AC-F6-05 | A clean-state browser journey proves the flow without seeded success assumptions. | missing. |

**Exclusions:** Seeded runbook Issues are not created unless explicitly selected by the operator.

**Failure behavior:** Skipping machine setup cannot produce a false successful dispatch.

### F7 - Provider and integration configuration

**Promise:** The modern console presents redacted effective provider and gateway state and can test connectivity. Persisted configuration writes are outside this contract until a safe write contract is approved.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F7-01 | Provider connectivity tests return explicit success/failure without leaking secrets. | present (E1/E2 only). |
| AC-F7-02 | Effective provider, gateway, routing, MCP, integration, and secret-handling state is truthful and redacted. | present/partial (E1/E2 only); provider readiness and tier wiring are structured, encrypted email-secret status is boolean-only, while remaining MCP/integration sections are informational. |
| AC-F7-03 | The UI clearly distinguishes read-only information from editable configuration. | present (E1/E2 only); provider/gateway inspection remains read-only, the Runtime Overlay editor performs validated non-secret writes, and Email/Secrets perform encrypted write-only secret changes without plaintext reads. |
| AC-F7-04 | Multi-process configuration reload semantics are documented and verified before writes are promised. | partial (E1/E2 only); encrypted email writes reload the handling process and persist in the Brains DB, environment values remain higher precedence, and every other long-lived Brains process still requires restart before treating the change as active. |

**Exclusions:** The modern SPA does not currently promise a general secret editor or full configuration mutation.

**Failure behavior:** Unknown providers return a bounded failure. Secret values never appear in responses, logs, docs, or browser state.

### F8 - GitHub work linkage

**Promise:** Authenticated GitHub events can link pull requests to Issues and transition an Issue only when the event and repository scope satisfy the configured contract.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F8-01 | A pull request reference can be linked to an Issue. | present at E1/E2/E3 for the configured webhook path. |
| AC-F8-02 | A verified merged event can move the linked Issue to Done idempotently. | present at E1/E2/E3 through durable delivery-ID replay refusal; external operation remains unverified. |
| AC-F8-03 | Webhook authentication validates the expected GitHub signature and repository scope. | present at E1/E2/E3 with HMAC-SHA256, required delivery/event headers, and an exact normalized repository-to-Org binding. |
| AC-F8-04 | Configuration and failure states are visible without exposing credentials. | present at E1/E2/E3 in the admin-only modern Config summary; repository names and secrets are not returned. |

**Exclusions:** The presence of a webhook route is not proof that a GitHub installation is configured.

**Failure behavior:** Unauthenticated, replayed, malformed, or out-of-scope events do not mutate Issues.

### F9 - Orgs, members, roles, and usage

**Promise:** Org owners administer membership and role-based access, while operators see usage scoped to their authorized Org, Persona, Runtime, Project, or Issue as promised by the UI.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F9-01 | Org create/read/update and active-Org switching are durable. | present (E1/E2 only). |
| AC-F9-02 | Owners/admins can add, change, and remove members; members cannot exceed their role. | present (E1/E2 only); `member` is refused Org administration, only an `owner` may grant, revoke or otherwise change the `owner` role, and an Org cannot be left with no owner outside an explicit local bootstrap recovery. |
| AC-F9-03 | HTTP reads and writes enforce operator identity plus Org/Workspace scope. | present (E1/E2 only); every protected route resolves one principal and applies a per-route Org capability check, per-ID reads apply the same `private` Workspace visibility as the listings, and every Session listing is Workspace-scoped whichever entity it hangs off (`/v1/sessions`, `/v1/issues/{issue}/sessions`, `/v1/personas/{persona}/sessions`). |
| AC-F9-04 | Usage totals identify their scope and exclude unauthorized data. | present; gateway totals declare `scope: gateway` and are restricted to the bootstrap admin (install-wide, not Org-attributed). `GET /v1/orgs/{org}/usage` declares `scope: org`, `org`, `org_id` and `days`, is readable by any principal with `org.read` on that Org, and joins `usage_ledger` through `usage_attributions` filtered on `org_id` so an unattributed call or another Org's call is excluded by the SQL join rather than filtered after the fact. |
| AC-F9-05 | A two-operator, two-Org deny matrix covers native APIs and browser sessions. | partial; the API matrix (including the Org-scoped usage cross-Org case) and the cookie-binding case are asserted in `tests/test_authz_identity_scope.py`; browser-session evidence (E4) is absent. |

**Exclusions:** Stored role labels alone do not satisfy RBAC.

**Failure behavior:** An unauthorized principal that may read the Org receives 403; anything in an Org it may not read receives 404, identical to a target that does not exist.

### F10 - Autopilots and Skills

**Promise:** Operators can define scoped recurring work and reusable Skills; every fire is durable, attributable, authorized, and executed through the same governance boundary as manual work.

| Acceptance criterion | Target contract | Current status |
|---|---|---|
| AC-F10-01 | Autopilot create/list/enable/disable/manual-fire is Org-scoped. | present; every lookup (list, enable, fire) re-derives and re-authorizes the autopilot's Org rather than trusting its globally-unique `name`, so another Org's principal cannot enable, fire, or see it (`test_cross_org_autopilot_list_and_lifecycle_are_scoped`). The autopilot `name` namespace itself remains install-wide, not per-Org. |
| AC-F10-02 | Supported schedules are validated as `manual`, `hourly`, `daily`, or `every:<N><s|m|h|d>`. | present; `control.recurring.is_valid_schedule` is enforced at create time (a cron-syntax or otherwise unsupported string is refused with 400, `test_autopilot_schedule_grammar_is_validated_at_create_time`), and the SPA and legacy dashboard label the field "Schedule" and state the grammar rather than calling it cron. |
| AC-F10-03 | Scheduled and manual fire create durable task/run/audit records. | present (E1/E2 only); the schedule advance, the `recurring_runs` row, and the `recurring.fired` audit entry commit in one transaction, so a fire that cannot be recorded does not happen. |
| AC-F10-04 | Recurring execution uses the enforceable approval/execution gate. | partial; auto-spawn is classified, approved and recorded through the same governed-action path as a manual command instead of calling `Popen`, but the boundary is cooperative and in-process (BL-P0-03). |
| AC-F10-05 | Skills attach to Personas or Projects and enter Session context with provenance. | present (E1/E2 only); `persona_skills`/`project_skills` (migration 138) attach a Skill with a unique `(entity, skill)` pair (idempotent re-attach) and provenance (`attached_by_operator_id`, `attached_at`); `control.skills.resolve_context_for_session` composes a deduplicated, source-tagged (`persona`/`project`) context and `exec.runner.run_session` prepends it to the spawned agent's actual prompt — the real launch path, not merely the `build_welcome` API response. |
| AC-F10-06 | Duplicate, disabled, unauthorized, or failed fires produce recoverable states. | partial. |

**Exclusions:** General cron syntax is not supported by current HEAD.

**Failure behavior:** A disabled or unauthorized definition does not fire. A failed fire remains durable and retryable without duplicate work.

## Supporting Brains capabilities

### B1 - Model gateway and faithful routing

**Promise:** OpenAI- and Anthropic-compatible clients can use exact models or explicit tier aliases without silent classifier substitution.

- AC-B1-01: explicit `provider/model` and catalog IDs resolve faithfully or return `404 model_not_found`.
- AC-B1-02: only `brains/auto` invokes classifier routing when routing is enabled.
- AC-B1-03: streaming and non-streaming responses identify the actual upstream model.
- AC-B1-04: auth, redaction, bounded errors, usage, retries, and circuit policy apply consistently.

**Status:** present/partial (E1/E2 only). Exact, tier, and explicit `brains/auto` routing are distinguished and every route records whether it is simulated; `/v1/responses` remains a thin compatibility wrapper and default tiers remain simulated through `echo` until configured. The model-serving routes and the `brains-ai run` launcher are gated experimental (`BRAINS_EXPERIMENTAL_GATEWAY`) — off in the normal install, with model access expected from each CLI's own provider logins.

### B2 - Coordination plane and MCP

**Promise:** Agents can share Sessions, tasks, claims, handoffs, messages, decisions, knowledge, patterns, recurring definitions, tool records, and checkpoints through stable MCP and CLI surfaces.

- AC-B2-01: session start returns current coordination context and ownership signals.
- AC-B2-02: claims and task transitions are atomic and expire or release predictably.
- AC-B2-03: messages, handoffs, checkpoints, and resume data preserve continuity.
- AC-B2-04: mutation tools are authenticated, scoped, and human-gated where required.

**Status:** present/partial (E1/E2/E3). The surface is broad. MCP SSE resolves the presented key to one principal and refuses Runtime-narrow credentials. Topic boards, harness-qualified help, one `inbox_wait` long poll, recency-based live-agent discovery, explicit predecessor/successor handle links, and successor reads of unread predecessor mail cover the observed multi-session coordination loop without silently changing recipient identity. `/app/coordination`, `/app/workspaces/:slug`, and `/app/act` expose scoped reads and named HTTP mutations over the same controls. Approval resolution remains separated from the requester. Stdio MCP still inherits the launching process's trust boundary, and per-tool destructive-action governance remains incomplete.

### B3 - Context, knowledge, semantic retrieval, and code graph

**Promise:** Agents can index approved sources, retrieve relevant context, inspect freshness, and query a code graph without treating generated context as authority.

- AC-B3-01: repository indexing is bounded, ignore-aware, and content-hash based.
- AC-B3-02: semantic search reports unavailable dependencies and never fabricates matches.
- AC-B3-03: graph queries identify their language and indexing limits.
- AC-B3-04: external freshness checks apply allowlists and SSRF protections.

**Status:** present/partial (E1/E2 only). Graph support is Python-focused; embeddings require a configured local model.

### B4 - Human governance, execution gate, and audit

**Promise:** Human decisions precede governed outward actions, and the resulting record is attributable and tamper-evident.

- AC-B4-01: approval-required actions fail closed until a valid decision exists.
- AC-B4-02: all execution paths, including recurring work and interpreters, share the same process/network boundary.
- AC-B4-03: audit append is transactional with the governed action and safe across processes.
- AC-B4-04: verification detects mutation, deletion, insertion, and truncation under the stated key threat model.

**Status:** partial (E1/E2 only). One governed-action contract now carries approvals, direct commands, recurring fire and Brains-launched subprocesses on the agent-execution path: the request, the decision and the outcome each commit with their audit entry in one transaction, an approval is consumed once and only when it is unexpired and scope-matched to the reviewed arguments, a repeated idempotency key replays instead of re-executing, appends are serialised across processes through the signed audit chain head, and verification fails closed on mutation, deletion, truncation, a forged head and an unsigned head over a non-empty log (a pre-signature store is adopted once by `brains-ai audit-adopt`, which verifies before it signs). Secret-shaped values are removed canonically at the request boundary, so a URL credential, a `NAME=VALUE` secret, a `curl -u` password (and the other tool- and subcommand-scoped short forms: `curl -b`, `redis-cli -a`, `sshpass -p`, `docker login -p`), an `Authorization`/`Cookie` header value, a credential field inside a form or JSON body, or a known token shape reaches neither the digest, the stored summary, the chain, the ASK body nor a bridge message, while ordinary arguments stay readable so an operator can see what they are approving. The abandoned-attempt lease is per attempt and refreshed atomically on reset and on execution start, an action that is *executing* is judged by silence since its last heartbeat (`governed_actions.heartbeat_at`, `BRAINS_EXECUTION_LEASE_SECONDS`) rather than by how long it has been running - so a session or deploy that outlasts any fixed budget stays `executing` and keeps its idempotency key, while an owner that crashed stops renewing and is settled once the budget is spent - renewal is a conditional update on action, `executing` status and attempt (so it cannot resurrect a terminal row or keep a superseded attempt alive) and writes no audit entry, concurrent retries of one key yield exactly one new attempt, and the expiry rules have a periodic owner (the recurring scheduler tick and `brains-ai governed-sweep`) that never settles a live attempt. Verification takes the log and the head from one snapshot, so an append landing mid-scan no longer reports an intact chain as truncated on either backend, and a released action is settled by the process that released it: an observable outcome is recorded as `succeeded`/`failed`, and a POSIX `execv` handoff is recorded as `released` - terminal, honest about what is unknown, and immune to the stale sweep - so a command that ran is no longer recorded as abandoned. The boundary is cooperative and in-process: it covers the processes Brains launches on that path and the commands the PATH shims intercept, and it classifies absolute paths, Windows paths, wrappers, shell/interpreter inline code, module runs and remote-code runners. A network fetcher's target is judged by parsing rather than by string prefix, so only a loopback address literal or the reserved `localhost`/`*.localhost` name is local and `127.0.0.1.attacker.com` is outward; every endpoint the invocation names is judged that way, including `--flag=value` targets, proxy/SOCKS/DoH values and the address half of `--resolve`/`--connect-to`, with ambiguous syntax gated rather than guessed. Admin effects that cannot share a transaction with their record (overlay and env-override writes, backup, restore, `db repair --apply`) commit an `.attempted` entry before the effect and append the completion or `.failed` after it, so none of them runs unrecorded and none is recorded as done before it is. It does not contain a third-party agent CLI that calls an absolute path itself, rewrites `PATH`, or opens its own socket; several operator-invoked paths still exec directly and are enumerated in BL-P0-03; and outbound bridge/provider network calls are not routed through it. The resolver is now bound: an approval records the principal that decided it, a Runtime credential can never resolve one, the Session that filed an ASK can never resolve it, and the Persona identity behind the request cannot either - a refusal appends `approval.self_resolution_denied` (BL-P0-01). AC-B4-02 remains open on BL-P0-03.

### B5 - Storage, migrations, backup, and recovery

**Promise:** SQLite default and optional Postgres state evolve consistently, enforce referential integrity, and can be backed up and restored safely.

- AC-B5-01: fresh and upgraded databases reach the same supported schema.
- AC-B5-02: SQLite foreign keys and concurrency settings are explicitly enforced.
- AC-B5-03: Postgres migrations execute equivalent deltas rather than recording skipped files.
- AC-B5-04: backup and restore validate manifests, hashes, compatibility, and target ownership.
- AC-B5-05: recovery defines schedule, retention, encryption, RTO, RPO, and isolated drills.

**Status:** partial (E1/E2/E3). Schema evolution is an ordered, checksummed, backend-honest contract: the frozen per-backend baseline plus numbered deltas replace `create_all` at startup, every ledger row records checksum, backend, outcome, attempts and error, an edited or unimplemented migration is refused, and post-migration schema drift raises. A pre-checksum ledger row is adopted backend-aware, so a Postgres store whose old ledger claimed unexecuted SQLite deltas keeps them reapplicable as `skipped`/`legacy-unproven` instead of frozen as applied, and the Postgres baseline's foreign-key guards match constraint identity rather than name. WAL/busy timeout, manual backup/restore, manifest source identity, isolated restore verification, archive schema-compatibility refusal, and a dry-run-by-default integrity repair exist. `brains.control.recovery_policy` (BL-P1-09) now declares a managed-recovery policy - scope, schedule, retention, encryption expectation/owner, offsite owner/location, RTO/RPO, and a restore-drill requirement - through `BRAINS_BACKUP_*` settings, reports it back redacted with an honest completeness verdict (an unconfigured install answers `complete: false` with the exact missing fields, never a fabricated "managed" claim), and runs a compatibility precheck that reuses `migration_status` and, for Postgres, the existing `pg_dump`/`pg_restore` tool-presence gate. Brains still runs no backup scheduler itself - the policy is a declaration an external scheduler is expected to honour - and AC-B5-05's isolated restore drill remains E4. FK enforcement is opt-in and unproven on real stores; AC-B5-03 is not met, because Postgres converges through the baseline while the SQLite deltas are recorded `skipped` and no live-Postgres upgrade matrix has been run in this repository's default run.

### B6 - CLI, wiring, and service management

**Promise:** The `brains-ai` CLI can initialize, run, wire, inspect, coordinate, back up, and manage the local service without mutating unrelated user configuration.

- AC-B6-01: `brains-ai` is the sole installed Brains executable.
- AC-B6-02: wiring preserves unmanaged configuration and supports status/unwire.
- AC-B6-03: service commands render and manage user-level services on supported operating systems.
- AC-B6-04: help and docs do not advertise removed commands.

**Status:** present/partial (E1/E2/E3). Wiring has conflict-safe, idempotent status/unwire adapters for Copilot CLI, Claude Code, Codex, and OpenCode; each renderer uses the tool's native MCP schema, preserves unrelated settings, backs up edits, and marks only its own entry removable. Service installation uses and verifies the exact `sys.executable` that can import Brains rather than guessing a sibling `pythonw.exe`; install fails loud when the interpreter probe fails. It also preflights the requested gateway port, refuses an unavailable explicit port, or persists a bindable fallback when the default is unavailable. `service.status()` reports the effective console/MCP endpoints, PID identity, and bounded listener probes, and is healthy only when the installed service both owns a process and serves. The supervisor refuses a permanent gateway bind conflict before entering child restart supervision. PID verification additionally accepts an exact Brains `serve-all` command-line match when Windows start-time reporting is unavailable/inconsistent, while still refusing unrelated processes. Legacy installers remain outside the supported path.

### B7 - Webhooks and messaging bridges

**Promise:** External triggers and messaging adapters use explicit credentials, bounded scopes, deduplication, and visible failure states.

- AC-B7-01: trigger and relay endpoints reject absent or invalid credentials.
- AC-B7-02: deliveries are idempotent where an external event ID exists.
- AC-B7-03: bridge credentials are redacted and optional subsystems fail loud when dependencies are absent.
- AC-B7-04: companion-device risks and third-party terms are explicit.

**Status:** partial (E1/E2/E3). Trigger tokens, relay credentials, bounded fallback dedupe, GitHub delivery dedupe, leased/attempt-fenced durable approval-bridge outcomes, and admin recovery of a confirmed-stuck attempt exist. External operation is not verified, configured bridge discovery remains process-local, and WhatsApp companion dedupe still resets on restart.

### B8 - Observability, health, and readiness

**Promise:** Operators can distinguish liveness, dependency readiness, product readiness, and operational degradation.

- AC-B8-01: `/health` remains an open liveness/inventory endpoint.
- AC-B8-02: readiness checks required dependencies, storage writes, migrations, provider configuration, and critical workers.
- AC-B8-03: logs, traces, and metrics are redacted and identify process boundaries.
- AC-B8-04: multi-process failures and stale Runtimes are observable.

**Status:** partial (E1/E2/E3). `/health` remains open and liveness-only. `GET /v1/admin/readiness` (bootstrap-admin only, BL-P1-09/BL-P1-12) is a separate, protected readiness contract: one overall `ready`/`degraded` verdict from four bounded, redacted component checks - storage/migration health (`migration_status`), coordination-queue health (`brains.control.queue_health.summarize`), Runtime lifecycle/staleness (`list_runtimes` + a read-only `count_stale` against the same TTL the scheduler sweep applies), and recovery-policy completeness/compatibility (`brains.control.recovery_policy.recovery_readiness`) - with `brains-ai readiness` as the CLI equivalent. No component returns a secret or a raw exception message. Configured-provider readiness is deliberately excluded from this contract (BL-P1-11 remains its own gap). Multi-process failures beyond the current process (a crashed gateway/dashboard/MCP child, an unreachable Postgres) are still not observed by this contract; AC-B8-03's redacted logs/traces/metrics contract is unchanged by this work.

### B9 - Legacy dashboard and admin surfaces

**Promise:** Existing server-rendered surfaces have an explicit support boundary and do not contradict or bypass the Brains console.

- AC-B9-01: `/app`, `/dashboard`, and `/admin` responsibilities are documented.
- AC-B9-02: authentication and authorization are consistent across mounted processes.
- AC-B9-03: duplicate or unsupported workflows are retired or integrated deliberately.

**Status:** partial (E1/E2 only). Three browser surfaces remain and a support/retirement decision is missing, but they no longer disagree about identity: `/app`, `/dashboard`, `/admin`, the native API, the realtime transports and MCP SSE all resolve the same credential store to the same principal, and the console cookie resolves only to the key that minted it, so a revoked key's cookie stops working immediately.

<!--
last_verified: 2026-08-05T05:56:14.196-06:00
verified_by: GitHub Copilot CLI
verification_basis: working-tree candidate based on HEAD 51e039b61539c4d3e1ace399239fa49e54215922; static inspection and targeted tests for Org-scoped usage attribution/authorization (BL-P1-07) and Skill attachment/context-injection/schedule-grammar validation (BL-P1-08); live-provider recovery not verified; deployment not verified; UAT not verified
-->

# Brains Personas and Journeys

## Human personas

### P1 - Solo operator/developer

- **Goal:** Move from a repository and available coding CLIs to governed, observable work.
- **Expects:** Clear first action, reliable Runtime/Persona binding, fast Issue dispatch, durable Session state, and understandable recovery.
- **Risk:** May assume a successful UI action means a real agent or provider executed.

### P2 - Org owner

- **Goal:** Define the Org, membership, role boundaries, integrations, and risk posture.
- **Expects:** Owner-only changes, secret safety, scoped usage, and evidence for decisions.
- **Risk:** Current role labels do not provide complete route-level authorization.

### P3 - Org admin/member

- **Goal:** Collaborate on Projects and Issues within authorized scope.
- **Expects:** No cross-Org visibility, attributable changes, and stable realtime updates.
- **Risk:** Current native API and topic authorization are incomplete.

### P4 - Runtime host operator

- **Goal:** Connect a machine, control installed CLIs and credentials, constrain working roots, and drain or stop execution.
- **Expects:** Narrow Runtime credentials, predictable heartbeat/offline behavior, and local process control.
- **Risk:** Current daemon keys are broadly accepted and the default daemon hub origin points at the dashboard port.

### P5 - Human approver

- **Goal:** Understand an ask or proposed action, approve or reject it, and see the result.
- **Expects:** Context, attribution, one-time resolution, timeout behavior, and no pre-approval execution.
- **Risk:** The current action gate does not cover every process or network path.

### P6 - Release/operations operator

- **Goal:** Establish candidate identity, run hard gates and isolated UAT, back up state, deploy safely, observe, and roll back.
- **Expects:** Exact commands, readiness distinct from liveness, recoverable data, and no unsupported deployment claim.
- **Risk:** Current deploy scaffolds are inconsistent and no deployment is verified.

### P7 - AI Persona

- **Goal:** Receive scoped work and context, execute through an eligible Runtime, emit events, coordinate with peers, and ask for human input.
- **Expects:** Stable assignment, working directory, model/tool configuration, durable handoff, and bounded permissions.
- **Risk:** A Persona is not an authentication principal; runtime and operator credentials define actual authority.

## System actors

| ID | Actor | Responsibility | Current boundary |
|---|---|---|---|
| SA1 | Gateway process | `/v1`, `/app`, admin mount, provider routing, WS/SSE bus | Separate process-local config, counters, and EventBus. |
| SA2 | Dashboard process | Legacy `/dashboard` and admin mount | Shares DB/files, not in-memory state, with gateway. |
| SA3 | MCP process | SSE/stdio tools and recurring scheduler | Shares DB/files; stdio relies on process boundary. |
| SA4 | Runtime daemon | Detects CLIs, registers Runtimes, polls/claims work, emits events | Uses an operator key that is not runtime-narrow. |
| SA5 | Provider or local model | Executes LLM requests | Availability and model quality are external to Brains. |
| SA6 | GitHub/webhook/bridge peer | Delivers external events or human messages | Requires per-surface credentials; live operation is unverified. |
| SA7 | SQLite/Postgres store | Durable product and coordination state | SQLite is default; Postgres migration parity is incomplete. |
| SA8 | In-process EventBus + durable event log | Realtime fan-out for gateway publishers, over a shared `realtime_events` record | Live fan-out is not cross-process; durability and cursor replay are, so another process's publish is caught up on resume rather than pushed. |

## Journey conventions

- Journey IDs `J1` through `J11` are stable.
- Acceptance criteria are defined in [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md).
- "Evidence gap" means current HEAD lacks E3/E4 proof or a required contract.
- A recovery path must return the user to a truthful state; retrying a hidden failure is not recovery.

## J1 - Sign in and complete first run

**Primary personas:** P1, P2
**Entry:** An operator opens `/app` or a protected browser route.
**Preconditions:** The gateway is reachable; an accepted key exists; the browser has no valid session cookie.

**Actions**

1. The operator is directed to the sign-in surface.
2. The operator submits a key.
3. Brains establishes a signed browser session.
4. If no usable Org/product state exists, Brains offers onboarding.
5. The operator creates or selects an Org.
6. The operator connects a Runtime or explicitly accepts a blocked local-only state.
7. The operator creates a Persona, Project, and Issue.
8. The operator dispatches or sees the exact unmet prerequisite.

**UX states:** signed out, submitting, invalid key, signed in, no Org, onboarding step, waiting for machine, blocked, complete.

**Errors and recovery**

- Invalid key: remain signed out, show bounded error, allow retry.
- API unavailable: retain entered non-secret state where safe and offer retry.
- Skipped machine: do not show dispatch success; offer Runtime connection or save work as blocked.
- Partial onboarding: resume at the last durable completed step.

**Success:** The operator lands on an attributable Session or a clearly blocked Issue with a recovery action.

**Acceptance IDs:** AC-F0-01, AC-F0-02, AC-F6-01, AC-F6-02, AC-F6-03, AC-F6-04, AC-F6-05.

**Evidence gaps:** `j01-first-run.spec.ts` proves invalid-key recovery and the complete isolated onboarding path through a Session. Clean-host CLI installation and a real external Runtime remain separate evidence needs.

## J2 - Connect a machine

**Primary personas:** P1, P4
**Entry:** Runtimes empty state, connect button, or onboarding machine step.
**Preconditions:** Authenticated operator; hub URL reachable from the target machine; at least one supported CLI may be installed.

**Actions**

1. Mint a connection token and command.
2. Copy and execute the command on the target machine.
3. Redeem the token.
4. Detect supported CLIs and capabilities.
5. Register one Runtime per machine and CLI.
6. Heartbeat and publish connected state.
7. Inspect or drain the Runtime.

**UX states:** idle, token issued, waiting, connected, no CLI found, expired, already used, offline, draining.

**Errors and recovery**

- Expired/used token: mint a new token.
- Hub unreachable: preserve the command context and show network guidance.
- No tools detected: show supported tools and re-detect action.
- Heartbeat loss: mark offline after the target TTL and allow reconnect.

**Success:** The Runtime appears with machine, tool, capabilities, status, and Org scope.

**Acceptance IDs:** AC-F1-01 through AC-F1-06, AC-B6-01.

**Evidence gaps:** Browser spec simulates redemption; no isolated real-daemon E4 evidence; redemption atomicity, credential scope, stale sweep scheduling, and default daemon URL remain gaps.

## J3 - Create and bind a Persona

**Primary personas:** P1, P2, P7
**Entry:** Personas screen or onboarding Persona step.
**Preconditions:** Active Org; at least one eligible Runtime for executable work.

**Actions**

1. Create or open a Persona.
2. Choose a Runtime.
3. Choose a model and tool from Runtime capability data.
4. Enter instructions and optional presentation metadata.
5. Save and review the binding.
6. Spawn against an Issue or navigate to Issue assignment.
7. Archive when no longer active.

**UX states:** empty, create drawer, Runtime unavailable, capability loading, validation error, saved, spawning, archived.

**Errors and recovery**

- Runtime offline: block spawn and offer another eligible Runtime.
- Model/tool mismatch: retain form state and explain the allowed choices.
- Spawn lacks Issue/work: route to Issue selection rather than create an inert success.
- Save conflict: refresh authoritative state before retry.

**Success:** The Persona persists with an executable binding and historical Sessions remain attributable after archive.

**Acceptance IDs:** AC-F2-01 through AC-F2-06, AC-F0-04.

**Evidence gaps:** Browser and backend contracts are present, and Skills now attach to a Persona with provenance and enter its spawned Session context (`tests/test_skill_attachments.py`); empty-body Spawn may not produce daemon work.

## J4 - Create and operate a Pod

**Primary personas:** P1, P2, P3, P7
**Entry:** Pods screen.
**Preconditions:** Active Org; eligible Persona members exist.

**Actions**

1. Create a Pod with name, purpose, and leader.
2. Add and remove members.
3. Change the leader while preserving a valid roster.
4. Assign an Issue to the Pod.
5. Dispatch and observe the selected execution path.
6. Archive the Pod without erasing history.

**UX states:** empty, create, roster, leader selection, invalid roster, dispatch blocked, active, archived.

**Errors and recovery**

- Leader removed/offline: require a replacement before dispatch.
- No eligible Persona/Runtime: show why routing failed.
- Stale membership: reload authoritative roster and retry.

**Success:** Pod membership and routing are understandable, durable, and reflected in the resulting Session.

**Acceptance IDs:** AC-F5-01 through AC-F5-04, AC-F4-06.

**Evidence gaps:** Persona roster, leader replacement, member removal, archive, and deterministic leader-first dispatch have E1/E2 coverage; complete J4 and Pod-assigned J6 browser evidence is absent.

## J5 - Create a Project and link a Workspace

**Primary personas:** P1, P2, P3, P4
**Entry:** Projects screen or onboarding work step.
**Preconditions:** Active Org; optional registered Workspace or repository path.

**Actions**

1. Create a Project.
2. Link or select a Workspace/repository scope.
3. Optionally associate a Pod.
4. Open the Project and its Issue board.
5. Pause or archive the Project while preserving history.

**UX states:** empty, create, Workspace picker, invalid path/scope, active, paused, archived.

**Errors and recovery**

- Workspace unavailable to the operator: deny without revealing private details.
- Runtime working root incompatible: keep Project but block dispatch with remediation.
- Deep link unknown: show not found, not the first Project.

**Success:** The Project has a durable Org and Workspace scope used by subsequent Issues and Sessions.

**Acceptance IDs:** AC-F4-01, AC-F0-05, AC-B2-02, AC-B5-01.

**Evidence gaps:** `j05-project-workspace.spec.ts` proves Workspace-linked Project creation plus known/unknown deep links. Project pause/archive behavior still needs journey proof.

## J6 - Create, assign, and dispatch an Issue

**Primary personas:** P1, P3, P7
**Entry:** Global Issues board or Project Issue board.
**Preconditions:** Active Project; eligible human, Persona, or Pod assignment target.

**Actions**

1. Create an Issue with title, body, priority, and acceptance context.
2. Assign exactly one human, Persona, or Pod.
3. Add comments or transition status.
4. Dispatch executable work.
5. Observe linked Session, events, comments, and state changes.
6. Review outcome and move to Done or a recoverable state.

**UX states:** create, validation error, open, assigned, dispatching, in progress, blocked, in review, done, cancelled.

**Errors and recovery**

- Invalid transition: retain current durable status and explain allowed transitions.
- Missing Runtime/binding: keep assignment and offer a binding action.
- Optimistic update failure: roll back UI to authoritative state.
- Duplicate dispatch: identify the existing active Session or create an idempotent retry.

**Success:** One attributable Session is linked to the Issue and the board reflects persisted state.

**Acceptance IDs:** AC-F4-01 through AC-F4-07, AC-F3-03, AC-F8-01, AC-F8-02.

**Evidence gaps:** Persisted execution rollup, structured-only Issue creation, Persona dispatch idempotency, and deterministic Pod resolution have E1/E2 coverage; complete J5/J6/J7 browser evidence and Pod execution on a real Runtime are absent.

## J7 - Dispatch and watch a Session

**Primary personas:** P1, P3, P4, P7
**Entry:** Persona Spawn, Issue Dispatch, Sessions list, or linked Issue history.
**Preconditions:** Eligible online Runtime, valid work scope, and executable tool configuration.

**Actions**

1. Dispatch work.
2. Create the hub Session and assignment.
3. Runtime claims the assignment.
4. Runtime starts the agent process.
5. Events and state are persisted.
6. The browser backfills events and subscribes to realtime.
7. Completion or failure reconciles Session and Issue state.

**UX states:** spawning, queued, running, disconnected, blocked, completed, failed, stale Runtime.

**Errors and recovery**

- Runtime never claims: expose timeout and reassign/retry.
- Browser disconnects: backfill persisted events before live continuation.
- Agent process exits: record terminal state and diagnostic event.
- Hub/local Session mismatch: reconcile rather than show duplicate or permanently running records.

**Success:** The operator can reload and still understand what ran, where, for what Issue, and with what outcome.

**Acceptance IDs:** AC-F3-01, AC-F3-02, AC-F4-03, AC-F4-04, AC-F1-06.

**Evidence gaps:** Active browser/backend contracts exist, but no exact-SHA real daemon lifecycle evidence; hub/local Session terminal reconciliation is incomplete.

## J8 - Ask, approve, steer, chat, and stop

**Primary personas:** P1, P5, P7
**Entry:** Inbox, Session detail, chat dock, bridge reply, or a gated action.
**Preconditions:** Active Session or open decision; authorized operator.

**Actions**

1. Agent files an Ask or governed action request.
2. Inbox shows context, actor, Session, and proposed effect.
3. Human answers, approves, edits, rejects, or defers.
4. The decision is delivered exactly once.
5. Optional chat reaches the running agent and is persisted.
6. Optional stop reaches the Runtime and terminal state is reconciled.

**UX states:** new, awaiting human, approved, rejected, deferred, delivered, timed out, chat sending, stop requested, stopped, delivery failed.

**Errors and recovery**

- Stale decision: show resolved state and prohibit a second effect.
- Missing Session context: permit safe rejection, not blind approval.
- Bridge unavailable: retain the open decision in the console.
- Chat/stop delivery failure: keep the request visible and retryable.

**Success:** The human decision or steering action is durable, scoped, attributable, and reflected in execution.

**Acceptance IDs:** AC-F3-04 through AC-F3-07, AC-B4-01 through AC-B4-04, AC-B7-01.

**Evidence gaps:** `j08-governance-session-control.spec.ts` proves one-time browser approval, truthful unsupported-chat state, and idempotent durable stop. Interactive follow-up delivery to the shipped Copilot launch shape remains unsupported, and the governance boundary remains in-process.

## J9 - Configure providers and integrations

**Primary personas:** P1, P2, P6
**Entry:** Config screens, legacy admin, CLI feature/provider commands.
**Preconditions:** Authorized operator; required optional dependencies and external credentials are available when needed.

**Actions**

1. Inspect redacted effective provider and gateway state.
2. Test a provider connection.
3. Inspect routing, MCP, integration, and secret-handling guidance.
4. Configure through an approved surface if the operation is supported.
5. Validate process-wide effect and rollback.

**UX states:** loading, configured, unconfigured, dependency missing, test running, success, failure, read-only, restart/reload required.

**Errors and recovery**

- Missing optional extra: fail loud with the required install hint.
- Bad secret/provider endpoint: show bounded failure without secret echo.
- Multi-process reload mismatch: require restart or verify all processes before success.
- Integration not configured: do not present it as active.

**Success:** The operator knows the effective configuration, whether a probe succeeded, and what remains unverified.

**Acceptance IDs:** AC-F7-01 through AC-F7-04, AC-F8-03, AC-F8-04, AC-B1-04, AC-B7-03.

**Evidence gaps:** Modern Config is explicitly read-only and provider/tier truth has E1/E2 coverage; MCP/integration sections remain informational, and live providers, GitHub, and bridges are unverified.

## J10 - Manage Org, members, usage, and automation

**Primary personas:** P2, P3, P6
**Entry:** Settings and Automation.
**Preconditions:** Active Org; authenticated operator; owner/admin privileges for protected changes.

**Actions**

1. Update Org metadata.
2. Add, role, or remove members.
3. Inspect scoped usage.
4. Create a Skill and attach it to intended work.
5. Create, enable, disable, or manually fire an Autopilot.
6. Inspect durable run and audit results.

**UX states:** no Org, unauthorized, member list, role edit, usage empty/data/error, automation empty, enabled, disabled, running, failed.

**Errors and recovery**

- Unauthorized role change: deny with 403.
- Unknown operator: explain the required operator creation path.
- Duplicate Skill/Autopilot: show conflict and existing record.
- Failed fire: persist failure and permit controlled retry.

**Success:** Administrative changes are role-authorized, usage is correctly scoped, and automation is governed and attributable.

**Acceptance IDs:** AC-F9-01 through AC-F9-05, AC-F10-01 through AC-F10-06, AC-B4-01, AC-B5-04.

**Evidence gaps:** Role enforcement, scoped usage (`GET /v1/orgs/{org}/usage`), Skill attachment/context-injection, and Org-safe Autopilot lookup are present at E1/E2/E3 (`test_f9_org_usage_summary_is_scoped_and_excludes_other_orgs`, `tests/test_skill_attachments.py`, `test_cross_org_autopilot_list_and_lifecycle_are_scoped`); the governed-action boundary recurring spawn uses remains in-process/cooperative (BL-P0-03/BL-P0-04), and browser-session E4 evidence for this journey is absent.

## J11 - Cross-cutting trust, realtime, errors, accessibility, and hygiene

**Primary personas:** P1-P7
**Entry:** Every Brains journey.
**Preconditions:** Any supported browser, API, CLI, MCP, Runtime, or operational path.

**Actions and expectations**

1. Navigate every declared modern SPA route.
2. Authenticate once and preserve only intended session state.
3. Exercise authorized and unauthorized reads, writes, WS topics, and SSE streams.
4. Disconnect and reconnect realtime.
5. Trigger validation, network, provider, database, and process failures.
6. Use keyboard navigation and inspect focus, labels, contrast, and responsive layout.
7. Confirm zero unexpected console errors and zero unhandled failed `/v1` requests.
8. Confirm secrets and personal identifiers do not appear in UI, logs, responses, screenshots, or committed fixtures.

**UX states:** loading, empty, success, validation error, authorization denied, disconnected, retrying, degraded, not found.

**Errors and recovery**

- Realtime loss: visible banner plus durable backfill. Reconnect resumes from the client's cursor; a gap the server cannot cover is signalled as a reset so the console re-reads over REST, and a revoked credential or lost membership stops the stream with a stated reason rather than reconnecting in a loop. A re-authorization the server could not run stops the stream too, under a distinct reason, so the console reconnects into a fresh check instead of treating a store outage as a revocation.
- API mismatch: visible error and blocked action, never silent success.
- Unauthorized entity: 403 without information disclosure.
- Unknown route/entity: clear not-found or safe redirect.
- Process degradation: distinguish liveness from readiness and identify affected capability.

**Success:** Every journey remains understandable, authorized, recoverable, accessible, and free of hidden console/network failures.

**Acceptance IDs:** AC-F0-01 through AC-F0-05, AC-F3-01, AC-F3-07, AC-F9-03, AC-B8-01 through AC-B8-04, AC-B9-01 through AC-B9-03.

**Evidence gaps:** The J11 browser sweep is a blocking CI gate, but accessibility, route-contract, and multi-process realtime coverage are incomplete. Realtime authorization, cursor replay, gap signalling and revocation are asserted at the API and unit level (`tests/test_realtime_scope.py`, `tests/test_frontend_realtime.py`) rather than through a browser disconnect/reconnect run.

## Supporting capability acceptance mapping

Supporting capability criteria enable user journeys but are not all visible as standalone
screens. They require the following explicit system or operational validation:

| Supporting feature | Acceptance criteria | Journey context | Required validation |
|---|---|---|---|
| B1 Gateway/routing | AC-B1-01 through AC-B1-04 | J9, J11 | Protocol, routing, streaming, redaction, retry, and model-identity tests. |
| B2 Coordination/MCP | AC-B2-01 through AC-B2-04 | J5-J8, J10, J11 | Session/task/claim/handoff/message/checkpoint lifecycle plus authorization tests. |
| B3 Context/retrieval | AC-B3-01 through AC-B3-04 | J6, J7, J11 | Bounded indexing, unavailable-dependency, graph-scope, and SSRF tests. |
| B4 Governance/audit | AC-B4-01 through AC-B4-04 | J8, J10, J11 | Gate-bypass, transaction, multiprocess-chain, and tamper tests. |
| B5 Storage/recovery | AC-B5-01 through AC-B5-05 | J5, J7, J10, J11 | Fresh/upgrade parity, FK, Postgres migration, backup, restore, and recovery drills. |
| B6 CLI/wiring/service | AC-B6-01 through AC-B6-04 | J1, J2, J9, J11 | Clean-host CLI, config-preservation, service lifecycle, and removed-command checks. |
| B7 Webhooks/bridges | AC-B7-01 through AC-B7-04 | J8, J9, J11 | Authentication, idempotency, redaction, dependency, and companion-risk tests. |
| B8 Observability/readiness | AC-B8-01 through AC-B8-04 | J7, J10, J11 | Liveness/readiness separation, redaction, process-failure, and stale-Runtime tests. |
| B9 Legacy surfaces | AC-B9-01 through AC-B9-03 | J9-J11 | Support-matrix, auth-consistency, navigation, and retirement/integration tests. |

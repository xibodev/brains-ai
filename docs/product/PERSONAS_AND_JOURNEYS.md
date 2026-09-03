<!--
last_verified: 2026-08-31T18:30:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 35ce5ff1b4a2eb8bce2777ca7e3cff4d7ceece99 plus the worktree contract correction and isolated Docker full quality, packaged browser, and real OpenCode/Claude/Codex mailbox UAT; installed-service recovery and deployment not verified
-->

# Brains Personas and Journeys

## Human personas

### P1 - Solo operator/developer

- **Goal:** Coordinate several coding-agent sessions around repositories without losing
  ownership or resume context.
- **Expects:** Clear first action, truthful presence, durable Workspace state, human
  decisions, and understandable recovery.
- **Risk:** May assume a durable event means Brains executed or delivered an external
  effect.

### P2 - Reserved multi-operator owner

- **Lifecycle:** frozen; retained for stable contract history only.
- **Goal:** Future multi-operator administration.
- **Risk:** Compatibility rows must not be mistaken for a supported tenancy boundary.

### P3 - Reserved multi-operator member

- **Lifecycle:** frozen; retained for stable contract history only.
- **Goal:** Future collaboration between human operators.
- **Risk:** This actor is not part of the supported single-operator product.

### P4 - Service host operator

- **Goal:** Install and supervise Brains, wire supported CLIs, control credentials and
  working roots, and recover local state.
- **Expects:** Windowless service operation, listener-aware health, configuration
  preservation, SQLite integrity, and bounded rollback.
- **Risk:** PID liveness can be mistaken for protocol readiness, and host mutations need
  exact rollback ownership.

### P5 - Human approver

- **Goal:** Understand an ask or proposed action, approve or reject it, and see the result.
- **Expects:** Context, attribution, one-time resolution, timeout behavior, and no pre-approval execution.
- **Risk:** The current action gate does not cover every process or network path.

### P6 - Release/operations operator

- **Goal:** Establish candidate identity, run hard gates and isolated UAT, back up state, deploy safely, observe, and roll back.
- **Expects:** Exact commands, readiness distinct from liveness, recoverable data, and no unsupported deployment claim.
- **Risk:** Current deploy scaffolds are inconsistent and no deployment is verified.

### P7 - AI agent session

- **Goal:** Coordinate scoped Workspace work, preserve handoffs and checkpoints, ask
  peers for help, and request human input.
- **Expects:** Stable Session identity, working directory, durable continuity, truthful
  tool posture, and bounded permissions.
- **Risk:** A Session label or harness identity is not authentication authority; the
  resolved credential and Workspace policy define actual access.

## System actors

| ID | Actor | Responsibility | Current boundary |
|---|---|---|---|
| SA1 | Gateway process | Supported `/v1`, `/app`, and WS/SSE surfaces | Process-local config, counters, and live EventBus; shared durable state. |
| SA2 | MCP process | Supported Streamable HTTP `/mcp` and stdio coordination tools; SSE is explicit legacy compatibility only | Shares SQLite/files; stdio relies on the local process boundary. |
| SA3 | Agent harness | Uses Brains MCP/CLI from a Workspace | Harness execution and provider authority remain outside Brains unless a governed path explicitly proves otherwise. |
| SA4 | GitHub | Frozen external integration | Source compatibility only; not an active evidence gap. |
| SA5 | SQLite store | Durable coordination, identity, governance, audit, and recovery state | Supported source of truth; one-writer contention and opt-in FK enforcement are explicit. |
| SA6 | Service supervisor | Owns the supported gateway and MCP child processes | PID identity alone is not protocol readiness. |
| SA7 | In-process EventBus + durable event log | Realtime fan-out and replay for the supervised local gateway | Cross-process scale is frozen. |
| SA8 | Withdrawn compatibility modules | Runtime execution, model gateway, automation, semantic/graph, bridges, alternate storage, and legacy HTML | Frozen source/data inventory only; no activation contract. |

## Journey conventions

- Journey IDs `J1` through `J11` are stable.
- Acceptance criteria are defined in [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md).
- "Evidence gap" means current HEAD lacks E3/E4 proof or a required contract.
- A recovery path must return the user to a truthful state; retrying a hidden failure is not recovery.
- A withdrawn journey retains its ID and target acceptance criteria for history and
  replacement evaluation, but exposes no supported action path.

## J1 - Sign in and complete first run

**Primary personas:** P1
**Entry:** An operator opens `/app` or a protected browser route.
**Preconditions:** The gateway is reachable; an accepted key exists; the browser has no valid session cookie.

**Lifecycle:** advertised for sign-in and Workspace-first entry. Execution-model
onboarding steps formerly attached to this journey are withdrawn.

**Actions**

1. The operator is directed to the sign-in surface.
2. The operator submits a key.
3. Brains establishes a signed browser session.
4. Brains opens Command Center with truthful empty, ready, or degraded state.
5. The operator creates or selects a Workspace; any implicit Org is compatibility state.
6. Brains offers setup, wiring, coordination, or recovery actions through typed normal
   surfaces only.
7. Any withdrawn execution-model URL returns retirement/not-found behavior and never
   becomes a first-run dependency.

**UX states:** signed out, submitting, invalid key, signed in, no Workspace,
ready, degraded, blocked.

**Errors and recovery**

- Invalid key: remain signed out, show bounded error, allow retry.
- API unavailable: retain entered non-secret state where safe and offer retry.
- Missing Workspace: retain safe local context and offer a supported setup action.
- Withdrawn route: return retirement/not-found guidance without exposing an activation
  switch.

**Success:** The operator lands on Command Center or an authorized Workspace with a
supported next action and no withdrawn route exposure.

**Acceptance IDs:** AC-F0-01, AC-F0-02, AC-F6-01, AC-F6-02, AC-F6-03, AC-F6-04, AC-F6-05.

**Evidence gaps:** `j01-first-run.spec.ts` now asserts clean-state Workspace-first entry
and fail-closed onboarding-route containment. Isolated-UAT E4 evidence for long-running
first-run recovery remains open; multi-process scale is frozen.

## J2 - Connect a machine

**Primary personas:** P1, P4
**Entry:** Runtimes empty state, connect button, or onboarding machine step.
**Preconditions:** Authenticated operator; hub URL reachable from the target machine; at least one supported CLI may be installed.

**Lifecycle:** withdrawn. The actions below are stable target history, not operator
instructions. Current acceptance is containment: no Runtime enrollment, daemon, or
activation surface is advertised, and direct access fails closed.

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

**Success:** No supported current path executes these actions; persisted compatibility
data remains readable during upgrade while exposure is removed.

**Acceptance IDs:** AC-F1-01 through AC-F1-06, AC-B6-01.

**Current boundary:** Enrollment, Runtime, daemon, and activation behavior has no
supported discovery or activation surface; retained rows and source are
compatibility-only.

## J3 - Create and bind a Persona

**Primary personas:** P1, P2, P7
**Entry:** Personas screen or onboarding Persona step.
**Preconditions:** Active Org; at least one eligible Runtime for executable work.

**Lifecycle:** withdrawn. The actions below are stable target history, not a supported
Persona, managed-Skill, or Spawn path.

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

**Success:** No supported current path executes these actions; historical rows retain
attribution while discovery and activation are removed.

**Acceptance IDs:** AC-F2-01 through AC-F2-06, AC-F0-04.

**Current boundary:** Persona, Skill, and spawn behavior has no supported discovery or
activation surface; retained rows and source are compatibility-only.

## J4 - Create and operate a Pod

**Primary personas:** P1, P2, P3, P7
**Entry:** Pods screen.
**Preconditions:** Active Org; eligible Persona members exist.

**Lifecycle:** withdrawn. The actions below preserve the target contract only; Pod and
legacy Squad implementations are compatibility inventory.

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

**Success:** No supported current path executes these actions; historical Pod/Squad rows
remain compatible while exposure is removed.

**Acceptance IDs:** AC-F5-01 through AC-F5-04, AC-F4-06.

**Current boundary:** Pod and legacy Squad behavior has no supported discovery or
activation surface; retained rows and source are compatibility-only.

## J5 - Create a Project and link a Workspace

**Primary personas:** P1, P2, P3, P4
**Entry:** Projects screen or onboarding work step.
**Preconditions:** Active Org; optional registered Workspace or repository path.

**Lifecycle:** withdrawn for Project execution. Workspace registration and coordination
remain advertised independently; the actions below preserve only the Project target
contract.

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

**Success:** No supported Project path executes these actions; Workspace identity and
historical Project links remain intact while Project exposure is removed.

**Acceptance IDs:** AC-F4-01, AC-F0-05, AC-B2-02, AC-B5-01.

**Current boundary:** Workspace behavior remains supported; Project discovery and
activation are absent from the shipped browser and product APIs.

## J6 - Create, assign, and dispatch an Issue

**Primary personas:** P1, P3, P7
**Entry:** Global Issues board or Project Issue board.
**Preconditions:** Active Project; eligible human, Persona, or Pod assignment target.

**Lifecycle:** withdrawn. The actions below are stable Issue/dispatch target history,
not a supported work-management or execution path.

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

**Success:** No supported current path executes these actions; historical Issue links
remain attributable while all assignment/dispatch exposure is removed.

**Acceptance IDs:** AC-F4-01 through AC-F4-07, AC-F3-03, AC-F8-01, AC-F8-02.

**Current boundary:** Issue and dispatch behavior has no supported discovery or
activation surface; retained data is compatibility-only.

## J7 - Dispatch and watch a Session

**Primary personas:** P1, P3, P4, P7
**Entry:** Workspace control room, Coordination, Act, CLI, or MCP.
**Preconditions:** Authorized Workspace and an agent harness using a coordination Session.

**Lifecycle:** advertised for coordination Session presence, durable events, ownership,
checkpoint/resume, and reconnect. Runtime dispatch and execution supervision are
withdrawn.

**Actions**

1. Start or resume a Workspace-scoped coordination Session. A supported adapter may
   atomically register/reattach its durable mailbox with native-ID and binding proof.
2. Inspect or claim durable work.
3. Record checkpoints, handoffs, messages, and scoped events.
4. Renew presence while the harness is active; mailbox-bound Sessions prove their
   native ID and binding rather than treating `ses_*` as a credential.
5. Backfill durable events before realtime continuation.
6. End cleanly, or become dormant and release ownership after lease expiry.

**UX states:** starting, active, disconnected, blocked, dormant, resumed, completed,
failed, stale presence.

**Errors and recovery**

- No eligible claimant: preserve open work and expose a recoverable undelivered state.
- Browser disconnects: backfill persisted events before live continuation.
- Harness exits: terminal/dormant paths transactionally detach the current mailbox
  incarnation; lease expiry marks dormant without fabricating execution failure.
- Successor Session: transfer eligible claims, tasks, subscriptions, and mailbox cursor
  continuity once after binding proof rather than duplicate ownership.

**Success:** The operator or successor agent can reload and understand who owned which
Workspace work, what durable context exists, and what remains unresolved, without a
false claim about process execution.

**Acceptance IDs:** AC-F3-01, AC-F3-02, AC-F4-03, AC-F4-04, AC-F1-06.

**Evidence gaps:** E3 covers mailbox identity validation, proof-bound
start/reuse/heartbeat/resume/successor transitions, cursor continuity, scope, and
terminal detach. Isolated E4 additionally covers real native-ID extraction and
cross-harness offline mail/resume between OpenCode/Codex and OpenCode/Claude. Automatic
adapter extraction, multi-hour idle, abrupt process exit, and host restart remain open.

## J8 - Ask, approve, steer, chat, and stop

**Primary personas:** P1, P5, P7
**Entry:** Governance, a Workspace control room, CLI/MCP coordination, or a governed
action.
**Preconditions:** Active Session or open decision; authorized operator.

**Actions**

1. Agent files an Ask or governed action request.
2. Governance shows context, actor, Workspace/Session, and proposed effect or human question.
3. Human answers, approves, edits, rejects, or defers.
4. The decision is consumed exactly once by an advertised governed path.
5. Address-based agent mail commits before local acceptance is reported, survives an
   offline recipient and Session replacement, and exposes Inbox/Sent, scoped threads,
   reply/forward provenance, explicit broadcast, and per-recipient read state.
6. An explicitly installed harness adapter may claim only a fixed body-free nudge and
   settle its observed result; regardless of that result, the agent pulls durable mail
   with current attachment and binding proof.
7. External email copies are frozen; local durable mail remains authoritative.
8. Unsupported running-agent steering or process stop is refused explicitly.

**UX states:** new, awaiting human, approved, rejected, deferred, consumed, timed out,
message unread/read, help open/claimed/answered, unsupported.

**Errors and recovery**

- Stale decision: show resolved state and prohibit a second effect.
- Missing Session context: permit safe rejection, not blind approval.
- Outbound delivery unavailable: retain the open decision in the console.
- Running-agent steering/stop requested: refuse rather than queue a claim that the
  withdrawn execution channel can deliver.
- Unknown, retired, conflicting, or unauthorized mailbox: return one non-enumerating
  unavailable result and keep prior local mail unchanged.
- Detached recipient: accept to the durable mailbox without claiming wakeup; the next
  proof-bound incarnation resumes from its cursor.
- Missing or failed notification adapter: preserve local acceptance and expose pull
  fallback; never inject peer-controlled mail text into model input.
- Abandoned hook claim: reclaim it after the bounded lease; after three unconfirmed
  attempts record delivery uncertainty and leave durable mail available to pull.
- Frozen adapters remain irrelevant to local acceptance and readiness.

**Success:** The human decision or coordination message is durable, scoped, attributable,
and represented no more strongly than its observed result.

**Acceptance IDs:** AC-F3-04 through AC-F3-07, AC-B4-01 through AC-B4-04, AC-B7-01.

**Evidence gaps:** Current E3 covers local offline acceptance, retries, read attribution,
thread/reply/forward, explicit broadcast, cursor continuity, and cross-Workspace refusal.
Container-only browser E4 covers selector, Inbox/Sent, explicit read, compose,
reply/forward, agent deep links, reload, keyboard, responsive layout, unknown deep-link
refusal, and accepted/read state. E3 also covers fixed body-free notification claims,
mode restrictions, concurrent claim/settle, detach/read fallback, binding proof,
consented Copilot CLI and Claude Code stop-hook wiring, fixed-output wakeups, pull-only
fallback for Codex/OpenCode, bounded reclaim, and terminal uncertainty. SMTP and external
notification adapters are frozen rather than active evidence gaps. Automatic native
identity lifecycle, abrupt process exit, and the residual in-process governance boundary
remain core concerns.

Operations E3 additionally distinguishes malformed registrations/live attachments,
aged unread delivery, notification fallback/failure, and SMTP backlog/failure without
making detached unread mail an outage. J11 shows count-only readiness without embedded
behavioral analytics; the projection contains no content, address, path, native Session
ID, or native mailbox object ID.

## J9 - Configure local Brains operation

**Primary personas:** P1, P6
**Entry:** Operations Configuration and supported CLI operations.
**Preconditions:** Authorized operator; required credentials for the supported operation
are available through their authoritative secret store.

**Lifecycle:** advertised for supported local service/MCP posture and approved writes.
Email/SMTP and GitHub are frozen; provider gateway, messaging bridge, alternate-storage,
telemetry, and legacy admin activation are withdrawn.

**Actions**

1. Inspect redacted effective local service, MCP, and secret-handling state.
2. Confirm withdrawn settings are absent from supported configuration controls.
3. Configure through an approved typed surface if the operation is supported.
4. Validate reload/restart semantics and rollback.

**UX states:** loading, configured, unconfigured, success, failure, read-only,
unsupported/withdrawn, restart/reload required.

**Errors and recovery**

- Withdrawn setting requested: refuse without providing an activation hint.
- Bad credential/endpoint: show bounded failure without secret echo.
- Multi-process reload mismatch: require restart or verify all processes before success.
- Integration not configured: do not present it as active.

**Success:** The operator knows the supported effective configuration, what changed,
which processes need restart, and which source-level capabilities are withdrawn.

**Acceptance IDs:** AC-F7-01 through AC-F7-04, AC-F8-03, AC-F8-04, AC-B1-04, AC-B7-03.

**Current boundary:** Frozen and withdrawn configuration is not discoverable or
activatable. Supported local service and MCP configuration remains redacted; GitHub
operation and multi-process reload are frozen.

## J10 - Manage Org, members, usage, and reusable guidance

**Primary personas:** P2, P3, P6
**Entry:** Operations Access, Coordination, and knowledge/pattern surfaces.
**Preconditions:** Active Org; authenticated operator; owner/admin privileges for protected changes.

**Lifecycle:** frozen target history. Multi-user Org administration, automatic pattern
intelligence, and related usage analysis are not supported local-product journeys.

**Actions**

1. Update Org metadata.
2. Add, role, or remove members.
3. Inspect scoped usage.
4. Create or approve Workspace knowledge and coordination patterns.
5. Inspect offer/use/decline receipts where the workflow supports them.
6. Verify managed Skills, Automation, and scheduled execution remain unavailable.

**UX states:** no Org, unauthorized, member list, role edit, usage empty/data/error,
knowledge empty/data/error, pattern proposed/approved/used, withdrawn.

**Errors and recovery**

- Unauthorized role change: deny with 403.
- Unknown operator: explain the required operator creation path.
- Duplicate knowledge/pattern: preserve the canonical entry and expose provenance.
- Withdrawn automation request: fail closed without creating a run.

**Success:** Administrative changes are role-authorized, usage is correctly scoped,
reusable coordination guidance is attributable, and withdrawn automation remains
non-activatable.

**Acceptance IDs:** AC-F9-01 through AC-F9-05, AC-F10-01 through AC-F10-06, AC-B4-01, AC-B5-04.

**Evidence gaps:** None for current core scope. Future work is tracked in
[FROZEN_BACKLOG.md](FROZEN_BACKLOG.md); existing source tests are compatibility evidence only.

## J11 - Cross-cutting trust, realtime, errors, accessibility, and hygiene

**Primary personas:** P1, P4-P7; P2-P3 remain frozen compatibility personas
**Entry:** Every Brains journey.
**Preconditions:** Any supported browser, API, CLI, MCP, agent-session, or operational
path.

**Actions and expectations**

1. Navigate every declared modern SPA route.
2. Authenticate once and preserve only intended session state.
3. Exercise authorized and unauthorized reads, writes, WS topics, and SSE streams.
4. Disconnect and reconnect realtime.
5. Trigger validation, network, SQLite, and process failures.
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

**Evidence gaps:** The J11 browser sweep is a blocking CI gate, but accessibility and
route-contract coverage are incomplete. Realtime authorization, cursor replay, gap
signalling and revocation are asserted at the API and unit level
(`tests/test_realtime_scope.py`, `tests/test_frontend_realtime.py`) rather than through a
browser disconnect/reconnect run. Multi-process realtime is frozen.

## Supporting capability acceptance mapping

Supporting capability criteria enable user journeys but are not all visible as standalone
screens. They require the following explicit system or operational validation:

| Supporting feature | Acceptance criteria | Journey context | Required validation |
|---|---|---|---|
| B1 Gateway/routing | AC-B1-01 through AC-B1-04 | J9, J11 | Withdrawn-surface advertisement inventory and fail-closed direct-call tests; target protocol tests remain historical source evidence only. |
| B2 Coordination/MCP | AC-B2-01 through AC-B2-04 | J5-J8, J10, J11 | Session/task/claim/handoff/message/checkpoint lifecycle plus authorization tests. |
| B3 Context/retrieval | AC-B3-01 through AC-B3-04 | J6, J7, J11 | Workspace knowledge and bounded non-semantic lookup tests plus containment tests for semantic, graph, embedding, and freshness surfaces. |
| B4 Governance/audit | AC-B4-01 through AC-B4-04 | J8, J10, J11 | Supported-path gate-bypass, transaction, and local tamper tests; multi-operator/concurrent-chain validation is frozen. |
| B5 Storage/recovery | AC-B5-01 through AC-B5-05 | J5, J7, J10, J11 | SQLite fresh/upgrade parity, FK, backup, restore, compatibility, and recovery drills; alternate backend containment. |
| B6 CLI/wiring/service | AC-B6-01 through AC-B6-04 | J1, J2, J9, J11 | Clean-host CLI, config-preservation, listener-aware service lifecycle, and removed-command checks. |
| B7 Authenticated external events | AC-B7-01 through AC-B7-04 | J8, J9, J11 | Frozen target history; core validation covers containment only. |
| B8 Observability/readiness | AC-B8-01 through AC-B8-04 | J7, J10, J11 | Local liveness/readiness separation, redaction, process/listener failure, and stale-presence tests. |
| B9 Legacy surfaces | AC-B9-01 through AC-B9-03 | J9-J11 | Modern-route inventory plus retired legacy route/static/launch containment tests. |

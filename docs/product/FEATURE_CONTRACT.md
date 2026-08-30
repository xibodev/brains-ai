<!--
last_verified: 2026-08-30T09:45:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 4e4819f02c621db5ceb75a13328a741208abdf42 plus Coordination mailbox UI/API candidate inspection and isolated Docker browser evidence; live notification and SMTP remain unimplemented; deployment not verified
-->

# Brains Feature Contract

## How to read this contract

Stable `F*`, `B*`, and `AC-*` identifiers survive lifecycle changes. A target criterion
does not by itself advertise a feature.

| Lifecycle | Meaning |
|---|---|
| Advertised | Supported in the normal installation. Evidence level is stated separately. |
| Active experiment | Implemented bounded field trial listed in [EXPERIMENTAL_BACKLOG.md](EXPERIMENTAL_BACKLOG.md). |
| Target-only | Future product contract with no current product surface. |
| Withdrawn | Known-faulty or retired implementation, unadvertised and non-activatable by contract. |
| Source compatibility | Code or data remains temporarily for existing stores or until BL-P0-09 removes exposure. Presence is not availability. |
| Missing | An advertised contract is absent from current HEAD. |

Evidence levels are defined in [QUALITY_GATES.md](../QUALITY_GATES.md). Source and test
presence is at most E1/E2 unless exact-candidate execution evidence says otherwise.

## Product invariants

1. Brains is the canonical product, repository, package, namespace, CLI, MCP, state,
   and browser identity.
2. Every protected operation resolves one principal and applies explicit Org/Workspace
   authorization.
3. Durable state commits before it is presented as recovered, delivered, or complete.
4. Human-gated actions do not execute before an attributable matching decision.
5. A durable event is not proof that Brains executed or delivered an external effect.
6. Health, readiness, test presence, local execution, isolated UAT, and deployment are
   different claims.
7. Acceptance requires Persona, Journey, `AC-*`, implementation, and evidence mappings.
8. The normal `/app` console is Workspace-first: Command Center, Workspaces,
   Coordination, Governance, Operations, and Act.
9. Withdrawn features have no supported flag, route, command, extra, tool allowlist, or
   direct-call activation. Remaining source/data is compatibility inventory only.
10. Only the field trials in the experimental backlog may be called active experiments.

## Core Brains features

### F0 - Console foundation and coherent product state

**Promise:** An operator can enter one authenticated Workspace-first console, navigate
stable supported surfaces, receive actionable errors, and launch only typed, truthfully
available actions.

**Lifecycle:** advertised/partial. Execution-model screens and redirects still present
in source are BL-P0-09 containment debt.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F0-01 | `/app` provides a stable authenticated shell and valid product start surface. | Advertised at E1/E2; `/app` starts at Command Center. |
| AC-F0-02 | Active Org/Workspace scope persists and every scoped screen applies it consistently. | Advertised/partial; Workspace aliases and deep links exist, while browser E4 remains open. |
| AC-F0-03 | API failures remain visible and distinct from empty data. | Advertised/partial; some screens still collapse failures into empty state. |
| AC-F0-04 | Persona Spawn creates an attributable execution Session. | Withdrawn target criterion; no supported Spawn path. |
| AC-F0-05 | Supported deep routes select the named entity or return non-disclosing not-found. | Advertised for Workspaces; withdrawn entity routes await removal. |

**Failure behavior:** Authentication failure leads to sign-in or a structured error.
Unknown/unauthorized entities never silently select another entity.

### F1 - Connect a machine and register Runtimes

**Promise:** Target contract for machine enrollment, tool discovery, Runtime registration,
and lifecycle state.

**Lifecycle:** withdrawn. Runtime routes, daemon, credentials, tables, UI, tests, and
activation controls at HEAD are source compatibility only.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F1-01 | Enrollment returns a complete command with hub identity and a one-time token. | Withdrawn/source compatibility. |
| AC-F1-02 | Redemption registers supported tools as scoped Runtimes with capabilities. | Withdrawn/source compatibility. |
| AC-F1-03 | Tokens are hash-only, expiring, and single-use under concurrency. | Withdrawn source retains defensive checks. |
| AC-F1-04 | UI states distinguish enrolling, waiting, connected, expired, and error. | Withdrawn UI. |
| AC-F1-05 | Runtime credentials authorize only intended machine/Org operations. | Withdrawn source retains defensive scope checks. |
| AC-F1-06 | Stale/offline Runtime state is maintained without an operator read. | Withdrawn target criterion; normal readiness must not depend on it. |

### F2 - Personas and capability binding

**Promise:** Target contract for reusable execution identities, compatible tool/model
binding, instructions, archive, and Spawn.

**Lifecycle:** withdrawn. Persona, managed Skill, and Spawn code/data is compatibility
inventory, not an authentication identity or user path.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F2-01 | Runtime selection constrains model/tool choices. | Withdrawn/source compatibility. |
| AC-F2-02 | Name, instructions, model, tool, color, and default Runtime persist. | Withdrawn/source compatibility. |
| AC-F2-03 | Invalid capability combinations fail with a visible explanation. | Withdrawn target criterion. |
| AC-F2-04 | Archive preserves historical Session attribution. | Withdrawn; compatibility rows remain. |
| AC-F2-05 | Spawn creates executable work or names the missing prerequisite. | Withdrawn; no supported Spawn path. |
| AC-F2-06 | Managed Skills attach with provenance. | Withdrawn; harness-native skills are outside this product model. |

### F3 - Coordination Sessions, events, and human control

**Promise:** Workspace-scoped coordination Sessions preserve lifecycle, events, asks,
decisions, ownership, and resume context. Unsupported execution steering is refused.

**Lifecycle:** advertised/partial. BL-P1-15 and BL-P1-16 add bounded active
experiments. BL-P1-20 is an implemented candidate blocked from experimental admission
until activation and worker transport are independently bounded. Running-agent
delivery, Runtime stop, and execution supervision are withdrawn.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F3-01 | Session events are durable and backfilled before realtime continuation. | Advertised at E1/E2/E3 for persisted event/replay paths; cross-process live fan-out remains open. |
| AC-F3-02 | Coordination Session state supports active, dormant, blocked, completed, and failed truthfully. | Advertised/partial; renewable PID-less leases and dormant transition exist, cross-harness end/detach E4 is open. |
| AC-F3-03 | Session actions update linked Issue state/comments with attribution. | Withdrawn with Project/Issue execution; Workspace task/handoff attribution remains under B2. |
| AC-F3-04 | Asks and approvals appear in Governance and resolve once with context. | Advertised/partial; human routing is separate from authorization and complete publish E4 remains open. |
| AC-F3-05 | Chat is durable, authorized, delivered to the running agent, and recoverable. | Running-agent delivery withdrawn; durable mailbox/topics remain B2 capabilities. |
| AC-F3-06 | Stop is authorized, durable, delivered to the Runtime, and reconciled. | Withdrawn with Runtime execution; coordination Session end is separate. |
| AC-F3-07 | Realtime subscriptions are principal-, Org-, Workspace-, and entity-authorized. | Advertised at E1/E2; closed topics, replay, and revalidation exist, browser reconnect E4 is open. |

**Failure behavior:** Reconnect uses scoped durable state. Unsupported process delivery
or stop is refused rather than queued or reported as successful.

### F4 - Projects, Issues, assignment, and dispatch

**Promise:** Target contract for structured Projects/Issues, assignment, dispatch, and
execution evidence.

**Lifecycle:** withdrawn. Current APIs, UI, controls, tests, and rows are compatibility
inventory only.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F4-01 | Projects/Issues have stable codes, state, priority, and comments. | Withdrawn/source compatibility. |
| AC-F4-02 | Exactly one human, Persona, or Pod assignment target is represented and validated. | Withdrawn/source compatibility. |
| AC-F4-03 | Dispatch validates assignment/Runtime, creates a Session, and advances work. | Withdrawn; no supported dispatch. |
| AC-F4-04 | Issue detail reconciles durable Session/event history and comments. | Withdrawn/source compatibility. |
| AC-F4-05 | Token/cost/event rollup is Issue-scoped and reconciled. | Withdrawn/source compatibility. |
| AC-F4-06 | Pod assignment resolves through documented deterministic behavior. | Withdrawn. |
| AC-F4-07 | Natural-language Issue creation is confirmed or excluded. | Withdrawn. |

### F5 - Pods

**Promise:** Target contract for a Persona team, leader, roster, assignment, and routing.

**Lifecycle:** withdrawn. Pod and legacy Squad rows remain only where persisted-data
compatibility requires them.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F5-01 | Pod create/read/update/archive exists in the modern console. | Withdrawn UI/source compatibility. |
| AC-F5-02 | Pod membership uses execution Personas. | Withdrawn. |
| AC-F5-03 | Leader/member changes preserve one valid leader. | Withdrawn source retains defensive validation. |
| AC-F5-04 | Issue-to-Pod dispatch is deterministic and visible. | Withdrawn; no supported dispatch. |

### F6 - First-run execution onboarding

**Promise:** Target contract for moving from sign-in through machine, Persona, work, and
one supervised execution result.

**Lifecycle:** withdrawn. The advertised first run opens Command Center and an
authorized Workspace; it does not expose execution-model onboarding.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F6-01 | Fresh state offers the correct onboarding path. | Advertised interpretation is Workspace-first entry; execution onboarding withdrawn. |
| AC-F6-02 | Execution onboarding composes Org, machine, Persona, work, and dispatch. | Withdrawn/source compatibility. |
| AC-F6-03 | Every execution-onboarding step has complete UX/recovery states. | Withdrawn target criterion. |
| AC-F6-04 | Completion lands on attributable execution or a clear blocker. | Withdrawn target criterion. |
| AC-F6-05 | Clean-state browser evidence proves the flow without seeded success. | Current need is containment plus Workspace-first J1 evidence. |

### F7 - Supported configuration truth

**Promise:** Operations presents redacted effective state and permits only approved
non-secret/encrypted writes with explicit reload or restart behavior.

**Lifecycle:** advertised/partial for service, MCP, email, secrets, and GitHub posture.
Gateway/provider, Runtime, automation, bridge, Postgres, telemetry, and legacy-admin
activation are withdrawn.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F7-01 | Supported connectivity probes return bounded success/failure without leaking secrets. | Advertised/partial; external GitHub E4 is absent and provider probes are withdrawn. |
| AC-F7-02 | Effective supported service/MCP/integration/secret state is truthful and redacted. | Advertised/partial; withdrawn fields still require BL-P0-09 containment. |
| AC-F7-03 | UI distinguishes read-only information from approved writes. | Advertised at E1/E2 for current typed writes; browser E4 remains open. |
| AC-F7-04 | Multi-process reload/restart semantics are documented and verified before writes are promised. | Advertised/partial; handling-process reload exists for some settings, full convergence requires restart/probe. |

### F8 - GitHub linkage

**Promise:** Authenticated GitHub events are accepted only when signature, delivery
identity, and repository scope satisfy the configured contract. Outbound public defect
creation additionally requires approval of the exact payload.

**Lifecycle:** advertised/partial for signed ingress. BL-P1-19 is active implementation
work, not yet an experiment or available outbound feature.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F8-01 | A pull-request reference can link to attributable Brains work. | Present at E1/E2/E3 for historical Issue linkage; Workspace-first destination needs BL-P1-19/containment design. |
| AC-F8-02 | A verified merged event updates linked state idempotently. | Present at E1/E2/E3 in source; external operation unverified. |
| AC-F8-03 | Webhook authentication validates GitHub signature and exact repository scope. | Present at E1/E2/E3 in source. |
| AC-F8-04 | Configuration/failure state is visible without credentials or repository disclosure. | Present at E1/E2/E3 in redacted posture; browser/external E4 absent. |

### F9 - Orgs, members, roles, and usage

**Promise:** Org owners administer membership and roles; operators see only authorized
Org/Workspace state and explicitly scoped usage.

**Lifecycle:** advertised/partial.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F9-01 | Org create/read/update and active-Org switching are durable. | Advertised at E1/E2. |
| AC-F9-02 | Owners/admins manage members without role escalation or last-owner loss. | Advertised at E1/E2/E3 for API paths. |
| AC-F9-03 | HTTP reads/writes enforce principal plus Org/Workspace scope. | Advertised at E1/E2/E3 for covered routes; withdrawn routes remain containment debt. |
| AC-F9-04 | Usage totals identify scope and exclude unauthorized/unattributed data. | Advertised/partial; Org-scoped SQL attribution exists, BL-P1-16 experiments remain separate. |
| AC-F9-05 | Two-user/two-Org denial covers native APIs and browser sessions. | Partial; API/cookie tests exist, browser E4 is absent. |

### F10 - Autopilots and managed Skills

**Promise:** Target contract for reusable managed instructions and recurring work under
the same authorization/governance boundary as manual work.

**Lifecycle:** withdrawn. Automation UI, managed Skills, recurring definitions,
generic webhooks, and scheduled/manual fire are non-activatable compatibility inventory.

| Acceptance criterion | Target contract | Current disposition |
|---|---|---|
| AC-F10-01 | Autopilot CRUD/enable/fire is Org-scoped. | Withdrawn source retains defensive authorization. |
| AC-F10-02 | Supported schedules use a bounded validated grammar. | Withdrawn/source compatibility. |
| AC-F10-03 | Scheduled/manual fire creates durable task/run/audit records. | Withdrawn; no supported fire path. |
| AC-F10-04 | Recurring execution uses the enforceable approval/execution gate. | Withdrawn target criterion. |
| AC-F10-05 | Managed Skills attach to Personas/Projects with provenance and enter Session context. | Withdrawn/source compatibility. |
| AC-F10-06 | Duplicate, disabled, unauthorized, or failed fires recover safely. | Withdrawn target criterion. |

## Supporting Brains capabilities

### B1 - Model gateway and faithful routing

**Promise:** Target contract for exact model intent and explicit policy routing.

**Lifecycle:** withdrawn. Model proxy routes, aliases, provider adapters, router,
LiteLLM, catalog, usage routing, and `brains-ai run` are source compatibility only.

- AC-B1-01: explicit model IDs resolve faithfully or return model-not-found.
- AC-B1-02: only explicit policy-routing requests may invoke classification.
- AC-B1-03: streaming/non-streaming responses identify the actual upstream model.
- AC-B1-04: auth, redaction, bounded errors, usage, retry, and circuit policy are consistent.

### B2 - Coordination plane and MCP

**Promise:** Agents share coordination Sessions, tasks, claims, handoffs, messages,
topics, peer help, decisions, knowledge, patterns, tool records, and checkpoints through
stable scoped MCP/CLI/browser surfaces.

**Lifecycle:** advertised/partial, with BL-P1-15 as an active field trial and BL-P1-20
blocked from experimental admission.

- AC-B2-01: Session start returns current context, presence, and ownership signals.
- AC-B2-02: claims/task transitions are atomic and expire or release predictably.
- AC-B2-03: messages, handoffs, checkpoints, successors, and resume preserve continuity.
- AC-B2-04: mutation tools authenticate, scope, and human-gate where required.

Current E1/E2/E3 source includes renewable PID-less leases, dormant expiry,
successor transfer, interest-scoped topics, asynchronous peer help, sequential retry
dedupe, Workspace browser adapters, feedback reporting, fenced ephemeral review, and
durable mailbox identity/attachment plus address-based local delivery. Mailbox
registration validates supported canonical
tools and native IDs, stores only a unique versioned binding hash, provisions operator
inboxes, authorizes phonebook/lookup by Workspace visibility, and makes start/reuse,
heartbeat, resume, tool linking, successor transfer, and terminal detach proof-bound and
transactional once a Session has mailbox history. Legacy link rows never fabricate an
address. Direct/offline delivery, explicit Workspace broadcast, Inbox/Sent,
per-recipient read state, filtered thread timelines, reply/forward provenance,
idempotent operation IDs, and cursor continuity now use migration 150 rows. Agent
operations require current attachment plus binding; operator-inbox reads require a
browser/local human channel. Cross-harness native-ID extraction, live notification,
SMTP, recovery, real-provider review, and broad per-tool authorization remain open. The
Coordination browser mailbox desk now exposes authorized mailbox selection, Inbox/Sent,
explicit read, participant-filtered threads, operator compose/reply/forward, delivery
state, address-book selection, agent deep links, and responsive keyboard operation.

Migration 150 and the current control/adapters implement the BL-P1-12/BL-P1-14 identity
foundation: hash-bound mailbox identity, one-current-incarnation attachment and cursor,
operator inbox provisioning, authorized phonebook/lookup, and non-enumerating conflict
refusal. The threaded message and per-recipient delivery/read rows are now active for
durable local mail. Notification, per-operator SMTP setting/outbox, and unverified
legacy-inventory state remain reserved; wakeup, SMTP, rotation/recovery, and
two-real-harness acceptance remain missing until later slices land.

### B3 - Workspace knowledge and repository lookup

**Promise:** Agents retrieve bounded, attributable Workspace knowledge and repository
matches without treating generated context as authority.

**Lifecycle:** advertised for knowledge and non-semantic text/symbol lookup. Semantic
indexing/search, embeddings, graph, and external freshness are withdrawn.

- AC-B3-01: any approved indexing is bounded, ignore-aware, and content-hash based.
- AC-B3-02: semantic search reports unavailable dependencies and never fabricates matches.
- AC-B3-03: graph queries identify language/indexing limits.
- AC-B3-04: external freshness checks apply allowlists and SSRF protection.

AC-B3-02 through AC-B3-04 remain target/containment criteria for withdrawn code.
BL-P1-18 owns normal-install local lookup and truthful unavailable-versus-empty results.

### B4 - Human governance and audit

**Promise:** Human decisions precede governed consequential actions, and the resulting
record is attributable and tamper-evident without overstating external harness effects.

**Lifecycle:** advertised/partial.

- AC-B4-01: approval-required actions fail closed until a matching decision exists.
- AC-B4-02: every advertised governed execution path shares the declared boundary.
- AC-B4-03: audit append is transactional with governed state and safe across processes.
- AC-B4-04: verification detects mutation, deletion, insertion, and truncation under the key model.

The source has redacted argument digests, single-use scoped approvals, idempotency,
attempt/execution leases, signed single-writer audit head, and human-only routing. The
boundary remains cooperative/in-process and cannot contain direct external harness
commands or sockets.

### B5 - SQLite storage, migrations, backup, and recovery

**Promise:** Supported SQLite state evolves consistently, applies its declared integrity
policy, and can be backed up, verified, restored, and rolled back safely.

**Lifecycle:** advertised/partial for SQLite. Postgres is withdrawn compatibility.

- AC-B5-01: fresh and upgraded databases reach the same supported schema.
- AC-B5-02: SQLite foreign keys and concurrency settings are explicit and verified.
- AC-B5-03: a future alternate backend must execute equivalent migrations rather than record skipped work as applied.
- AC-B5-04: backup/restore validate manifest, hash, compatibility, and target ownership.
- AC-B5-05: recovery declares schedule, retention, encryption, RTO/RPO, and isolated drills.

Ordered checksummed migrations, WAL/busy timeout, manifest backup, isolated
verification, schema-compatibility refusal, dry-run-first repair, and redacted recovery
policy exist. FK default enforcement and exact-candidate E4 remain open. Brains does not
run a backup scheduler.

Migration 150 has equivalent SQLite and PostgreSQL DDL so an existing compatibility
store never records skipped work as applied. The supported product remains SQLite;
the PostgreSQL delta is archive/store compatibility, not reactivation of that backend.

### B6 - CLI, wiring, and service management

**Promise:** `brains-ai` initializes, runs, wires, inspects, coordinates, backs up, and
manages the local supported service without mutating unrelated configuration.

**Lifecycle:** advertised/partial.

- AC-B6-01: `brains-ai` is the sole installed Brains executable.
- AC-B6-02: wiring preserves unmanaged configuration and supports status/unwire.
- AC-B6-03: service commands render/manage user-level services on supported OSes.
- AC-B6-04: help/docs do not advertise removed or withdrawn commands.

Wire adapters for Copilot, Claude, Codex, and OpenCode preserve unrelated settings and
ownership. Service installation verifies the exact interpreter, refuses conflicting
gateway/MCP ports, and either refuses an unavailable explicit gateway port or persists
a bindable fallback. The supervisor preflights every enabled listener on its actual bind
host, holds a bounded degraded state while a bind is blocked, then exits with code 3;
the systemd unit suppresses restart for that configuration exit. Service status combines
PID identity with bounded endpoint probes. Windowless Windows operation, child-protocol
readiness, and clean-host E4 remain active backlog work.

### B7 - Authenticated external events

**Promise:** Advertised external events use explicit credentials, bounded scope,
deduplication, privacy controls, and visible failure states.

**Lifecycle:** advertised/partial for signed GitHub ingress. BL-P1-19 is active work for
an exact human-approved outbound public defect payload. Generic triggers, relay,
Telegram, Slack, WhatsApp, and WhatsApp Web are withdrawn.

- AC-B7-01: ingress and any future relay reject absent/invalid credentials.
- AC-B7-02: delivery is idempotent where an external event ID exists.
- AC-B7-03: credentials are redacted and unavailable dependencies fail closed.
- AC-B7-04: third-party terms, disclosure, and companion risks are explicit.

### B8 - Observability, health, and readiness

**Promise:** Operators distinguish process liveness, supported-feature readiness,
dependency failure, stale coordination state, and recovery posture.

**Lifecycle:** advertised/partial, with BL-P1-16 as an observational field trial.

- AC-B8-01: `/health` remains an open liveness/inventory endpoint.
- AC-B8-02: readiness checks supported dependencies, storage writes/migrations, service children, scheduler, wiring, and recovery.
- AC-B8-03: logs/metrics are redacted and identify process boundaries.
- AC-B8-04: multi-process failures and stale coordination presence are observable.

Protected readiness currently reports bounded storage/migration, queue,
Runtime-compatibility, and recovery policy. Child listener/protocol health, scheduler
progress, registry/package/schema convergence, supported wire transport, and
cross-process failure remain open. Withdrawn Runtime/provider/Postgres state must not
degrade normal readiness after containment.

### B9 - Retired legacy browser surfaces

**Promise:** Users encounter one deliberate modern browser contract and no retired
workflow can contradict or bypass it.

**Lifecycle:** withdrawn for dashboard and legacy admin HTML. `/app` remains advertised.

- AC-B9-01: `/app`, `/dashboard`, and `/admin` responsibilities/retirement are explicit.
- AC-B9-02: authentication/authorization remain consistent until retired mounts are removed.
- AC-B9-03: duplicate or unsupported workflows are unreachable and their assets/launch paths are removed.

Remaining routes, templates, static assets, flags, and commands are BL-P0-09/BL-P2-01
containment debt. Shared authentication code is not an activation contract.

<!--
last_verified: 2026-08-29T11:25:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 2630f04e31ca47ff93eda1e2b616b3e657b0c877 plus the approved feature lifecycle and withdrawal decisions; implementation not changed; deployment not verified
-->

# Brains Active Feature Backlog

## Delivery Contract

This backlog contains only normal-install product features and cross-cutting
foundations eligible for implementation. Each delivery slice:

1. starts from current `staging` on one short-lived feature branch;
2. names one feature outcome, stable IDs, non-goals, and acceptance evidence;
3. includes code, tests, configuration contract, docs, and traceability together;
4. merges to `staging` only after its own required gates;
5. reaches `main` only through promotion of an exact integrated staging candidate.

Withdrawn implementations cannot receive feature branches. Their only active work is
BL-P0-09 containment, approved removal, shared-data compatibility, or separately
isolated replacement research.

## Product Features

### Installation, Service, and Wiring

**Outcome:** A user installs Brains once, starts it without a visible console, and each
supported harness reaches one healthy shared coordination service.

**Owned items:** BL-P0-06, BL-P1-09, BL-P1-10, BL-P1-18.

**Open requirements:** The Windows user service must be windowless and preserve user
HOME/OAuth access; child health must be listener/protocol-aware rather than PID-only;
an alive-but-unserving child must be fenced and restarted with bounded backoff; wiring
must select a transport the harness actually supports and readiness can prove; package
build, migration corpus, database schema, endpoint configuration, and wire metadata
must be compatible; restart and rollback must preserve the invariant.

**Evidence:** E3 hung-listener, transport mismatch, package/schema/wiring identity,
restart, and config-preservation tests; E4 clean-host Windows plus Linux/macOS service,
wire, restart, persistence, and rollback journeys.

### Workspace-First Console

**Outcome:** The normal console exposes only Command Center, Workspaces, Coordination,
Governance, Operations, Access/Configuration, and Act with truthful capabilities.

**Owned items:** BL-P3-01.

**Open requirements:** Normal deep links select the named Workspace or a non-disclosing
not-found state; API errors remain distinct from empty data; keyboard, focus, labels,
contrast, responsive behavior, and connection degradation are blocking contracts; no
route redirects users into a withdrawn execution-model screen.

**Evidence:** E3 route/component/accessibility failures; E4 J1/J7-J11 normal-route sweep
with no Labs flag.

### Workspace Portfolio and Presence

**Outcome:** Presence, ownership, and continuity reflect agents that are actually live.

**Owned items:** BL-P1-14.

**Open requirements:** Canonical tool identity retains raw adapter provenance;
supported harnesses renew while active and end or detach when their process finishes;
expired PID-less Sessions become dormant without an operator read, disappear from live
projections, and transactionally release claims/tasks; replacement handles transfer
owned continuity once; machine restart leaves no permanently running stale handles.

**Evidence:** E3 alias, end/detach, lease, false-reap, successor, predecessor-mail, and
ownership-release tests; E4 multi-hour, abrupt-exit, and restart journeys across
Copilot, Claude, Codex, and OpenCode.

### Workspace Coordination and Sessions

**Outcome:** Agents share durable work and resume context without collisions or false
execution claims.

**Owned items:** BL-P0-05.

**Open requirements:** Coordination Sessions, tasks, claims, handoffs, checkpoints,
resume, terminal state, and Workspace scope are durable and idempotent. Unsupported
steering remains an explicit refusal. Running-agent message delivery is withdrawn and
must not be implied by durable mailbox or command persistence.

**Evidence:** E3 lifecycle, concurrent ownership, duplicate, reload, and refusal tests;
E4 two-harness interruption and resume journey.

### Agent Communications

**Outcome:** Mail, topics, handoffs, and peer help reach an eligible consumer or expose
a recoverable undelivered state.

**Owned items:** BL-P1-12.

**Open requirements:** Default guidance uses asynchronous help: file returns
immediately, waits leave requests open, fresh eligible peers claim/release/answer with
evidence, requesters cancel, and inbox wakeup reports the terminal result. Brief
connectivity probes remain distinct from review deadlines. Empty reads do not count as
consumption; explicit handoff pickup, passive welcome consumption, successor delivery,
and undelivered mail are distinct outcomes. Queue repair handles stale Sessions,
claims, messages, handoffs, requests, aliases, and missing roots without deleting
unresolved work.

**Evidence:** E3 asynchronous lifecycle, deadline, consumer, cursor, passive
consumption, and repair tests; E4 two-real-harness help/mail/handoff/recovery journey.

### Knowledge and Coordination Patterns

**Outcome:** Agents discover reusable coordination guidance and can explain whether it
was applicable and used.

**Owned items:** BL-P1-17.

**Open requirements:** Approved coordination patterns are matched against bounded task
intent, carry source/version and an offer/omit reason, and produce privacy-safe
`used`, `declined`, `unavailable`, or `not_applicable` receipts. Prescribed
harness-native workflows such as release or instruction-rule review are observable;
withdrawn Persona/Project managed Skills are not part of this feature.

**Evidence:** E3 matching, omission, receipt, privacy, and four-harness parity tests;
E4 task-class matrix without forcing irrelevant guidance.

### Human Governance

**Outcome:** Consequential actions remain human-authorized and evidence distinguishes
what Brains governed from what an external harness merely reported.

**Owned items:** BL-P0-01, BL-P0-03, BL-P0-04.

**Open requirements:** Every protected surface resolves one principal and explicit
Org/Workspace capability. Effects are classified as `governed`,
`externally_observed`, or `unverified_claim`; a free-form event cannot imply governed
execution. Approval/action/result and audit entries correlate or fail closed. The
process/network boundary covers approved execution shapes, and residual out-of-band
paths are stated rather than hidden.

**Evidence:** E3 two-Org deny matrix, bypass, redaction, event-correlation, and audit
race/tamper tests; E4 denied, approved, and out-of-band effect journey.

### Operations, Readiness, and Recovery

**Outcome:** Operators distinguish process liveness, feature readiness, dependency
failure, and recovery state.

**Owned items:** BL-P1-09.

**Open requirements:** Readiness includes required child protocols/listeners, scheduler
progress, configured wire transports, registry freshness, package/migration/schema
identity, coordination queues, SQLite integrity, and declared recovery policy. It
names affected capability without raw exceptions or secrets. Backup scope, retention,
encryption ownership, RTO/RPO, compatibility, compaction, restore drill, and rollback
order are explicit.

**Evidence:** E3 child, scheduler, transport, schema, registry, backup-compatibility,
and stale-state tests; E4 service failure/recovery plus isolated backup/restore/rollback.

### Access, Usage, and Configuration

**Outcome:** Owners manage authorized Org access and operators see truthful supported
configuration and usage scope.

**Owned items:** BL-P1-05, BL-P1-07.

**Open requirements:** Owner/admin/member boundaries and no-owner-loss are enforced;
usage is scope-labelled and excludes unauthorized/unattributed calls; supported
encrypted/non-secret writes state restart semantics; withdrawn gateway, Runtime,
automation, bridge, Postgres, and telemetry settings are not presented as activatable.

**Evidence:** E3 role, two-Org, usage, redaction, write/reload/rollback, and withdrawn
configuration tests; E4 multi-user J9/J10.

### GitHub Linkage

**Outcome:** Signed GitHub events link public development activity to Brains work
without trusting replayed or out-of-scope deliveries.

**Owned items:** BL-P1-06.

**Open requirements:** HMAC, delivery/event headers, repository-to-Org binding,
idempotency, failure visibility, and durable local linkage are controlled and
attributable. Existing Issue transition code is compatibility behavior, not a normal
Issue UI claim; the Workspace-first destination must be explicit before containment
removes that dependency. Messaging bridges are withdrawn and do not share this backlog.

**Evidence:** E3 signed/invalid/replayed/out-of-scope tests plus no withdrawn-surface
dependency; E4 controlled GitHub event and Workspace-first browser reconciliation.

### Stable Local Lookup

**Outcome:** Default wire guidance recommends only lookup capabilities available in a
normal installation.

**Owned items:** BL-P1-18.

**Open requirements:** Workspace knowledge and bounded substring/symbol search work
without embeddings; lookup distinguishes empty from unavailable; wire instructions
remove semantic and graph claims; no indexing or embedding setup is required by the
normal coordination workflow.

**Evidence:** E3 clean-state knowledge/substring lookup, unavailable-versus-empty, and
wire-copy tests; E4 one fresh Workspace lookup journey.

### Community Defect Relay

**Outcome:** Local defect evidence can become a privacy-safe public GitHub issue only
after a human approves the exact outgoing payload.

**Owned items:** BL-P1-19.

**Open requirements:** Cluster and deduplicate local feedback/usage defects; redact
secrets, customer data, prompts, source, logs, identities, hostnames, and local paths;
search existing public issues before proposing; show exact title/body/metadata;
support discard, link-existing, or create; rate-limit per install and feature/build;
execute create/comment through the governed GitHub path; persist local fingerprint and
public link. No background upload or automatic issue creation.

**Evidence:** E3 redaction, dedupe, existing-issue, preview, separation-of-duty,
governed-effect, retry, and rate-limit tests; E4 local proposal through approved public
issue in a disposable test repository. After safe implementation, field observation
moves to the experimental backlog.

## Cross-Cutting Foundations

### Security, Identity, and Human Authority

**Owned items:** BL-P0-01, BL-P0-03, BL-P0-04.

Close route-level identity/RBAC evidence, process/network enforcement, governed-effect
correlation, audit integrity, and public-relay privacy before features depending on
them graduate.

### Realtime and Distributed Consistency

**Owned items:** BL-P0-02.

Cross-process live fan-out, publisher idempotency keys, scoped retention/gap detection,
and browser disconnect/reconnect evidence remain open. Durable replay is not a claim of
live delivery.

### Storage, Migrations, and Recovery

**Owned items:** BL-P0-04, BL-P0-07, BL-P0-08, BL-P1-09.

Enable SQLite foreign-key enforcement only after backup-backed repair of real stores;
preserve checksummed migration and archive compatibility; do not maintain Postgres as
a supported backend while it is withdrawn.

### Quality, Release, and Traceability

**Owned items:** BL-P1-01, BL-P1-10, BL-P2-02.

Reassess BL-P1-01 against the latest exact hosted candidate evidence. Generate
CLI/MCP/alias/wire inventories in addition to routes, entities, migrations, tests, and
AC references. Store immutable run evidence outside canonical docs. Every feature
branch targets `staging`; only an exact integrated staging candidate is promoted.

### Frozen Capability Containment

**Owned items:** BL-P0-09, BL-P2-01.

Withdraw all current opt-in claims and activation paths for Runtime execution,
Personas, Projects/Issues, Pods, execution onboarding and Session supervision,
Automation/managed Skills, semantic retrieval, code graph, running-agent delivery,
scheduled auto-fire, model gateway/LiteLLM, Postgres, OpenTelemetry, Telegram, Slack,
WhatsApp/WhatsApp Web, and legacy browser HTML. Remove them from README, default CLI
help, default MCP, wire rules, normal/Labs navigation, feature wizard, examples, and
operator activation guidance. Preserve only shared-data compatibility needed to open
existing stores. Remove isolated source in separately reviewed slices; no replacement
implementation belongs in the containment branch.

**Evidence:** Generated advertisement inventory proves zero withdrawn discovery or
activation paths; direct calls fail closed; default install and upgrade preserve stable
data; retired legacy HTML is unreachable; clean-host normal UAT runs with no withdrawn
flag or optional dependency.

### Maintainability

**Owned items:** BL-P2-03.

Remove high-risk import cycles or isolate them behind explicit interfaces, especially
where frozen modules remain imported by the stable service. Removal must not introduce
compatibility scaffolding without persisted-data or external-consumer need.

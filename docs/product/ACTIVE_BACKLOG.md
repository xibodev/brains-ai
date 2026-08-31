<!--
last_verified: 2026-08-30T22:45:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD eedab318896d87fa9520f92736e42445383b2c6f plus mailbox-readiness and privacy-safe analytics candidate inspection and isolated Docker lint, type, suppression, lifecycle, API, and packaged browser evidence; real field outcomes and deployment not verified
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

**Implemented foundation:** Canonical supported-tool normalization, validated native-ID
registration, unique versioned hash-only binding, one current attachment and cursor,
operator inbox provisioning, proof-bound start/reuse/heartbeat/resume/tool-link/successor
transitions, and transactional detach on terminal/dormant paths are implemented at E3.
Legacy link values such as `current` remain inventory only and cannot create mailboxes.

**Open requirements:** Canonical tool identity retains raw adapter provenance;
supported harness adapters extract their actual native IDs, renew while active, and end
or detach when their process finishes;
expired PID-less Sessions become dormant without an operator read, disappear from live
projections, and transactionally release claims/tasks; replacement handles transfer
owned continuity once; machine restart leaves no permanently running stale handles.
Identity, activity, reachability, and Workspace ownership are separate signals. A
durable agent mailbox is keyed by canonical Workspace, canonical tool, and validated
tool-native Session ID; its current `ses_*` incarnation may attach, detach, and resume
without changing the address or moving stored mail. Meaningful artefacts contribute
`last_active_at`, while a current lease/process/transport determines reachability. An
artefact timestamp, Workspace claim, or `state='running'` alone never proves that an
agent is reachable. Concurrent or conflicting address attachments fail closed and are
visible to the operator.

**Evidence:** E3 real-native-ID validation, alias, address registration/reattachment,
conflicting attachment, activity-versus-reachability, end/detach, lease, false-reap,
successor, predecessor-mail, and ownership-release tests; E4 multi-hour, idle,
abrupt-exit, restart, Workspace movement, and resume journeys across Copilot, Claude,
Codex, and OpenCode.

### Workspace Coordination and Sessions

**Outcome:** Agents share durable work and resume context without collisions or false
execution claims.

**Owned items:** BL-P0-05.

**Implemented foundation:** Mailbox-aware Session start/reuse, heartbeat, resume, and
successor operations return the address, unread count, and per-incarnation cursor and
roll back lifecycle transfer on failed binding proof. Legacy mailbox-less Session calls
remain compatible; once mailbox history exists, `ses_*` alone cannot reactivate it.

**Open requirements:** Coordination Sessions, tasks, claims, handoffs, checkpoints,
resume, terminal state, and Workspace scope are durable and idempotent. Unsupported
steering remains an explicit refusal. Running-agent message delivery is withdrawn and
must not be implied by durable mailbox or command persistence. The ephemeral `ses_*`
row is one current incarnation, not a durable mail recipient. Session start/resume
idempotently opens or finds the address
`(workspace_id, tool, native_tool_session_id)`, attaches the current incarnation, and
returns the mailbox address, unread count, and cursor. Successor/resume preserves the
durable address and mailbox history without copying messages between Session IDs.

**Evidence:** E3 lifecycle, concurrent ownership, duplicate, reload, and refusal tests;
E4 two-harness interruption and resume journey.

### Agent Communications and Durable Mailboxes

**Outcome:** Agents and operators have durable, authorized Brains mailboxes. Mail,
threads, topics, handoffs, and peer help survive agent restarts and expose truthful
delivery, read, and notification state.

**Owned items:** BL-P1-12.

**Delivery dependency:** Address registration, local direct/offline delivery, explicit
Workspace broadcast, Inbox/Sent, thread/reply/forward, per-recipient read state, cursors,
and the Coordination mailbox desk are implemented. A body-free adapter notification
protocol and one-way operator SMTP copy are implemented, but concrete harness hook/plugin
installation, adapter-native ID extraction, recovery, real-provider SMTP, and
two-real-harness E4 remain open. Until those
slices pass, do not rely on Brains mail as the sole carrier of parallel-work ownership,
requirements, approval, or handoff.

**Address and registration:** An agent address is
`tool:native-tool-session-id@workspace-slug`, backed by the unique canonical key
`(workspace_id, tool, native_tool_session_id)`. `workspace_path` is registration input
resolved through Workspace aliases; a phonebook reader with administrative visibility
may display the resolved path, but no full local path appears in the address. Each
supported harness supplies its actual native Session ID and idempotently opens or finds
its mailbox once; values
such as `current`, model names, task labels, or guessed IDs are rejected. Existing
invalid/ambiguous links are diagnosed and quarantined rather than fabricated into valid
addresses. A native ID is an address component, not an authentication principal. First
registration persists the authenticated address owner and a non-exported reattachment
binding established by the harness adapter. Every later find/attach must prove that
binding as well as current Workspace authorization; knowing a detached mailbox address
or native ID is never sufficient to attach an incarnation or read mail. Rotation,
revocation, loss, and conflicting ownership fail closed and remain recoverable by an
explicit local-human administrative flow.

Current E3 implements registration, reattachment, operator inbox provisioning,
visibility-filtered phonebook/lookup, fixed unavailable refusal, binding-file/header
adapters, and lifecycle proof/detach. Per-adapter native-ID extraction plus binding
rotation, revocation/loss diagnosis, and local-human recovery remain open.

**Delivery and authorization:** A message commits directly to a registered durable
mailbox, whether its agent is online or offline; current-Session resolution is used only
for wakeup and read attribution. Unknown, retired, conflicting, or unauthorized
addresses are rejected without enumeration. Cross-Workspace send, forward, phonebook,
and mailbox inspection authorize the sender/reader against the originating Workspace
and every recipient or represented mailbox Workspace. A sender authorized only in
Workspace A cannot address or discover a mailbox in Workspace B. Broadcast is an
explicit operation and never the accidental meaning of a null recipient. Local
acceptance, agent notification, reading, and SMTP copy are separate states so a
send/end race cannot fabricate delivery.

Current E3 commits direct and explicit-broadcast messages under sender-scoped operation
IDs, accepts mail for detached/offline active addresses, returns filtered Inbox/Sent and
thread timelines, records per-recipient reads, retains reply/forward provenance, and
preserves the delivery cursor across incarnations. Agent actors prove the current
attachment and binding; human operator-mailbox reads require a browser/local channel.
Count-only observability now reports registration, attachment, delivery/read,
notification, and SMTP state without exposing address, content, path, native Session ID,
or native mailbox object IDs. Outcome analytics uses right-censored windows and
minimum-group suppression across registration, acceptance/refusal, wakeup, read, reply,
forward, broadcast, and SMTP families.
Local acceptance creates a notification attempt only when the current attachment has
explicitly declared a supported stronger mode. Pull-only and detached recipients create
no attempt. A verified operator-mailbox copy policy may enqueue separate SMTP state in
the same transaction, but acceptance never implies wakeup or external send success.

**Notification adapters:** Durable mail remains authoritative. A current proof-bound
attachment may declare `immediate` for Claude Code/OpenCode or `turn_boundary` for
Codex; Copilot CLI is pull-only. Brains then creates one idempotent body-free attempt per
delivery and incarnation. The adapter claims it through MCP/CLI, receives only the fixed
nudge `Brains mailbox: new mail is waiting. Pull your durable inbox.`, and settles the
observed result as `delivered` or `failed`. Subject, body, sender, recipient, and delivery
identity never cross that notification boundary. Detach, mode change, or an authoritative
read settles stale work without changing local delivery. Failure always falls back to
proof-bound inbox pull.

Current M5 evidence covers this secure adapter-facing protocol and reports `pull` from
`wire` for every harness because wiring does not yet install a notification hook/plugin.
It does not prove that a running external model was interrupted, prompted, or awakened.
Concrete hook/plugin assets, explicit installation/consent, and real-harness E4 remain
open; no follower daemon, shell relay, or model-input channel is part of this slice.

**Threads and browser:** Messages retain a durable sender mailbox, point-in-time sender
Session, durable recipients, originating Workspace, `thread_id`, `in_reply_to`, and
forward provenance. Coordination provides an authorized mailbox selector, Inbox, Sent,
thread timeline, unread state, compose, reply, forward, delivery/read state, address
book, and a deep link from an agent to its mailbox. The first release does not require
attachments, HTML mail, drafts, spam, folders, rules, or a bespoke top-level mail app.

Current browser E4 provides a human-bound mailbox selector, Inbox/Sent, explicit unread
and read actions, a participant-filtered thread timeline, operator compose/reply/forward,
per-recipient accepted/read state, authorized address book, non-enumerating unknown deep
links, and agent-mailbox links from Workspace presence. Agent mailboxes remain read-only
in the browser because sending as an agent requires adapter-held binding proof. The
normal route is responsive and keyboard reachable; isolated Docker UAT runs with no
published host port and synthetic state only.

**Operator and SMTP copy:** Every authenticated operator has a separate durable Brains
mailbox at `operator:slug@brains`. Agents can address it within authorized scope.
Ordinary members may discover addresses in Workspaces they can read but may open only
their own agent-visible addresses. Operator-mailbox reads, configuration, and SMTP
consent require a browser or local human-bound principal; a raw operator API key or
agent channel is send-only to an operator address and cannot read that human inbox. The
install administrator or an Org owner/admin using a human-bound channel and holding
visibility of every represented Workspace may inspect an agent mailbox. Optional SMTP is a one-way,
post-commit copy from the operator's local Brains inbox to the configured real email:
local mail remains authoritative, SMTP uses a durable retryable outbox, copy failure
does not change local delivery, and the external message directs the operator to reply
inside Brains. SMTP notification-only content is the default; copying the full body is
an explicit operator opt-in. The verified SMTP destination and body-copy consent bind
to that specific operator mailbox; the existing install-wide notification address is
not reused as another operator's destination. Each operator can configure only their own
mailbox through a browser/local human channel. The destination is encrypted, challenged
by a short-lived verification code, and exposed only as a masked hint. Verification
enables `notification` mode, whose subject/body are constant and contain no local mail
metadata or content. `full_body` requires a separate explicit consent action and
timestamp. Changing mode or destination cancels copies that have not started; a live
SMTP claim fences those changes until it settles.

The bounded scheduler claims outbox rows once, retries only failures known to occur
before SMTP acceptance, uses a stable per-outbox `Message-ID`, and marks send-stage or
expired-claim outcomes `uncertain` rather than risking duplicate external delivery.
Every attempt is audit-recorded before network I/O; destination and mail content never
enter audit/events. Local delivery/read state never changes with SMTP outcome. Synthetic
SMTP and container browser evidence cover this contract; no real provider has been
certified. There is no inbound email polling, webhook, reply parsing, or
external-to-agent delivery.

**Other communications:** Default guidance uses asynchronous help: file returns
immediately, waits leave requests open, fresh eligible peers claim/release/answer with
evidence, requesters cancel, and inbox wakeup reports the terminal result. Brief
connectivity probes remain distinct from review deadlines. Empty reads do not count as
consumption; explicit handoff pickup, passive welcome consumption, address delivery,
agent wakeup, read, reply, and undeliverable attempts are distinct outcomes. Queue
repair handles stale Sessions, address conflicts, aged unread mail, failed SMTP copies,
claims, handoffs, requests, aliases, and missing roots without deleting unresolved work.

**Evidence:** E3 address validation/uniqueness, idempotent registration, offline accept,
unknown/ambiguous/unauthorized refusal, send/end race, explicit broadcast,
cross-Workspace disclosure, thread/reply/forward, operator mailbox, cursor, notification,
read, SMTP outbox/retry/redaction, asynchronous-help lifecycle, and repair tests; E4
two-real-harness and one-operator browser journey covering restart/resume, offline mail,
threaded reply/forward, phonebook state, one-way SMTP copy, and recovery.

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
paths are stated rather than hidden. Mailbox registration binds an address to the
authenticated caller's authorized Workspace and adapter-provided native identity;
operator mailbox access, agent-mailbox inspection, address-book lookup, cross-Workspace
send/forward, and full-body SMTP copy each have explicit non-enumerating authorization.

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
order are explicit. Mail readiness reports invalid/ambiguous address registrations,
conflicting live attachments, aged accepted-but-unread mail, wakeup failures, and SMTP
outbox backlog/failure separately; an offline registered mailbox with accepted mail is
not itself degraded.

Current E3 now implements the mail-specific readiness projection and excludes withdrawn
Runtime state from the normal verdict. It reports invalid active registration, invalid or
conflicting live attachment, aged unread, stalled/wakeup-failed notification, and SMTP
retry/backlog/failure/uncertainty as separate count-only classes; a fresh retry is
reported but degrades readiness only after the bounded backlog threshold. Child protocols/listeners,
scheduler heartbeat, registry/package identity, and the recovery drill remain open.

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

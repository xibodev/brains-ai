# Brains Architecture

## Product boundary

Brains is a local-first operator control plane for coordinating coding-agent sessions
through shared Workspaces, durable queues, and human decisions.

The implementation identity is:

- distribution and executable: `brains-ai`;
- Python namespace: `brains`;
- frontend package: `brains-spa`;
- MCP tool prefix: `brains_`;
- default state directory: `~/.brains`;
- browser product: Brains.

Architecture descriptions use four lifecycle states:

| State | Architectural meaning |
|---|---|
| Advertised | Part of the supported normal-install topology. |
| Experimental | Implemented behavior whose normal-use ergonomics or edge cases remain uncertain; full validation still applies. |
| Target-only | Stable future contract with no current product surface. |
| Withdrawn | Frozen or retired implementation. Source/data may remain for compatibility, but there is no supported activation path. |

The shipped composition is defined by `brains.capabilities` and checked from generated
CLI, MCP, OpenAPI, browser, package-extra, example, and wire inventories. Historical
modules and tables may remain importable only to open and migrate existing SQLite data.

## Supported topology

```text
Human operator                          Coding-agent harness
      | browser / CLI                         | MCP / CLI
      v                                       v
Gateway process                         MCP process
  - /app Workspace-first SPA              - coordination tools
  - protected native API                  - local or authenticated transport
  - WS/SSE realtime                       - bounded maintenance
  - protected core API                         |
      |                                          |
      +------------------+-----------------------+
                         v
                 Shared control layer
        identity, Workspaces, Sessions, tasks,
        claims, handoffs, messages, knowledge,
        decisions, governed actions, audit,
        readiness, backup, and recovery
                         |
                         v
                  SQLite + state files
```

`brains-ai serve-all` supervises the supported gateway and MCP children. The default
gateway is loopback on port `8787`; MCP defaults to authenticated Streamable HTTP at
`http://127.0.0.1:9877/mcp`. Legacy SSE at `/sse` is explicit compatibility only. The
children share durable state but not Python memory.

The normal browser surface is `/app`:

- Command Center;
- Workspaces, including Assignments and Deliberations in the existing Work tab;
- Coordination;
- Governance;
- Operations, including Access and supported Configuration;
- Act, which launches named typed capabilities rather than shell or arbitrary MCP.

The retired dashboard/admin HTML process and execution-model screens are not part of
this topology. Only the sign-in and sign-out cookie endpoints remain under `/admin`.

## Process boundaries

The supported processes have separate memory and one shared SQLite store.

- Gateway live realtime fan-out is process-local. Durable `realtime_events` allow
  another process's committed event to appear on cursor replay, not immediate push.
- Settings objects, rate counters, caches, and connection state are process-local.
  A supported configuration write must state whether all long-lived processes need a
  restart.
- SQLite uses WAL, a bounded busy timeout, and a one-writer model. Sustained lock
  failure is an outage, not evidence that retry will eventually succeed.
- PID identity is not readiness. Service status must also prove the expected listener
  and protocol response. The supervisor independently probes each owned child's HTTP
  listener and restarts its process tree when the process survives listener loss.
- `GET /health` proves only process liveness and bounded inventory. Protected readiness
  separately proves the retained HTTP control gateway's identity/auth boundary, SQLite
  migration/integrity, authenticated MCP protocol, queue/mailbox progress, and verified
  recovery posture. It never probes the withdrawn model/provider gateway.

## Component map

| Lifecycle | Component | Responsibility | Primary location |
|---|---|---|---|
| Advertised | Application composition | Gateway app, protected routes, SPA, startup state | `src/brains/main.py` |
| Advertised | Identity and authorization | Credential resolution, principals, Org/Workspace capability checks | `src/brains/authz` |
| Advertised | Workspace-first console | Command Center, Workspaces, Coordination, Governance, Operations, Act | `frontend`, `src/brains/web/spa` |
| Advertised | Coordination controls | Sessions, tasks, claims, handoffs, durable mailbox, peer help, knowledge, checkpoints | `src/brains/control`, `src/brains/mcp` |
| Advertised (this branch) | Local work assignments | Immutable specifications, revision-fenced acceptance, evidence-bearing attempts; CLI/MCP agent actions and HTTP/browser human controls | `src/brains/control/work_assignments.py`, `src/brains/storage/models.py` |
| Advertised (this branch) | Existing-peer deliberation | Versioned proposals, exact-hash acknowledgements, blinded initials, bounded discussion, dissent-preserving synthesis; CLI/MCP agent actions and HTTP/browser human controls | `src/brains/control/coordination.py`, `src/brains/storage/models.py` |
| Advertised | Human governance | Asks, decisions, governed actions, approval routing, audit | `src/brains/control`, `src/brains/govern`, `src/brains/audit` |
| Advertised | Realtime | Closed scoped subscriptions, durable event replay, WS/SSE delivery | `src/brains/api/ws.py`, `src/brains/events` |
| Advertised | Storage and recovery | SQLite engine, migrations, integrity, backup/restore, recovery policy | `src/brains/storage`, `src/brains/backup` |
| Advertised | Service operations | CLI, wiring, service renderers, supervisor, readiness | `src/brains/cli`, `src/brains/wire`, `src/brains/service` |
| Withdrawn | Execution model | Runtimes, Personas, Pods, Projects, Issues, execution onboarding/Sessions | `src/brains/api`, `src/brains/daemon`, execution-model frontend screens |
| Withdrawn | Automation | Managed Skills, recurring definitions, generic triggers, scheduled auto-fire | `src/brains/control`, `src/brains/mcp`, Automation frontend |
| Withdrawn | Model edge | OpenAI/Anthropic facades, router, providers, LiteLLM, tool launcher | `src/brains/api`, `src/brains/router`, `src/brains/providers` |
| Withdrawn | Advanced context | Semantic indexing/search, embeddings, graph, external freshness | `src/brains/context` |
| Withdrawn | Alternate services | Postgres, OpenTelemetry export, messaging bridges, WhatsApp Web | storage adapters, observability, bridges, `services/wa-web` |
| Deleted | Legacy browser | No dashboard/admin browser implementation or static assets remain; only `/admin/login` and `/admin/logout` support the modern SPA cookie lifecycle | `brains.admin.routes`, `brains.web.spa` |

Withdrawn modules are not mounted, registered, packaged as optional extras, or linked
from the browser. Their internal checks are compatibility defense, not activation.

## Durable state

SQLite is the supported source of truth. Markdown under `.brains/views` is an optional
projection, never authority.

Advertised durable families include:

- local operator identity, Workspaces, aliases, and compatibility scope rows;
- coordination Sessions and events;
- tasks, claims, handoffs, durable mailbox rows, peer help, checkpoints, snapshots, and knowledge;
- local work assignments and their durable attempt history (available in this branch);
- existing-peer proposal versions and append-only protocol contributions (available in this branch);
- approvals, routing, governed actions, audit rows, and the signed audit-chain head;
- event context, realtime replay rows, secure local settings, and migration state.

Migration 150 reserves the durable-mailbox data boundary. Its active core rows cover:

- agent/operator mailbox identity and unique, versioned hash-only reattachment binding;
- one current ephemeral Session attachment plus detached history and a per-incarnation
  delivery cursor;
- threads, messages, per-recipient local delivery/read attribution, and explicit
  direct/broadcast audience;
- body-free local notification attempts;
- non-destructive classification of legacy `mailbox_messages` and
  `tool_session_links` rows present when the migration runs as unverified.

Historical SMTP consent and outbox rows are preserved. Local operator-mail delivery can
still enqueue a copy under a qualifying historical verified destination and copy mode;
this is compatibility behavior, not an advertised external delivery path.

The migration itself creates no mailbox, infers no address or owner, copies no message
body, and changes no existing row. The current control/API/CLI/MCP layer now creates one
operator inbox per operator and explicitly registers agent addresses from a canonical
Workspace, supported harness, validated native Session ID, authenticated owner, and a
hash-only adapter binding. Registration and successor attachment commit atomically;
wrong, missing, retired, conflicting, or unauthorized identity answers one unavailable
result. Phonebook and lookup reads filter by Org/Workspace visibility, and only Org
admins/owners may request a resolved local path. Legacy `tool_session_links`, including
`current`, never create an address.

The current attachment is the only Session incarnation that may renew or inherit a
mailbox. Once attachment history exists, start reuse, heartbeat, resume, tool-linking,
and successor transfer require the native ID and binding proof. End, terminal state,
dormancy, reaping, and ephemeral-review completion/cancellation detach in the same
transaction as their Session transition. Address-based direct delivery, explicit
Workspace broadcast, Inbox/Sent, scoped thread timelines, reply/forward provenance,
per-recipient acceptance/read state, and per-incarnation delivery cursors now use the
reserved rows. A local commit is authoritative; current Session state is used only to
prove an agent actor and attribute a read, so an offline active mailbox still accepts
mail. Sender operation IDs deduplicate retries, GET history reads never mark mail read,
and agent reads require current attachment plus binding proof. Raw operator API keys are
send-only to human inboxes; browser/local human channels may read owned operator mail.
Cross-Workspace history is returned only while every represented Workspace remains
visible, and thread projections include only messages the opened mailbox sent or
received. The Coordination mailbox desk provides human-bound selection, Inbox/Sent,
explicit read, filtered threads, operator compose/reply/forward, address-book state, and
agent deep links; it never grants browser authority to send as an agent. Legacy
Session-addressed messages remain separate.

Migration 151 activates and constrains the reserved notification rows. Pull remains the
default and authoritative recovery path. An explicitly declared, harness-compatible
attachment may create one idempotent attempt per delivery/incarnation. CLI/MCP adapters
atomically claim it, receive only a constant body-free nudge, and settle the observed
result. Reads, mode changes, and detach close stale attempts; no attempt outcome changes
local delivery. With explicit consent, `wire --mailbox-wakeups` installs a managed stop
hook for Claude Code. At its turn boundary, an existing proof-bound
attachment may emit the constant nudge and request one continuation; abandoned claims
are lease-reclaimed and become uncertain after three attempts. Copilot CLI, Codex, and
OpenCode remain pull-only because their notification continuation behavior has no
equivalent real-binary proof. Claude settings mutation is cross-process locked and uses
a recoverable atomic exchange that preserves displaced bytes. Each release candidate
must pass the native exchange and owner-only recovery probes on every supported native
platform before publication.
Brains does not retain a generic live model-input channel.

Migration 152 preserves the reserved per-operator SMTP setting and outbox schema for
historical-store compatibility. Core does not schedule or lease that outbox. Its retained
processor can be called manually or externally through unsupported paths, so this is
not a claim that it is universally stopped. ASK email opt-in neither activates that
worker nor replays pending copies. Historical SMTP configuration routes stay withdrawn.

`mailbox_wait` adds a proof-bound CLI/MCP read loop over the existing inbox reader, with
current Session/attachment/binding validation on every poll. Transactions close before
sleeping. Its 0–25000 ms budget bounds polling, not database work; the 1–200 limit bounds
returned messages, not the authorization scan. It returns unread messages and a
`next_after_delivery_id` continuation floor, distinct from message IDs and the unchanged
attachment cursor. It never marks read, advances persisted cursors, renews leases,
settles notifications, or accepts work. This adds no migration, HTTP route or frontend.

MCP registration applies `StrictInt` to the wait's integer parameters, rejecting booleans,
and offloads the blocking loop with `anyio.to_thread.run_sync`. Request-local authority
is copied into the worker; concurrent sends can proceed on the event loop. Workers use
AnyIO's shared default capacity limiter, with no per-request pool or unlimited thread
allocation. `abandon_on_cancel=False` waits for an active worker to complete its bounded
poll; database work and capacity waiting prevent a hard 25-second completion guarantee.
Direct CLI/core calls remain blocking. Client-wait cancellation supplies no job-cancellation
evidence.

Durable-mail readiness is a bootstrap-admin-only count projection over active core
rows. It checks registration shape, live attachment consistency, unread age, and
body-free local notification progress. A detached
active mailbox with unread mail remains healthy until the mail crosses the declared age
threshold; offline acceptance is the feature, not an outage. Withdrawn Runtime lifecycle
does not affect normal-product readiness, and the migration's explicit unverified legacy
inventory is reported without being mistaken for a broken active registration.

Operational readiness aggregates only current mailbox registration, attachment, unread,
and local notification state. Historical SMTP diagnostics are separately count-only:
`affects_local_readiness: false`, `processor_state: not_scheduled_by_core`. Open outbox
rows derive a `blocked` SMTP state and `processor_not_scheduled_by_core` reason without
mutating their stored status or inferring external-worker liveness. SMTP issues alone
do not degrade local readiness. This is not behavioral analytics and makes no claim
about adoption, task success, or product value. Ordinary feedback, automated contracts,
and isolated validation drive engineering revision.

The schema also contains withdrawn Runtime, Persona, Project, Issue, Pod, Skill,
recurring, generic-webhook, provider-routing, semantic, graph, bridge, and alternate
backend state. Those rows remain only where required to open or migrate an existing
store; they do not register or activate a product capability. New product work must not
depend on them merely because they exist.

### Local work-assignment state

`WorkAssignment` and `WorkAssignmentAttempt` map to the standalone `work_assignments`
and `work_assignment_attempts` tables added by `154_work_assignments`. They reference
Workspaces, operators, and coordination Sessions, not legacy Issues, Personas, Runtimes,
or execution assignments. The additive SQLite migration preserves existing rows and
prior migration history; transactional DDL rolls back on failure and is idempotent on
rerun. The PostgreSQL delta preserves corpus compatibility without adopting that backend.

The assignment stores a canonical immutable version-1 specification, specification/request
hashes, creator provenance, Workspace/operator-scoped creation key, status, revision,
generation, and cancellation attribution. Identical creation replay survives creator
Session replacement; a changed title or specification under the same key is refused.
Attempts retain generation, source Session and actual tool, cooperative deadline/budget,
report and settlement times, evidence/result, and nullable usage. Null usage is unknown.

Session mutations authorize the operator's live Session and current Workspace visibility under
the Session lifecycle writer lock. Conditional revision updates fence stale requests;
assignment/attempt changes, lease renewal, and a Workspace-scoped `work_assignment_*`
event commit or roll back together. The event records code/revision/generation rather
than specification or evidence bodies. These are coordination events, not proof of a
governed external effect. Read snapshots keep revision and attempt history consistent
without renewing leases or changing assignment state.

SQL constraints enforce unique creation identity, unique assignment/generation, valid
statuses and settlement timestamps, and at most one unresolved attempt per assignment.
`accepted`, `cancel_requested`, and `uncertain` all occupy that unresolved slot.
Conclusive `completed`, `failed`, and `cancelled` attempts have `settled_at`; a reported
`uncertain` attempt does not. Explicit retry permits only conclusively failed/cancelled
work and retains every prior attempt. No implicit retry or uncertain-state reconciliation
is implemented.

Reads derive `observed_status: uncertain` when an unresolved attempt exceeds its deadline
or its source Session is unavailable; stored `status` and `revision` remain unchanged.
The default one-hour budget, optionally shortened by a specification deadline, is
cooperative and does not enforce OS timeout or process exit. Cancellation before
acceptance is final without an attempt; after acceptance it is a request awaiting the
source Session's report, and a winning request fences completion. Cancelling a reported
uncertain attempt leaves it uncertain.

Same-Session reattachment is distinct from replacement: only the original live accepting
Session can report its current attempt. Successor linking does not transfer assignment
attempts. `checkout_ref`, `links`, and specification `tool` are inert; acceptance neither
launches a process nor reads, creates, or owns a checkout. No universal process-control
or containment guarantee follows from the state machine.

Seven CLI/MCP operations expose this foundation within the 89-tool current-main MCP surface.
The operator adapter adds HTTP create/get/list/cancel and browser components within
Workspace Work; agent accept/settle and execution retry remain CLI/MCP-only.
The SPA route inventory is unchanged. [MCP](MCP.md#local-work-assignments) defines the
public fields. Remote runners and specialist execution remain planned; this local
foundation does not complete [#36](https://github.com/xibodev/brains-ai/issues/36).

### Existing-peer proposal state

`CoordinationProposal` and `CoordinationContribution` map to `coordination_proposals`
and `coordination_contributions`, added by `155_peer_coordination`. The additive SQLite
delta creates these two standalone tables and their constraints/index without rewriting
existing data or migration history. It uses the runner's transactional DDL boundary and
is rerunnable after failure. The PostgreSQL companion is compatibility inventory, not
backend support. Proposals reference Workspaces, operators and existing coordination
Sessions; contributions reference a proposal's composite code/version key and author
Session. No worker, execution assignment, or checkout is created.

Each proposal version stores canonical JSON, SHA-256 specification/request hashes,
requester and result-owner provenance, a Workspace/operator-scoped creation key, deadline,
phase, round and revision. Participants are sorted by Session ID; stored tool identity
comes from the Session, while model labels remain unverified declarations. Schema version
1 is distinct from the incrementing proposal version. Specification bounds are 32 KiB,
2–8 participants and 0–3 discussion rounds; payloads fit 64 KiB. Context and links are
inert and never fetched or translated into assignments.

The Session path in `control/coordination.py` authorizes a live operator-owned member
Session and current Workspace visibility. Mutations take the Session lifecycle writer lock and use conditional
revision updates. Version/revision fences, contribution insertion, Session lease renewal
and a Workspace-scoped `coordination_*` event commit or roll back together. Event metadata
contains code/version/revision/round/Workspace, not report bodies; these are protocol
events, not evidence of human approval or governed execution. Reads use a consistent
snapshot without renewing leases, changing phases, or settling work.

New Session-authored proposals start at version 1, revision 0, `planned`, with requester acceptance of
the stored hash recorded automatically. All required members (requester, participants,
result owner) must acknowledge that version/hash before `accepted`. Only requester
advances to `collecting`, after checking all members remain live. All participants must
submit one initial before requester can explicitly close collection. Closure sets
`initial_closed` and enters `discussing`: round 0 for zero discussion rounds, otherwise
round 1. Each configured round needs one report per participant before requester advances.
Only result owner may submit final when `final_ready`, producing `completed`.

SQL uniqueness bounds acceptance/initial/discussion slots per author and round, permits
one final per version, and deduplicates contribution keys per code/version/author. Reports
are append-only. All nonblank original initial/discussion dissent remains mechanically
projected as unresolved, including after synthesis; there is no resolution/erasure API.
Every public protocol response uses the same `_snapshot` filter: until explicit initial
closure, only the caller's own initial report is visible, even for requester/result owner,
while counts and missing-member IDs expose progress. Get, list, mutations and retries
share this behavior. Cancellation/replacement does not unblind a version never closed.
This is cooperative API blinding, not a security boundary against a shared operator who
can act as another owned Session or inspect the database. It cannot prove independent
reviewer identity or model diversity. A Session acknowledgement is not human approval.

Revisions increase across one proposal code's versions. Replacement by original requester
cancels an open, unexpired latest version at r+1 and stores its replacement at r+2 in one
transaction. New acknowledgements and reports are required; only requester acceptance is
automatic. Completed/cancelled/expired work is not replaceable, and old versions remain
historical. Mutations refuse stale versions/revisions even for known retries; current-fence
identical retries are no-ops where allowed, with no event or lease renewal. Creation replay
returns the member-filtered latest version without extending its deadline. No successor
Session inherits membership by linking alone.

Deadlines are cooperative: omitted means creation plus one hour; explicit means future
and within 30 days. Reads flag open expired work as incomplete with final readiness false,
without changing stored status/revision. There is no automatic execution, phase advance,
or expiry settlement. Requester may cancel after expiry, but acceptance, advance,
submission and replacement are refused. Cancellation changes protocol state only: it
neither cancels a work assignment nor stops a process nor sends/cancels mail.

Seven core/lean MCP tools and matching CLI commands expose this local part of
[#38](https://github.com/xibodev/brains-ai/issues/38). With the separate `mailbox_wait`
addition, current-main MCP has 89 tools.
The operator HTTP adapter and Workspace Work components provide human creation,
observation, cancellation and phase advancement, with no agent accept/submit endpoints
or proposal-specific readiness promise. Worker panels, multi-day execution and checkout management remain
outside this implementation. The local assignment foundation of
[#36](https://github.com/xibodev/brains-ai/issues/36) remains separate. See
[MCP](MCP.md#existing-peer-coordination) for exact schemas, returned helper fields and
retry behavior, and [Operations](OPERATIONS.md#existing-peer-proposal-inspection-and-recovery)
for inspection and recovery.

### Operator-authored work and browser observation

`156_operator_work_authorship` adds `creator_kind` to both parent tables and makes
`creator_session_id` nullable under a check constraint: `session` requires a Session ID,
`operator` requires null. The proposal model's `requester_session_id` remains an alias
of the stored creator column. Existing authors default to `session`; specifications,
hashes, attempt/contribution history and prior migrations are preserved. The SQLite
rebuild uses stored historical DDL and restores indexes/triggers, retaining incoming
foreign keys. A savepoint keeps both rebuilds atomic within the caller's transaction;
rollback and rerun preserve history with foreign-key enforcement on or off. The model's
column order matches the migrated tables. The PostgreSQL companion is compatibility
inventory, not a supported backend.

`api/operator.py` exposes ten work endpoints in the existing protected operator family:
assignment create/get/list/cancel, proposal create/get/list/advance/cancel and a bounded
participant-candidate read. [Guide](GUIDE.md#operator-work-http-family) lists the exact
paths and bodies. The adapter passes the authenticated principal to the control layer;
it accepts no caller-declared author and never substitutes a Session. HTTP mutations
require a human browser-cookie channel and Workspace write capability. Raw API credentials
may read within scope but cannot perform these mutations. Get/list require Workspace read
capability and matching `creator_operator_id`, for either creator kind; bootstrap admin
has no cross-creator override. Unknown and inaccessible work share a refusal.

Human creation records `creator_kind: operator`, the authenticated `creator_operator_id`
and null creator Session. Human proposals require an explicit existing live owned
result-owner Session and 2–8 live owned participant Sessions in the Workspace; the owner
may be outside the panel in the HTTP specification. No automatic agent acknowledgement
is inserted. All required agents acknowledge the exact hash/version through CLI/MCP;
only the named result owner submits final synthesis. Human advance can operate on the
latest Session-authored proposal after its requester ends, but rechecks participants and
result owner for liveness and ownership at each transition. Cancellation remains a
cooperative state action, never process control.

Operator transactions reserve the SQLite writer before reading mutation state, recheck
scope/capability, and condition updates on the current revision (also proposal version).
State and a scoped `work_assignment_*` or `coordination_*` event commit together.
Human events have null `session_id` and metadata `actor_kind: operator`, `operator_id`
and `channel`, not agent authorship or evidence bodies. They prove a recorded local
action, not a governed external effect. Operator reads and writes never create or renew
a Session; history remains readable after creator/participant Sessions end. Creation keys
remain Workspace/operator-scoped, with author kind included in new request hashes.
Pre-156 Session hashes remain replay-compatible without rewriting historical rows.

The shared proposal serializer receives no viewer Session for operator reads, including
mutation responses and creation replay. Before `initial_closed`, it returns no reports:
`contributions` and `unresolved_dissent` are empty, `final` is null, while progress counts
and missing-member IDs remain visible. After explicit closure all recorded reports,
evidence and unresolved original dissent are visible. Cancellation, replacement or expiry
cannot unblind a never-closed version. This observer filter also applies to historical
Session-authored proposals; it is still a cooperative API boundary, not protection from
direct database access or acting through another owned Session's agent interface.

Operator snapshots add server-computed permissions: assignment `can_cancel`/`reason`,
and proposal `can_advance`/`advance_blocked_reason` plus
`can_cancel`/`cancel_blocked_reason`. These guide controls, not replace write-time checks.
`WorkspaceAssignments`, `WorkspaceDeliberations` and `WorkspaceWorkShared` use structured
forms and inspectable evidence in `/app/workspaces/:slug`'s Work tab. Participant choices
come from recorded live owned Sessions; the UI selects an explicit result owner from the
panel, not an “Act as” identity. There is no arbitrary-JSON input.

Work panels use manual refresh and post-mutation readback, not automatic polling or
guaranteed realtime delivery. A conflict refreshes latest state and requires explicit
re-review before another action; it never automatically accepts changes or resubmits.
An unchanged open creation form reuses its idempotency key after a lost response; editing
starts a new request. No assignment execution-retry or proposal-replacement endpoint is
added. Existing send controls still record local delivery, not agent execution.

This is an unreleased local foundation for part of
[#42](https://github.com/xibodev/brains-ai/issues/42). Broader cross-process events/replay
and transport comparison remain incomplete. Remote runners (#36) and specialist workers
(#38) remain deferred to #37 planning. MCP stays at 89 tools; the pinned 1.5 website
retains 74. No new SPA route is introduced.

### Knowledge and reference evidence

`control/knowledge.py` derives effective lifecycle at read time without rewriting
knowledge rows. Search applies visibility and effective-status predicates before its
1–100 result limit, retaining flagged history by default. Supersession takes precedence
over expiry for effective status. Successor IDs/references are exposed only when that
successor is visible. Lifecycle writes use conditional updates: supersession may link a
predecessor once, and resolution compares the read status/link snapshot before updating.
A competing change cannot silently replace the chain or reactivate superseded knowledge.

The existing `retrieve_original(ref)` interface in `control/retrieve.py` returns bounded
evidence, not immutable, versioned content. Artifact/chunk ancestry is authorized through
Source and Workspace before content or descriptive columns are loaded. Current-file
reads accept only regular files under both the registered Workspace and a local
`repo_dir`/`docs_dir` Source root; metadata absolute paths are ignored. Parent traversal,
root escapes, symlinks and reparse points are refused. Bootstrap visibility of a Source
with no Workspace allows stored evidence only, not filesystem access.

POSIX reads use no-follow directory descriptors where available. Other platforms use
component and descriptor identity checks but retain a concurrent path-replacement race.
This is a cooperative local filesystem boundary, not guest isolation or a security sandbox.

Stored bodies, recorded evidence, current-file text and fallback summaries are independently
bounded to 64 KiB UTF-8, with explicit truncation/incompleteness. Only complete current-file
bytes matching a valid full SHA-256 in `Artifact.hash` set `original_verified`; this verifies
the read against the recorded digest, not index integrity or immutable retention. Chunk
hashes describe captured files, not chunk text. Stored rows remain mutable. See
[MCP](MCP.md#bounded-reference-retrieval) for the response contract.

### Schema evolution

Startup and `brains-ai db migrate` use one ordered, checksummed migration corpus. The
frozen baseline creates a fresh schema; numbered deltas update it. The ledger records
order, checksum, origin, backend, status, attempts, timings, and error. Edited history,
unknown migrations, gaps, interrupted attempts, and model/schema drift fail closed.

`Base.metadata.create_all` is not the startup migration strategy. SQLite deltas execute
transactionally. Alternate-backend baseline and migration code remains compatibility
inventory and is not a Postgres support claim.

### Workspace identity

Normalized repository paths are aliases of durable Workspaces. Linked Git worktrees can
converge on the oldest identity within one Org; cross-Org convergence is refused.
Historical duplicate rows are archived rather than rewritten or deleted.

## Coordination lifecycle

A supported Session is a durable agent coordination handle, not proof that Brains
launched a process.

1. A harness starts or resumes a Session for one Workspace and tool identity. Supported
   adapters may atomically register the durable mailbox with a native Session ID and an
   adapter-owned binding file.
2. The Session receives current ownership, handoff, task, message, and knowledge
   context.
3. Tool calls renew its lease while the harness remains active. A mailbox-bound Session
   must prove its native ID and binding; knowing only `ses_*` is insufficient.
4. The Session can claim work, checkpoint, hand off, communicate, ask for help, and
   file human decisions.
5. A clean end releases eligible ownership. An expired PID-less handle becomes dormant
   without being mislabeled as execution failure.
6. An explicit successor can inherit eligible claims, in-progress tasks, and mailbox
   attachment/cursor continuity once; mailbox inheritance
   requires the same binding proof and rolls back the whole transfer on failure.

Adapter identity, offline mail, successor reattachment, and threaded replies use the
same binding contract. Automatic adapter extraction, abrupt process exit, and host
restart remain open. Scheduler-driven lease expiry itself does not depend on an
operator read.

### Queue semantics

- Tasks and Workspace claims use atomic ownership transitions.
- Checkpoint and active-handoff exact retries are idempotent for sequential retry.
- Direct mail is durable until read; an empty read is not consumption telemetry.
- Peer help is asynchronous: file, claim, release, answer with evidence, cancel, or
  wait without making a client timeout expire the request.
- Queue diagnosis is read-only. Apply mode may run only objectively safe expiry and
  continuity repairs; it must not delete unresolved human work.

Running-agent chat delivery and Runtime process stop are withdrawn. Source-level
`session_commands` rows do not change that product boundary.

## Realtime

WS and SSE use a closed server-resolved subscription grammar for advertised collections.
The server derives Org/Workspace scope, applies non-enumerating refusal, and revalidates
identity and membership during the connection. Retained generic topic controls are not
registered as normal CLI or MCP capabilities. Runtime credentials are not operator
realtime principals.

Durable events commit before notification and carry a monotonic `event_id`. Resume uses
the highest applied cursor. Bounded replay reports an explicit reset when retention or
volume prevents complete catch-up. Per-connection serialization prevents live frames
from overtaking replay frames.

Current limitations are material:

- live fan-out is gateway-process local;
- not every publisher has a stable dedupe key, so some delivery is at-least-once;
- retention and gap detection are install-wide rather than per subscription scope;
- notification-only frames require another durable source for recovery.

## Human governance and audit

Protected actions resolve one principal and explicit Org/Workspace policy. Approval
routing is organizational metadata only; it never authorizes or executes an action.
Separation rules prevent a requesting Session or its bound identity from resolving its
own approval where the implementation can establish that identity.

The governed-action contract records request, decision, attempt, and result with an
argument digest after canonical secret redaction. Database transitions commit with
their audit entry. Non-database effects use attempted/result records so an effect whose
attempt cannot be recorded does not run.

Audit rows form an HMAC chain with a signed single-writer head. Verification detects
mutation, insertion, deletion, truncation, missing/forged heads, and count divergence
under the stated key model. A stolen audit key can forge history; an in-process action
gate cannot contain an external harness that bypasses it. Both limits remain explicit.

## External boundary

The default local service requires no external integration. Optional ASK email is one
narrow outward path: `file_decision_request` commits the ASK before calling `notify_ask`.
The owner must explicitly enable `BRAINS_ASK_EMAIL_NOTIFICATIONS_ENABLED` (default false)
through environment settings sources; YAML, admin overlays and encrypted-setting mutation
cannot enable it. The flag is read locally at server/process startup, not enabled through
an API or agent tool. Existing SMTP configuration alone is not consent. Standing consent
permits one notification per newly filed ASK, not approval of the underlying action.

Only the configured owner address is accepted, never a recipient supplied by the filing
agent. Content is limited to a validated ASK code (at most 32 ASCII characters), a
whitespace-normalized title preview capped at 200 Unicode scalars including the ellipsis
(at most 800 UTF-8 bytes), and the fixed local console URL. CR/LF in the original title
rejects email before SMTP, leaving the stored ASK title/body unchanged. The bounded
preview can still disclose private information. The sender hardcodes
`http://127.0.0.1:8787/app`, without adapting to custom gateway ports. Loopback opens the
recipient's computer and is useful on the same owner's local-app host; it is not a public
or credential-bearing link.
An attributed ledger attempt precedes SMTP. Failure, uncertain acceptance and known SMTP
acceptance remain distinct; acceptance proves neither recipient delivery nor reading.
The filing response includes allowlisted notification facts alongside the durable ASK
identity/status. Unexpected hook failure or a malformed outcome yields `status: uncertain`,
`attempted: null`, `sent: false`, and `error: notification_failed` in that object.
Optional `title_truncated: true` reports actual preview capping, not confidentiality filtering.
There are no hidden retries, queue activation or historical replay. See
[Operations](OPERATIONS.md#optional-ask-email-notifications) for result and setup details.

GitHub linkage, generic webhooks, public relay, historical mailbox SMTP-copy delivery,
model gateways, telemetry exporters, and messaging bridges remain outside the supported
surface. The retained generic sender does not expose a public arbitrary-recipient send;
`mail_send` remains withdrawn.

## Recovery boundary

SQLite backup uses the online backup API so committed WAL content is included. Manifest
archives identify format, schema, payload hash, and source identity. Verification
restores into isolation and checks manifest claims. Destructive restore requires an
explicit operator action, refuses incompatible candidates before target mutation,
captures a verified rollback point, and verifies SQLite integrity after replacement. An
isolated recovery drill then applies that rollback archive to a second disposable target
and compares its logical schema and row state with the captured pre-replacement state.

Integrity repair is dry-run by default. Apply mode requires a current verified backup,
holds the write fence across diagnosis and mutation, performs only deterministic
repairs unless deletion is explicitly authorized, and rolls back as a unit on failure.

Brains declares recovery schedule, retention, encryption ownership, offsite ownership,
RTO, RPO, and restore-drill expectation, but it does not run a backup scheduler. A
complete declaration or manually entered drill date is not evidence that a drill
occurred; only the audited disposable restore probe establishes that state.

## Deployment shapes

| Shape | Architectural status |
|---|---|
| Native CLI and user service | Supported source path; clean-host operation remains an E4 requirement. |
| Root runtime image | Build source exists; no live deployment is verified. |
| Isolated sandbox | Test starting point only; presence is not a run result. |
| Dev Compose, shared-DB battle harness, UAT sidecars, box scaffold | Contain withdrawn or internally inconsistent topology; not supported deployment paths. |

No deployment is established by this document.

## Current limitations

1. Presence can remain stale when a harness does not end/detach or renew correctly.
3. Cross-process realtime is durable on replay but not live fan-out.
4. The action boundary is cooperative and in-process, not universal process/network
   confinement.
5. SQLite foreign-key enforcement is opt-in until existing stores are proven clean.
6. Legacy and withdrawn source that remains for data compatibility still requires
   separately reviewed deletion where compatibility no longer needs it.

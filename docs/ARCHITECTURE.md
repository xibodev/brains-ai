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
- Workspaces;
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
| Advertised (this branch) | Local work assignments | Immutable specifications, revision-fenced acceptance, evidence-bearing attempts; CLI/MCP only | `src/brains/control/work_assignments.py`, `src/brains/storage/models.py` |
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

SMTP consent and outbox rows also exist in the migration corpus so newer historical
stores can be opened. They are compatibility inventory, not an advertised delivery
path.

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
historical-store compatibility. Core exposes no SMTP configuration, does not lease its
outbox, and performs no external mail delivery.

Durable-mail readiness is a bootstrap-admin-only count projection over active core
rows. It checks registration shape, live attachment consistency, unread age, and
body-free local notification progress. A detached
active mailbox with unread mail remains healthy until the mail crosses the declared age
threshold; offline acceptance is the feature, not an outage. Withdrawn Runtime lifecycle
does not affect normal-product readiness, and the migration's explicit unverified legacy
inventory is reported without being mistaken for a broken active registration.

Operational readiness aggregates only current mailbox registration, attachment, unread,
and local notification state. It is not behavioral analytics and makes no claim
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

Mutations authorize the operator's live Session and current Workspace visibility under
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

Seven CLI/MCP operations expose this foundation, bringing current-main MCP to 81 tools.
There are no assignment native HTTP routes, frontend components, or browser controls;
the SPA route inventory is unchanged. [MCP](MCP.md#local-work-assignments) defines the
public fields. Remote runners and specialist execution remain planned; this local
foundation does not complete [#36](https://github.com/xibodev/brains-ai/issues/36).

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

Core has no external integration boundary. GitHub linkage, generic webhooks, public
relay, SMTP copies, model gateways, telemetry exporters, and messaging bridges are not
mounted or packaged as normal-install capabilities.

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

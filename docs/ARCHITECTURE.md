<!--
last_verified: 2026-08-31T18:30:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 35ce5ff1b4a2eb8bce2777ca7e3cff4d7ceece99 plus the worktree contract correction and isolated Docker full quality, packaged browser, and real OpenCode/Claude/Codex mailbox UAT; installed-service recovery and deployment not verified
-->

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
| Experimental | Implemented behavior whose normal-use ergonomics or edge cases remain uncertain; full UAT still applies. |
| Target-only | Stable future contract with no current product surface. |
| Withdrawn | Frozen or retired implementation. Source/data may remain for compatibility, but there is no supported activation path. |

At HEAD, BL-P0-09 remains open: routes, commands, tools, flags, extras, tables, and
modules for withdrawn features still exist. The architecture records that mismatch; it
does not turn source presence into product availability.

## Supported topology

```text
Human operator                          Coding-agent harness
      | browser / CLI                         | MCP / CLI
      v                                       v
Gateway process                         MCP process
  - /app Workspace-first SPA              - coordination tools
  - protected native API                  - local or authenticated transport
  - WS/SSE realtime                       - bounded maintenance
  - signed GitHub ingress                       |
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
this topology even though source routes and assets remain pending containment.

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
  is a separate contract and remains incomplete for child protocol health, scheduler
  progress, registry freshness, and cross-process failure.

## Component map

| Lifecycle | Component | Responsibility | Primary location |
|---|---|---|---|
| Advertised | Application composition | Gateway app, protected routes, SPA, startup state | `src/brains/main.py` |
| Advertised | Identity and authorization | Credential resolution, principals, Org/Workspace capability checks | `src/brains/authz` |
| Advertised | Workspace-first console | Command Center, Workspaces, Coordination, Governance, Operations, Act | `frontend`, `src/brains/web/spa` |
| Advertised | Coordination controls | Sessions, tasks, claims, handoffs, messages, topics, peer help, knowledge, patterns, checkpoints | `src/brains/control`, `src/brains/mcp` |
| Advertised | Human governance | Asks, decisions, governed actions, approval routing, audit | `src/brains/control`, `src/brains/govern`, `src/brains/audit` |
| Advertised | Realtime | Closed topics, durable event replay, WS/SSE delivery | `src/brains/api/ws.py`, `src/brains/events` |
| Advertised | Storage and recovery | SQLite engine, migrations, integrity, backup/restore, recovery policy | `src/brains/storage`, `src/brains/backup` |
| Advertised | Service operations | CLI, wiring, service renderers, supervisor, readiness | `src/brains/cli`, `src/brains/wire`, `src/brains/service` |
| Advertised | GitHub ingress | Signature, repository scope, delivery identity, replay refusal | `src/brains/api/webhooks.py` |
| Advertised | Agent feedback inbox | Redacted ordinary feedback and human-only triage/promotion | `src/brains/control` |
| Admission candidate | Ephemeral peer review | Fenced disposable tracked snapshot; not field-active until default activation and worker transport are corrected | `src/brains/control`, Runtime compatibility endpoints |
| Withdrawn | Execution model | Runtimes, Personas, Pods, Projects, Issues, execution onboarding/Sessions | `src/brains/api`, `src/brains/daemon`, execution-model frontend screens |
| Withdrawn | Automation | Managed Skills, recurring definitions, generic triggers, scheduled auto-fire | `src/brains/control`, `src/brains/mcp`, Automation frontend |
| Withdrawn | Model edge | OpenAI/Anthropic facades, router, providers, LiteLLM, tool launcher | `src/brains/api`, `src/brains/router`, `src/brains/providers` |
| Withdrawn | Advanced context | Semantic indexing/search, embeddings, graph, external freshness | `src/brains/context` |
| Withdrawn | Alternate services | Postgres, OpenTelemetry export, messaging bridges, WhatsApp Web | storage adapters, observability, bridges, `services/wa-web` |
| Withdrawn | Legacy browser | Dashboard and legacy admin HTML/static assets | `src/brains/dashboard`, `src/brains/admin` |

Withdrawn modules retain their previous authorization and validation checks while they
remain mounted. Those checks limit current source risk; they do not define a supported
feature or permission to activate it.

## Durable state

SQLite is the supported source of truth. Markdown under `.brains/views` is an optional
projection, never authority.

Advertised durable families include:

- operators, credentials, Orgs, members, Workspaces, aliases, and memberships;
- coordination Sessions and events;
- tasks, claims, handoffs, legacy Session-addressed mailbox rows, topics, peer help,
  checkpoints, snapshots, and knowledge;
- approvals, routing, governed actions, audit rows, and the signed audit-chain head;
- feedback, event context, adoption events, and ephemeral-review attempt metadata;
- realtime replay rows, integration delivery identity, usage attribution, secure local
  settings, and migration state.

Migration 150 reserves the durable-mailbox data boundary:

- agent/operator mailbox identity and unique, versioned hash-only reattachment binding;
- one current ephemeral Session attachment plus detached history and a per-incarnation
  delivery cursor;
- threads, messages, per-recipient local delivery/read attribution, and explicit
  direct/broadcast audience;
- body-free notification attempts and per-operator SMTP consent/destination references;
- one retryable SMTP outbox row per local delivery;
- non-destructive classification of legacy `mailbox_messages` and
  `tool_session_links` rows present when the migration runs as unverified.

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
local delivery. Brains does not retain a generic live model-input channel, and current
`wire` installs no notification hook/plugin, so this protocol is not evidence that an
external running model was awakened. Concrete harness integration remains a later slice.

Migration 152 activates the reserved per-operator SMTP setting and outbox rows. The
human-bound mailbox owner stores a destination as AES-GCM ciphertext behind a versioned
reference, proves control through a short-lived emailed challenge, and receives only a
masked hint on reads. Verification defaults to a constant content-free notification;
full subject/body forwarding is a distinct explicit consent state. The local delivery
transaction snapshots only destination reference and copy mode into one outbox row.

The scheduler leases that row after commit. A required audit attempt precedes SMTP I/O,
and the outbox ID supplies a stable RFC Message-ID. Known pre-send failures back off and
retry; any send-stage exception, lost outcome audit, or expired send lease is terminally
uncertain so Brains does not knowingly duplicate an external copy. Mode/destination
changes fence live sends and cancel work that has not started. Neither SMTP outcome nor
configuration changes modify the local message/delivery/read record. No destination or
mail content enters audit/event metadata, and no inbound email path exists. Synthetic
SMTP evidence does not certify a real provider.

Durable-mail readiness is a bootstrap-admin-only count projection over those
authoritative rows. It
checks active registration shape, live attachment consistency, unread age, body-free
notification progress, and SMTP backlog/failure/uncertainty separately. A detached
active mailbox with unread mail remains healthy until the mail crosses the declared age
threshold; offline acceptance is the feature, not an outage. Withdrawn Runtime lifecycle
does not affect normal-product readiness, and the migration's explicit unverified legacy
inventory is reported without being mistaken for a broken active registration.

Operational readiness aggregates only current mailbox registration, attachment, unread,
notification, and SMTP failure state. It is not behavioral analytics and makes no claim
about adoption, task success, or product value. Ordinary feedback, automated contracts,
and isolated end-to-end UAT drive engineering revision.

The schema also contains withdrawn Runtime, Persona, Project, Issue, Pod, Skill,
recurring, generic-webhook, provider-routing, semantic, graph, bridge, and alternate
backend state. BL-P0-09 decides what must remain to open an existing store. New product
work must not depend on those rows merely because they exist.

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
2. The Session receives current ownership, handoff, task, message, knowledge, and
   pattern context.
3. Tool calls renew its lease while the harness remains active. A mailbox-bound Session
   must prove its native ID and binding; knowing only `ses_*` is insufficient.
4. The Session can claim work, checkpoint, hand off, communicate, ask for help, and
   file human decisions.
5. A clean end releases eligible ownership. An expired PID-less handle becomes dormant
   without being mislabeled as execution failure.
6. An explicit successor can inherit eligible claims, in-progress tasks, topic
   subscriptions, and mailbox attachment/cursor continuity once; mailbox inheritance
   requires the same binding proof and rolls back the whole transfer on failure.

Isolated OpenCode/Codex and OpenCode/Claude journeys prove explicit native-ID extraction,
offline mail, successor reattachment, and threaded replies. Automatic adapter extraction,
abrupt process exit, and host restart remain open. Scheduler-driven lease expiry itself
does not depend on an operator read.

### Queue semantics

- Tasks and Workspace claims use atomic ownership transitions.
- Checkpoint and active-handoff exact retries are idempotent for sequential retry.
- Direct mail is durable until read; an empty read is not consumption telemetry.
- Topic posts are append-only. Subscription cursors are per Session and topic.
- Peer help is asynchronous: file, claim, release, answer with evidence, cancel, or
  wait without making a client timeout expire the request.
- Queue diagnosis is read-only. Apply mode may run only objectively safe expiry and
  continuity repairs; it must not delete unresolved human work.

Running-agent chat delivery and Runtime process stop are withdrawn. Source-level
`session_commands` rows do not change that product boundary.

## Realtime

WS and SSE use a closed server-resolved topic grammar. The server derives Org/Workspace
scope, applies non-enumerating refusal, and revalidates identity and membership during
the connection. Runtime credentials are not operator realtime principals.

Durable events commit before notification and carry a monotonic `event_id`. Resume uses
the highest applied cursor. Bounded replay reports an explicit reset when retention or
volume prevents complete catch-up. Per-connection serialization prevents live frames
from overtaking replay frames.

Current limitations are material:

- live fan-out is gateway-process local;
- not every publisher has a stable dedupe key, so some delivery is at-least-once;
- retention and gap detection are install-wide rather than per topic;
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

## Experimental features

The experimental label records uncertainty in normal-use behavior. It does not start a
field trial or create a telemetry requirement. BL-P1-15 is the advertised ordinary
feedback path; the mis-scoped BL-P1-16 analytics projection is removed.

Ephemeral peer review is an implemented admission candidate, not a supported experiment,
until normal peer help defaults to existing peers and its worker transport is separated
from withdrawn Runtime execution.

Each experimental feature needs a user promise, full UAT, truthful activation, a feedback
path, disable/rollback behavior, and revision/withdrawal criteria. None may silently
widen normal-product readiness or authority.

Ephemeral peer review may reuse narrowly scoped Runtime compatibility endpoints while
BL-P0-09 separates them from withdrawn Runtime execution. The reviewer receives a
temporary Git-tracked snapshot rather than the registered source path; bounded output
is accepted only if the source fingerprint remains unchanged. This is not a universal
network-confinement claim.

## External boundary

Signed GitHub ingress is the only advertised external integration. It requires the
expected signature, delivery/event identity, exact repository-to-Org binding, and
durable replay refusal. Live GitHub operation remains unverified.

BL-P1-19 proposes a separate outbound path: local defect evidence is redacted,
clustered, deduplicated, compared with public issues, and presented as an exact payload.
A human must choose discard, link existing, create, or comment. The effect then uses the
governed GitHub path. No background upload or automatic issue creation is allowed.

Generic webhooks, relay, email-to-agent behavior, Telegram, Slack, WhatsApp, and
WhatsApp Web are not part of this boundary.

## Recovery boundary

SQLite backup uses the online backup API so committed WAL content is included. Manifest
archives identify format, schema, payload hash, and source identity. Verification
restores into isolation and checks manifest claims. Destructive restore requires an
explicit operator action and refuses schema history the current build cannot express.

Integrity repair is dry-run by default. Apply mode requires a current verified backup,
holds the write fence across diagnosis and mutation, performs only deterministic
repairs unless deletion is explicitly authorized, and rolls back as a unit on failure.

Brains declares recovery schedule, retention, encryption ownership, offsite ownership,
RTO, RPO, and restore-drill expectation, but it does not run a backup scheduler. A
complete declaration is not evidence that a drill occurred.

## Deployment shapes

| Shape | Architectural status |
|---|---|
| Native CLI and user service | Supported source path; clean-host operation remains an E4 requirement. |
| Root runtime image | Build source exists; no live deployment is verified. |
| Isolated sandbox | Test starting point only; presence is not a run result. |
| Dev Compose, shared-DB battle harness, UAT sidecars, box scaffold | Contain withdrawn or internally inconsistent topology; not supported deployment paths. |

No deployment is established by this document.

## Current limitations

1. BL-P0-09 has not yet removed every withdrawn discovery and activation path.
2. Presence can remain stale when a harness does not end/detach or renew correctly.
3. Cross-process realtime is durable on replay but not live fan-out.
4. The action boundary is cooperative and in-process, not universal process/network
   confinement.
5. Child listener/protocol readiness, scheduler progress, and registry/package/schema
   convergence are incomplete.
6. SQLite foreign-key enforcement is opt-in until existing stores are proven clean.
7. Recovery mechanics exist, but exact-candidate backup/restore/rollback E4 is absent.
8. Legacy and withdrawn source increases import, packaging, and security surface until
   separately reviewed removal.

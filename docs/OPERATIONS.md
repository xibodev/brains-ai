<!--
last_verified: 2026-08-31T18:30:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 35ce5ff1b4a2eb8bce2777ca7e3cff4d7ceece99 plus the worktree contract correction and isolated Docker full quality, packaged browser, and real OpenCode/Claude/Codex mailbox UAT; installed-service recovery and deployment not verified
-->

# Brains Operations

## Scope and proof boundary

This document gives supported operating contracts and repeatable probes. Command
presence is E1 evidence only. No installed service, external GitHub connection, backup
drill, UAT environment, container, or deployment is verified here.

The supported product is Workspace-first coordination, human governance, SQLite
operations, service/wiring posture, and signed GitHub ingress. The following are
withdrawn even where current source still registers commands or flags:

- Runtime enrollment/execution, Personas, Pods, Projects, Issues, execution onboarding,
  execution Session supervision, running-agent chat, and Runtime process stop;
- Automation UI, managed Skills, recurring definitions, generic webhooks, and scheduled
  auto-fire;
- model gateway, provider routing, LiteLLM, and the tool launcher;
- semantic indexing/search, embeddings, code graph, and external freshness;
- Postgres as an operating backend and OpenTelemetry export;
- Telegram, Slack, WhatsApp, WhatsApp Web, and relay/bridge delivery;
- legacy dashboard and admin HTML.

Do not enable or operate those paths. No environment switch, pip extra, direct URL, or
explicit tool allowlist makes a withdrawn capability supported. BL-P0-09 owns removal
of the remaining discovery and activation surface.

## Install and start

Brains supports Python 3.11 and 3.12. Use an isolated installation:

```text
python -m pip install --user pipx
python -m pipx ensurepath
pipx install brains-ai
```

Initialize one Workspace and install the supervised user service:

```text
cd <project>
brains-ai setup --path . --service
```

The service runs without a terminal window, restarts on failure, and starts at login.
Run `brains-ai service status`, then open `http://127.0.0.1:8787/app`. `setup` prints the
generated admin-key location; use `brains-ai admin-key show --reveal` only when the key
is needed. Never place it in a URL, log, issue, fixture, or repository. Use foreground
`brains-ai serve-all` only for diagnosis or development.

The supported installed executable is `brains-ai`. Helpers that invoke `brains` are
obsolete.

## Supported command families

This is a capability summary, not an exhaustive `--help` copy.

| Family | Supported purpose |
|---|---|
| `setup`, `serve-all`, `serve`, `mcp`, `up` | Initialize or run the supported gateway/MCP stack. |
| `wire`, `unwire` | Add, inspect, or remove only the Brains-owned MCP entry for a supported harness. |
| `service install|start|stop|restart|status|logs|uninstall` | Manage the user-level supervised stack. |
| Session/state/task/claim/handoff/message/topic/help/checkpoint commands | Coordinate durable Workspace work. Mailbox-aware start/heartbeat/successor calls take a native Session ID plus an adapter binding-file path. |
| `mailbox register|phonebook|lookup` | Register one durable address through an adapter-owned binding file or inspect visible active addresses. |
| `mailbox send|broadcast|reply|forward|inbox|sent|thread` | Commit or inspect address-based durable mail. Agent operations require the attached Session plus binding file; human inbox reads require a local/browser human channel. |
| `mailbox notification-take|notification-settle` | Adapter-only fixed-nudge claim and observed-result settlement. These commands never return mail content or replace inbox pull. |
| knowledge/pattern/tool commands | Maintain reusable coordination knowledge and tool posture. |
| decision/governed/audit commands | Route human decisions and inspect governed effects. |
| `readiness`, `queue-health`, `recovery-policy` | Inspect supported operational posture. |
| `db migrations|migrate|diagnose|repair|fk-check` | Inspect or repair supported SQLite state. |
| `backup`, `backup-inspect`, `db verify-backup`, `restore` | Create, verify, inspect, or restore manifest backups. |
| Org/operator/credential commands | Manage explicit principals and access. |

Commands for dashboard, daemon, model launching, recurring/jobs, generic webhooks,
semantic/graph indexing, or feature-extra activation may still appear at HEAD. Treat
them as withdrawal defects, not operating alternatives.

## Process and port map

| Process | Default bind/port | Supported surface |
|---|---|---|
| Gateway | `127.0.0.1:8787` | `/app`, protected native `/v1`, `/health`, WS/SSE, signed GitHub ingress |
| MCP Streamable HTTP | port `9877`; bind controlled by supported MCP settings | Authenticated `/mcp` transport; `/sse` is explicit legacy compatibility only |

`serve-all` supervises the gateway and MCP children. The retired dashboard port and
WhatsApp Web sidecar are not part of the supported stack.

Loopback is the safe default. Do not publish gateway or MCP listeners without an
explicit ingress, credential, authorization, and CSRF/origin review. Container port
publication alone is not an ingress contract.

## State and configuration

`BRAINS_STATE_DIR` overrides the state root; the default is `~/.brains`. Supported state
may include:

- `brains.db` and SQLite WAL files;
- admin/operator/audit key files;
- encrypted secure settings and non-secret runtime overlay;
- service PID and rotating log files;
- optional generated Markdown views.

Provider OAuth caches, daemon state, execution transcripts, alternate-backend state,
and bridge state may exist from withdrawn features. Their presence is not readiness or
permission to configure them.

Configuration precedence depends on the specific supported setting, but an operator
must assume a long-lived process keeps its loaded value until that process explicitly
reloads or restarts. A write is not complete until its response states the required
reload/restart behavior and the affected process passes its probe.

Supported secret rules:

- environment values remain outside Git;
- encrypted settings are write-only through the API/UI and never return plaintext;
- admin-key rotation must re-key encrypted rows before replacing a file-managed key;
- external secret managers remain authoritative when the process reads its key from
  the environment;
- errors, logs, audit summaries, and public defect proposals must not contain secret
  values.

## Authentication and authorization

Every protected native route resolves one credential-store row to one principal.
Credentials are hashed at rest and carry kind, owner, provenance, expiry, revocation,
and available Org/Workspace scope.

| Surface | Supported boundary |
|---|---|
| `/health` | Open liveness only; never readiness. |
| Native `/v1` | `require_api_key` plus route-specific Org/Workspace capability. |
| `/app` | Signed browser cookie bound to the credential that minted it, or accepted header/key flow. |
| WS/SSE | Principal plus server-derived topic authorization, revalidated during the connection. |
| MCP Streamable HTTP `/mcp` | Credential-store lookup and loopback Host policy by default; SSE is explicit legacy compatibility only. |
| MCP stdio | Local OS process boundary; inherits local state authority. |
| GitHub ingress | Signature, delivery/event headers, exact repository binding, replay refusal. |

Roles are `owner`, `admin`, and `member`. Route capability checks, not labels alone,
provide authorization. A principal that may read an Org but lacks a capability receives
`403`; an entity outside readable scope receives the same `404` as an unknown entity.
An Org cannot lose its last owner through normal API mutation.

Use operator credentials with explicit membership for people and harnesses. Treat the
bootstrap admin key as install-wide authority.

Useful credential probes:

```text
brains-ai credentials list
brains-ai credentials doctor
```

Revocation is explicit. A process that cannot see a local key must not infer that the
credential should be revoked install-wide.

## Wiring

Supported adapters target Copilot CLI, Claude Code, Codex, and OpenCode using each
harness's native MCP schema. Wiring must:

- preserve unrelated configuration;
- write a Brains ownership marker;
- back up a changed file;
- report conflicts rather than overwrite them;
- make `unwire` remove only the Brains-owned entry;
- select a transport the harness supports and readiness can probe.

Probe without changing configuration:

```text
brains-ai wire --status
```

The default wired guidance may recommend only normal-install capabilities: Workspace
coordination, knowledge, and bounded non-semantic repository lookup. Semantic search,
graph, execution, automation, and bridge guidance is a BL-P0-09/BL-P1-18 defect.

## Service operation

Supported user-service renderers target Windows Task Scheduler, macOS launchd, and
Linux systemd user services.

```text
brains-ai service status
brains-ai service logs
```

`brains-ai service install` preflights the requested loopback gateway port. An
explicit unavailable port is refused. When no port is supplied and the default
cannot be bound, the installer selects a bindable fallback, writes it into the
OS service definition, and persists the non-secret endpoint contract under the
Brains service state directory. `service status` probes those persisted ports
and returns the effective gateway, console, and MCP URLs. Installation refuses a
specification whose gateway and MCP ports are the same, because the supervisor
rejects that pair deterministically. The supervisor preflights every enabled
listener on its actual bind host. A blocked bind holds a bounded degraded state
(`BRAINS_SUPERVISOR_PREFLIGHT_WAIT_SECONDS`, default 300, retried with backoff);
when the window closes it exits with code 3, which the systemd unit excludes
from restart (`RestartPreventExitStatus`) instead of relaunching forever.

After startup, each owned child has a protocol-aware listener watchdog. Gateway must
answer `/health`; MCP and any explicitly enabled dashboard child must complete a bounded
HTTP response. A child that never becomes ready or stays alive after losing its listener
has its owned process tree terminated, allowing the supervisor's existing bounded
restart loop to recover it. On Windows the tree is stopped with Task Scheduler-compatible
`taskkill /T`; on POSIX each child uses its own process group.

A healthy status requires all of the following:

1. the recorded PID belongs to the expected Brains command and process instance;
2. the expected listener belongs to the supervised stack;
3. a bounded protocol probe succeeds on the persisted endpoint;
4. required children are healthy and not in an unbounded restart loop.

An alive PID without a serving listener is degraded. Stop/uninstall must target only a
verified owned process. An explicit busy gateway port is refused; when the default port
is unavailable, installation may persist a bindable loopback fallback and must report
the resulting console/MCP endpoints.

Clean-host Windows restart/rollback remains an E4 requirement.

`brains-ai setup --path . --service` or `brains-ai service install` is the supported
way to avoid keeping a `serve-all` terminal open. Windows uses a hidden per-user Task
Scheduler task whose action is the installed environment's verified `pythonw.exe`; the
installer refuses a headed or missing launcher rather than leaving a persistent console.
Supervisor and child output continues to rotate in the service log. macOS uses launchd
and Linux uses a systemd user service. Inspect it with `brains-ai service status`. Do not
run a second foreground `serve-all` against the same ports or state directory.

## Coordination operation

A harness starts or resumes a Workspace-scoped Session, performs work, and ends or
detaches it. Tool calls renew liveness; an expired PID-less handle becomes dormant and
releases eligible ownership rather than being marked as a failed execution.

Operational invariants:

- use one canonical Workspace identity and honor path aliases;
- claim before editing shared scope;
- keep one owner for a task/claim transition;
- checkpoint at natural interruption boundaries;
- leave a handoff when work stops;
- use asynchronous peer help for work that may outlive a client wait;
- include evidence in peer answers;
- end or detach the Session when the harness exits;
- do not infer running-agent delivery from durable mail, events, or a command row.

`inbox_wait` is the bounded wakeup primitive for direct mail, subscribed topics, and
claimable peer help. A timeout means no wakeup during that wait, not that durable work
expired. Empty mailbox reads do not count as adoption.

Queue health:

```text
brains-ai queue-health status
brains-ai queue-health repair
brains-ai queue-health repair --apply
```

The default repair is dry-run. Apply may expire leases and run deterministic continuity
repairs; it must not delete an open approval, unread message, unresolved feedback, or
other human-owned work.

Running-agent message delivery and Runtime process stop are withdrawn. Use harness-native
interaction outside Brains and record only what can be truthfully observed.

## Experimental features and feedback

Experimental is a support label for behavior whose normal-use ergonomics or edge cases
remain uncertain. It is not a running field trial and does not enable embedded analytics.
All release candidates still require automated contracts and isolated end-to-end UAT.

The agent feedback inbox (BL-P1-15) is the ordinary feedback path. Humans triage reports,
and engineers reproduce and revise behavior as appropriate. BL-P1-16's welcome and
mailbox behavioral analytics surface is removed.

Ephemeral peer review (BL-P1-20) is implemented but not admitted: normal help currently
defaults to auto-launch and remote execution still overlaps withdrawn Runtime surfaces.
Do not advertise it as experimental until both boundaries are corrected and its isolated
UAT passes.

Feedback reporting stores redacted Workspace-scoped records. Triage/promotion is human
only and cannot edit the roadmap or authorize release. Ephemeral review uses a temporary
tracked snapshot, bounded runtime/output, source-fingerprint checks, and no automatic
merge or execution.

## Health and readiness

Liveness probe:

```text
GET /health
```

Healthy liveness means the gateway process answered. It says nothing about SQLite
writes, migrations, child listeners, MCP, queue progress, wiring, GitHub, backups, or
user journeys.

Protected readiness probe:

```text
brains-ai readiness
GET /v1/admin/readiness
```

At HEAD, readiness reports bounded storage/migration, coordination queue, durable-mail,
and recovery-policy checks. Durable mail separates invalid registration/live attachment,
aged unread, stalled/failed notification, and SMTP retry/failure/uncertainty. A detached
mailbox with unread accepted mail is reported but is not degraded until the unread-age
threshold is crossed. Runtime execution is withdrawn and does not affect normal-product
readiness. The active backlog still requires child
listener/protocol health, scheduler progress, registry freshness, installed package and
migration/schema identity, and configured wire transport checks.

No readiness field may return a secret or raw exception. A `ready` response still does
not prove GitHub operation, browser journeys, backup/restore, ingress, or deployment.

## SQLite migrations and integrity

Read the migration state before applying anything:

```text
brains-ai db migrations
brains-ai db migrate
```

The migration ledger is ordered and checksummed. Edited history, unknown migration IDs,
gaps, interrupted/failed attempts, missing implementation, and schema/model drift fail
closed. Restore a modified historical file and add a new migration; never alter the
recorded migration to force an upgrade through.

Migration `150_durable_mailboxes` is additive. It creates the durable mailbox,
attachment, thread, message, delivery, notification, per-operator
SMTP setting, retryable SMTP outbox, and legacy-inventory tables. It leaves every
existing mail/tool-link row unchanged and inventories rows present at migration time by
only table/key plus an `unverified` reason; no subject, body, address, owner, or
credential is copied.

The application now provisions operator inboxes and supports explicit agent registration,
reattachment, visible phonebook/lookup, and proof-bound Session lifecycle. Binding values
are read from a local adapter-owned file by CLI/stdio MCP or sent in the protected
`x-brains-mailbox-binding` registration header; they are hash-only in SQLite and are not
returned, logged, or placed in CLI arguments. On POSIX, binding files must be owner-only;
all platforms reject files larger than 1 KiB. Authenticated SSE adapters may reference
only files under `BRAINS_STATE_DIR/mailbox-bindings`; resolved symlinks may not escape
that directory. Do not use `tool_session_links` or values
such as `current` as mailbox identities. A mailbox-aware `session-start`, heartbeat,
resume, or successor call must carry the actual native Session ID and binding-file path.
Missing/wrong/conflicting proof fails closed without identifying which condition failed.

Address-based `mailbox send`, explicit `mailbox broadcast`, `reply`, `forward`, `inbox`,
`sent`, and `thread` now use migration 150 rows. Every send supplies an operation ID;
retry with the same sender/action/operation ID returns the original message, while a
changed payload is refused. Agent send/read requires the current Session attachment plus
binding proof. `mailbox inbox` is non-mutating by default; pass `--mark-read`
deliberately. HTTP GET history is always non-mutating, and explicit POST read routes
record per-recipient attribution. Successful send means local SQLite acceptance only;
it does not claim agent wakeup, live harness delivery, or SMTP copy.

Migration `151_mail_notification_state` constrains attachment modes and the
`queued -> claimed -> delivered|failed` attempt lifecycle. Pull is the default. A
harness integration that it has actually installed may explicitly register `immediate`
for Claude Code/OpenCode or `turn_boundary` for Codex; Copilot CLI accepts only pull.
For a stronger mode, `mailbox notification-take` claims one attempt and returns only the
fixed nudge `Brains mailbox: new mail is waiting. Pull your durable inbox.` plus bounded
attempt metadata. It never returns subject, body, sender, recipient, or delivery
identity. The adapter records what it observed with `notification-settle`; failure,
detach, mode change, timeout, and prior inbox read leave local delivery intact.

Current `wire` output reports `mailbox_notification_mode: pull` for all harnesses. The
wiring command installs MCP/rules only, not a notification hook or plugin, so stronger
modes must not be selected merely because a harness platform could support one. There is
no Brains follower daemon or model-input injection in this slice. Use proof-bound
`mailbox inbox` as the authoritative recovery path.

The Coordination browser mailbox desk supports authorized human reads and operator
compose/reply/forward; agent mailboxes remain read-only because agent send authority
requires adapter-held proof. The notification take/settle protocol is available to
explicit adapters. An operator may configure one external copy destination on their own
operator mailbox from the same human-bound desk. The destination is encrypted and must
answer a short-lived emailed challenge; status returns only a masked hint.

Migration `152_mail_smtp_state` constrains destination/consent and outbox transitions.
Verification enables `notification`, which sends a constant subject/body that says new
mail is waiting and to reply inside Brains; it includes no sender, local subject/body,
mailbox address, or delivery ID. `full_body` requires a separate confirmation and stores
who consented and when. Destination/mode changes cancel `queued`/`retry` copies and are
refused while a `sending` lease is live.

The MCP scheduler drains a bounded batch each tick. It records an audit attempt before
network I/O, uses one stable SMTP `Message-ID` per outbox row, retries only failures known
before the SMTP send stage, and marks send-stage or expired-lease outcomes `uncertain`
instead of retrying blindly. SMTP state never changes local acceptance/read state.
`mailer_status` remains the probe for install-level SMTP configuration; mailbox-copy
status is shown in Coordination. Test evidence uses synthetic SMTP only, so verify a real
provider separately before operational reliance. There is no inbound email or reply
parsing. Concrete notification hook/plugin installation also remains unavailable.
Rollback uses the normal application/archive compatibility contract; do not drop these
tables from a store that a newer build may have written.

Prefer Workspace archive when mailbox history must remain. The explicit destructive
Workspace prune treats an agent mailbox as owned by its Workspace and removes that
mailbox plus its required descendant rows; operator mailboxes have no Workspace foreign
key and are not selected by that cascade.

Diagnose before repair:

```text
brains-ai db diagnose
brains-ai db repair
brains-ai db fk-check
```

Diagnosis runs SQLite integrity, foreign-key, and product-invariant checks. Missing
coverage is unknown, not clean. Repair is dry-run unless `--apply` is explicit. Apply
must take the write fence, create or verify a current manifest backup, perform only the
approved deterministic plan, verify the result, and roll back as a unit on failure.

Foreign-key enforcement remains opt-in until `db fk-check` proves the store clean.
Postgres commands and drivers are withdrawn and must not be used as an alternate
operating path.

## Backup, restore, and rollback

Supported SQLite backups use the online backup API, not a raw copy of the live WAL
file. Relevant commands:

```text
brains-ai backup
brains-ai backup-inspect
brains-ai db verify-backup <archive> [--expect-source <sqlite-file>]
brains-ai restore
brains-ai recovery-policy
```

A valid archive has a readable manifest, verified payload hash, compatible schema
history, source identity where available, and a successful isolated restore. A bound
verification additionally proves the archive still represents the current source at
the instant of the probe.

Restore is destructive and requires explicit confirmation. Before rollback:

1. stop the owned service tree so writers are quiescent;
2. capture and verify a fresh backup of the state being replaced;
3. restore an archive compatible with the target build;
4. restart on loopback;
5. run liveness, readiness, integrity, auth, and in-scope journey probes;
6. reopen ingress only after acceptance.

The recovery policy declares scope, schedule, retention, encryption owner, offsite
owner/location, RTO, RPO, and restore-drill expectation. Brains does not schedule
backups itself. A complete policy is not evidence that a backup or drill occurred.

## Governance and audit

Inspect governed actions and the audit chain:

```text
brains-ai governed-list --limit 50
brains-ai governed-sweep
brains-ai audit-list --action-prefix governed.
brains-ai audit-verify
```

An approval-required supported action must have a matching, unexpired, attributable
decision before execution. Arguments are normalized and secret-redacted before the
digest or human request is stored. Request, decision, attempt, and result are distinct
states. A released handoff is not a success claim.

The audit chain is HMAC-protected with a signed head. Do not clear or rewrite a broken
head. `audit-adopt` is only for a genuine pre-signed-head store and verifies before it
signs. A failed verification is a tamper/integrity incident, not a migration hint.

The execution boundary is in-process. It governs paths that use it; it does not contain
arbitrary commands or network calls made directly by an external coding-agent harness.
Record such effects as external/unverified rather than governed.

## GitHub operation

GitHub ingress requires:

- the configured webhook secret;
- `X-Hub-Signature-256`;
- delivery and event headers;
- exact normalized repository-to-Org binding;
- durable delivery-ID replay refusal.

Repository names and secrets are not returned by redacted configuration posture. Live
operation must be tested with a controlled disposable repository before it is claimed.

BL-P1-19 does not yet provide public defect creation. When implemented, the operator
must approve the exact title, body, and metadata after redaction, dedupe, and public
issue search. No agent or background worker may upload local evidence automatically.

## Isolated UAT

UAT uses:

- isolated HOME, `BRAINS_STATE_DIR`, SQLite database, ports, and credentials;
- disposable or read-only source plus an unchanged-worktree assertion;
- simulated external execution unless a named integration is the test subject;
- exact candidate SHA/artifact identity;
- complete process-tree and temporary-state teardown;
- machine-readable evidence stored outside canonical docs.

For the advertised product, UAT must exercise Workspace-first J1/J7-J11 behavior and
containment of withdrawn J2-J6/F10 paths. Existing browser specs that still activate
withdrawn source are test-debt inputs, not acceptance evidence.

## Known gaps

- BL-P0-09 withdrawal containment is not implemented.
- Windows service listener recovery needs clean-host E4 after package upgrade.
- Session end/detach and liveness renewal are not reliable across every harness.
- Cross-process realtime live fan-out is absent.
- Governed action confinement is cooperative and in-process.
- Readiness lacks complete child, scheduler, registry, transport, and package/schema
  convergence checks.
- SQLite FK enforcement is not the default for unproven existing stores.
- Exact-candidate backup/restore/rollback E4 is absent.
- GitHub external operation and the public defect relay are not verified/implemented.
- Dev Compose, shared-Postgres harnesses, UAT sidecars, and box deployment scaffolds
  contain withdrawn or inconsistent topology and are not supported deployment paths.

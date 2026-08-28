<!--
last_verified: 2026-08-05T12:22:18.971-06:00
verified_by: GitHub Copilot CLI
verification_basis: working-tree candidate based on HEAD 865794899901b7893759bb5b582f089b856a268f; static inspection and targeted tests for operational readiness, coordination queue health, recovery policy, and Runtime/service PID identity (BL-P1-09, BL-P1-12, BL-P1-13); live-provider recovery not verified; deployment not verified; UAT not verified
-->

# Brains Architecture

## Product identity and implementation

Brains is the operator control plane and the executable system:

- Python distribution and CLI: `brains-ai`
- Python namespace: `brains`
- modern React package: `brains-spa`
- MCP tool prefix: `brains_`
- default state directory: `~/.brains`
- browser product: Brains

These names are one product identity across documentation, packaging, source, state, MCP, and browser surfaces.

## System context

```text
Human operator
  | browser, CLI, MCP client, bridge reply
  v
Brains
  |-- modern operator SPA
  |-- native product API and realtime
  |-- coordination and MCP
  |-- model gateway and providers
  |-- runtime daemon and agent processes
  |-- storage, audit, backup, observability
  v
Repositories, coding CLIs, model providers, GitHub, and optional messaging systems
```

The default operating model is local-first. SQLite and state files live on one machine unless Postgres or remote Runtimes are configured. A shared database does not by itself provide tenant isolation.

## Runtime containers and processes

### `serve-all` topology

```text
Browser / API clients
       |
       v
gateway process :8787
  - `/app`
  - `/v1`
  - `/admin`
  - WS/SSE
  - model router/providers
  - process-local EventBus + shared durable event log
       |
       +----------------------+
                              |
Browser                       |       Agent MCP clients
  |                           |              |
  v                           v              v
dashboard process :9876    shared DB     MCP process :9877
  - `/dashboard`            and files      - SSE/stdio tools
  - `/admin`                               - recurring scheduler

Remote/local Runtime daemon
  - detects CLIs
  - registers and heartbeats Runtimes
  - polls/claims derived assignments
  - starts agent CLI processes
  - ingests Session events
```

`brains-ai serve-all` supervises gateway, dashboard, and MCP as child processes with restart backoff. Default bind addresses are loopback. The three children share database and files but do not share Python memory.

### Process isolation implications

- Realtime *live fan-out* is gateway-process local; the durable log (`realtime_events`) is shared, so a client resuming by cursor sees events published by any process.
- MCP scheduler mutations do not automatically publish through the gateway EventBus, and are picked up on the next cursor resume rather than pushed.
- Runtime configuration reload affects only the process that reloads it unless all processes restart or independently reload.
- Rate-limit counters, provider circuit state, settings objects, and in-memory caches are process-local.
- SQLite is a concurrent multi-process file database with one-writer constraints; WAL, a busy timeout, and normal synchronous mode are configured.
- A process reporting healthy does not prove the other child processes or external providers are ready. `GET /v1/admin/readiness` (bootstrap-admin only, BL-P1-09/BL-P1-12) is a separate, protected contract that reports one overall ready/degraded verdict from storage/migration, coordination-queue, Runtime-lifecycle, and recovery-policy component checks — `GET /health` itself remains open and liveness-only.

## Component map

| Component | Responsibility | Primary location |
|---|---|---|
| Application composition | Gateway FastAPI app, route mounts, SPA/static, startup DB/key/operator setup | `src/brains/main.py` |
| Identity and authorization | Credential store, principal resolution, roles/capabilities, Org/Workspace scope, FastAPI gates | `src/brains/authz` |
| Modern console | Brains React UI under `/app` | `frontend`, `src/brains/web/spa` |
| Native product API | Orgs, Runtimes, Personas, Projects, Issues, Sessions, config summary, usage, operational readiness/queue-health/recovery-policy (bootstrap-admin) | `src/brains/api` |
| Realtime | Closed topic grammar, server-derived subscriptions, durable event log with cursor replay, WS/SSE transports | `src/brains/api/ws.py`, `src/brains/events` |
| Domain controls | Validation and persistence for product and coordination entities | `src/brains/control` |
| Runtime daemon | CLI discovery, enrollment config, heartbeat, assignment loop, execution | `src/brains/daemon` |
| Executor and gate | Agent process launch, transcript store, action shims, in-process execution boundary, relay | `src/brains/exec` |
| Model edge | OpenAI/Anthropic compatibility, errors, token count, model catalog | `src/brains/api/openai.py`, `anthropic.py`, `models.py` |
| Router/providers | Explicit resolution, tier/auto routing, retries, circuits, adapters | `src/brains/router`, `src/brains/providers` |
| MCP | SSE/stdio server, tool registry, auth, recurring scheduler | `src/brains/mcp` |
| Context | Repository/doc indexing, embeddings, search, graph, freshness, prewarm | `src/brains/context` |
| Storage | SQLAlchemy engine, models, repositories, migrations | `src/brains/storage` |
| Governance | asks, approvals, governed actions, audit, claims, handoffs, task and knowledge controls, coordination queue-health/continuity repair | `src/brains/control`, `src/brains/govern`, `src/brains/audit` |
| Recovery | SQLite/Postgres backup, manifest validation, destructive restore, declared recovery policy (scope/schedule/retention/encryption/RTO/RPO/offsite/drill) and its compatibility precheck | `src/brains/backup`, `src/brains/control/recovery_policy.py` |
| Legacy browser | Server-rendered dashboard and admin/config surface | `src/brains/dashboard`, `src/brains/admin`, templates |
| Integrations | Webhooks, relay, Telegram/Slack/WhatsApp bridges, wa-web sidecar | `src/brains/api/webhooks.py`, `src/brains/bridges`, `services/wa-web` |
| Operations | CLI, wiring, OS services, supervisor, periodic Runtime-staleness sweep, PID identity verification, optional telemetry | `src/brains/cli`, `wire`, `service`, `observability` |

## Product execution flow

### Runtime enrollment

```text
operator -> POST /v1/runtimes/enrol
         <- one-time token and command
machine  -> POST /v1/runtimes/enrol/redeem
daemon   -> detect CLIs
daemon   -> register one Runtime per machine/tool
daemon   -> heartbeat
browser  <- poll and gateway realtime status
```

The enrollment token is the redeem credential. It is stored only as a hash, it expires, and redemption is a single conditional update (`UPDATE ... WHERE redeemed_at IS NULL`), so concurrent redemptions produce exactly one winner. The token also names the Org it was minted for - the `default` Org when the operator names none - so the intended Org is part of the credential the machine presents rather than something resolved at redeem time. Redemption mints a Runtime-narrow, Org-bound credential rather than an operator key: it authorizes only the Runtime operations of the machine it was minted for, it expires, and it is revocable on its own. The Org is the one the token names, so a redeemer cannot widen its own scope.

Redemption claims the machine before it mints anything, in the same transaction as the token claim. The claim counts the machine's live Runtime credential as well as its `runtimes` rows, because a redemption with an empty CLI list registers no Runtime at all and a claim read only from `runtimes` would see an enrolled machine as unclaimed. A token whose Org is not the machine's owner mints nothing and is refused with the same wording as an unknown token, so the unauthenticated route cannot be used to probe which Org owns which machine id; the refused token stays unredeemed and can be retried against a machine its Org may claim. The model is deliberately claim-on-first-use: an Org that holds a valid connect token can enrol a machine id that has never been seen, which is the same trust as being handed that token, but no Org can take a machine id another Org already holds, and no machine-generated proof of identity is claimed.

### Issue dispatch

```text
operator -> create/assign Issue
operator -> dispatch Issue
hub      -> create Session and derive assignment from Issue + Persona/Pod + Runtime
daemon   -> poll, claim, start agent CLI
daemon   -> ingest Session events and acknowledge assignment
browser  -> fetch persisted events, then subscribe for realtime
```

There is no separate assignment table. Open Issues plus assignment resolution form the queue. Hub Session state and the daemon's local execution Session are not fully reconciled at terminal state.

### Model request

```text
client -> authenticated OpenAI/Anthropic-compatible route
       -> explicit model/tier resolver
       -> classifier only for explicit `brains/auto`
       -> provider adapter with optional retry/circuit policy
       -> upstream/local model
       -> normalized response and usage record
```

Unknown explicit models fail rather than being silently rerouted. Default tier configuration uses the `echo` provider until the operator configures real providers.

### Governed action

```text
caller  -> classify command/tool (absolute, Windows, wrapper, inline code, module, runner)
        -> reserve governed action + audit entry            [one transaction]
local   -> record allow                                     [one transaction] -> effect
outward -> file approval bound to action/tool/target/args-digest + TTL
                                                            [one transaction]
        -> human resolves
        -> consume approval once, if unexpired and scope-matched
                                                            [one transaction] -> effect
        -> record result, or the handoff when the outcome
           is not observable from here                      [one transaction]
```

Everything that can produce an outward effect uses this one contract
(`src/brains/govern`): the PATH-shim gate, `brains.exec.guard` (every process
Brains itself launches, including recurring/autopilot spawn), and the CLI/MCP
governance surfaces. Each transition commits with its audit entry, so a
governed action whose record cannot be written is refused before the effect
rather than continuing unrecorded. An approval is spendable exactly once
(`UNIQUE(governed_actions.approval_code)` plus a conditional consume), only
within `BRAINS_APPROVAL_TTL_SECONDS`, and only for the exact normalised
argument vector that was reviewed. A repeated `idempotency_key` replays the
recorded outcome instead of executing or deciding again. The approval code
itself is minted above the highest suffix in `approval_requests` *and* in
`governed_actions.approval_code`, because a Workspace prune cascades away the
ASK rows but never the governed actions that spent them: deriving the next code
from the live table alone would re-issue a code that a permanent row still
holds. If a duplicate ever does reach the store, binding it is flushed on its
own so the refusal is reported as an approval-code collision rather than as an
audit-append failure over a chain that is intact.

A network fetcher is local only when every endpoint it names parses as a
loopback address literal (`127.0.0.0/8`, `::1`, bracketed or not, with
credentials and a port stripped) or is the reserved `localhost` name or a
`.localhost` subdomain. A host that merely *begins* with a loopback literal -
`127.0.0.1.attacker.com` - is a name its owner resolves, so it is outward and
gated, as are `0.0.0.0` and `host.docker.internal`, whose meaning depends on
what answers the query, and an obfuscated numeric spelling (`2130706433`,
`0x7f000001`, `127.1`), which depends on the resolver rather than on argv.
"Every endpoint it names" is wider than the URL: `--flag=value` targets
(`curl --url=...`), values that redirect the connection (`-x`/`--proxy`,
`--proxy1.0`, `--socks4`/`--socks5`/`--socks5-hostname`, `--preproxy`,
`--doh-url`, `--dns-servers`, `wget -e http_proxy=...`) and the address half of
a composite override (`--resolve HOST:PORT:ADDRESS`,
`--connect-to HOST1:PORT1:HOST2:PORT2`) are each judged on their own, so a
loopback URL routed through a remote proxy is gated. A local path is
distinguished from a target by shape rather than by "contains a backslash" -
`C:\dir`, `.\out`, `\\?\C:\dir`, `\\.\pipe\x` and `\\localhost\share` name no
host, while `\\fileserver\share`, a URL with a backslash in its authority, and
any other backslash token are outward or ambiguous, and ambiguity gates. A bare
number is a port only where the grammar makes that unambiguous (`nc`/`ncat`/
`telnet` `HOST PORT`, in range); anywhere else it is an address spelling.

Redaction is canonical and happens at the request boundary, not at each sink
(`src/brains/govern/redaction.py`): URL credentials, `NAME=VALUE` pairs with a
secret-shaped name whatever their prefix, `--user`/`--password` and the
tool-scoped short forms (`curl -u`, `curl -b`, `mysql -pSECRET`,
`redis-cli -a`, `sshpass -p`, `mongosh -p`) are removed, as are the short forms
that only mean a credential under one subcommand (`docker login -p`,
`podman login -p`), credential-bearing
header values (`Authorization`, `Cookie`, `X-Api-Key`), secret fields inside a
request body (form-encoded or JSON), known provider token shapes and JWTs, and
high-confidence opaque tokens - all before the digest, the stored
summary, the audit payload, the ASK body or a bridge message can see them.
The summary and every recorded reason or error are redacted with the same
tool-aware rules, so a caller that hands over a raw command line, or a
subprocess error that echoes one, loses the credential that only a flag
identifies.
A name is secret-shaped when it contains a distinctive word (`token`,
`password`, `api_key`) or when a whole segment of it is a bare credential word
(`DB_PASS`, `MASTER_KEY`, `dbPass`); segment bounds are what keep `bypass`,
`passenger`, `keyboard` and `monkey` out of it. Scope follows the binding: a
name bound to its own value (`--key=x`, `KEY=x`, a header, a body field) is
redacted, while a *lone* bare word claims the argument after it only when it is
qualified (`--db-pass`, `--master-key`) - so `aws s3api delete-object --key
prod/db/backup.tar.gz` keeps the object it names, and two different objects
remain two different digests. A secret-shaped value is still redacted by shape
wherever it appears.
Ordinary arguments - a git SHA, a path or `s3://` URI, a branch name, an
accepted content type, `python -u script.py`, `ssh -p 2222`,
`docker run -p 8080:80`, `redis-cli -p 6379`, `wget -b URL` - are left intact,
because a digest that redacted everything could neither tell one reviewed
command from another nor show an operator what they are approving.

Effects that are not database writes cannot share a transaction with their
record, so `brains.audit.required_effect` gives them the next strongest thing:
`<action>.attempted` commits before the effect, `<action>` or `<action>.failed`
is appended after it. Admin overlay and env-override writes, backup, restore
and `db repair --apply` use it, so none of them can complete unrecorded and
none of them can be recorded as done before they are.

An attempt carries its own clock (`governed_actions.attempt_started_at`), so
the lease that decides whether an in-flight action was abandoned is per
attempt, not per row: a retry that legitimately resets an abandoned attempt
starts a fresh lease instead of being born expired, concurrent retries settle
the old attempt through one conditional update so exactly one of them opens the
new attempt, and `mark_executing` refreshes the lease at the moment the effect
starts. The expiry rules have an owner that actually runs: the recurring
scheduler tick calls `govern.run_maintenance()` (also `brains-ai
governed-sweep`), and each settlement is guarded on the status and attempt it
read, so a live execution is never swept out from under itself.

An action that is *executing* is judged on silence rather than on elapsed
runtime, because how long an execution has been running says nothing about
whether it is alive: a governed agent session, a deploy, or the Windows child
the PATH-shim gate waits on can legitimately outlast any fixed budget, and a
sweep that settled them would fabricate a failure for work still in progress
and burn the idempotency key with it. The owner proves liveness instead
(`governed_actions.heartbeat_at`, migration 128): `govern.heartbeat` renews the
lease under a conditional update matched on action, `executing` status *and*
attempt, so it is safe from any process, cannot resurrect a terminal row (a
heartbeat racing a completion loses), and cannot let a hung owner keep the
attempt that replaced it alive. A heartbeat is not a transition and appends
nothing to the audit log, which keeps proof-of-life out of the record that
exists for decisions. `run_governed` and the gate's Windows child path hold the
lease through `govern.execution_lease` - a daemon beater that sleeps on an
event, stops before the outcome is recorded, and never delays interpreter exit
- and a storage failure is counted and logged rather than reported as a
renewal. An owner that crashed simply stops renewing, so the sweep still
settles it once `BRAINS_EXECUTION_LEASE_SECONDS` of silence has passed, as
"abandoned while executing" - unknown, not failed-without-effect.

A released action is settled by the process that released it, on both tiers.
Where the outcome is observable the outcome is recorded; where it is not, the
record says so instead of guessing. The PATH-shim gate on POSIX replaces its
own process with the real binary (`os.execv`), so the last thing it can
truthfully record is the handoff: the row is settled `released` - terminal, and
therefore never swept - which claims neither success nor abandonment. If
`execv` itself fails, the process is still there and records the failure. On
Windows the binary runs as a child, so its exit status is recorded as success
or failure. A command Brains runs to completion itself (`brains.exec.guard`
`run`) is settled from the same evidence - `exit 0` succeeds, any other exit
fails as `exit N` - because it is run with `check=False` and would otherwise
return a failure that the record called success; a spawned child
(`guard.spawn`) outlives the call, so only the launch is recorded. A
local-tier command takes the same lifecycle as a gated one, which
is what stops a command that ran to completion from later being swept as
"abandoned before any effect".

The boundary is in-process. It covers what Brains launches and what the shims
intercept; it does not contain a third-party agent CLI that calls an absolute
path itself, rewrites `PATH`, or opens a raw socket, and shapes that cannot be
classified (`shell=True`, a string command line) are denied rather than
allowed. That residual reach is BL-P0-03.

### Audit chain

Each `audit_log` row is `HMAC-SHA256(key, prev_hash || canonical_json(entry))`.
Appends claim the single `audit_chain_head` row before reading their
predecessor - a write on SQLite, `SELECT ... FOR UPDATE` on Postgres - so two
Brains processes against one store cannot fork the chain, and an append over a
head that no longer matches the newest stored row, or whose signature does not
match its contents, is refused. The head triple (`seq`, `head_hash`,
`head_entry_id`) is itself HMAC-signed with the audit key, so an attacker who
truncates `audit_log` and moves the head to match is still detected. The head
also counts every append ever made, which is what makes a truncated tail
visible: the surviving rows still chain cleanly, but the count does not match.

An *unsigned* head over a non-empty log is itself a divergence: verification
fails and every append is refused, so clearing `head_mac` cannot present a
truncated store as a pre-signature one. The store's commitment to signed heads
persists in `audit_chain_head.adopted_version`/`adopted_at`, and the log itself
carries the evidence - a fresh store's first append writes an
`audit.chain.initialized` genesis marker, and adoption writes
`audit.chain.adopted` - so removing either marker breaks the `prev_hash` of
everything after it. A genuine pre-signature store is adopted once, explicitly,
by `brains-ai audit-adopt`, which verifies every entry, the head triple and the
append count *before* signing and marking in one transaction, and refuses a
store whose log already records a signed origin. `chain_status()` reports
`head_signed`, `adopted_version` and `adoption_required` so the two states are
distinguishable without implying the stronger one.

`brains-ai audit-verify` and the `audit_verify` MCP tool fail closed on
mutation, deletion, insertion, truncation, a forged head, an unsigned head over
a non-empty log, a cleared signature, a missing head, and a count mismatch.
Because a deleted row would refuse every later append, the Workspace deletion
cascade clears an audit entry's Workspace reference instead of deleting the
entry.

Verification reads the log and the head from one consistent snapshot, so a
concurrent append cannot make an intact chain look truncated. Isolation is
backend-correct rather than a lock held over the scan: Postgres takes a
`REPEATABLE READ` snapshot, SQLite opens an explicit read transaction (pysqlite
starts one for writes but not for `SELECT`, which is what let two reads
straddle a commit), and under WAL that reader does not block the appender.
Verification writes nothing, so it never mutates or extends the chain it is
checking; the same snapshot backs `chain_status()`, so its verdict and its
counts describe the same instant.

## Data architecture

### Durable SQL state

SQLAlchemy declares product, coordination, routing, retrieval, audit, and automation tables. Major families:

- identity and scope: operators, Orgs, members, Workspaces, memberships;
- product execution: Runtimes, enrollment tokens, Personas, Projects, Issues, comments, Sessions, events, Skills, Issue usage attribution, and durable onboarding attempts/steps;
- governance: approvals, handoffs, tasks, claims, mailbox, snapshots, patterns, help, checkpoints, governed actions, audit log and audit chain head;
- automation: Pods (`squads` compatibility identity plus `pod_profiles` and Persona `pod_members`), recurring definitions/runs, webhook triggers/deliveries;
- retrieval: sources, artifacts, chunks, metadata, code graph, knowledge;
- routing/usage: traces, route decisions, freshness, memories, usage ledger.

SQLite is the default. Postgres is optional through the `postgres` extra and synchronous SQLAlchemy path.

### Non-database state

The state directory contains or may contain:

- `brains.db` and SQLite WAL files;
- admin and operator key files;
- audit key;
- `secrets.env`;
- runtime overlay YAML;
- daemon JSON configuration;
- provider OAuth/cache material;
- executor transcripts and metadata;
- service PID and rotating log files;
- optional generated Markdown views.

These files are operational state and require separate ownership, backup, and secret handling.

### Migration behavior

Schema evolution is a single ordered, checksummed contract. `init_db()` runs it
on every entry point and is the only startup path.

- The corpus is `0000_baseline` (the frozen per-backend DDL under
  `src/brains/storage/baseline`), the historical ledger markers `0001_initial`,
  `0002_schema_versions`, `104_squads`, `111_recurring_runs`,
  `112_webhook_triggers`, and the numbered disk deltas through
  `138_skill_attachments.py` under `src/brains/storage/sql_migrations`. Order
  is the lexical order of the migration ID.
- `Base.metadata.create_all` is not on the startup path. The only table the
  runner creates from the ORM is its own `schema_versions` ledger, which the
  baseline therefore excludes. A fresh database is created from the checked-in
  baseline artifact, so the meaning of the initial migration does not depend on
  the installed model code.
- The baseline is organised into `-- @baseline-block: table=<name>` blocks. A
  block runs only when that table is absent, so the baseline provisions a table
  together with its indexes and never touches a table an older store already
  has; in-place changes are the numbered deltas' job. Comment-only preambles
  are ignored rather than sent to a database driver, while executable unmarked
  SQL remains an unconditional block. `always` blocks carry
  their own guard: on Postgres, a deferred foreign key is matched on its
  identity in `pg_constraint` - constrained relation, constrained columns,
  referenced relation, referenced columns - and never on its constraint name, so
  a store whose foreign keys were created by an older `create_all` under
  Postgres' own `<table>_<column>_fkey` names gains no duplicate constraints.
- `schema_versions` records, per migration: order, content checksum, whether
  that checksum was recorded by the runner or adopted from a pre-checksum
  ledger, backend, status (`applied`, `skipped`, `failed`, `running`), attempt
  count, start/completion timestamps, duration, an outcome detail, and the error
  text of an attempt that rolled back. A store written by the previous ledger is
  upgraded in place: the columns are added, existing rows are marked `applied`,
  and their checksums are adopted rather than presented as verified.
- Adoption of a pre-checksum row is backend-aware, because that ledger recorded
  no backend and the runner that wrote it only executed deltas on SQLite - on
  any other backend it inserted the version without running anything:
  - No implementation for the active backend: the row becomes `skipped` with
    checksum origin `legacy-unproven`, so it is not frozen as an immutable
    applied sentinel and the delta still runs if that backend's implementation
    ships later.
  - An implementation exists and the legacy row could have executed it (SQLite,
    or a marker with no delta at all): the checksum is adopted as
    `legacy-adopted` - recorded, not verified.
  - An implementation exists but the legacy row cannot be evidence it ran here:
    the row stays `applied`, is labelled `legacy-unproven`, and is reported as a
    finding instead of being re-executed, because re-running a delta a store may
    already carry is not safe. The post-migration schema verification is what
    proves the resulting schema.
- Each delta runs in one transaction and is recorded `applied` only after that
  transaction commits. SQLite deltas run under `BEGIN IMMEDIATE` with explicit
  statement splitting; `executescript` is not used because its implicit commit
  would let a half-applied delta survive.
- A migration with no implementation for the active backend is recorded
  `skipped` with the reason, never `applied`. That is only permitted for the
  frozen set of historical SQLite catch-up patches whose target state the
  baseline provisions; any other missing backend implementation is refused by
  name, and the refusal states the `<id>.<backend>.sql` file that would satisfy
  it. Postgres therefore executes the baseline and skips the SQLite patches
  explicitly instead of recording them as applied.
- Detected and reported rather than absorbed: an edited migration
  (checksum mismatch, hard refusal), duplicate or malformed migration IDs,
  ledger gaps and out-of-order ledgers, interrupted (`running`) and failed
  attempts, migrations the ledger knows but this build does not ship, and a
  backend recorded against a different store.
- After migrating, the live schema is verified against the declared models.
  Missing tables or columns raise and name what is missing, so a model change
  without a migration fails at startup instead of at query time.
- Migration `123_session_state` defaults all pre-existing rows to `running`; it does not derive terminal state from `ended_at` or terminal events. `brains-ai db repair` derives that state from recorded `session_end` / `session_reaped` events and reports the rest as ambiguous.
- Migration `120_org_workspace` performs a one-time Workspace Org backfill; the generic Workspace registration path now also assigns the default Org, and `brains-ai db repair` backfills legacy Org-less rows.
- SQLite sets WAL, configurable `busy_timeout` (30 seconds by default via
  `BRAINS_SQLITE_BUSY_TIMEOUT_MS`), and `synchronous=NORMAL`. The timeout is
  installed before WAL negotiation on every connection so startup and ordinary
  multi-agent writer bursts receive the same bounded wait.
- SQLite foreign-key enforcement is off by default. Setting `BRAINS_SQLITE_ENFORCE_FOREIGN_KEYS=1` turns it on, and the connection hook then proves `PRAGMA foreign_key_check` is empty for that database before enabling it, raising instead of enforcing over a store that already violates its schema.

Foreign-key enforcement is available but is not the default, because existing
stores are not proven clean until they are diagnosed and repaired. The Postgres
baseline is generated and compiled for the Postgres dialect but is not executed
in this repository's default test run; the Postgres integration tests that
execute it require `BRAINS_TEST_PG_URL` and are skipped without it.

### Integrity diagnosis and repair

`brains.storage.integrity` reads the database's own foreign-key graph
(`PRAGMA foreign_key_list`) rather than a maintained table list, and uses it for
both diagnosis and dependency-safe cleanup:

- diagnosis runs `PRAGMA integrity_check` and `PRAGMA foreign_key_check` plus the
  product invariants in [BACKLOG.md](product/BACKLOG.md) BL-P0-07: terminal Session state
  contradictions, Org-less Workspaces, and orphaned or expired Session claims;
- rows whose correct value cannot be derived from stored evidence are classified
  `ambiguous_legacy` or `requires_operator` and are reported, not guessed;
- checks whose table or columns are absent on the store's schema are skipped and
  listed in `skipped_checks`, so a partially migrated store is diagnosed rather
  than crashed on. Such a report is not `ok`: `Report.complete` is false and `ok`
  requires it, so missing coverage fails closed instead of reading as clean;
- repair is dry-run by default and takes no lock in that mode; applying takes the
  SQLite write lock first and holds it across diagnosis, backup capture, backup
  verification, every repair pass, and the commit;
- the backup prerequisite is a manifest archive verified by isolated restore *and*
  proven to still represent the live database; repair refuses to run when
  `integrity_check` is not `ok`, and applies every action in one transaction that
  rolls back as a whole;
- that transaction re-plans until it converges: repairing one invariant can expose
  another (stamping `ended_at` makes a live claim a stale lease), so the committed
  result is a converged store or the original one;
- engine scans are separated from that replanning. The whole-database
  `integrity_check`/`foreign_key_check` pair runs once as preflight under the lock
  and once as the full post-repair verdict; convergence passes re-check foreign
  keys only over the tables the previous pass could have broken (the tables it
  wrote to and their children in the schema's reverse foreign-key graph);
- dangling references on nullable columns are cleared so events, handoffs, and
  other durable records survive; rows whose required parent is gone are reported
  and only removed when the operator passes `--delete-orphans`. Lease tables
  (`integrity.LEASE_TABLES`) are the stated exception: a claim is lock state, not
  history, so an orphaned one is removed deterministically.

Backup freshness is what makes the repair prerequisite meaningful, and the write
lock is what keeps that freshness true until the mutation. A SQLite manifest
records a `source_fingerprint` - the SHA-256 of the online-backup image of the
source - which is a deterministic function of committed content, stable across
connections, inclusive of un-checkpointed WAL frames, and unchanged by a later
checkpoint. `verify_backup(expected_source_path=...)` recomputes it from the live
file, so an archive superseded by any later write is refused. On its own that is
an instantaneous verdict; `repair_database(apply=True)` therefore passes a
`backup.SourceWriteLock` so capture, verification, and mutation all happen inside
one `BEGIN IMMEDIATE` window. SQLite's online backup API cannot step against a
connection that holds a write transaction, so the image is taken by a separate
reader while that transaction holds every writer off - and the lock layer proves
the claim on every step (the connection must still be in its transaction, and no
other connection may be able to take the write lock) rather than assuming it.
`PRAGMA data_version` is reported for diagnostics only, because SQLite only
guarantees it is comparable within one connection.

Backup archives carry a `manifest_version`. This build writes `2` and reads `1`
and `2`, so a `2` archive needs this build or later to inspect, verify, or
restore, while `1` archives written by earlier builds remain readable here.
Unknown fields inside a readable version are ignored; an unknown version, a
malformed manifest, or a field of the wrong type is converted into a
`BackupError` with one message instead of a traceback.

The cascade classifies each declared foreign key: a `NOT NULL` reference means the
row cannot exist without its parent and is deleted, while a nullable reference
means an independent record that keeps its row and loses the reference. Delete
order is the longest distance from the deleted root, so a dependant reachable by
several paths is still removed before the rows it points at. One narrow policy
input is stated in code rather than derived, because the schema cannot express it:
`integrity.WORKSPACE_SCOPED_TABLES` names the direct children whose *optional*
Workspace reference still means ownership (activity rows such as events, mailbox
messages, checkpoints, help requests, and sources). A table that is
not named is preserved, so the failure mode of forgetting one is a kept row, never
a destroyed record. `audit_log` is deliberately not named: its rows are links in a
hash chain, so deleting the newest one would leave a head the log no longer
matches and every later governed append would be refused.

Migration `148_workspace_aliases` separates path spelling from durable Workspace
identity. Registration stores normalized paths as aliases and uses Git's local
common directory to converge linked worktrees inside one Org. Existing duplicate
Workspace rows are archived, not rewritten or deleted, so their historical rows
remain attributable; the alias moves future path-based calls to the oldest
canonical Workspace. A common identity spanning Orgs is refused.

Workspace deletion in explicit `brains-ai workspaces prune` and `workspaces doctor
--prune-missing` uses that cascade. The preferred `workspaces doctor
--archive-missing` path only changes active Workspace status and preserves every
dependent row.

## Realtime architecture

Envelope shape:

```json
{"v":1,"type":"...","entity":"...","id":"...","topic":"...","ts":"...","payload":{},"seq":1,"event_id":12,"durable":true,"org_id":7,"workspace_id":3}
```

`seq` is a per-connection counter and means nothing across connections.
`event_id` is the durable cursor: it is assigned by the store, is monotonic
across processes and restarts, and is `null` with `durable:false` on a
notification-only frame. `org_id`/`workspace_id` are the scope the publisher
resolved and are what delivery filters on.

Client message types are subscribe, unsubscribe, `resync`, `chat.send`, and
ping. SSE is the read-only fallback with keep-alives, a `realtime.ready` frame
naming the derived topics, and `id:` lines carrying the same cursor.

### Topics are a closed grammar, resolved by the server

`brains.events.topics` defines the whole vocabulary, and nothing outside it is
subscribable:

| Topic | Entity | Scope derived from | Audience |
|---|---|---|---|
| `org/{org_id\|slug\|default}/{channel}` | Org | the Org row | operator |
| `issue/{issue_code}` | Issue | the Issue's Project Org | operator |
| `session/{session_id}/{stdout\|chat\|state}` | Session | the Session's Workspace | operator, own Runtime |
| `machine/{machine_id}/{assignments\|control}` | Runtime | the machine's Runtime Org | operator, own Runtime |
| `runtime/{runtime_id}/{assignments\|status}` | Runtime | the Runtime's Org | operator, own Runtime |

Org channels are `issues`, `sessions`, `runtimes`, `inbox`, `projects`,
`personas`, `pods`, `automation`. A wildcard (`*`, `#`, `?`, `%`), a traversal
(`..`), an unknown family, an unknown channel, a malformed segment, a topic
longer than 160 characters and a non-string are all outside the grammar.

`brains.authz.policy.resolve_topic` then resolves the entity the topic names
against the store and returns the **canonical** topic plus the Org/Workspace it
carries, so `org/acme/issues` and `org/default/issues` both become
`org/{id}/issues` and a subscriber never decides what a name means. The ack
reports the derived name and the alias that produced it.

Refusals are uniform. A malformed topic, a wildcard, an unknown family, an
entity that does not exist, an entity in another Org, and a Session in a
`private` Workspace all return the same answer - the topic in the `denied` list
on WebSocket, a `403` naming no topic on SSE - so a subscription cannot be used
to test whether an Issue code, Session id, machine or Org exists. A machine
whose Runtimes straddle two Orgs claims both rather than collapsing to the
default Org, so a member of the default Org cannot learn it exists.

Reading a topic is not permission to write to it: `chat.send` is accepted only
on a Session's own `chat` stream.

A Runtime credential is refused the operator WebSocket and the SSE stream
outright. Where a Runtime principal is authorized at the policy layer, it
reaches only its own machine, its own Runtime, its own Session and its own
assignment streams, always inside its own Org; Org-wide channels have no
Runtime audience at all.

### Authorization is re-checked, not assumed for the life of the socket

Every client message re-resolves the credential, and a revalidation loop does
the same on a timer (`BRAINS_REALTIME_REVALIDATE_SECONDS`, default 10s; the SSE
idle wait is the smaller of that and the 5s keep-alive). A revoked, expired or
unbound credential is sent `realtime.revoked` and the socket is closed with
`4401`; a membership removed mid-connection drops exactly the topics it granted
and is reported as `scope_revoked`, which is survivable - the console forgets
those topics and stays connected rather than blacking out. A check that cannot
be *completed* - the store is unreachable, a policy read raises - is not
evidence that the credential still holds, so it fails closed as well: the
connection is sent `revalidation_failed` and closed, and the tasks behind it
are cancelled and awaited rather than left running behind a socket nobody is
re-checking. That reason is deliberately distinct from a revocation, because
the console's correct answer to it is to reconnect and be checked again, not to
stop reconnecting. Behind topic authorization, each connection carries the
Org/Workspace scope its principal resolved to, and an envelope whose recorded
scope falls outside it is dropped even though its topic matched. Credential
resolution, topic derivation, the Workspace visibility set and replay reads run
off the event loop - on connection setup as much as on revalidation - so one
connection's store read cannot stall every other socket the process serves.

### Durability is the record; the bus is the notifier

`realtime_events` (migration 132) is the log. Session, Issue, approval and
Runtime events commit there *before* anything is announced
(`brains.events.store.publish_durable`), so nobody is told about a change that
did not commit, and a publisher resolves the Org from the store rather than
from whatever the caller passed.

`dedupe_key` is unique, so a publisher that supplies one gets one row, one
`event_id` and one delivery however many times the same logical event is
republished - including from two processes at once. The Session *command*
publisher supplies one: a message or stop has a command id and a state, so
`session_command:{command_id}:{status}:{result}` is the same string however
many times the mutation is retried, and a retried send delivers once
logically. The remaining mutation publishers do not: a Session lifecycle,
Issue, approval or Runtime write has no operation id to derive a stable key
from, so for them delivery is **at-least-once** - a retried publish writes a
second row with a second `event_id` and a client applies both.

A client resumes with the highest `event_id` it applied - `cursor` on the WS
`subscribe`/`resync` message, `cursor` or `Last-Event-ID` on SSE - and receives
a bounded replay (`BRAINS_REALTIME_REPLAY_LIMIT`, default 500). The cursor
follows delivery, not intent. The ack is written before the catch-up frames, so
it reports only what the client could already resume from; the frames advance
the client's cursor themselves; and a trailing `replay_complete` - written
after the last frame of the batch, and carrying the cursor in the SSE `id:`
field - confirms the rest, including ids the connection will never be sent
because its own delivery scope dropped them. A replay interrupted halfway
therefore leaves the client holding the id of the last event it actually
applied, and its reconnect delivers the remainder instead of skipping it. When
the cursor cannot be honoured, `realtime.reset` says why (`cursor_expired`
after pruning, `cursor_ahead` for a rebuilt or foreign store, `replay_truncated`
for a backlog larger than the bound) instead of sending a silently short
stream. Retention is by row count (`BRAINS_REALTIME_RETENTION_ROWS`, default
5000), pruned on an amortised schedule.

That cursor rule only holds if frames arrive in cursor order, and they do not
by themselves: a connection's subscription is registered on the bus *before*
the replay snapshot is read - otherwise an event published in between would be
missed by both paths - so live fan-out and the catch-up batch are in flight at
the same time. A live event written between two catch-up frames carries a
*higher* `event_id` than the frames still to come, so a client that applied it
and then dropped would resume past the remainder. Delivery is therefore
serialised per connection: a replay phase opens before the subscription is
registered and closes after the receipt is written, live frames queue behind it
rather than interleave, and a live frame carrying an `event_id` the batch
already delivered **on that same topic** is dropped instead of sent twice. That
suppression is per topic and by exact id, never a watermark: subscriptions are
added one at a time, so a catch-up for a newly held topic normally carries ids
above the live events queued for a topic that was already being delivered, and
one shared floor would read those as duplicates and drop them for good. For the
same reason a batch that did not read every topic the connection holds - the
usual shape of an incremental `subscribe` - hands the client no cursor at all.
Its ack says so (`covers_connection: false`) and its `replay_complete` reports
`cursor: null` with the high-water mark it wrote as `batch_cursor`, because a
client holds one cursor for the whole connection and the live frames it is
still owed on the topics that batch never read are queued *behind* it, below
every id it carried. The console applies those frames without resuming from
them, and the next full-coverage batch - any reconnect, any `resync`, every SSE
stream - settles the difference. A frame queued for a topic that was
unsubscribed, or lost with a membership, while it waited is dropped for the
same reason. In the same spirit, one publish commits and announces inside a
single critical section, so two concurrent publishers cannot announce in the
opposite order to the ids the store gave them. Replay and live delivery still
overlap *across* batches - a `resync` re-sends what a client already holds - so
a client applies each event at most once by `event_id`.

Limitations:

- live fan-out remains **per-process**: a publish in the MCP or dashboard
  process is durable and is caught up by cursor, but it is not pushed to a
  socket attached to the gateway process;
- delivery of a Session lifecycle, Issue, approval or Runtime event is
  at-least-once, as above; Session *command* events are deduped by their
  operation-derived key;
- transcript chunks (`session/{id}/stdout`), the `chat.send` echo and
  `runtime.heartbeat` are notification-only. Transcript chunks are backfilled
  from the `events` rows; the `chat.send` echo is backfilled by nothing,
  because it is a live mirror rather than a record - the durable operator
  message is the `session_commands` row the console backfills over REST; the
  heartbeat is a liveness tick whose durable counterpart is the
  `runtime.status` transition, and recording every tick would churn the shared
  replay window for every console;
- retention and gap detection are install-wide rather than per topic, so a
  client can be told to resynchronise because of traffic on topics it does not
  hold;
- Project, Persona, and Pod mutations do not all publish despite frontend
  subscriptions;
- a replay is bounded rather than unbounded, so a client far enough behind is
  told to resynchronise rather than caught up.

### Core journey reconciliation

Issue detail is assembled from persisted rows rather than frontend inference:
linked Sessions, durable events and commands, approval decisions, comments, and
`usage_attributions`. Each usage-ledger row can be attributed once; calls with
no valid Session remain explicitly unattributed. `GET /v1/orgs/{org}/usage`
(BL-P1-07) joins the ledger through `usage_attributions` filtered on `org_id`,
so it is readable by any principal with `org.read` on that Org - unlike the
install-wide `GET /v1/usage`, which stays bootstrap-admin-only because it
carries no Org attribution at all - and never returns another Org's or an
unattributed call. Issue dispatch is idempotent while a Session is open and
creates the Session in the Runtime working Workspace under the Issue's Org,
refusing a Workspace already owned by another Org.

Pods retain `squads` as the compatibility identity referenced by Project and
Issue rows, while `pod_profiles` and `pod_members` define the product contract:
one leader Persona, a Persona roster, leader-preserving removal, archive, and
deterministic leader-first Runtime resolution. Legacy operator memberships are
reported but are not dispatch candidates unless migration 134 resolves them
unambiguously to an active Persona in the Pod's Org.

Skill attachment (BL-P1-08) is `persona_skills`/`project_skills` (migration
138): a Skill attaches to a Persona or a Project with a unique `(entity,
skill)` pair, so a repeated attach updates nothing rather than duplicating
context, and provenance (`attached_by_operator_id`, `attached_at`) records who
attached it and when. `control.skills.resolve_context_for_session` resolves one
Session's Persona and (via its Issue) Project, composing their attached
Skills into one deduplicated, ordered list where a Skill attached through both
is reported once with both sources recorded. `exec.runner.run_session`
prepends this as a markdown block to the spawned agent's actual prompt -
before the repo-orientation block - so the Skill enters the real launch path
every spawned Session goes through, not merely a `build_welcome` field an
API caller might never read.

Fresh-state onboarding is durable server state in `onboarding_attempts` and
`onboarding_steps`. Every referenced Runtime, Persona, Project, Issue, and
Session must resolve to one visible Org; private Session Workspace visibility
still applies. Completion is derived only from a Session linked to the
attempt's Issue. Deferred setup, unavailable Runtime, refused dispatch, and a
missing Session are named blocked outcomes rather than success.

### Session control is a durable queue, not a socket frame

A message typed into the console and a stop pressed on a Session are
*requests*. The browser cannot deliver them, and the Runtime that can may be
busy, restarting or gone. `session_commands` (migration 133) is the record:
`POST /v1/sessions/{id}/message` and `POST /v1/sessions/{id}/stop` authorize
the Session through the same Org/Workspace policy every other per-ID surface
uses, commit the row, and only then attempt delivery.

- **Idempotent.** `operation_key` is unique. The console mints an
  `operation_id` per composer submit and re-sends it on retry, so a replayed
  request returns the original command with `duplicate: true`. A stop with no
  explicit operation id keys on the Session, so pressing stop twice - or twice
  concurrently - is one logical command. The single exception is a stop that
  reached a terminal *failure* while the Session kept running (`not_owned`,
  `abandoned`, a cancellation the Session outlived): the next press mints a new
  durable attempt under the next epoch of the same key, because handing back a
  dead command would leave an operator pressing a button that can only ever
  return the failure it already saw. A caller that names its own
  `operation_id` keeps strict identity, failure included.
- **Ordered.** `(session_id, sequence)` is unique and dense per Session, so
  delivery order and console order are the same order.
- **At most one active consumer.** A claim is one conditional `UPDATE` off
  `requested`, so two Runtimes racing a command resolve to one winner. The
  winner holds a lease (`BRAINS_SESSION_COMMAND_LEASE_SECONDS`), and only the
  lease holder may settle it: a previous holder whose lease expired is refused
  rather than allowed to overwrite the attempt that replaced it.
- **One owner, by binding rather than by box.** A command carries its Session's
  binding, and only that consumer may take it: a Session bound to a Runtime is
  that Runtime's, and a Session with no binding belongs to the local process
  that launched the agent. Several workers, an operator's CLI session and the
  hub can share one machine, and each holds only its own process handles, so
  "on this machine" is not ownership - it hands one consumer's stop to another,
  which can only answer `not_owned` and burn the command. Listing, claiming and
  the local dispatch all filter on the binding, and a consumer that finds
  itself holding a command it does not own releases it
  (`POST /v1/runtimes/{id}/session-commands/{id}/release`) instead of settling
  it on the owner's behalf.

  The machine stamp on a Session or a command is a diagnostic, never the
  ownership test. It records where the *row* was created, and in the
  production shape - hub on one box, Runtime on another - a spawn row is
  created by the hub, so it names the hub's machine until the Runtime opens
  it. A Runtime therefore lists, claims, releases and settles by binding
  alone, and reconciles by binding alone, so an operator's stop reaches the
  process that can answer it whatever the stamp says. The stamp is still kept
  honest: a spawn Session records the machine of the Runtime it is bound to,
  the Runtime re-stamps the row when it opens it, and a queued command records
  its Runtime's machine rather than its Session's. Only the local consumer,
  which has no Runtime binding to reason about, uses the machine as its
  ownership test.
- **Recoverable.** An expired lease returns the command to `requested` with
  `attempt` incremented; a command that exhausts `BRAINS_SESSION_COMMAND_MAX_ATTEMPTS`
  is settled `failed`/`abandoned` rather than retried forever; a Session that
  reaches a terminal state cancels its open commands instead of leaving them
  pending.
- **Truthful.** The consumer acknowledges what it observed. `brains.exec.session_channel`
  holds the process handles this Runtime launched and declares which tools have
  an input channel at all. None of the shipped CLIs do - `copilot` takes its
  prompt in argv, `claude` and `codex` read one prompt from stdin and then see
  EOF - so a message to them is settled `failed`/`unsupported` with the reason,
  and `GET /v1/sessions/{id}` reports `message_capability` so the console
  disables its composer with that reason instead of accepting text that cannot
  arrive. Where a tool *is* declared interactive, `run_session` keeps stdin
  open and the message is written to it.
- **Owned.** A stop signals a `subprocess.Popen` handle this process created
  and still holds, never a process matched by name - a Runtime shares a box
  with the operator's own tools. A Runtime that restarted owns nothing, answers
  `not_owned`, and the Session is *not* recorded as stopped on that basis. The
  daemon drains the queue on its own thread, because assignment execution
  blocks for the life of the agent CLI and a command drained from that loop
  could only ever be delivered while the Runtime owned nothing.
- **Reconciled.** On startup and re-registration the daemon reports, *per
  Runtime*, the Sessions it can still prove it owns
  (`POST /v1/runtimes/{id}/sessions/reconcile`); everything else the hub shows
  running for that Runtime, and older than the grace window, is brought to a
  terminal state with a truthful summary and has its queue cancelled. The
  grouping matters on a box that runs several CLIs: one daemon hosts one
  Runtime per tool, the process handle registry records which Runtime each
  launch belongs to, and a Runtime that holds nothing is told so with an empty
  list rather than being handed its siblings' Sessions - a claim the hub
  refuses, which would take that Runtime's whole reconciliation with it and
  leave its own stale rows running. A step that fails is recorded and logged
  in one consistent shape rather than swallowed, because a silent failure is
  indistinguishable from having nothing to do. The same path releases the
  Session's Workspace claim and its in-progress Tasks, and moves an
  `in_progress` Issue to `blocked` rather than back to `open`, which would have
  the assignment poll re-spawn the work an operator just stopped.
- **Race-safe.** `finalize_session` is a conditional stamp on
  `ended_at IS NULL`, so a natural completion and a stop race for it and the
  loser changes nothing. A Session that ends between a failed stop and the next
  press is terminal, so that press is answered with the recorded command rather
  than a new attempt nobody could deliver.
- **Scoped in the console.** Every request the dock issues captures the Session
  it was issued for and is discarded when the operator has selected another one
  by the time it answers; pending sends and the stop-in-flight flag are held
  per Session (`frontend/src/components/sessionScope.ts`). A durable queue on
  the server does not prevent a correct answer being painted into the wrong
  thread by the client.

Limitations: a Session with no Runtime binding and no process owned by the hub
process has no consumer, so its commands stay `requested` until the process
that owns it dispatches them or the Session ends; and the shipped agent CLIs
have no input channel, so `message` is a durable, honest refusal rather than a
delivery until an interactive launch shape ships.


## Trust boundaries

### Identity, roles, and scope

Authentication is a lookup in the credential store, not membership in a broad
key set. Every accepted secret is one row in `api_credentials`, keyed by its
sha256 hash; the raw secret is never persisted. The row names the credential's
kind (`admin`, `operator`, `runtime`), the operator it belongs to, the Org and
machine a Runtime credential is bound to, and its expiry and revocation state,
which are honoured on every request.

Resolution produces exactly one `Principal` (`src/brains/authz/principal.py`)
carrying the actor kind, the operator identity, the credential kind, the
*channel* it arrived on, the Org memberships and roles, and an optional Runtime
identity. The keys an install already holds on disk (`settings.api_key`,
`settings.api_keys`, and `~/.brains/operator-keys/*.key`) are adopted into the
store on first use, so an existing install keeps working and still resolves to
an explicit principal.

Adoption records provenance (`api_credentials.source`), and adoption is all
reconciliation does. A raw value present on disk is adopted; nothing is ever
revoked because it is *absent*. A process's view of the on-disk sources is
neither authoritative nor complete - a worker with a different `BRAINS_API_KEY`,
a container without the state directory mounted, an unreadable key directory and
an unauthenticated request carrying a bad token all narrow it - and a revocation
derived from that view would be written to the shared store and deny the
credential install-wide. Superseding is therefore explicit and exact: rotating
the admin key and deleting an operator key file each name the one hash they
retire (`credentials.revoke_local_secret`), so the old secret is refused on the
very next request while a process that merely cannot see a key revokes nothing.
A Runtime credential minted by enrollment and a manually registered one are
never touched by either path. A revoked credential is not reinstated by a
resync - restoring a deleted key file does not undo the revocation - and comes
back only through an explicit rotation or registration. A source that cannot be
read raises rather than reporting an empty install, and the failure surfaces in
`credentials.diagnose()`.

Two caches bound what an unauthenticated caller can cost the process: the
on-disk key sources are re-checked by `stat` at most once every few seconds and
re-read only when they change, and a bounded, short-lived negative cache
remembers digests that resolved to nothing, so a flood of invented tokens
triggers neither a directory scan nor unbounded growth. The negative cache
holds sha256 digests only, never a secret.

The channel records *how* the credential reached the process: `api` for a raw
secret on an HTTP request, `browser` for the signed console cookie, and `local`
for the CLI, stdio MCP, and an install that has explicitly disabled
authentication. A raw operator key is deliberately not human-bindable - an
agent process holding a shared key presents exactly the bytes its owner would -
and approval separation of duty depends on that distinction.

Roles are the ones the product contract already uses and no more: `owner`,
`admin`, and `member` on an Org. They unlock four capabilities, checked
deny-by-default against one resolved Org:

| Capability | Minimum role | Covers |
|---|---|---|
| `org.read` | `member` | Org, Pods, Skills, Personas, Projects, Issues, Sessions, Runtimes, approvals, automation |
| `org.write` | `member` | Content creation and mutation, Session spawn, approval resolution |
| `org.admin` | `admin` | Org rename/archive, membership, automation enable/fire, Runtime lifecycle, enrollment minting |
| `org.owner` | `owner` | Granting or revoking the `owner` role, and any change to an existing owner |

Answer semantics are uniform: `401` for no/unknown/revoked/expired credential,
`403` for an authenticated principal that may see the Org but lacks the
capability, and `404` for anything in an Org the principal may not read, so an
unknown entity and an unauthorized one are indistinguishable.

Ownership is protected in both directions. Only an `owner` may grant `owner`,
and only an `owner` may change or remove a member that already holds it,
whichever spelling of the member id is used - otherwise an `admin` could demote
every owner and take the Org. An Org may not be left with no owner at all: the
demotion and the removal are each a single conditional statement whose `WHERE`
counts the remaining owners, so two concurrent writers cannot both pass a
read-then-write check and empty it. The HTTP API answers `409`; the only way
past the rule is an explicit local `bootstrap_recovery` call.

Browser and console surfaces refuse two principals outright, both with `403`: a
Runtime credential, which exists to run work on one machine, and a principal
that holds no Org role at all, which every scoped API already answers
"nothing". The `/admin/*` configuration surfaces go further - provider
credentials, environment overrides and router policy are install-level, not
Org-attributed, so they are restricted to the bootstrap admin holding the
install's own key. Owning an Org is not owning the install. The login form
applies the same rule before it mints a console cookie.

A machine belongs to exactly one Org. Its claim is what its Runtimes declare
*and* what its live Runtime credential names, so a machine enrolled without
registering a single tool is still claimed. Registration is an upsert, so it
authorizes the Org that already owns the machine rather than the one the caller
asks for, and a mismatch is answered with the same non-disclosing `404` as an
unknown machine. The row and the claim it carries are one transaction: the
claim is verified from inside the transaction that holds the insert, so a
refused registration leaves no row at all rather than an Org-less Runtime on
somebody else's machine, and a registration that names no Org inherits the
machine's existing claim instead of writing `NULL` beside it. Enrollment
redemption takes the same claim in the same transaction as the token claim and
mints the credential inside it, so a refused redemption leaves neither a
credential nor a consumed token. Concurrent first-claims from different Orgs -
two registrations of different tools, two redemptions, or one of each - are
serialized by SQLite's write lock, and on PostgreSQL by a transaction-scoped
advisory lock keyed by the machine id, so exactly one claim survives. A Runtime
row that still has no Org is a pre-Org registration that claims nothing: it is
not read as belonging to the `default` Org, it is listed and addressable only
by the install administrator, and the daemon will not claim work through it.

A Runtime credential is checked on both of its bindings, never on the machine
alone. Every Runtime-id-scoped route, the batched heartbeat, the Runtime
listing and `POST /v1/sessions/{id}/state` compare the credential's Org to the
Org of the Runtime, machine and Session they are acting on, so a credential
that named another Org's machine would still authorize nothing. That is
defence in depth behind the claim: enrollment refuses to mint such a credential
in the first place. Where a Session names a Runtime, the machine compared is
that Runtime's registered machine rather than the Session's own stamp, which
records only where the row was created - otherwise a Runtime could not report
the terminal state of a Session the hub opened for it, and the console would
show that Session running forever.

Per-ID reads apply the same Workspace visibility the listings do. A `private`
Workspace is filtered out of `/v1/sessions` and `/v1/approvals`, and the detail,
event, state and approval-resolution routes resolve through the same
membership check, so an entity absent from a listing is absent from every
per-ID surface too rather than readable by id. Every Session listing applies
that filter whichever entity it hangs off: `/v1/issues/{issue}/sessions` and
`/v1/personas/{persona}/sessions` read the same rows as `/v1/sessions` and are
scoped by the same `policy.scope_sessions`, so authorizing the Issue or the
Persona does not answer the Workspace question on their behalf.

Two compatibility rules are explicit rather than implied:

- The auto-provisioned `admin` operator is the bootstrap principal and is
  treated as `owner` of every Org, which is what keeps a pre-existing
  single-operator install working. It is a named principal an unknown key can
  never become.
- Migration `130_org_member_backfill` turns the previously implicit grant into
  rows: every operator that existed at upgrade time joins the `default` Org as
  `member`, and `admin` as `owner`. `daemon-*` operators minted by
  pre-BL-P0-01 enrollment are deliberately excluded, keep authenticating, and
  see nothing; `brains-ai credentials doctor` reports them. Operators created
  after the upgrade are granted nothing until they are invited.

Separation of duty on approvals is enforced in
`brains.control.decisions.assert_resolver_allowed`. A Runtime credential can
never resolve an approval. An ASK filed **by a Session** may only be resolved
through a channel that can be bound to a human - the signed console cookie, or
a local CLI / stdio invocation whose trust boundary is the operating-system
user - so a shared operator key presented over HTTP is refused rather than
trusted. The Session that filed the ASK can never resolve it, and that Session
is taken from what the server knows the credential is running (a Runtime
credential names its machine, and the machine names its live Sessions), not
from the request body: a caller-declared `session_id` can only ever *add* a
denial, never establish separation by being omitted. Finally, the Persona
identity behind the request (the operator bound through `personas.operator_id`,
or another Session of the same Persona) can never resolve it. Both the decision
and its audit entry commit in one transaction, and a refusal appends
`approval.self_resolution_denied` naming the reason. The limit of the rule is
stated plainly: a human and an agent sharing one browser session are
indistinguishable, which is why Personas are bound to their own operator
identity.

Two rows that declare no Org are treated as claiming none rather than as
belonging to everyone: a `runtimes` row with `org_id IS NULL` (a pre-Org
registration; enrollment always binds one) and a Workspace whose Org is the
`default` bucket a Workspace created on the fly lands in. They neither grant
nor block a cross-entity check. Where an Org *is* declared, a Runtime call may
only name an Issue, Persona, assignment or Session inside it, a Session already
bound to another Runtime is refused outright, and a spawn authorizes every
identifier it is given rather than only the first, so it cannot straddle two
Orgs.

Each mounted ASGI app binds its own principal slot: the gateway
(`brains.main`), the legacy dashboard (`brains.dashboard.app`) and the MCP SSE
transport all resolve the same credential store, so no surface silently falls
back to the bootstrap admin. A request that authenticates with no credential
resolves to a scopeless anonymous principal rather than to the admin.

| Boundary | Current mechanism | Limitation |
|---|---|---|
| Model `/v1` routes | credential-store lookup through `require_api_key`; Runtime credentials refused | Optional unauthenticated setting exists (resolves to the named `admin` principal); rate limit defaults off. |
| Native product `/v1` routes | one resolved principal plus a per-route Org capability check | Usage totals and provider config remain install-wide and are restricted to the bootstrap admin rather than Org-attributed. |
| Browser `/app`, `/dashboard`, protected `/admin` | signed opaque cookie bound to the key that minted it, raw header, or legacy query key; Runtime credentials and scopeless principals refused | The legacy `?key=` query form still exists; `/admin` config surfaces are install-level and restricted to the bootstrap admin, so an Org owner cannot reach them. |
| WebSocket | principal resolution on upgrade, then server-derived topic authorization, re-checked on every client message and on a timer; Runtime credentials refused | Query credentials can appear in surrounding infrastructure logs; live fan-out is per-process, so a client reconnecting to another process catches up by cursor rather than being pushed to. |
| SSE | console gate plus the same server-derived topic model, cursor/`Last-Event-ID` resume, and the same revalidation | Same per-process fan-out limit as WebSocket; a refusal is uniform and names no topic. |
| MCP SSE | credential-store resolution plus loopback Host policy by default; Runtime credentials refused | Full tool mode exposes powerful mutations; per-tool human roles are incomplete. |
| MCP stdio | local process boundary | No network auth; inherits process environment and database access. |
| Runtime redeem | enrollment token, claimed by one conditional update, with the machine's Org claimed in the same transaction before anything is minted | Route is deliberately unauthenticated; the token is the credential. An Org holding a valid token can still claim a machine id nobody has enrolled yet; it cannot take one another Org holds. |
| Runtime credential | Org-bound, machine-bound, expiring, revocable; limited to register/heartbeat/status/claim/execute, and every route compares the Org as well as the machine | A stolen credential can still act as that machine until it is revoked or expires. |
| Trigger webhook | per-trigger bearer and delivery dedupe | Scope depends on trigger definition and control logic. |
| Relay | separate relay bearer; disabled when unset | External bridge operation remains unverified. |
| Action gate | classification plus one in-process governed-action contract (`brains.govern`) for PATH shims and every process Brains launches | Cooperative and in-process. A third-party CLI that calls an absolute path itself, rewrites `PATH`, or opens a raw socket is never seen; outbound bridge/provider network calls are not routed through it. |
| Audit | HMAC chain, HMAC-signed single-writer chain head with a persisted adoption marker, and fail-closed verification | Transactional with the governed transition and serialised across processes; effects that are not database writes (overlay, env override, backup, restore, repair) get a two-phase attempted/completed record instead, so the attempt is durable before the effect and the completion cannot precede it; a stolen audit key still forges a chain, an unsigned head over a non-empty log is refused until an operator adopts it with `brains-ai audit-adopt` (which verifies first), an attacker who deletes the whole log back through its genesis marker leaves a store that looks pre-signature again, and telemetry-shaped `record` appends remain best-effort by design. |
| Shared Postgres | database credentials | Direct database access is full database trust, not tenant isolation. |

## Dependency topology

### Required runtime dependencies

Python 3.11 or newer, FastAPI/Uvicorn, a WebSocket transport, Pydantic
settings, SQLAlchemy, Typer, MCP SDK, Jinja2, HTTP client, and YAML support.

### Optional subsystems

- Postgres drivers;
- LiteLLM;
- Telegram, Slack, WhatsApp, and WhatsApp Web bridges;
- OpenTelemetry export.

Enabled optional subsystems are expected to fail startup with an install hint when their extra is absent.

### External touchpoints

- OpenAI-compatible endpoints;
- Ollama for models and optional embeddings;
- GitHub Copilot OAuth/session endpoints and optional `gh` CLI fallback;
- GitHub webhook events;
- messaging platforms and the wa-web companion protocol;
- OTLP collector;
- external URLs used by freshness checks.

No live external dependency is verified by this document.

## Deployment topology present in the repository

| Shape | Present source | Current architectural status |
|---|---|---|
| Native CLI | `brains-ai serve-all` | Source and tests present; no process run asserted here. |
| User OS service | Task Scheduler, launchd, systemd user service renderers | Source/tests present; installed state unverified. |
| Root runtime image | root `Dockerfile` | Non-root image, `/data` state, and gateway/dashboard/MCP healthcheck; registry/deployment unverified. |
| Dev Compose | `docker/` | Broken at HEAD because its entrypoint names removed executable `brains`. |
| Isolated sandbox | `sandbox/` | Read-only repo mount, isolated state, shifted ports; current run unverified. |
| Shared-DB battle harness | `sandbox/battle/` | Postgres plus two isolated processes; current run unverified. |
| UAT sidecars | `docker-compose.uat.yml` | Supplies Postgres/OTel only, not a complete app UAT environment. |
| Box scaffold | `deploy/box/` | Internally inconsistent and not deployable as documented without repair. |

No Atlas file or live deployment evidence is present at HEAD.

## Current architectural limitations

1. Realtime topics are server-derived, scoped and durable, but live fan-out is per-process: a publish in the MCP or dashboard process reaches another process's socket only when that client resumes by cursor.
2. Action gating is cooperative rather than an enforceable process/network boundary.
3. Audit append is best-effort.
4. Session message and stop are durable, authorized, idempotent, leased and reconciled, but the shipped agent CLIs have no input channel, so a message to them is an honest `unsupported` refusal rather than a delivery, and a Session with no Runtime binding has no consumer until one claims it.
5. Multi-process in-memory state can diverge.
6. SQLite FK enforcement is opt-in, and Postgres executes the baseline while explicitly skipping the SQLite catch-up deltas, so backend parity rests on the baseline rather than on equivalent per-delta implementations.
7. Modern and legacy browser surfaces overlap.
8. Deployment scaffolds disagree on executable, state path, UID, extras, and ingress routes.
9. Four Python import strongly connected components are present in context/session/view code, config/admin-key, exec/runner, and provider policy/registry.

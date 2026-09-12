# Brains Operations

## Scope and proof boundary

This document gives supported operating contracts and repeatable probes. Command
presence is E1 evidence only. This runbook does not establish that an installed service,
external connection, recovery drill, container, or deployment is operating.

The supported product is Workspace-first coordination, human governance, SQLite
operations, and service/wiring posture. The following are withdrawn from the supported
CLI, MCP, HTTP, and browser surfaces:

- Runtime enrollment/execution, Personas, Pods, Projects, Issues, execution onboarding,
  execution Session supervision, running-agent chat, and Runtime process stop;
- Automation UI, managed Skills, recurring definitions, generic webhooks, and scheduled
  auto-fire;
- model gateway, provider routing, LiteLLM, and the tool launcher;
- semantic indexing/search, embeddings, code graph, and external freshness;
- Postgres as an operating backend and OpenTelemetry export;
- Telegram, Slack, WhatsApp, WhatsApp Web, and relay/bridge delivery;
- legacy dashboard and configuration HTML; `/admin/login` and `/admin/logout` remain
  only for the supported SPA cookie lifecycle.

Environment switches, pip extras, direct URLs, and explicit MCP tool selections do not
restore those withdrawn public surfaces. This is not a guarantee that all internal
activation paths have been removed.

Session start and reuse do not schedule graph building or embedding. Retained prewarm
settings and embedding-model configuration cannot reactivate indexing through Session
registration. Indexing modules, configuration and stored sources, chunks, vectors and
graphs remain intact; this change does not delete data or expose withdrawn tools.

Welcome output preserves historical previews but no longer recommends withdrawn tools.
Legacy unread-mail counts are not the durable mailbox inbox. Local harness PATH readiness
checks remain part of welcome assembly; they do not launch harnesses.

These changes apply to processes running the updated package. They do not cancel work
already scheduled by an older process. To suppress scheduling while remaining on an
older package, set `BRAINS_PREWARM_INDEX_ON_SESSION=0` in each registering process's
environment and restart that process so it loads the setting.

## Install and start

Brains supports Python 3.11 and 3.12. Use an isolated installation:

```text
python -m pip install --user pipx
python -m pipx ensurepath
pipx install brains-ai
```

Initialize one Workspace and start Brains in the foreground:

```text
cd <project>
brains-ai setup --path .
brains-ai serve-all
```

Open `http://127.0.0.1:8787/app` after the stack is ready. Keep the generated admin key out
of URLs, logs, issues, fixtures, and repositories.

Repository checks exercise exact-wheel installation, service-definition rendering,
reversible wiring, and hermetic lifecycle behavior. An exact candidate is not qualified
until the fail-closed aggregate accepts its required native and container results.
Successful native manager-cycle and cleanup evidence qualifies the tested candidate
and host, not login or reboot persistence. Reboot persistence remains unknown without
the separate machine-observed reboot probe. Review the service section below before
opting into `--service`.

The supported installed executable is `brains-ai`. Helpers that invoke `brains` are
obsolete.

## Supported command families

This is a capability summary, not an exhaustive `--help` copy.

| Family | Supported purpose |
|---|---|
| `setup`, `serve-all`, `serve`, `mcp`, `up` | Initialize or run the supported gateway/MCP stack. |
| `wire`, `unwire` | Add, inspect, or remove the Brains-owned MCP entry and explicitly consented supported mailbox wakeup hook. |
| `service install|start|stop|restart|status|logs|uninstall` | Manage the user-level supervised stack. |
| Session/state/task/claim/handoff/help/checkpoint commands | Coordinate durable Workspace work. Mailbox-aware start/heartbeat/successor calls take a native Session ID plus an adapter binding-file path. |
| `assignment-create`, `assignment-get`, `assignment-list`, `assignment-accept`, `assignment-settle`, `assignment-cancel`, `assignment-retry` | Local state-only work specifications and evidence-bearing attempts through an existing live owned Session; no launch or checkout management. |
| `coordination-propose`, `coordination-get`, `coordination-list`, `coordination-accept`, `coordination-advance`, `coordination-submit`, `coordination-cancel` | Existing-peer proposal versions, acknowledgements, blinded initial collection, bounded discussion and final synthesis; local CLI/MCP state only. |
| `mailbox register|phonebook|lookup` | Register one durable address through an adapter-owned binding file or inspect visible active addresses. |
| `mailbox send|broadcast|reply|forward|inbox|sent|thread` | Commit or inspect address-based durable mail. Agent operations require the attached Session plus binding file; human inbox reads require a local/browser human channel. |
| `mailbox wait` | Wait for unread durable mail with `--session` and `--binding-file`; no cursor advancement, read marking, lease renewal, or work acceptance. |
| `mailbox notification-take|notification-settle` | Adapter-only fixed-nudge claim and observed-result settlement. These commands never return mail content or replace inbox pull. |
| knowledge commands | Maintain reusable Workspace knowledge. |
| decision/governed/audit commands | Route human decisions and inspect governed effects. |
| `readiness`, `queue-health`, `recovery-policy`, `recovery-drill` | Inspect supported operational posture and prove an isolated restore. |
| `db migrations|migrate|diagnose|repair|fk-check` | Inspect or repair supported SQLite state. |
| `backup`, `backup-inspect`, `db verify-backup`, `restore` | Create, verify, inspect, or restore manifest backups. |

Dashboard, daemon, model launching, recurring/jobs, generic webhook, semantic/graph,
provider, feedback/pattern, and feature-extra commands are not registered by core.

## Process and port map

| Process | Default bind/port | Supported surface |
|---|---|---|
| Gateway | `127.0.0.1:8787` | `/app`, protected core `/v1`, `/health`, WS/SSE |
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

The modern Config screen and `GET /v1/operator/configuration` expose only a positive,
redacted manifest of local service, Streamable HTTP MCP, SQLite, and supported harness
posture. `PUT /v1/operator/configuration` accepts only the advertised non-secret fields,
requires bootstrap-admin authentication plus the revision returned by GET, and records
the resolved operator in the audit trail. Every write returns `restart_required` because
the gateway may have multiple workers and MCP is a separate process; changing only the
handling process would not establish stack convergence. Values take effect after a
supervised-stack restart. A validation or apply failure restores the prior runtime overlay.
Email settings and withdrawn provider, bridge, gateway-preamble, alternate-storage, and
telemetry fields are neither returned nor accepted by this surface. The deleted legacy browser
has no alternate configuration writer.

Supported secret rules:

- environment values remain outside Git;
- encrypted settings are write-only through the API/UI and never return plaintext;
- admin-key rotation must re-key encrypted rows before replacing a file-managed key;
- external secret managers remain authoritative when the process reads its key from
  the environment;
- errors, logs, audit summaries, and public defect proposals must not contain secret
  values.

### Optional ASK email notifications

ASK email is disabled by default. **Existing SMTP credentials and recipient settings do
not imply consent after an upgrade.** The owner must explicitly set
`BRAINS_ASK_EMAIL_NOTIFICATIONS_ENABLED=true` in the environment of each process that
files ASKs. This single opt-in supplies standing owner consent for one courtesy
notification per newly filed ASK; it is not approval of the requested action and does
not require another decision for each notification.

The flag uses environment settings sources (including loaded `.env`/`secrets.env`),
not YAML configuration, the runtime overlay, encrypted-setting allowlists, or admin
mutation. It is read locally at server/process startup; no API or agent tool enables it.
Recipient and SMTP setup are separate prerequisites. Existing encrypted SMTP
and recipient settings remain usable, but cannot turn on consent. Configure these
locally, outside the repository; all values below are placeholders:

```dotenv
BRAINS_ASK_EMAIL_NOTIFICATIONS_ENABLED=true
BRAINS_OPERATOR_NOTIFY_EMAIL=owner@example.invalid
BRAINS_SMTP_HOST=smtp.example.invalid
BRAINS_SMTP_PORT=587
BRAINS_SMTP_USERNAME=SMTP_USERNAME_PLACEHOLDER
BRAINS_SMTP_PASSWORD=${LOCAL_SMTP_PASSWORD}
BRAINS_SMTP_FROM=brains@example.invalid
BRAINS_SMTP_USE_STARTTLS=true
BRAINS_SMTP_TIMEOUT_SECONDS=15
```

Supply `LOCAL_SMTP_PASSWORD` privately in the process environment when authentication is
required. The password reference syntax is `${NAME}`. Set one plain email address in
`BRAINS_OPERATOR_NOTIFY_EMAIL`, not a display-name list or group. The filing agent cannot
override it. No model or provider setup is involved. SMTP defaults are port 587, STARTTLS
on, and a 15-second socket timeout; that timeout is not an end-to-end delivery guarantee.

Restart affected long-lived processes after setting the flag; for an installed supervised
stack use `brains-ai service restart` after configuring its environment. One-shot CLI and
stdio processes must receive the setting too. To disable, set the flag to `false` (or
remove it from every effective environment source) and restart. This does not recall an
already attempted email or replay old ASKs.

Only the validated ASK code, a bounded title preview, and the server-chosen
`http://127.0.0.1:8787/app` link are included in ASK email. Notification codes must match
`ASK-[A-Za-z0-9]{1,28}` (at most 32 ASCII characters); normal generated codes are unchanged.
The title preview normalizes whitespace and caps at 200 Unicode scalars, including an
ellipsis when truncated (at most 800 UTF-8 bytes). CR or LF anywhere in the original
title rejects the email before SMTP; an unpaired surrogate in the preview is also
rejected. The original stored ASK title/body remain unchanged. The ASK body, proposed
answer, Workspace and Session context are excluded. This is a size bound, not a
confidentiality filter: the allowed preview can still disclose private information.
Loopback opens on the recipient's computer, for the same owner's local-app host, not
public access to another machine. It carries no credentials and does not bypass normal
console authentication. The sender hardcodes port 8787 and `/app`, without reading
custom gateway settings; when using another port, open your configured console directly.

The durable ASK is committed before the courtesy attempt. `notify_ask` records a
`decision_email_notification` ledger event before network access and an outcome event,
with local Session/Workspace attribution and code/status metadata, not email content or
credentials. Invalid notification codes are recorded as null, never raw input. Failure
to record the attempted event prevents SMTP. `file_decision_request` returns an
allowlisted `notification` object alongside `code`, `status: open`, and `workspace`:

| Result | Meaning |
|---|---|
| `status: disabled` | No opt-in; `attempted: false`, `sent: false`. |
| `status: attempted` | Allowed by the filing wrapper; the helper records this pre-SMTP state, then returns a terminal outcome. It is not acceptance. |
| `status: failed` | Incomplete/invalid setup, a local failure, or a conclusive SMTP failure. `attempted` indicates whether the recorded SMTP attempt began. |
| `status: uncertain` | `attempted: true` when sending started but SMTP acceptance could not be established; `attempted: null` when the filing wrapper cannot establish the hook outcome. Both have `sent: false`; do not infer non-delivery. |
| `status: smtp_accepted` | SMTP acknowledged acceptance; `attempted: true`, `sent: true`. Not recipient delivery or reading. |

`sent` is a compatibility boolean, not a separate recipient-delivery receipt.
Optional `error` values are bounded markers (`incomplete_configuration`,
`notification_failed`, `smtp_failed`, `smtp_uncertain`).
An unexpected hook exception or malformed result falls back to
`{"status":"uncertain","attempted":null,"sent":false,"error":"notification_failed"}`;
unknown attempt is not a claim of no attempt. Optional `title_truncated` is a boolean:
the helper adds `true` only when it actually caps the normalized preview, including if
a later setup or send step fails. Disabled or early-rejected notifications omit it;
absence does not establish that the title was processed.
`audit_error: outcome_record_failed` reports failure to record the terminal event without erasing known
SMTP acceptance. A QUIT failure after acceptance also does not turn acceptance into
failure. There are no hidden retries or ASK notification outbox replays. Check the
durable decision and local ledger independently; SMTP configuration alone is no proof
of an attempt. See [MCP](MCP.md#human-decisions) for the filing response boundary.

This optional notification does not reactivate `mail_send`, generic external messaging,
or the historical durable-mail SMTP worker described below.

## Authentication and authorization

Every protected native route resolves one credential-store row to one principal.
Credentials are hashed at rest and carry kind, owner, provenance, expiry, revocation,
and available Org/Workspace scope.

| Surface | Supported boundary |
|---|---|
| `/health` | Open liveness only; never readiness. |
| Native `/v1` | `require_api_key` plus route-specific Org/Workspace capability. |
| `/app` | Signed browser cookie bound to the credential that minted it, or accepted header/key flow. |
| Gateway WS/SSE | Principal plus server-derived subscription authorization, revalidated during the connection. |
| MCP Streamable HTTP `/mcp` and legacy SSE | Credential-store lookup, loopback Host policy, and SDK transport-owner binding by default; SSE is explicit legacy compatibility only. |
| MCP stdio | Local OS process boundary; inherits local state authority. |

The existing `allow_unauthenticated_api` setting is an explicit authentication opt-out,
not a new mode introduced by the transport fix. It bypasses MCP auth middleware,
including the Host check. The authenticated boundaries described here assume it is off.

An MCP `Mcp-Session-Id` or legacy SSE session identifier is a transport address, not a
credential or durable Brains Session ID. Brains bridges the authenticated principal into
the SDK's `AuthenticatedUser` carrier, whose `client_id` is the credential ID (actor ID
fallback) and whose `subject` is the actor ID. The SDK uses that pair to bind the transport
owner and reject foreign-operator reuse before dispatch. A missing credential is refused;
the transport identifier cannot supply the opening operator's authority to another caller.

The carrier uses an empty `AccessToken.token`, avoiding an additional stored secret copy.
The incoming authentication header still contains the credential. Existing credentials
retain normal same-operator behavior subject to the SDK identity pair; rotation that
changes the credential ID changes the pair. Continued transport reuse after credential
rotation is not guaranteed. Nor does this provide per-CLI isolation under one operator.
These MCP checks do not establish uniform token-liveness or revocation behavior across
native HTTP routes and already-open legacy SSE streams.

Restart the MCP serving process with the updated package to load the fix (restart the
supervised stack when using `serve-all`). Installing the package alone does not update
an already-running process. For an isolated transport regression probe, use
`python -m pytest tests/test_coordination_transport.py`: healthy results reject foreign
operator reuse and boolean revision/version arguments over actual Streamable HTTP and
legacy SSE protocol flows. This targeted probe does not replace the candidate's full gate.

Roles are `owner`, `admin`, and `member`. Route capability checks, not labels alone,
provide authorization. A principal that may read an Org but lacks a capability receives
`403`; an entity outside readable scope receives the same `404` as an unknown entity.
An Org cannot lose its last owner through normal API mutation.

Use operator credentials with explicit membership for people and harnesses. Treat the
bootstrap admin key as install-wide authority subject to each control's ownership rules.
In particular, operator assignment/proposal get/list and mutations require matching
`creator_operator_id`; bootstrap admin cannot override another creator's scope.

The [operator work HTTP family](GUIDE.md#operator-work-http-family) consists of ten
protected endpoints under `/v1/operator/workspaces/{slug}`. Reads require Workspace read
capability; human writes require Workspace write capability and browser-cookie identity.
Raw API credentials may read owned work but are refused for these mutations, including
when an API header is supplied alongside a browser cookie. The body cannot declare an
operator or substitute an agent Session. No public authentication exemption is added.
Get/list include Session-authored rows under the same creator operator and remain usable
after Sessions end, without Session creation or lease renewal. Permission fields in
snapshots explain available actions; the server rechecks them when writing.

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

Mailbox wakeup hooks are separate, explicit consent:

```text
brains-ai wire --mailbox-wakeups
```

This installs the Brains-owned stop hook for Claude Code only. At its supported turn
boundary it can request one more turn with a constant, body-free prompt. Copilot CLI,
Codex, and OpenCode remain pull-only notification adapters. A hook
conflict or MCP wiring failure leaves that adapter in pull mode. Claude settings changes
are cross-process locked and use a recoverable atomic exchange that captures displaced
bytes. Ambiguous concurrent edits remain preserved for recovery instead of being
discarded. Native exchange and owner-only recovery behavior is limited to Windows and
macOS.

Probe without changing configuration:

```text
brains-ai wire --status
```

The default wired guidance recommends only Workspace coordination, knowledge, and
bounded substring/symbol repository lookup. Use `brains_search_repo` over MCP or
`brains-ai search-repo QUERY --path WORKSPACE`; the authenticated Workspace Knowledge
tab uses the same control. Results are root-relative line-numbered snippets. `ok` means
matches were found, `empty` means the readable Workspace had no match, and `unavailable`
means the query or Workspace root could not be used. `limited` means a directory, file,
byte, result, traversal, or read boundary prevented a complete answer; returned matches
are partial and an empty partial set is never represented as `no_matches`. Lookup is
read-only and needs no preparation.

## Knowledge and retrieval checks

Use `brains_knowledge_search(status="active", limit=10)` to request effective active
findings, or omit status to inspect flagged history. Expiry and supersession are applied
before the bounded result limit without changing knowledge rows; an expiry sweeper is
not required to exclude historical entries from an active search.

For `brains_retrieve_original(ref)`, inspect `evidence.origin`, `freshness`, `reason`,
`truncated`, and `incomplete` before relying on content. `original_verified: true` means
only that the complete current file matched its recorded full SHA-256 at read time.
Stored rows are mutable, and references are neither immutable backups nor an unbounded
recovery path. Body and recorded evidence have separate 64 KiB UTF-8 caps.

An unknown/inaccessible refusal calls for checking the current principal and stored
Artifact/Source/Workspace ancestry. For an authorized artifact fallback, inspect the
reason: `file_missing`, `binary_file`, `file_unreadable`, `unsafe_path`,
`unsupported_source`, or `unscoped_source`, for example. A readable current file must
be regular and within both its registered Workspace and `repo_dir`/`docs_dir` Source
root, with no symlink/reparse components. Metadata absolute paths cannot override these
roots. Bootstrap admin can read stored evidence from an existing unscoped Source but
cannot use it to read a file. Summary/missing fallbacks are always incomplete.
Hybrid semantic search fuses lexical and local vector ranking with test penalties
and bi-temporal filtering. Specialist execution runs bounded inside assignment
worktrees with scrubbed credentials. Code graph uses stable URNs with
confidence-tiered call edges.
See [MCP](MCP.md#bounded-reference-retrieval) for field semantics and
[Architecture](ARCHITECTURE.md#knowledge-and-reference-evidence) for the cooperative boundary.

## Service operation

User-service renderers target Windows Task Scheduler, macOS launchd, and Linux systemd
user services. Repository checks exercise renderer and hermetic backend behavior. Each
release candidate separately requires the disposable real service-manager cycle and
cleanup evidence defined in [Quality gates](QUALITY_GATES.md). Actual reboot testing is
optional. A manager cycle does not establish login or reboot persistence; any reboot
claim requires a machine-observed boot transition and post-reboot verification.

The guarded lifecycle probe reads native registration and the local definition separately
and compares identity-bearing fields before mutation. Its in-process rollback is limited
to trusted invocation context, with native teardown independent of configuration cleanup.
After quiescence, cleanup removes only the marked journey root's accounted files and
known mutable database/log paths. Drifted configurations, links and unknown resources
are retained; incomplete cleanup is not passing evidence. Guard/provenance failures never
authorize cleanup from a preexisting plan. Abrupt process death before sealing requires
operator review, not automatic recovery from unsealed evidence. Native probes require a
disposable single-writer account and a proven service launch environment. Windows task
XML persists the service spec's `state_dir` in a standard-library `pythonw -c` bootstrap
that sets `BRAINS_STATE_DIR` before importing Brains, including service-package config
imports. It dispatches `brains.service.windows_runner`, which launches the foreground
`-m brains serve-all` child using the exact verified `pythonw.exe` path from the spec,
not a redirector's potentially different `sys.executable`. The child inherits the bound
state and the validated serve-all arguments. Alternate commands, daemon flags, and
unsupported interpreter argument shapes are refused. The XML definition is stored under
that spec's state directory; rendering and dry-run installation write nothing. Reinstall
an existing task to acquire the runner and persist its chosen state root.
Only `BRAINS_STATE_DIR` is captured, not the installing shell's other
environment values or secrets. An explicit `BRAINS_DB_URL` in the launch environment
can still select a different database; state-root persistence does not override all
external configuration or establish real login/reboot persistence.

Windows supervisor recovery is a task-owned restart loop supervised by Task Scheduler,
not reliance on Scheduler retrying a failed supervisor action. The runner waits for one
child at a time; a nonzero exit or launch failure waits 60 seconds before another attempt,
with at most 9999 restarts after the initial launch per action lifetime. Exit zero stops
without restarting. Exhaustion exits nonzero. Scheduler's separate `RestartOnFailure`
policy remains configured at one minute and 9999 retries for action failure; configuration
alone does not prove Scheduler performed a retry. The runner has no service-duration
deadline and no detached worker or PID file. Its child wait lasts for the supervisor's
lifetime, and its failure budget bounds retries rather than healthy service uptime.

Only the supervisor writes `service.pid`. Windows stop captures that record, ends the
task with `/End` to stop the runner (including during backoff), then verifies and kills
the captured supervisor's owned PID tree. Failure to end the action refuses child cleanup
because a surviving runner could respawn it. Exit and unchanged-record checks still gate
PID cleanup and uninstall. Killing only the recorded supervisor tree deliberately leaves
the task-owned runner alive to recover it; it is not a service stop. Native lifecycle
qualification must prove both recovery and no respawn after stop, including any venv
launcher/redirector process chain.

```text
brains-ai service status
brains-ai service logs
```

Every mutating service command exits non-zero when its structured result reports
failure. `service install --label brains-serve-all-<suffix>` provides an explicitly
Brains-owned identity for disposable native validation; subsequent commands accept the
same `--label`. Labels outside that namespace are refused. The default remains
`brains-serve-all`. Linux installation does not change the account's independent linger
policy. Failed deregistration retains the native definition for diagnosis and retry.

On macOS, ordinary start uses `launchctl kickstart` without force-restarting an
already-running supervisor. Stop unloads the job and sends TERM only to a verified
recorded process instance, then polls that same identity for up to 60 seconds. A
successful unload or signal alone is not a completed stop. Stale PID files are cleaned
after exit; reused or unverifiable PIDs are not signalled by number. macOS samples PID,
start time, and executable together. If the captured identity was verified before a
successful unload and both executable and start time subsequently contradict it, stop
can remove only the unchanged captured stale record while leaving the foreign process
alone. A timestamp-only or executable-only contradiction does not prove exit; uncertain
samples get bounded read-only polling, not a signal. Cleanup rechecks identity and exact
file bytes before unlinking and requires file absence afterward. This cooperative
readback is not an atomic fence against a concurrent writer. A changed PID
record, unresolved exit, or failed cleanup reports failure, and uninstall retains the
plist. This bounds the exit polling, not the native command runtime, and does not
independently prove that detached descendants have exited.

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
answer `/health`; MCP must complete a bounded HTTP response. A child that never becomes
ready or stays alive after losing its listener
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

Use `brains-ai setup --path . --service` or `brains-ai service install` only after
reviewing the rendered definition and configuration backup. Inspect the result with
`brains-ai service status`. Do not run a foreground `serve-all` against the same ports
or state directory. Test install, restart, uninstall, and restoration only on a disposable
native host. Optional reboot-persistence testing uses the same isolation and cleanup
rules. Treat successful proof for an earlier commit as stale.

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

`inbox_wait` waits for claimable peer help, not durable mail or topic subscriptions. A
timeout means no claimable request arrived during that wait, not that the request was
cancelled. Wait for durable messages through `mailbox wait`, read them through the mailbox
tools, and use only the notification mode supported by the selected adapter.

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

### Local assignment inspection and recovery

This branch exposes local assignments through CLI and seven MCP tools, plus human
creation, inspection and cancellation in the existing Workspace Work tab and protected
operator HTTP family. The current-main MCP count stays 89; the website's pinned 1.5
release count stays 74. These are unreleased local controls, not remote execution.

Read the assignment directly; generic queue health is not assignment reconciliation:

```text
brains-ai assignment-get <assignment-code> --session <live-owned-session-id>
brains-ai assignment-list --workspace <registered-path> --session <live-owned-session-id> --limit 50
```

A successful read returns stored `status`, current `revision`, `observed_status`, and
attempt history. Check `deadline_exceeded` and each attempt's
`source_session_unavailable`. An uncertainty observation does not mutate stored state,
settle an attempt, or renew a lease. `usage: null` is unknown, not zero.

The default cooperative budget is 3600 seconds; `max_runtime_seconds` permits 1–604800,
and an earlier explicit specification deadline shortens the attempt deadline. No OS
timeout is enforced. Session loss or budget expiry is not evidence of process exit.

After a lost mutation response, read back before submitting the current
`expected_revision`. Stale revisions fail even on replay. Resume the same accepting
Session through its supported lifecycle if it remains eligible; a new Session or linked
successor cannot settle or take over its attempt. A replacement creator Session under
the same operator/Workspace can replay the original creation key, but cannot change its
immutable specification or title under that key.

Cancellation of accepted work records a request, not a confirmed stop. The accepting
Session reports `cancelled`, `failed`, or `uncertain` with evidence; completion after the
request is refused. Retry is explicit and allowed only after conclusive failure or
cancellation, with no unresolved attempt. Active, cancellation-pending, completed, and
uncertain work cannot be retried. Reported uncertainty has no reconciliation operation
in this foundation; cancellation leaves it uncertain. Do not force a status change or
infer that queue repair makes a retry safe. Read-time uncertainty alone still permits
the original live Session to report its actual outcome.

Operator-authored assignments carry `creator_kind: operator` and
`creator_session_id: null`. Browser creation/cancellation records operator/channel
attribution in the same transaction as the state change, without renewing any Session.
Operator reads need no live Session; `permissions.can_cancel` and `permissions.reason`
describe cancellation eligibility. The browser has no assignment execution-retry control
or endpoint. Its unchanged-form creation retry retrieves the same creation key; editing
starts a new request. Agent acceptance/settlement/retry remain CLI/MCP operations.

`checkout_ref`, `links`, and specification `tool` are inert advisory data: no checkout
creation, filesystem ownership, reference fetching, or harness launch follows from them.
See [MCP](MCP.md#local-work-assignments) for fields and [Guide](GUIDE.md#local-work-assignments)
for creation and settlement examples.

### Existing-peer proposal inspection and recovery

The seven peer-coordination tools expose local state for part of
[#38](https://github.com/xibodev/brains-ai/issues/38). They do not provide worker panels,
multi-day checkout management or proposal-specific readiness coverage. Human creation,
inspection, cancellation and phase controls are available through Workspace Work and
the operator HTTP family. Assignment state remains the separate local foundation of
[#36](https://github.com/xibodev/brains-ai/issues/36).

Inspect through a live owned member Session in the proposal's Workspace:

```text
brains-ai coordination-get <code> --session <member-session-id>
brains-ai coordination-list --workspace <registered-path> --session <member-session-id> --limit 50
brains-ai coordination-get <code> --session <member-session-id> --version <historical-version>
```

Alternatively use Workspace Work or operator GET for Session-independent observation,
including after the requester ends. These reads are creator-operator-scoped, not an
admin view of other operators' work. They show latest versions in lists and allow explicit
historical get. Operator snapshots include `permissions.can_advance`,
`advance_blocked_reason`, `can_cancel`, and `cancel_blocked_reason`; historical versions
cannot be mutated. Operator advance requires live owned participants and result owner,
even when all recorded reports needed for the transition are present.

A successful read returns stored status, current version/revision, member-visible reports,
helper counts and remaining-Session lists. Check `remaining_acceptance_session_ids`,
`remaining_initial_session_ids`, `remaining_discussion_session_ids`, `initial_closed`,
`final_ready`, `expired_flag`, and `incomplete_flag`. Session requester acknowledgement is
automatic for Session-authored creation. Human-authored creation has no requester Session
or automatic agent acknowledgement; an explicit live owned result-owner Session and all
participants must acknowledge the exact stored hash.
Session acknowledgement is not human approval. A count of all initials does not close
collection: requester must explicitly advance. All API responses blind other members'
initial payloads, including from requester, until closure; this is not protection against
a shared operator acting as another owned Session or inspecting SQLite. Declared models
are unverified. Final synthesis retains every original nonblank dissent as unresolved.

The human requester/observer sees no initial content before explicit closure: reports,
evidence, uncertainty, clarifications and dissent are withheld on get/list, mutations and
creation replay. Progress counts remain visible. After closure, all recorded reports and
dissent are visible. Neither cancellation nor expiry unblinds a never-closed version.

After a stale-fence refusal or lost response, read before retrying with current `version`
and `expected_revision`. Coordination MCP integer arguments are strict: JSON `true` is
rejected, not coerced to version or revision `1`; optional `version` and list `limit` also
reject booleans. This fix changes neither public signatures nor wire schemas nor the
89-tool count. Scope replacement is requester-only, uses a new creation key and
complete input specification, cancels the old version at revision r+1, and creates the new
version at r+2. Other members acknowledge again; no contribution carries forward. Completed,
cancelled and expired versions cannot be replaced. Historical get retains the old version's
membership and blinding rules; a successor Session does not inherit proposal membership.

The deadline defaults to creation plus one hour; an explicit aware deadline must be future
and within 30 days. Open expired state reads as expired/incomplete with final readiness
false, without changing stored status/revision or renewing leases. There is no sweeper
settlement or automatic advance/execution. Expired work refuses acceptance, advance,
submission and replacement. Requester may still explicitly `coordination-cancel` with a
reason and current version/revision. It cancels protocol state only, not work assignments,
processes, or mail; pre-closure cancellation leaves initial reports blinded.

Do not hand-edit rows or infer recovery from generic queue repair. Links and context are
inert and create no assignments or integration. See [MCP](MCP.md#existing-peer-coordination)
for schema/retry limits and [Guide](GUIDE.md#existing-peer-deliberation) for the sequence.

### Browser work refresh and delivery boundary

Workspace Work uses structured assignment/proposal forms, manual refresh and post-action
readback. It does not automatically poll for peer updates. Refresh and inspect the
last-refreshed timestamp when waiting for acknowledgements or evidence. On a `409`
conflict, the UI refreshes latest state and requires explicit re-review before another
action; it does not automatically retry or accept changed scope. Creation recovery keeps
the same key only for an unchanged open form. Existing send controls record local delivery;
neither a send nor a work-state event proves agent wakeup, acceptance or execution.

This local operator foundation is documented under [#42](https://github.com/xibodev/brains-ai/issues/42).
The ten HTTP endpoints do not include agent accept/submit, assignment execution retry or proposal
replacement. See [Guide](GUIDE.md#operator-work-http-family) for the compact route table.

### Realtime delivery contract and transport comparison

Brains provides local realtime event delivery across separate client processes through three
evaluated transports. Realtime events do not replace SQLite persistence; SQLite remains the
canonical source of truth, and realtime notifications serve as push signals.

#### Delivery contract

Brains distinguishes five distinct phases in work delivery:
1. **Event persistence:** Durable SQLite insert into `events` and `realtime_events` with a monotonic sequence ID.
2. **Live publication:** Fan-out to active listeners on the in-process event bus.
3. **Client receipt:** The subscriber receives the envelope over WebSocket or SSE.
4. **Replay recovery:** Reconnected clients pass `cursor` or `Last-Event-ID` to replay missed events from storage with `replayed: true`.
5. **Work settlement:** Explicit CAS transaction settling an assignment or coordination proposal with verified evidence.

Receipt of a realtime event does not prove agent execution; completion requires verified evidence submitted to storage.

#### Transport comparison

| Metric / Dimension | WebSocket (`/v1/ws`) | Server-Sent Events (`/v1/events`) | HTTP Polling (`GET /v1/...`) |
|---|---|---|---|
| **Delivery latency** | Sub-millisecond local loopback delivery | <2ms local stream delivery | Bound by poll interval (1–5s) |
| **Connection overhead** | Single persistent full-duplex TCP socket | Single persistent HTTP streaming connection | New HTTP request/response cycle per poll |
| **Peak RAM** | ~15 KB per active socket connection | ~8 KB per active connection | Negligible idle RAM; transient per-request buffer |
| **CPU / Threads** | Low; event loop wakeup on frame send | Low; event loop wakeup on chunk write | CPU spikes on poll burst |
| **Offline recovery** | Reconnects with `cursor` in `subscribe` frame | Browser-native `Last-Event-ID` reconnection | Client fetches `since_id` or latest snapshot |
| **Proxy / Middlebox** | May require proxy WebSocket upgrade support | Works over standard HTTP/1.1 and HTTP/2 | Universally supported by all HTTP proxies |
| **Client complexity** | Requires WebSocket client and frame parser | Built-in browser `EventSource` | Plain `fetch()` / `curl` |

Both WebSocket and SSE enforce strict Org and Workspace subscription boundaries, denying unauthorized topic subscriptions and preventing cross-workspace event leakage during replay.

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

Readiness reports bounded SQLite migration and quick/full/FK integrity, the retained HTTP
control gateway's liveness identity and protected-route auth boundary, authenticated MCP
protocol, coordination queue, durable-mail, and verified recovery posture. The withdrawn
model/provider gateway is not a readiness input. Durable
mail separates invalid registration/live attachment and aged unread state. A detached
mailbox with unread accepted mail is reported but is not degraded until the unread-age
threshold is crossed. Runtime execution is withdrawn and does not affect normal-product
readiness. Each supported dependency degrades independently.

Acceptance uses real isolated state transitions rather than replacing component probes:
schema loss, foreign-key violation, stale queue work, invalid durable-mail registration,
and missing, stale, or incompatible recovery evidence each drive their own bounded result.

No readiness field may return a secret or raw exception. A `ready` response still does
not prove GitHub operation, browser journeys, a live-store restore, ingress, or deployment.

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

Migration `154_work_assignments` adds the standalone `work_assignments` and
`work_assignment_attempts` tables, constraints, and indexes. Existing coordination data
and prior migration history are preserved. SQLite DDL participates in the migration
runner's rollback boundary, and the delta is rerunnable after a failed attempt. Its
PostgreSQL companion is compatibility inventory, not an alternate supported backend.
Back up before upgrading and use the normal migration/diagnosis probes; do not delete
historical migrations or hand-edit assignment state to clear an unresolved attempt.

Migration `155_peer_coordination` adds two standalone tables: `coordination_proposals`
and `coordination_contributions`. They retain immutable versioned specifications and
append-only acknowledgements/reports, with scoped creation keys, version/slot constraints,
and an index for Workspace creation order. The additive, rerunnable SQLite delta uses the
migration runner's transactional DDL boundary and preserves existing data and migration
history. Its PostgreSQL companion is compatibility inventory only. Use the same backup,
migration, diagnosis and isolated restore procedures; do not rewrite historical versions
to clear incomplete work. A schema upgrade does not launch peers or send mail.

Migration `156_operator_work_authorship` adds explicit `creator_kind` to
`work_assignments` and `coordination_proposals` and permits null `creator_session_id`
only for `operator` authors. Existing rows default to `session` and keep their original
Session authors, specifications, hashes, attempts and contributions. Proposal
`requester_session_id` is the model/API alias of the stored creator Session column.
The SQLite rebuild preserves historical constraints, indexes, triggers and incoming
foreign keys, with a savepoint inside the caller's transaction for rollback and rerun.
Existing migration 154/155 history is not rewritten; old Session creation hashes retain
replay compatibility. Its PostgreSQL companion remains compatibility inventory only.

For isolated migration verification, run `python -m pytest tests/test_operator_work_migration.py`.
A healthy result preserves both author kinds and prior history, rejects mixed/missing
authorship, matches full ordered model/table column definitions, and exercises interrupted
upgrade rollback and ledger retry with foreign-key enforcement on and off. Core and HTTP
contract probes are `tests/test_operator_work_assignments.py`,
`tests/test_operator_peer_coordination.py`, and `tests/test_operator_workspace_work.py`:
they check attribution, creator scope, blinding, fences and Session-independent reads.
Use disposable SQLite state and the isolation rules in [Quality gates](QUALITY_GATES.md).

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
`queued -> claimed -> delivered|failed` attempt lifecycle. Pull is the default. An
installed Claude Code stop hook may explicitly register `turn_boundary`; Copilot CLI,
Codex, and OpenCode remain pull-only through supported wiring.
For a stronger mode, `mailbox notification-take` claims one attempt and returns only the
fixed nudge `Brains mailbox: new mail is waiting. Pull your durable inbox.` plus bounded
attempt metadata. It never returns subject, body, sender, recipient, or delivery
identity. The adapter records what it observed with `notification-settle`; failure,
detach, mode change, timeout, and prior inbox read leave local delivery intact.

Default `wire` output reports `mailbox_notification_mode: pull` for all harnesses.
`--mailbox-wakeups` installs only the supported consented hooks and reports
`turn_boundary` only after their managed configuration is present. A claim abandoned
after output is reclaimed after its bounded lease; three unconfirmed attempts end in a
stable `delivery_uncertain` state and pull fallback. The hook never receives or emits
message content, addresses, credentials, binding paths, or notification identifiers.
There is no Brains follower daemon or model-input injection. Use proof-bound `mailbox
inbox` as the authoritative recovery path.

The Coordination browser mailbox desk supports authorized human reads and operator
compose/reply/forward; agent mailboxes remain read-only because agent send authority
requires adapter-held proof. Historical SMTP destination, consent, and outbox rows are
preserved. A local delivery to an operator mailbox can still enqueue a copy when its
historical verified destination and copy mode qualify. Enqueuing is not sending: core
does not schedule or lease this outbox, and the ASK opt-in does not activate it or replay
pending rows. The retained `process_smtp_outbox` implementation can be invoked manually
or externally, but that is unsupported; core's scheduling boundary does not prove that
no such worker is running elsewhere. Historical SMTP configuration routes remain withdrawn.

`mailbox wait --session <session-id> --binding-file <binding-file-path>` is a proof-bound
CLI/MCP pull operation, with `--timeout-ms` 0–25000 and `--limit` 1–200. The timeout
bounds polling rather than database work. Direct CLI calls block; registered MCP waits
use AnyIO's shared, default-capacity-limited workers so the event loop can process
concurrent sends. Cancellation waits for an active poll worker to finish rather than
abandoning it; worker-capacity waits and database latency are not covered by a hard
25-second guarantee. This does not cancel jobs. It returns unread mail without marking read,
advancing a stored cursor, renewing a Session lease, or accepting work. Use returned
`next_after_delivery_id` as `--after-delivery-id` to continue by delivery ID; do not use
the message ID or unchanged attachment `cursor`. See [MCP](MCP.md#waiting-for-durable-mail).

The durable-mail health report separates local `state`, `issue_count`, and `reasons`
from its count-only `smtp` diagnostics. `smtp.affects_local_readiness` is false;
`core_scheduler_enabled: false` and `processor_state: not_scheduled_by_core` describe
the core contract, not observed external-worker liveness. Pending `queued`, `retry`, or
`sending` rows derive `smtp.state: blocked` and
`delivery_blocked_reason: processor_not_scheduled_by_core`; observation does not rewrite
those rows. With no open rows, SMTP issues can yield `degraded`, otherwise `ready`.
These diagnostics do not send, settle, delete, or replay mail and do not degrade local
readiness solely because of SMTP. ASK notification attempts are separate ledger events,
not these historical outbox counts, mailbox reads, or work acceptance.

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
brains-ai recovery-drill [archive]
brains-ai readiness
```

A valid archive has a readable manifest, verified payload hash, compatible schema
history, source identity where available, and a successful isolated restore. A bound
verification additionally proves the archive still represents the current source at
the instant of the probe.

Restore is destructive and requires explicit confirmation. Run destructive restore and
rollback tests only with synthetic data in an isolated destination. Restore validates the archive
before changing the target. A live restore captures and verifies a rollback archive
under the state database's `recovery` directory (or at an explicitly supplied path),
then runs SQLite integrity and foreign-key checks after replacement. Before rollback:

1. stop the owned service tree so writers are quiescent;
2. capture and verify a fresh backup of the state being replaced;
3. restore an archive compatible with the target build;
4. restart on loopback;
5. run liveness, readiness, integrity, auth, and in-scope journey probes;
6. reopen ingress only after acceptance.

The recovery policy declares scope, schedule, retention, encryption owner, offsite
owner/location, RTO, RPO, and restore-drill expectation. Brains does not schedule
backups itself. A configured policy is not proof that a backup or drill ran; use
`recovery-drill` with disposable SQLite state to exercise restore and rollback behavior.

`brains-ai readiness` reports SQLite migration and quick/full/FK checks, a real retained
HTTP control-gateway identity/auth-boundary probe, an authenticated MCP initialize plus
tools/list handshake, queue and durable-mail progress, and verified recovery posture
independently. Stable reason codes identify the failed component without returning database
paths, credentials, or raw exception messages. Model-gateway provider routing, Postgres,
and other frozen dependencies are not readiness inputs. Optional SMTP diagnostics are
reported separately and do not affect local readiness.

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

## Website maintenance

The [public website](https://xibodev.github.io/brains-ai/) serves static HTML from
`site/` on `main`, deployed through GitHub Actions at the same URL. The website is
not included in the Python wheel or source distribution. GitHub Releases are the
canonical release history; the browser does not
fetch release data. `scripts/sync_release_site.py --site site` accepts a directory
and changes only marked regions in the exact root-level allowlist `index.html`,
`quickstart.html`, `mcp.html`, and `releases.html`:

- Exactly one `<!-- brains:release-summary:start -->` / `<!-- brains:release-summary:end -->` pair, only in required `index.html`.
- Exactly one `<!-- brains:release-history:start -->` / `<!-- brains:release-history:end -->` pair, only in required `releases.html`.
- Zero to four `<!-- brains:release-version:start -->` / `<!-- brains:release-version:end -->` pairs per page, with at least one across the site; version text has no `v` prefix.
- Zero to four `<!-- brains:mcp-count:start -->` / `<!-- brains:mcp-count:end -->` pairs per page, with at least one across the site.

`quickstart.html` and `mcp.html` may omit markers or be absent. Other root HTML files
are preserved but must contain no Brains markers; root HTML names with different
capitalization are not allowlisted. Unknown, misplaced, nested, unpaired, or excess
markers fail validation. Symlinks and Windows reparse points in the input path or
among its immediate directory entries are rejected; subdirectories are not traversed.
Copy and markup outside managed regions are preserved byte for byte.

The legacy single-page format remains supported: `--site site/index.html` requires
one history pair and one or two pairs each for version and count, without a summary.
A directory whose only root HTML file is `index.html` with history markers uses this
same legacy schema. The checked-in site uses the multi-page schema.

The latest version is the highest published stable `vX.Y.Z`, excluding drafts,
prereleases, and nonstable tags. It appears first; up to five other stable releases
follow by `published_at` descending. Dates are actual UTC publication dates. Titles
and highlights come from release names and `## Highlights` sections, falling back
to the first meaningful body line or just the canonical release link. Markdown is
reduced to escaped plain text, never rendered as trusted HTML. Highlights are limited
to six entries of 500 characters each.
The homepage summary links to the latest release's `releases.html#release-vX-Y-Z`
anchor, shows its publication date, and includes the first two extracted highlights
with the same 500-character limit per entry, plus an All release notes link. It does
not invent substitute highlights when notes are empty.
HTML comments (including an unterminated comment's remainder) and backtick/tilde
fenced blocks are removed before finding Highlights or choosing fallback text.

The version must match `project.version` in that release tag's `pyproject.toml`.
The MCP count is read statically from the literal `CORE_MCP_TOOLS` set in that same
tag's `src/brains/capabilities.py`, without importing or executing Brains. Unreleased
`main` versions and documentation counts are not release facts. Missing or malformed
markers, invalid canonical URLs, empty stable history, or mismatched sources fail
before any output is written. All pages and metadata are validated before changed
outputs are staged in temporary sibling files. Each replacement is atomic, but the
whole directory is not a filesystem transaction: a replacement failure can leave
partial local output. Temporary files are cleaned up, the generator exits nonzero,
and the workflow does not commit or publish that failed run. Retry from a clean
checkout after fixing the cause. Identical output preserves file bytes and mtimes.

`.github/workflows/sync-release-site.yml` runs manually and on release publication or
edits. `release.yml` also calls it explicitly after GitHub Release creation, because
events created by `GITHUB_TOKEN` do not start another event-triggered workflow. The
sync checks out trusted automation and `site/` together from `main`, fetches all
release pages and the selected tag's sources, and invokes directory mode. It derives
the commit path list from tracked files within the four-page `site/` allowlist, so
absent legacy pages do not break `git commit --only`; unrelated files are never
included. Generated changes are committed and pushed to `main`. Publication is
serialized, never force-pushes, and a concurrent branch change fails visibly rather
than overwriting another edit. Rerun after resolving a conflict.

`.github/workflows/site-deploy.yml` deploys pushes to `main` that change `site/**`
or that workflow, and supports manual repair on `main`. It checks out trusted
`main`, configures Pages, uploads `site/` as a Pages artifact, and deploys it with
`actions/deploy-pages` in the `github-pages` environment. No Jekyll or application
build runs. Both publishing workflows share the `pages` concurrency group without
cancelling a running deployment. GitHub may replace an older pending run; these
workflows are not a FIFO queue or a lock against external publishers.

The sync job needs `contents: write` to commit the four allowed pages. Artifact
preparation needs `pages: read`; only the deployment jobs receive `pages: write`
and `id-token: write`. Release callers grant those permissions to the reusable
sync workflow. Token-authored commits do not trigger another workflow, so sync
uploads and deploys its own generated artifact after a successful push, even when
unchanged. A generation or push failure prevents deployment. Deployment status is
reported by `actions/deploy-pages`, not by legacy Pages build polling.

The operator must select **GitHub Actions** as the repository's Pages source and
review the `github-pages` environment protection rules, including release-tag
callers. The workflows do not enable Pages or change its settings automatically.
Do not delete the previous Pages source branch until an Actions deployment and the
live URLs are verified. A workflow success does not prevent a later external
deployment from replacing the site.

Repair and verify with GitHub CLI:

```text
gh workflow run sync-release-site.yml --ref main --repo xibodev/brains-ai
gh run list --workflow sync-release-site.yml --repo xibodev/brains-ai
gh run list --workflow site-deploy.yml --repo xibodev/brains-ai
gh api repos/xibodev/brains-ai/pages --jq '{build_type, html_url}'
```

A healthy result is `build_type: workflow`, the unchanged public URL, and a
successful artifact deployment job in the selected Actions run. To redeploy source
without regenerating release facts, dispatch `site-deploy.yml` on `main` instead.
Review the live site's marked version/count against the selected release's tagged
sources. For an offline generation check with downloaded inputs:

```text
python scripts/sync_release_site.py --site site --releases releases.json --project tagged-pyproject.toml --capabilities tagged-capabilities.py --repository xibodev/brains-ai
```

Keep layout and copy edits outside managed regions. Edit canonical GitHub release
notes to change generated highlights, then rerun the sync if necessary.

## Validation isolation

Run tests that can alter service managers, client configuration, state, databases, or
ports only in a disposable environment with synthetic credentials and state. Do not
mount or address another Brains installation. Verify teardown and configuration
restoration before discarding the environment.

Candidate-specific validation follows [Quality gates](QUALITY_GATES.md). Work that is
intended but unbuilt is tracked in the repository's issues; the
[Product Brief](product/PRODUCT_BRIEF.md) distinguishes current support, intended
direction, and non-goals.

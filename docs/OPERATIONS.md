<!--
last_verified: 2026-08-06T19:13:03.000-06:00
verified_by: GitHub Copilot CLI
verification_basis: clean-state corrective candidate based on HEAD 96c2b66fe8adddd9ea29f59f2944e8e702453f27; source inspection and regression coverage for first-run SQLite state creation; public package/GHCR/GitHub publication pipeline verified; corrective publication, external integration operation, and live deployment not verified
-->

# Brains Operations

## Scope and proof boundary

This document records source-verifiable operating contracts for Brains and its `brains-ai` executable.

- Commands listed here are registered or referenced at HEAD.
- No command is claimed to have succeeded on a clean host unless a separate exact-SHA evidence record says so.
- No service installation, provider connection, database state, backup, UAT environment, container, public ingress, or deployment is verified.
- `/health` is liveness and inventory, not candidate readiness.

## Executable and launch surfaces

The only installed project executable in `pyproject.toml` is `brains-ai`.
The core runtime dependencies include WebSocket transport support for
`/v1/ws`; a server environment without that transport cannot satisfy the
realtime contract.

Source-defined launch commands:

| Command | Source-defined effect | Verification status |
|---|---|---|
| `brains-ai setup` | Initialize shared state, key/operator/workspace setup, and agent wiring workflow. | Source/tests present; not run by this verification. |
| `brains-ai serve-all` | Supervise gateway and MCP child processes; the legacy dashboard child runs only with `--dashboard` or `BRAINS_LEGACY_SURFACES=1`. | Source/tests present; not run. |
| `brains-ai up` | Convenience stack command used by repository harnesses. | Source/tests present; not run. |
| `brains-ai serve` | Run the gateway surface. | Source present; not run. |
| `brains-ai dashboard` | Run the retired legacy dashboard/admin process (refuses without `BRAINS_LEGACY_SURFACES=1`). | Source present; not run. |
| `brains-ai mcp` | Run the MCP server. | Source present; not run. |
| `brains-ai daemon start` | Start the Runtime daemon path. | Source/tests present; not run. |
| `brains-ai service install|start|stop|restart|status|logs|uninstall` | Manage a user-level OS service. `install` accepts `--gateway-port` / `--mcp-port`; omitting the gateway port reuses the persisted endpoint or selects a bindable loopback fallback when the default is unavailable. | Renderer/command tests present; installed state unverified. |

Do not use the removed `brains` executable. Legacy install helpers and the dev Dockerfile still reference it and are not supported operating paths at this HEAD.

## Installation and package publication

The public Python distribution is `brains-ai` and the recommended isolated installation is `pipx install brains-ai`. `brains-ai setup --path <workspace>` initializes local state and wiring; `brains-ai serve-all` starts the gateway (including the `/app` console) and the MCP server. The opt-in `--service` setup flag installs the same supervised stack as a user-level autostart service.

For a file-backed SQLite URL, storage bootstrap creates the database's missing parent directory before SQLAlchemy opens the file. A first run on a clean home directory therefore creates `~/.brains` itself; in-memory and SQLite `file:` URI databases are left to SQLite's own handling.

`.github/workflows/release.yml` publishes a wheel and source archive to PyPI, a multi-architecture image to GHCR, and a GitHub Release when a `v*` tag is pushed. Before building, `scripts/check_release_version.py` fails closed unless the tag equals `v` plus the exact `[project].version` in `pyproject.toml`. PyPI artifacts are immutable; a faulty publication is corrected with a new version rather than replacing an existing file.

## Default process and port map

| Process/surface | Default bind/port | Primary routes | Notes |
|---|---|---|---|
| Gateway | `127.0.0.1:8787` by default; installed services may persist a bindable fallback | `/health`, `/v1/*`, `/app*`, `/admin*`, `/hooks/*`, `/relay/*` | Modern Brains surface and model gateway; `brains-ai service status` reports the effective console URL. Legacy `/admin` HTML redirects to `/app` unless `BRAINS_LEGACY_SURFACES=1`. |
| Legacy dashboard | `127.0.0.1:9876` | `/dashboard*`, `/admin*` | Separate retired process; started only via `serve-all --dashboard` or `BRAINS_LEGACY_SURFACES=1`. |
| MCP SSE | port `9877`; bind controlled by MCP settings | `/sse` transport | Public bind requires explicit opt-in and still requires auth. |
| wa-web sidecar | `8788` by default | `/health`, `/send` | Separate Node service. |

`serve-all` defaults all browser/API processes to loopback. Container port publication is insufficient unless gateway/dashboard bind inside the container to `0.0.0.0`. MCP additionally requires its public-bind opt-in for non-loopback access.

## State and configuration

### Default state

`BRAINS_STATE_DIR` overrides the state root. Without it, current code uses `~/.brains`.

State may include:

- `brains.db` and SQLite WAL files;
- `admin-key`;
- operator key files;
- `audit-key`;
- `secrets.env`;
- `daemon.json`;
- provider OAuth/cache data;
- `sessions/service.pid` and `sessions/service.log`;
- executor transcript files;
- optional generated views.

The bare default database URL is rewritten to `<state>/brains.db` so CLI and long-running processes share one machine database.

### Configuration layers

Main settings use the `BRAINS_` environment prefix. Current configuration sources include:

1. explicit CLI flags where a command provides them;
2. environment variables;
3. `.env`;
4. operator configuration and `brains.runtime.yaml` overlay;
5. built-in defaults.

The runtime overlay schema version is `1`. Admin-managed secrets may be stored in `<state>/secrets.env` only when explicitly requested; environment values take precedence. YAML environment references are restricted to allowed fields.

Runtime daemon configuration has its own precedence:

```text
CLI flag > BRAINS_DAEMON_* env > <state>/daemon.json > hub defaults > built-in defaults
```

Current daemon built-in `hub_url` is `http://127.0.0.1:9876`, but native Runtime APIs are mounted on the gateway at `:8787`. Treat the default as broken; set the correct gateway URL explicitly until code is repaired.

## Authentication and authorization

| Surface | Current authentication | Operational limitation |
|---|---|---|
| `/health` | none | Reveals liveness/subsystem inventory by design. |
| Model `/v1` routes | credential-store lookup via `require_api_key`; Runtime credentials refused | Rate limiting defaults off; unauthenticated mode is an explicit unsafe option and resolves to the named `admin` principal. |
| Native product `/v1` routes | one resolved principal plus a per-route Org capability check | `GET /v1/usage` (install-wide gateway totals) and provider configuration are restricted to the bootstrap admin because they carry no Org attribution; `GET /v1/orgs/{org}/usage` (BL-P1-07) is the Org-scoped equivalent, readable by any principal with `org.read` on that Org, joined through `usage_attributions` filtered on `org_id`. |
| `/app`, `/dashboard`, protected `/admin` | signed cookie bound to the key that minted it, or an accepted key/header; legacy query-key support exists | Admin config surfaces are install-level, not Org-scoped; avoid the legacy `?key=` form at public ingress. |
| WS `/v1/ws` | principal from query/header/cookie, then per-topic authorization | Topics are authorized, not derived from the principal; avoid query credentials at public ingress. |
| SSE `/v1/events` | console gate plus per-topic authorization | Same topic model as WebSocket; Runtime credentials are refused. |
| MCP SSE | credential-store lookup and loopback Host policy by default | Full mode exposes powerful mutation tools; Runtime credentials are refused. |
| MCP stdio | process boundary | Inherits local environment and database authority. |
| Runtime enrollment redeem | one-time token in request body, claimed by one conditional update | Route is deliberately unauthenticated; the token is the credential. |
| Runtime credential | Org-bound, machine-bound, expiring, revocable | Limited to register/heartbeat/status/claim/execute for its own machine; a stolen credential acts as that machine until revoked or expired. |
| `/hooks/{slug}` | per-trigger bearer | Definition scope and dedupe apply. |
| `/hooks/github` | `BRAINS_GITHUB_WEBHOOK_SECRET`, `X-Hub-Signature-256`, `X-GitHub-Delivery`, `X-GitHub-Event`, and `BRAINS_GITHUB_REPOSITORY_ORG_BINDINGS` entries shaped as `owner/repo=org-slug` | Public GitHub ingress uses exact repository-to-Org scope and durable replay refusal. The `/v1/integrations/github/webhook` compatibility alias applies the same checks plus protected-route operator authentication. |
| `/relay/*` | `BRAINS_RELAY_TOKEN`; route returns unavailable when unset | `X-Dedupe-Key` is preferred; when absent, the raw-body SHA-256 is scoped to a five-minute replay window. Reply and triage outcomes use expiring leases and attempt-fenced durable settlement. External bridge operation remains unverified. |

An expired integration lease is evidence of a possibly crashed worker, not proof
that its effect did not happen. The bootstrap-admin-only
`GET /v1/config/integrations/deliveries?status=processing` surface exposes the
attempt, and `POST /v1/config/integrations/deliveries/{id}/release` changes the
exact still-owning attempt to `failed` only after the operator confirms the
worker is gone. A later redelivery may then reclaim it; Brains never
automatically replays an expired attempt over a worker that may still be active.

Onboarding step writes are operator-owned and Org-consistent. A Runtime,
Persona, Project, Issue, or Session supplied to an attempt must belong to the
same visible Org; Session Workspace visibility is also enforced. A Session
from another Issue cannot complete onboarding. Issue/Persona dispatch creates
the pending Session in the Runtime working Workspace under the target Org and
refuses a Workspace already owned by another Org.

Roles and capabilities (`owner` > `admin` > `member`, deny by default):

| Capability | Minimum role | Covers |
|---|---|---|
| `org.read` | `member` | Org, Pods, Skills, Personas, Projects, Issues, Sessions, Runtimes, approvals, automation |
| `org.write` | `member` | Content creation and mutation, Session spawn, approval resolution |
| `org.admin` | `admin` | Org rename/archive, membership, automation enable/fire, Runtime lifecycle, enrollment minting |
| `org.owner` | `owner` | Granting or revoking the `owner` role, and any change to an existing owner |

Answers are uniform: `401` for no/unknown/revoked/expired credential, `403` for
a principal that may read the Org but lacks the capability, `404` for anything
in an Org it may not read. Changing or removing an existing `owner` requires
`org.owner`, and an Org may not be left with no owner at all: both the demotion
and the removal are refused with `409` unless another owner remains. Recovery
from a genuinely ownerless Org is an explicit local call
(`orgs.remove_member(..., bootstrap_recovery=True)`), never an HTTP request.

The `/admin/*` configuration console is install-level, not Org-scoped: it edits
provider credentials, environment overrides and router policy, so it is
restricted to the bootstrap admin that holds the install's own key. An Org
`owner` is answered `403`, as is any Runtime credential or any principal with
no Org role on any browser or console surface.

### Credential operations

Every accepted secret is one row in `api_credentials`, stored as a sha256 hash.
Nothing prints a raw secret except the one-time output of `operator add` and of
enrollment redemption.

```bash
brains-ai credentials list                       # kind, binding, expiry, last use
brains-ai credentials list --kind runtime
brains-ai credentials revoke <credential_id>     # effective on the next request
brains-ai credentials revoke-machine <machine>   # disconnect one box
brains-ai credentials doctor                     # exits 1 when anything is ambiguous
brains-ai operator add <slug> --org <org> --org-role member
```

Retiring a key is an explicit act, not an inference from disk. Each row records
where it came from (`api_credentials.source`). `brains-ai admin-key rotate` and
`operators.remove_operator_key` (which deletes
`~/.brains/operator-keys/<slug>.key`) each name the exact secret they supersede
- the rotated key is read before it is overwritten, the key file before it is
unlinked - so that one hash is revoked and the replacement adopted in the same
step, and the old secret is refused on the very next request.

Passive reconciliation only ever *adopts*. A key that a given process cannot
see is not treated as retired: a worker started with a different
`BRAINS_API_KEY`, a container without `~/.brains` mounted, or an unreadable key
directory would otherwise revoke, for the whole install, credentials it never
issued. An unreadable key source is reported by `brains-ai credentials doctor`
(`local_source_error`) rather than being read as "no keys", and until it is
fixed that process adopts nothing and denies nothing.

Two consequences to plan for:

* A Runtime credential minted by enrollment, and any credential registered
  explicitly, are never revoked by a rotation.
* Restoring a deleted key file does **not** bring the credential back, and
  re-adding a rotated key to `BRAINS_API_KEYS` does not either. An operator's
  revocation stands until the key is issued again deliberately
  (`brains-ai operator add`, an admin-key rotation, or an explicit
  registration).

Two consequences to plan for:

* A Runtime credential minted by enrollment, and any credential registered
  explicitly, are never revoked by a rotation.
* Restoring a deleted key file does **not** bring the credential back, and
  re-adding a rotated key to `BRAINS_API_KEYS` does not either. An operator's
  revocation stands until the key is issued again deliberately
  (`brains-ai operator add`, an admin-key rotation, or an explicit
  registration).

Upgrade note: migration `130_org_member_backfill` turns the previously implicit
grant into rows - every operator that existed at upgrade time joins the
`default` Org as `member`, `admin` as `owner`. `daemon-*` operators minted by
pre-BL-P0-01 enrollment are deliberately excluded: they keep authenticating,
see nothing, and `brains-ai credentials doctor` lists them. Re-enroll those
machines (`POST /v1/runtimes/enrol` then redeem) to replace them with a
Runtime-narrow credential, then remove the stale operator's key file. An
operator created after the upgrade is granted nothing until it is invited to an
Org.

Operational rules:

- Never bypass `require_api_key` or `require_console_auth`, and never widen a
  route's capability check to make a client work.
- Do not expose gateway, dashboard, MCP, or wa-web publicly without an explicit
  ingress, credential, and authorization review.
- Treat the admin key as install-wide authority: it is the bootstrap principal
  and is `owner` of every Org. Give people operator keys with explicit Org
  membership instead.
- Store provider, relay, bridge, and database credentials outside Git.

### Realtime subscriptions and replay

`WS /v1/ws` and `GET /v1/events` share one authorization model. A client names a
topic, the server resolves it against its own state and returns the canonical
name, and anything outside the closed grammar (wildcards, unknown families or
channels, unknown entities, another Org's entity, a Session in a `private`
Workspace) is refused with one uniform answer that names no topic. A Runtime
credential is refused both transports.

Both transports resume by cursor. Durable events carry a monotonic `event_id`
from `realtime_events`; a client sends the highest one it applied as `cursor`
(WS `subscribe`/`resync`) or as `cursor`/`Last-Event-ID` (SSE) and receives a
bounded replay. The ack that precedes a replay never reports a cursor past the
frames it announces, and a `replay_complete` frame closes a batch that was
fully delivered, so a client that drops mid-replay resumes from the last event
it applied rather than skipping the remainder. If the cursor cannot be
honoured, the client is sent `realtime.reset` with a reason and is expected to
re-read over REST rather than to trust a short stream. A catch-up batch is
written whole: live frames published while it is being read and sent queue
behind it and follow `replay_complete`, so the frames always arrive in cursor
order and a client that drops mid-batch is never carried past what it holds.
The live copy of an event a batch already delivered is dropped by exact
`event_id` **on that topic only**, so adding a subscription never suppresses
traffic on one the connection already held - and for the same reason a batch
that read only some of the connection's topics hands over no cursor: its ack
carries `covers_connection: false` and its `replay_complete` reports
`cursor: null` plus the `batch_cursor` it wrote, so the console applies those
frames without resuming from them and the events queued below them on the
topics that batch never read are still recovered by the next reconnect.

Operating notes:

- events are committed before they are announced, so a state change that did
  not commit is never broadcast. Publishing is idempotent for a caller that
  supplies a `dedupe_key`: the Session *command* publisher derives one from
  the command id and the state it reached, so a retried message or a stop
  pressed twice is delivered once logically. The Session lifecycle, Issue,
  approval and Runtime publishers have no stable operation id to derive one
  from, so their delivery is at-least-once and a retried publish is a second
  event a client applies twice;
- `runtime.heartbeat` is deliberately not recorded: it fires every 15 seconds
  per Runtime, and the state worth replaying is the `runtime.status`
  transition. Recording liveness would churn the shared replay window and make
  every console that was away resynchronise for traffic it never wanted;
- retention and gap detection are install-wide, not per topic, so a very busy
  install can hand a long-disconnected console a reset even for a quiet topic;
- live fan-out is per gateway process. A publish from the MCP or dashboard
  process is durable and is picked up when a client resumes by cursor; it is
  not pushed to a socket attached to another process. Run one gateway, or
  expect catch-up latency rather than push latency for cross-process events;
- revoking a credential or removing a membership takes effect on the live
  stream within one revalidation interval, not at the client's next reconnect.
  A removed membership is not a dropped connection: the console loses those
  topics and keeps the rest;
- a revalidation that cannot *run* closes the connection too, with the reason
  `revalidation_failed`. If the store is unavailable, expect realtime clients
  to be disconnected and to reconnect on their backoff rather than to keep
  streaming: an unproven credential is refused, not assumed;
- `realtime_events` grows with product activity and is trimmed to
  `BRAINS_REALTIME_RETENTION_ROWS` rows. Lower it on a small box, but a cursor
  older than the oldest retained row is answered with a reset, so a very small
  retention turns brief disconnects into full resynchronisations.

| Setting | Effect |
|---|---|
| `BRAINS_REALTIME_RETENTION_ROWS` | How many rows the durable event log keeps (default 5000). `0` disables pruning. |
| `BRAINS_REALTIME_REPLAY_LIMIT` | How many events one catch-up may deliver before it reports `replay_truncated` instead (default 500). |
| `BRAINS_REALTIME_REVALIDATE_SECONDS` | How often a live WS/SSE connection re-resolves its credential and re-authorizes its topics (default 10). Lower it to shorten the window a revoked credential keeps streaming; it costs one credential lookup per connection per interval. |

## Session control queue

`POST /v1/sessions/{id}/message` and `POST /v1/sessions/{id}/stop` write a
`session_commands` row before anything is delivered;
`GET /v1/sessions/{id}/commands` is the durable history a reloaded console
renders. The Runtime consumer surface is
`GET /v1/runtimes/{id}/session-commands`,
`POST /v1/runtimes/{id}/session-commands/{command}/claim`,
`POST /v1/runtimes/{id}/session-commands/{command}/ack`,
`POST /v1/runtimes/{id}/session-commands/{command}/release` and
`POST /v1/runtimes/{id}/sessions/reconcile`. The same operations are available
locally as `brains-ai session-message|session-stop|session-commands` and as the
`brains_session_message`, `brains_session_stop` and `brains_session_commands`
MCP tools.

Operating notes:

- a command is `requested` until a consumer claims it. A Session with no
  Runtime binding, and no process owned by the hub process, has no consumer:
  its commands stay queued until the process that owns it dispatches them or
  the Session ends, which cancels them. That is visible, not silent;
- a command belongs to one consumer, decided by its Session's binding rather
  than by which machine it sits on. A Runtime lists and claims only commands
  whose Session is bound to *it*; the commands of an unbound Session (one
  started from the CLI, or streamed by the hub) belong to the process that
  launched the agent. Several workers, an operator's own CLI session and the
  hub can share one box, and each holds only its own process handles - so a
  foreign claim would answer `not_owned` and consume the operator's command
  for nothing. A consumer that ends up holding a command it does not own
  (its Session was re-bound in flight) releases it rather than acknowledging
  it; the release route only accepts the current holder, so it cannot reopen
  an attempt that was already reassigned;
- the machine recorded on a Session or a command is a diagnostic, not the
  ownership test. A hub and its Runtimes are usually different boxes, and a
  spawn Session is created by the hub, so the row names the hub's machine
  until the Runtime opens it. A Runtime routes by binding alone - listing,
  claiming, releasing, settling and reconciling - so a stop pressed on a
  Session spawned from the console still reaches the remote Runtime holding
  the process. Rows written from now on record the bound Runtime's machine
  (at spawn, when the Runtime opens the Session, and when a command is
  queued); rows written before that are not migrated and keep working;
- the daemon polls commands on its own thread, separately from the assignment
  loop. Assignment execution blocks for the whole life of the agent CLI, so a
  command drained from that loop could only ever be delivered while the
  Runtime owned no process - which is exactly when a stop cannot be delivered;
- a claim holds a lease. If a Runtime dies mid-delivery the command returns to
  the queue when the lease expires and its `attempt` increments; after
  `BRAINS_SESSION_COMMAND_MAX_ATTEMPTS` it is settled `failed`/`abandoned`, so
  a command that no consumer can complete does not block the queue behind it;
- a stop only ends a Session when a consumer proves the process is gone
  (`stopped`, `already_exited`). `not_owned` means the Runtime restarted; the
  Session is left alone and corrected by reconciliation instead;
- a stop that failed while the Session kept running is retryable: pressing
  stop again records a *new* attempt rather than returning the failed one, so
  the operator is not left with an inert button. Pressing stop while an
  attempt is still open - or after one already stopped the Session - is still
  the same command, and a caller that supplies its own `operation_id` always
  gets exactly one command back;
- reconciliation runs on daemon startup and re-registration, once per Runtime,
  and ends the Sessions that Runtime no longer owns. Each Runtime is told only
  the handles this daemon holds for it, so on a box running several CLIs a
  Runtime with nothing running sends an empty list and reconciles its own
  stale rows instead of claiming its siblings' Sessions and being refused.
  Sessions younger than the 90-second grace window are never reconciled,
  because a daemon opens the hub row a moment before it owns the process. A
  reconciliation, poll, claim, release or acknowledgement that fails is
  logged and reported (`brains-ai daemon start --once` prints it as a `stage`
  record) rather than dropped;
- a message to `copilot`, `claude` or `codex` is settled
  `failed`/`unsupported`: those CLIs are launched in their non-interactive
  shapes and have no open input channel. `GET /v1/sessions/{id}` reports
  `message_capability`, and the console disables its composer with that reason
  rather than accepting text that cannot arrive.

| Setting | Effect |
|---|---|
| `BRAINS_SESSION_COMMAND_LEASE_SECONDS` | How long a consumer holds a claimed command before it is re-queued (default 60). |
| `BRAINS_SESSION_COMMAND_MAX_ATTEMPTS` | How many claims a command may take before it is settled `failed`/`abandoned` (default 5). |

## Provider and routing configuration

Provider adapters present in source:

- `echo`
- `ollama`
- `openai_compatible`
- `litellm`
- `github_copilot`

The gateway exposes OpenAI- and Anthropic-compatible request shapes. Built-in
model tiers point to the simulated `echo` provider until configured. The modern
Config screen is read-only: it reports each provider as
`simulated`/`unconfigured`/`configured`, shows the exact provider/model behind
every tier, and runs a bounded probe that settles
`reachable`/`degraded` without returning raw upstream exceptions.

Routing contract:

- explicit provider/model and catalog IDs are resolved directly;
- `brains/cheap`, `brains/default`, `brains/strong`, and `brains/deep` use operator-pinned tier routes;
- only explicit `brains/auto` invokes classification when routing is enabled;
- an unknown explicit ID returns a model-not-found error;
- response model fields identify the actual upstream model.

Optional provider/subsystem dependencies are not installed by default. Enabling a gated subsystem without its extra is expected to fail startup with an install hint.

Configuration changes remain outside the modern console. Legacy admin
overlay/environment writes reload only the process handling the write. Restart
every gateway, worker, daemon, MCP, and CLI process before treating such a
change as active; no cross-process reload signal is claimed.

The GitHub Copilot provider is default-off, uses local OAuth/cache state, and
is restricted by configuration in shared contexts. `/health` remains liveness,
not provider readiness; a successful bounded Config probe is evidence only for
that provider at that instant.

## MCP and agent wiring

Source-defined commands:

```text
brains-ai wire
brains-ai wire --status
brains-ai unwire
```

Current adapters target detected agent configuration for Copilot CLI, Claude Code, Codex, and OpenCode. OpenCode uses `~/.config/opencode/opencode.json` with its native `mcp.brains` local/remote schema; the other adapters retain their tool-specific JSON or TOML contracts. Wiring preserves unmanaged configuration, writes a managed ownership marker, and maintains backups/conflict reporting so `unwire` removes only Brains-owned entries.

Current MCP public tool names use the `brains_` prefix. Internal compatibility for dotted names is not the documentation contract.

Tool selection:

- unset, `full`, or `all`: broad tool set;
- `lean`: curated coordination/retrieval set;
- comma-separated names: explicit allowlist.

For shared or exposed environments, prefer the smallest allowlist that supports the operator workflow.

### Experimental gate

Some surfaces are real but not yet mature (unproven end to end, evidence below E3, or a cooperative enforcement boundary). The normal install hides and refuses them; an explicit environment flag is the only opt-in, and every refusal names its switch. The registry lives in `brains.experimental`.

`BRAINS_MCP_EXPERIMENTAL=1` enables:

- the experimental MCP tools: `search_semantic`, `graph_build`, `graph_query`, `graph_neighbors`, `graph_path`, `graph_subsystems`, `graph_export`, and `session_message`;
- Autopilot *scheduled auto-fire* (the MCP scheduler's due loop); manual fire stays ungated;
- the CLI equivalents: `graph-*`, `docs-index`, `embed-repo`, `search-semantic`.

An explicit `BRAINS_MCP_TOOLS` allowlist does not bypass this gate — only the environment flag does.

`BRAINS_LEGACY_SURFACES=1` enables:

- the legacy dashboard child (`serve-all --dashboard` also works; `--no-dashboard` vetoes both);
- the gateway's legacy `/admin` HTML pages (otherwise they redirect to `/app`; non-GET answers 404). The `/admin/api/*` JSON surface stays available either way.

`BRAINS_EXPERIMENTAL_GATEWAY=1` enables:

- the model-serving surface: the OpenAI/Anthropic-compatible proxy routes (`/v1/chat/completions`, `/v1/completions`, `/v1/responses`, `/v1/messages`, `/v1/models`, `/v1/count_tokens`) and the `brains-ai run <tool>` launcher. Disabled, those routes answer 404 with a pointer to this switch; model access is expected to come from each CLI's own provider logins. The native control-plane API (`/v1/sessions`, `/v1/admin/*`, coordination, webhooks) and the console are unaffected.

`BRAINS_UI_LABS=1` enables the unfinished execution-model screens under
`/app/labs`: onboarding, Runtimes, Personas, Pods, Projects, Issues, Sessions,
and Automation. The normal console does not link to or serve those screens when
the switch is off; old entity-first URLs redirect through the same fail-closed
Labs gate.

Optional pip extras (`telegram`, `slack`, `whatsapp`, `whatsapp_web`, `postgres`, `otel`, `litellm`) remain install-gated through `brains-ai features`, and their wizard labels carry `(experimental)` — enabling one is an explicit act already.

### Agent-to-agent comms

The coordination plane carries four comms primitives alongside tasks/claims/handoffs. All are mailbox-centric by design — an agent only ever polls its own inbox:

- **Discovery** — `list_live_agents` (MCP) / `brains-ai live-agents` returns every live session on the brain across all workspaces with its harness (`tool`), state, and freshness. Live means not ended and active within `BRAINS_TOPIC_BLAST_TTL` seconds (default 900); freshness is the opportunistic heartbeat every brain tool call stamps.
- **Direct + workspace mail** — existing `send_message`/`read_messages`; a message addressed via `workspace_path` reaches whatever session is alive in that workspace.
- **Peer help** — existing `ask_peer`/`wait_for_request`/`answer_request`, now with an optional harness constraint: `required_tool="claude"` or `"not:copilot"` (case-insensitive). A session whose tool does not satisfy the constraint never claims the request; it stays open for a matching harness. This is the cross-CLI validation path (PR review asks, plan checks, adversarial passes) without either side sharing context.
- **One poll primitive** — `inbox_wait` (MCP) / `brains-ai inbox-wait` blocks until the session has unread mail OR a claimable peer request, returning which woke it. Cadence remains operator/agent policy; this makes each tick latency-bounded instead of sleep-guessed.
- **Attribution and dead handles are validated** — sends and asks from an ended session are refused; reading/polling an ended session errors loudly naming `ended_at`, the recorded reason, and live replacement candidates in the same workspace. Rerouting mail to a workspace's current live session is explicit sender opt-in (`route_to_current`) and only when exactly one candidate exists — silent rerouting is rejected by design.
- **Topic boards** — `topic_post`/`topic_read`/`topic_list` (MCP) / `brains-ai topic-post|topic-read|topic-list`. Topics are install-wide named boards; posts may thread via `reply_to`. Posting blasts exactly one `kind="topic"` inbox notification per *other* workspace with live sessions — never the poster's own — so scenario 5's simplification holds: agents poll only their inbox. The board itself is the archive for workspaces that were quiet at post time. `required_tool` on a post is an advisory hint in this slice; enforced claiming is exclusive to peer help.

Topic posts are an append-only archive like knowledge entries — no unresolved-work lifecycle — so they sit outside the coordination-queue health families by design.

### Outbound email

Brains can send plain-text email over SMTP (Amazon SES works through its SMTP endpoint, so SES is configuration, not a separate sender). Configure it in `/app/operations/config/email`. Persistent values are encrypted in the Brains database with AES-256-GCM; a per-row key is derived with Scrypt from the current admin key and a random salt. Admin-key rotation re-encrypts all rows before replacing the key. Secret plaintext is never returned by the API or UI.

When `BRAINS_API_KEY` is process-environment managed, `brains-ai admin-key rotate` refuses: rotate the external secret in its authoritative store and restart Brains. File-managed installs re-key encrypted rows automatically during rotation.

Process environment values (`BRAINS_SMTP_HOST`, `BRAINS_SMTP_PORT`, `BRAINS_SMTP_USERNAME`, `BRAINS_SMTP_PASSWORD`, `BRAINS_SMTP_FROM`, `BRAINS_SMTP_USE_STARTTLS`, `BRAINS_SMTP_TIMEOUT_SECONDS`, `BRAINS_OPERATOR_NOTIFY_EMAIL`) remain higher precedence than encrypted DB values. The write reloads the handling process; restart every other long-lived Brains process after changing configuration.

`/app/operations/config/secrets` uses the same encrypted store for the existing provider and bridge credentials (GitHub Copilot OAuth, GitHub webhook, OpenAI-compatible, Telegram, Slack, WhatsApp and WhatsApp Web). The status API returns only `set`, `source` and `secret` metadata; plaintext is write-only. `/app/operations/config/general` edits the validated non-secret runtime overlay.

- `mail_send` (MCP) / `brains-ai mail-send` — one mail; audited via the events ledger (`email_sent`, recipient + subject, never body).
- `mail_status` (MCP) / `brains-ai mail-status` — redacted configuration snapshot.
- ASKs are copied to `BRAINS_OPERATOR_NOTIFY_EMAIL` best-effort when the mailer is configured; an email outage never blocks or fails an ask (the durable row is authoritative).

Enforcement truthfulness: sends are config-gated and audited but not yet routed through the governed-action approval contract — treat this as an operator-trusted surface until that lands.

### Canonical operator console

`/app` is Workspace-first. Command Center summarizes visible Workspaces and attention; Workspaces provides the scoped control room; Coordination presents shared task, claim, handoff, comms, and learning queues; Governance presents human decisions and audit evidence; Operations presents bounded install posture; Act launches named typed HTTP capabilities. The browser never invokes `brains-ai`, MCP, or a generic shell endpoint. The separate `/dashboard` process remains retired.

## Service operation

The service module renders user-level service definitions for:

- Windows Task Scheduler;
- macOS launchd;
- Linux systemd user services.

`brains-ai service install` preflights the requested loopback gateway port. An
explicit unavailable port is refused. When no port is supplied and the default
cannot be bound, the installer selects a bindable fallback, writes it into the
OS service definition, and persists the non-secret endpoint contract under the
Brains service state directory. `service status` probes those persisted ports
and returns the effective gateway, console, and MCP URLs. The supervisor also
preflights its gateway bind and exits once on a permanent conflict instead of
starting an unbounded child restart loop.

The supervisor writes:

- PID: `<state>/sessions/service.pid` - an additive JSON record (`format`,
  `pid`, `exe`, `cmdline`, `start_time` where the platform exposes it,
  `recorded_at`), not a bare integer
- log: `<state>/sessions/service.log`
- rotation: 5 MiB with three backups

Each child restarts with exponential backoff from 1 to 60 seconds. A child that remains up for 60 seconds resets its backoff.

`brains.service.common.verify_pid` (BL-P1-09) validates a recorded PID
against the live process table before any caller treats it as proof of
liveness: it reports `"verified"` (PID alive AND recorded process start time
matches, with executable identity matching when available), `"degraded"` (alive, but this platform/permission level could not
confirm identity), `"unverified"` (a legacy plain-integer pidfile - alive,
nothing recorded to compare), `"stale"` (the PID either answers to nothing,
or answers to a different executable/start time - almost certainly reused),
or `"absent"` (no pidfile). `service.status()` reports this under
`service_pid`; `service.stop()` on Windows/macOS refuses to tree-kill or
signal any PID that is not `"verified"`; a `"stale"` pidfile is removed,
while `"degraded"` or `"unverified"` state is retained for operator
diagnosis. The Runtime daemon's own `daemon.pid` (`brains-ai daemon stop`)
applies the identical fail-closed check. A PID is still never targeted by
process name - only by a specific PID whose recorded identity was verified.

Service installation and boot persistence are host mutations. They are not verified by this document and must be tested on the target OS before relying on them.

## Container and harness status

| Path | Intended use | HEAD-verifiable status |
|---|---|---|
| root `Dockerfile` | Slim non-root runtime image with `/data` volume, `brains-ai` entrypoint, and gateway/dashboard/MCP healthcheck | Source present; image/deployment not verified. |
| `docker/Dockerfile.dev` + compose | Editable dev stack on shifted loopback ports | **Broken:** entrypoint is removed executable `brains`. Do not present as runnable until repaired. |
| `sandbox/` | Isolated container, read-only repository mount, isolated HOME/state, shifted ports | Configuration present; current run unverified. |
| `sandbox/battle/` | Two Brains processes with shared Postgres and read-only repo mount | Harness present; no current result is asserted. |
| `docker-compose.uat.yml` | Postgres and OTLP sidecars | Not a complete Brains UAT stack. |
| `deploy/box/` | Postgres/Ollama/Brains/Caddy scaffold | **Broken and unverified.** |

### Box scaffold blockers

- The root image entrypoint already invokes `brains-ai`, while compose also begins its command with `brains-ai`.
- The image state root is `/data/.brains`, while compose mounts `/home/brains/.brains`.
- The image user is UID 1000, while compose defaults to UID/GID 10001.
- The root image installs core dependencies, while the scaffold selects Postgres without installing the Postgres extra.
- Caddy exposes `/dashboard*` and `/relay/*`, not `/app*`, `/admin*`, or the gateway API.
- The dashboard auth redirect targets `/admin/login`, which the catch-all ingress rejects.
- No Atlas registration, hostname, image digest, listener, certificate, or live health evidence is present.

Do not deploy this scaffold without resolving the blockers and running the full quality and UAT contract.

## Health and readiness

### Liveness

`GET /health` returns HTTP 200 with:

- status;
- runtime overlay schema version;
- installed extras;
- storage backend;
- enabled bridges;
- OpenTelemetry configuration snapshot.

It intentionally returns 200 when optional extras are missing. It does not check database writes, schema parity, child process health, provider calls, Runtime freshness, queue progress, backup state, or product journeys.

`/admin/healthz` is a separate admin route. Root `/healthz` does not exist.

### Readiness

`GET /v1/admin/readiness` is a protected, bootstrap-admin-only surface (auth
identical to `/v1/config/summary` and `/v1/usage`: the in-handler
`principal.is_bootstrap_admin` check, `403` for anyone else). It reports one
overall `ready`/`degraded` verdict plus four bounded, redacted component
states - `storage` (`brains.storage.migrations.migration_status`), `queue`
(`brains.control.queue_health.summarize`, degraded when any family has a
stale/expired row not yet swept), `runtime_lifecycle`
(`brains.control.runtimes.list_runtimes` + `count_stale`, using the same TTL
the scheduler sweep applies), and `recovery_policy`
(`brains.control.recovery_policy.recovery_readiness`, degraded when the
declared backup policy is incomplete or the migration/backup compatibility
precheck fails). No component ever returns a secret or a raw exception
message - only the exception's type name. Live model-provider readiness is
deliberately excluded (see BL-P1-11): a simulated/unconfigured provider is a
routing fact, not an operational outage, and would otherwise make every
lean-core install permanently "degraded" for an unrelated reason.

Equivalent CLI: `brains-ai readiness` (exit 1 when the overall verdict is
`degraded`). Equivalent coordination-queue detail: `GET /v1/admin/queue-health`
/ `brains-ai queue-health status`, and its dry-run/apply repair:
`POST /v1/admin/queue-health/repair` / `brains-ai queue-health repair
[--apply]` - see "Coordination queue health" below. Equivalent recovery
policy: `GET /v1/admin/recovery-policy` / `brains-ai recovery-policy` - see
"Recovery policy" below.

This readiness contract does not by itself prove: gateway/dashboard/MCP
process health beyond the current process; Runtime enrollment/heartbeat/claim
against a real Runtime; a configured provider's live connectivity; realtime
connect/backfill; an actual backup/restore cycle; the required J1-J11 browser
journeys; or ingress/rollback. Before relying on a candidate, an operator must
separately verify:

1. gateway, dashboard, and MCP processes expected for the topology;
2. authenticated native API read/write;
3. database schema and write (`GET /v1/admin/readiness` covers this);
4. Runtime enrollment/heartbeat/claim;
5. configured provider probe if model execution is required;
6. realtime connect/backfill;
7. backup creation and isolated restore;
8. required J1-J11 paths;
9. ingress and rollback.

## Coordination queue health

`brains.control.queue_health` (BL-P1-12) is the single place that names every
durable coordination-queue family's owner, scope, lifecycle, and expiry
policy (or explicit indefinite policy), and reports its health:

| Family | Owner | Expiry policy |
|---|---|---|
| `approvals` | the human resolver separated from the requester | indefinite while open by design - no automatic expiry |
| `handoffs` | the Session that set it | `BRAINS_HANDOFF_STALE_HOURS` (default 24h), swept opportunistically by `mark_stale_handoffs` |
| `mailbox` | the addressed Session or Workspace broadcast | indefinite - a message is marked read, never expired or deleted |
| `help_requests` | the peer Session/Workspace it targets | per-request `expires_at` (its `timeout_ms`), swept by `_expire_due` |
| `workspace_claims` | the Session holding the claim | `expires_at` (its `duration_minutes`); expired rows are deleted (released), not merely marked |
| `session_commands` | the Runtime/local consumer bound to the Session | `BRAINS_SESSION_COMMAND_LEASE_SECONDS` per attempt, requeued or failed by `expire_leases` |
| `checkpoints` (`snapshots`) | the Workspace it snapshots | indefinite by design - kept until the owning Workspace is pruned |

`GET /v1/admin/queue-health` (bootstrap-admin only) / `brains-ai queue-health
status` returns the family summary above (with live total/open/stale-or-
expired counts) plus a bounded, non-destructive orphan-reference diagnosis:
every family's Session/Workspace foreign-key-shaped column is checked against
the live `agent_sessions`/`workspaces` tables (SQLite foreign-key enforcement
is opt-in - see AC-B5-02 - so an orphan can exist even when nothing else is
wrong), with a bounded sample of affected rows. Nothing is deleted by
diagnosis.

`POST /v1/admin/queue-health/repair` / `brains-ai queue-health repair
[--apply]` is a dry-run by default (`would_affect_rows` per action) and, with
`--apply`/`{"apply": true}`, performs only the objectively-safe continuity
repairs - each one exactly the fenced helper (`mark_stale_handoffs`,
claims' `_expire_claims`, help's `_expire_due`, `expire_leases`) the affected
family's own read path already calls opportunistically. It never deletes an
open approval, unread mail, or any other unresolved work; it is idempotent
across repeated/concurrent invocations.

## Recovery policy

`brains.control.recovery_policy` (BL-P1-09) declares - it does not enforce -
an install's managed-recovery commitment via `BRAINS_BACKUP_*` settings:
`backup_scope`, `backup_schedule`, `backup_retention_days`,
`backup_encryption_at_rest` / `backup_encryption_owner`,
`backup_offsite_owner` / `backup_offsite_location`, `backup_rto_minutes`,
`backup_rpo_minutes`, `backup_restore_drill_required` /
`backup_last_restore_drill_at`. Brains does not run a backup scheduler
itself; an external scheduler invoking `brains-ai backup` on the declared
cadence is expected to honour it. Every field defaults to an explicit unset
sentinel (empty string / zero) rather than a fabricated value, so an
unconfigured install reports `complete: false` with the exact missing
fields - never a silent "managed" claim. `backup_encryption_owner` is only
mandatory once `backup_encryption_at_rest` is set; `backup_last_restore_drill_at`
is only mandatory while `backup_restore_drill_required` (the default) holds.

`GET /v1/admin/recovery-policy` / `brains-ai recovery-policy` returns the
redacted policy plus a compatibility precheck that reuses the existing
migration and backup contracts rather than inventing new ones:
`brains.storage.migrations.migration_status` for schema health, and, for a
Postgres backend, the same `pg_dump`/`pg_restore` tool-presence check
`brains.backup` itself requires before a real backup or restore. `ready` is
`true` only when the declared policy is complete AND the mechanics precheck
passed - never when the install has simply configured nothing.

No field here ever carries a secret: `backup_encryption_owner` and
`backup_offsite_owner`/`backup_offsite_location` are pointers/descriptions
("infra-team", "S3 bucket `ops-backups`, see runbook"), not credentials - the
credential itself lives in that store's own secret management, never in
brains config.

### Rollback order (current-facts contract)

For the exact candidate under test, the documented rollback order is:

1. stop the supervised stack (`brains-ai service stop`, or the topology's
   equivalent) so nothing writes during the restore;
2. capture a fresh manifest backup of the *current* (about-to-be-replaced)
   state, verified by isolated restore (`db verify-backup`), so the
   rollback itself is reversible;
3. restore the target manifest backup (`brains-ai restore`), which refuses a
   schema this build cannot express (`schema_compatibility` /
   `SchemaIncompatible`) rather than silently truncating history;
4. restart the stack and re-run `GET /v1/admin/readiness` - a `degraded`
   storage or queue component after restore is a signal to stop, not a
   signal to proceed;
5. re-verify the exact J1-J11 paths the release depends on before declaring
   the rollback complete.

The exact remaining gap is E4: no isolated backup/restore/rollback drill has
been run and evidenced for this candidate. Everything above is a documented
current-facts order this repository's tooling supports mechanically
(schema-compatibility refusal, isolated restore verification, readiness
re-check); it is not a claim that the drill has been performed.

## Schema migrations

```bash
brains-ai db migrations
brains-ai db migrate
```

- `db migrations` is read-only: it applies no delta and prints the ordered
  ledger with each migration's status (`applied`, `skipped`, `failed`,
  `running`), backend, checksum and checksum origin, attempt count, timings, and
  the error text of any attempt that rolled back, plus findings for edited
  migrations, ledger gaps, interrupted runs, and migrations the ledger knows
  but this build does not ship. It exits non-zero when anything is pending or
  failed, or when the schema does not contain every model-declared object. The
  database is identified by backend, host, and name only; no credentials are
  printed. It does bootstrap the ledger table itself and adopt a pre-checksum
  ledger's rows, because that is what makes the ledger readable at all. That
  adoption is backend-aware: a row for a migration this backend has no
  implementation for becomes `skipped` with checksum origin `legacy-unproven`
  and stays applicable if that backend's delta ships later, and a row that
  cannot be evidence of execution on this backend stays `applied` but is
  labelled `legacy-unproven` and reported rather than re-executed.
- `db migrate` applies the pending migrations in order. Each delta runs in its
  own transaction and is recorded `applied` only after it commits. It exits 2
  when the runner refuses - an edited migration, a backend with no
  implementation, a corrupt corpus - and 1 when the store is still not healthy
  afterwards.
- Every entry point already calls the same runner at startup, so `db migrate` is
  for operators who want the upgrade to happen (and be inspected) at a chosen
  moment rather than on the next command.
- Baseline comment-only preambles are metadata and are never submitted as SQL;
  executable unmarked SQL remains an unconditional baseline block.
- Refusals are deliberate and are not worked around by configuration. An edited
  historical migration is fixed by restoring the file and adding a new numbered
  migration. A backend with no implementation is fixed by shipping
  `<migration-id>.<backend>.sql`. A schema missing a model-declared object is
  fixed by adding the migration that creates it.
- `db repair --apply` refuses to run while the ledger is unsettled - anything
  pending, failed, interrupted, gapped, or recorded by a build this one does not
  know - because a repair rewrites rows against a schema history that has to be
  settled first. Run `db migrate`, re-check `db migrations`, then repair.

## Backup and restore

Source-defined CLI/MCP capabilities support:

- SQLite online backup through the standard library backup API, which is the only
  correct way to copy a live WAL database;
- Postgres dump through `pg_dump` when available;
- gzip/tar archive containing a manifest and database payload;
- SHA-256 payload verification;
- backend/schema metadata inspection;
- destructive restore through SQLite replacement or `psql`.

SQLite manifests (`manifest_version` 2) additionally record the resolved source
path, SQLite header identity, a fingerprint of every schema object, per-table row
counts, the `foreign_key_check` count observed on the copy, and a
`source_fingerprint` of the live database at capture time. The copy itself is
checked with `PRAGMA integrity_check` and compared against the source schema
before the archive is written; a failure aborts the backup instead of producing an
archive that only looks valid.

### Manifest compatibility

`manifest_version` is the archive's read contract and it moves in one direction:

- this build **reads** `1` and `2`. A `1` archive written by an earlier build
  inspects and restores normally here; its source-identity fields are absent, so
  verification reports it as unverifiable rather than passing it;
- this build **writes** `2`. A `2` archive therefore requires **this build or
  later** to inspect, verify, or restore. An older build has no reader contract
  for it. Keep the build that wrote an archive available for as long as that
  archive is part of a recovery plan, or re-take the backup with the build you
  intend to restore with;
- an unreadable future version (a `3`) is refused by name, listing the versions
  this build reads;
- unknown *fields* inside a readable version are ignored, so a later build may add
  manifest fields without breaking this reader.

Every way a manifest can be malformed - not a tarball, not JSON, not a JSON
object, a missing required field, a field of the wrong type, an unsafe
`data_file` - is reported as a backup error with one message, and the CLI exits
non-zero without a traceback.

The `source_fingerprint` is the SHA-256 of the online-backup image of the source,
which for a fresh archive is the same value as `data_sha256`. It is a
deterministic function of the committed content, so it can be recomputed from any
connection or process, includes WAL frames that are not yet checkpointed, and does
not change when a checkpoint later moves those frames into the main file.
`PRAGMA data_version` is recorded for diagnostics only: SQLite only guarantees it
is comparable within a single connection.

Relevant CLI commands:

```text
brains-ai backup
brains-ai backup-inspect
brains-ai restore
brains-ai db verify-backup <archive> [--expect-source <sqlite file>]
```

`db verify-backup` restores the archive into a temporary directory and checks the
restored database against every manifest claim: hash, `integrity_check`, schema
fingerprint, row counts, and SQLite identity. With `--expect-source` it also binds
the archive to one database *and to that database's current content*: the manifest
source path must match, the live schema must still match the archive, and the live
`source_fingerprint` must still equal the recorded one. An archive that any later
committed write has superseded is reported as stale, because restoring it would
lose that write. Binding reads only the schema and one online-backup image pass of
the live file: no `integrity_check`, no `foreign_key_check`, no row counting, and
no long-lived read transaction against the operator's store. Nothing outside the
temporary directory is written, so it is safe to run against a live install.

An unbound `db verify-backup` (no `--expect-source`) still passes for a stale but
intact archive: it answers "can this be restored", not "is this current". A bound
verification answers "is this current" *at the instant it runs*; only
`db repair --apply`, which holds the write lock across verification and mutation,
can keep that answer true until the write. Anything else must quiesce writers
itself.

### Schema compatibility of an archive

The manifest's `schema_versions` list is the set of migrations that had actually
been **applied** when the archive was written; a migration that was skipped on
that backend, or that failed, is not in it. `restore` and `db verify-backup`
compare that list against the migrations this build ships:

- an archive whose list contains a migration this build does not know was taken
  from a newer store. Restoring it would leave a schema no installed migration
  can account for, so restore refuses by name and verification reports the
  unknown IDs;
- a `1` manifest, and any archive whose ledger was empty, records nothing to
  compare; that is reported as unknown, not as compatible.

Restore requires explicit confirmation at the CLI surface. Current code does not define:

- a backup schedule;
- offsite ownership;
- encryption policy;
- retention policy;
- RTO or RPO;
- exact-version compatibility refusal;
- periodic restore drills.

Never call a backup valid until an isolated restore has been verified against the exact candidate.
An old raw SQLite copy is not equivalent to a current manifest backup, checksum
verification, or successful current-schema restore.

## Database integrity and repair

```text
brains-ai db diagnose
brains-ai db repair [--apply --backup <archive> | --backup-to <path>] [--delete-orphans]
brains-ai db fk-check
```

- `db diagnose` is read-only. It runs `PRAGMA integrity_check` and
  `PRAGMA foreign_key_check` plus the product invariants tracked by BL-P0-07 in
  [product/BACKLOG.md](product/BACKLOG.md): Sessions whose `ended_at` and explicit state
  contradict each other, Org-less Workspaces, and orphaned or expired Workspace
  claims. It prints one deterministic machine-readable report: the same database
  and the same evaluation time produce the same output. Checks whose table or
  column does not exist on the store's schema are listed under `skipped_checks`,
  so a store that predates a migration is diagnosed instead of erroring.
- Diagnosis fails closed. The report carries an explicit `complete` field, and
  `ok` requires it: a report with any `skipped_checks` entry is never `ok`, and
  `db diagnose` exits non-zero for a finding *or* for missing coverage. An
  invariant that could not be evaluated is unknown, not clean.
- `db repair` is a dry-run unless `--apply` is passed. A dry run opens the file
  read-only and takes no lock, so it can run beside live writers.
- `--apply` serializes the whole destructive sequence. It takes the SQLite write
  lock (`BEGIN IMMEDIATE`) **first**, and only then diagnoses the store, captures
  and verifies the backup, applies every repair pass, and commits. The lock is
  released only by that commit or by the rollback that replaces it, so there is no
  instant at which another connection can commit between the state the archive
  captured and the state the repair mutates. If another writer already holds the
  lock, the repair refuses to start rather than proceeding unquiesced.
- SQLite's online backup API cannot read from a connection that is inside a write
  transaction, so the archive is captured by a separate reader while the repair's
  transaction holds every writer off. That claim is enforced, not documented: each
  backup step re-proves that the repair connection is still in its transaction and
  that no other connection can take the write lock.
- Applying requires a manifest backup that has been verified by isolated restore
  *and shown to still represent the live database* (`--backup` for an existing
  archive, `--backup-to` to create and verify one under the lock), refuses to run
  when `integrity_check` is not `ok`, and commits every action inside one
  transaction that rolls back as a whole on failure. It records
  `admin.db_repaired.attempted` **before** it takes the write lock and
  `admin.db_repaired` (or `admin.db_repaired.failed`) after the transaction
  closes: the repair holds the write lock across its whole transaction, so its
  record cannot join that transaction, and a store that cannot accept the
  attempt refuses the repair (exit 3) instead of mutating first and finding out
  afterwards. A refused `--apply` therefore leaves attempted+failed evidence and
  no data change. A backup taken before a later write is refused:
  the repair would then be running behind a safety net that no longer contains the
  current state. `--backup-to` is therefore truthful by construction - the archive
  it writes is the pre-repair state, not an approximation of it.
- Repair converges inside that one transaction. Actions run in dependency order,
  and the plan is re-derived from the database after each pass until nothing
  deterministic is left, because fixing one invariant can expose another - a
  Session that gains `ended_at` turns its live claim into a lease held by an ended
  Session. The `passes` field reports how many rounds were needed; failing to
  converge within the bound rolls the whole repair back.
- Engine scans and invariant replanning are separate. The whole-database
  `integrity_check`/`foreign_key_check` pair runs once as preflight under the lock
  and once as the full post-repair verdict; the convergence passes in between only
  evaluate the product and foreign-key state they need to plan, re-checking
  foreign keys just over the tables the previous pass could have broken. A
  structurally sound database cannot be made unsound by the DML in between, and
  the final full check has the last word either way.
- `db repair --apply` exits non-zero whenever the post-repair diagnosis is not
  clean, which includes findings that only an operator can resolve and invariant
  checks that could not run on this schema. A clean exit means the store has no
  remaining findings *and* every check was evaluated.
- Repair never deletes durable records to satisfy a foreign key: dangling
  references on nullable columns are cleared and the record is kept. Rows whose
  required parent is gone are reported as `requires_operator` and are removed only
  when `--delete-orphans` is passed, using the schema-derived cascade.
- The one exception is the lease tables (`workspace_claims`). A claim is ephemeral
  lock state, not history: a claim that has expired, whose owning Session has
  ended, or whose owning Session no longer exists is removed deterministically
  without `--delete-orphans`, because there is nothing to preserve and nothing to
  decide. Every other table whose required parent is missing holds a durable record
  and keeps waiting for the flag and the operator's decision.
- Rows whose correct value cannot be derived from stored evidence are reported as
  `ambiguous_legacy` and left untouched. Repair narrows the problem; it does not
  invent history.
- `db fk-check` reports whether foreign-key enforcement can be enabled safely.
  Enforcement is opt-in through `BRAINS_SQLITE_ENFORCE_FOREIGN_KEYS=1`, and the
  connection hook refuses to enable it while violations exist. `db diagnose` and
  `db fk-check` open the database file directly and keep working while that
  refusal is active; unset the variable to run `db repair --apply`.
- `workspaces prune` and `workspaces doctor --prune-missing` delete a Workspace
  together with the rows that cannot exist without it, and clear the optional
  references held by records that can (Projects, Issues, Personas, knowledge).
  Both remain dry-run until `--apply`.

Operating order for an existing store: `db migrations` -> `db migrate` ->
`db diagnose` -> `backup` -> `db verify-backup` -> `db repair --apply` ->
`db fk-check` -> enable enforcement.
`db repair --apply --backup-to <path>` is the safe form: it takes the write lock
first and captures, verifies, and repairs behind it, so writers do not have to be
quiesced by hand. Passing an archive taken earlier with `--backup` is still
supported and still refused if anything committed in between.
Repair does not remove the need for the E4 backup, repair, restart, and restore
drill on the exact candidate; that drill is still unperformed.

SQLite contention policy:

- every connection waits `BRAINS_SQLITE_BUSY_TIMEOUT_MS` milliseconds for the
  single writer; the default is `30000` for multi-agent hosts;
- `workspace-claims` is a pure read and filters expired leases in SQL. Physical
  expiry runs only in claim mutations, explicit queue-health repair, or fenced
  database repair;
- a sustained `database is locked` after the bounded wait is an outage, not a
  retry hint. Take an audited backup, stop the owned service tree, diagnose,
  checkpoint WAL, repair behind the write-lock fence, then restart and recheck;
- bare `session-start` records no PID because the CLI helper is short-lived.
  Harnesses that own a durable process may pass `--pid <agent-pid>` explicitly.

## Governed actions and the audit chain

Every path that can produce an outward effect - the PATH-shim gate, any process
Brains launches (including recurring/autopilot spawn), and the CLI/MCP
governance surfaces - files one governed action first, and the decision commits
with its audit entry in a single transaction. A governed action whose record
cannot be written is refused before the effect, not carried out unrecorded.

Admin effects that are *not* database writes - an overlay/env-override write, a
backup archive, a restore over the live store, `db repair --apply` - cannot
share a transaction with their record, so they use the two-phase form instead:
`<action>.attempted` is committed **before** the effect runs, and `<action>` (or
`<action>.failed`) is appended after it returned. A store that cannot accept the
attempt refuses the effect; an effect that raised keeps its attempt entry plus a
`.failed` entry; and a success entry is only ever written after the thing it
names. One consequence is stated rather than hidden: `restore` writes its
attempt into the database it then replaces, so that entry stays with the
pre-restore store while the completion entry lands in the restored chain.

Inspecting the record:

```text
brains-ai governed-list --limit 50
brains-ai governed-list --status pending
brains-ai governed-sweep
brains-ai audit-list --action-prefix governed.
brains-ai audit-verify
brains-ai audit-adopt      # once, only for a store older than signed heads
```

`governed-list` reports actor, target, tool, the normalised-argument digest
(never the arguments), tier, decision, approval code, attempt, attempt start,
result, error, and timestamps. Secret-shaped values are removed before anything
is stored: URL credentials, `NAME=VALUE` pairs with a secret-shaped name
whatever their prefix, `--user`/`--password` and the tool-scoped short forms
(`curl -u`, `curl -b`, `mysql -pSECRET`, `redis-cli -a`, `sshpass -p`,
`mongosh -p`, and `docker login -p`/`podman login -p`, which are credentials
only under that subcommand), `Authorization`/`Cookie`/`X-Api-Key` header
values, credential fields inside a form-encoded or JSON body, known provider
token shapes and JWTs, and high-confidence opaque tokens never reach the
digest, the stored summary, the audit payload, the ASK body, or a bridge
message. The same tool-aware rules are applied to the summary and to any
recorded reason or error, so a subprocess failure that echoes its own command
line cannot smuggle a password into the chain. A name counts as secret-shaped
when it contains a distinctive word
(`token`, `password`, `api_key`) *or* when a whole segment of it is one of the
bare credential words - `DB_PASS`, `MASTER_KEY`, `dbPass` - which `bypass`,
`passenger`, `keyboard` and `monkey` are not. A *lone* bare word claims the
argument after it only when it is qualified, so `aws s3api delete-object --key
prod/db/backup.tar.gz` still shows the object being deleted and two objects
still produce two digests, while `--key=VALUE`, `KEY=VALUE`, a `Key:` header,
a `key` body field, and any secret-shaped value are redacted as before.
Ordinary arguments are deliberately left readable - `python -u`,
`ssh -p 2222`, `docker run -p 8080:80`, `redis-cli -p 6379`, `wget -b URL`, a
path or `s3://` URI, a git SHA - because an operator cannot
approve a command whose target has been redacted away.

Approval codes are minted above the highest suffix held by a live
`approval_requests` row *and* by a permanent `governed_actions.approval_code`,
so pruning the Workspace that owned an ASK cannot make the next ASK re-use a
code that an approved, denied or expired governed action already records. A
duplicate that reaches the store anyway is refused as an approval-code
collision - not as an audit-append failure - so `audit-verify` is not the place
to look for it.

`governed-sweep` settles actions whose approval window or attempt lease has
expired. The recurring scheduler tick runs the same maintenance every cycle, so
a host running `brains-ai serve`/the MCP server does not need to call it; use
it on a host that runs neither. The lease is per attempt, and every settlement
is conditional on the status and attempt it read, so an action that is actively
executing is never swept.

An executing action is measured by silence, not by runtime. Its owner renews a
heartbeat while the effect runs, and only `BRAINS_EXECUTION_LEASE_SECONDS`
(default: the attempt lease) *without* a renewal makes it sweepable - so an
agent session or deploy that runs for hours stays `executing`, while a process
that died stops renewing and is settled once that budget is spent, recorded as
"abandoned while executing" because whether its effect happened is exactly what
is unknown. `BRAINS_EXECUTION_HEARTBEAT_SECONDS` sets the renewal interval
(default: a third of the lease, and never more than half of it). Heartbeats are
not audit events: `audit-list --action-prefix governed.` shows transitions, not
proof of life. A store that cannot accept a renewal is logged as a warning
(`brains.govern`) rather than treated as health, and the sweep remains the
authority either way.

Statuses are `requested`, `pending`, `authorized`, `executing`, then one of
`succeeded`, `failed`, `released`, `denied`, `expired`. `released` is the
honest end state for a command the gate handed off by replacing its own process
(`os.execv` on POSIX): the decision was made, the authorisation was spent, and
the real binary took over, so no later transition can be observed from here. It
is terminal - the sweep leaves it alone rather than rewriting a correct handoff
as "abandoned while executing" - and it is not a claim that the command
succeeded. Where the outcome *is* observable (a Windows child process, an
in-process `run_governed` effect) the row ends `succeeded` or `failed` with the
real result, and a local-tier command is settled the same way as a gated one.
A command run to completion in-process is settled from its exit status
(`result: exit N`, and `failed` for any non-zero status), so a `check=False`
run that failed is never recorded as success; a spawned child outlives the
call, so its row records the launch only.

`audit-verify` exits 1 and reports the diverging entry when the chain is
broken; it fails closed on mutation, deletion, insertion, truncation, a forged
or missing chain head, an unsigned head over a non-empty log, a signature
cleared after adoption, and a head count that disagrees with the stored rows.
`stored_entries` and `appended_entries` in its output must match - a lower
stored count is a truncated tail even when the surviving rows still chain.
Verification is safe to run against a live store: the log and the head come
from one snapshot (a Postgres `REPEATABLE READ` transaction, a SQLite read
transaction), so an append landing mid-scan is either wholly counted or wholly
unseen and never reported as tamper, and it neither blocks appends nor writes
anything itself.

`head_signed`, `adopted_version` and `adoption_required` describe the head. A
store created by this version signs its head on first use and writes an
`audit.chain.initialized` genesis marker, so `adoption_required` is false and
stays false. A store written before signed heads reports `adoption_required:
true`, fails verification and refuses every append until an operator runs
`brains-ai audit-adopt` once: adoption verifies every entry, the head triple
and the append count *before* signing, marks the store, records itself as
`audit.chain.adopted`, and refuses outright if the log already records a signed
origin or does not verify. If `audit-adopt` refuses, the store is a tamper
report, not a migration step - do not clear `head_mac` to "fix" it, since a
cleared signature on an adopted store is itself reported.

One authorization limit is worth stating before relying on this: outbound
bridge, provider, and webhook network calls do not pass through the contract at
all. The resolver *is* bound: a Runtime credential can never resolve an
approval; an ASK filed by a Session may only be resolved from the console
cookie or a local CLI, because a shared operator key presented over HTTP cannot
be bound to a human; the Session the server knows the credential is running can
never resolve its own ASK; and the Persona identity behind the request cannot
either. A caller-declared `session_id` can only add a denial - omitting it does
not make a caller separated. A refusal appends
`approval.self_resolution_denied` with its reason; a resolution records the
deciding principal and its channel, and commits with its audit entry in one
transaction. The rule's limit is equally explicit - a human and an agent
sharing one browser session are indistinguishable, which is why Personas are
bound to their own operator identity and why agents should be given
persona-bound operator keys rather than the admin key.

Operational knobs:

| Setting | Effect |
|---|---|
| `BRAINS_APPROVAL_TTL_SECONDS` | How long a human decision stays spendable (default 900). An approval past its window is refused at consumption and the pending action settles as expired. |
| `BRAINS_EXECUTION_LEASE_SECONDS` | How long an `executing` action may go without a heartbeat before the sweep may settle it (default: the attempt lease). It is a silence budget, not a runtime budget - a long command that keeps renewing is never swept. |
| `BRAINS_EXECUTION_HEARTBEAT_SECONDS` | How often a running action renews its execution lease (default: a third of the lease, clamped to at most half of it). |
| `BRAINS_GATE_MODE=strict` | Gate any binary that is not on the known-local allowlist, and gate shell/interpreter inline code outright. |
| `BRAINS_ALLOW_RECURRING_SPAWN` | Must be `1` before a recurring definition may spawn at all; the governed gate still applies on top of it. |
| `BRAINS_AUDIT_KEY` / `BRAINS_AUDIT_KEY_FILE` | The HMAC key the chain is signed with. Losing it makes existing entries unverifiable; leaking it makes forgery possible. |

Reach, stated plainly: this is an in-process boundary. It covers what Brains
launches on the agent-execution path and what the shims intercept, and it
classifies absolute paths, Windows paths and extensions, wrapper commands,
shell/interpreter inline code, `-m` module runs, and remote-code runners -
`npx`/`uvx`/`pipx`, and the fetch-and-execute shapes their multiplexed cousins
spell differently (`pip install`, `uv pip install`, `uv tool install`,
`uv tool run`, `uv run`, `uv add`/`sync`/`build`, `uv python install`,
`uv self update`), while read-only `uv` commands (`uv pip list`,
`uv tool list`, `uv tree`) stay local. It
does not contain a third-party agent CLI that invokes an absolute path itself,
rewrites `PATH`, or opens a raw socket; several operator-invoked paths still
exec directly (`brains-ai self-update`'s `git pull`/`pip install`, the `gh`
device login, `pg_dump`/`psql` during backup, the supervisor's child services,
`brains-ai run`); and outbound bridge, provider, and webhook network calls are
not routed through it. Treat those as ungoverned until BL-P0-03 closes.

## Isolated UAT

Repository harnesses are starting points, not proof.

Minimum UAT isolation:

- isolated HOME and `BRAINS_STATE_DIR`;
- isolated database and credentials;
- loopback-only shifted ports unless ingress is under test;
- disposable or read-only source, with an unchanged-worktree assertion;
- simulated tool execution unless the test explicitly targets a real Runtime;
- exact process-tree ownership and teardown verification;
- no access to the operator's live `~/.brains` or real agent config;
- explicit provider simulation or named real-provider scope;
- exact SHA/artifact identification;
- teardown that removes test containers, volumes, networks, and temporary state.

Source-defined sandbox lifecycle:

```text
docker compose -f sandbox/docker-compose.yml up -d --build
docker compose -f sandbox/docker-compose.yml exec -w /opt/brains sandbox python -m pytest -q -p no:cacheprovider
docker compose -f sandbox/docker-compose.yml down -v
```

These commands were not run by this documentation verification. Browser testing details are in [tests/e2e/README.md](../tests/e2e/README.md), and the acceptance contract is in [QUALITY_GATES.md](QUALITY_GATES.md).

## Rollback

No production deployment or rehearsed deployment rollback is verified.

Source-level rollback building blocks:

- stop or restart the user service;
- stop container stacks;
- restore a validated database archive;
- restore compatible state/config files;
- redeploy a previously accepted artifact through the operator's deployment system.

Required rollback order for any future candidate:

1. Stop new writes and recurring execution.
2. Capture diagnostics and a pre-rollback backup when safe.
3. Restore the application artifact and configuration that match the target schema.
4. Restore data only when schema compatibility requires it. An archive is only
   restorable by a build that ships every migration the archive recorded as
   applied, so roll the application back no further than the build that wrote
   the data you intend to keep.
5. Start on isolated or loopback ingress.
6. verify liveness, readiness, auth, data integrity, and critical journeys.
7. Reopen ingress and observe.

Git checkout, tag selection, container restart, or database restore alone is not a complete rollback.

## Known broken or unverified paths

- dev Docker entrypoint;
- legacy `install/` helpers that search for `brains`;
- Runtime daemon default hub origin;
- box compose/image/state/user/Postgres/ingress alignment;
- native API RBAC and realtime authorization;
- hard execution gate beyond the in-process boundary, and outbound bridge/provider/webhook calls outside the governed-action contract;
- Session message delivery *to a shipped agent CLI*: the queue, authorization, idempotency, leasing, stop ownership and reconciliation are implemented and tested, but no shipped CLI is launched with an open input channel, so a console message to `copilot`, `claude` or `codex` is a durable `unsupported` refusal rather than a delivery;
- managed backup/recovery *execution*: a declared recovery policy (scope/schedule/retention/encryption/RTO/RPO/offsite/drill) and its completeness + compatibility precheck exist (`brains.control.recovery_policy`, BL-P1-09), but Brains runs no backup scheduler itself, no field is pre-populated with a fabricated default, and the E4 isolated backup/restore/rollback drill for a real candidate remains open;
- Postgres schema evolution beyond the baseline: the Postgres baseline is compiled for the Postgres dialect and covered by tests that require `BRAINS_TEST_PG_URL`, and it has not been executed against a live Postgres in this repository's default run;
- live providers, bridges, webhooks, services, UAT, and deployment.

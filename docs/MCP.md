# MCP surface

Current main exposes 89 tools over the Model Context Protocol, all prefixed `brains_`. The
registry is filtered against `CORE_MCP_TOOLS` in `src/brains/capabilities.py` at startup.
Tools outside that allowlist are neither registered nor callable through MCP. Actionable
Session welcome hints recommend supported tools only.

This source contract includes seven local work-assignment tools, seven existing-peer
coordination tools, and `mailbox_wait` available in this branch; it is not a release claim.
The website's pinned 1.5 release retains its 74-tool count.

## Connecting

```text
brains-ai wire --tool claude-code --transport streamable-http
```

Supported harnesses: `claude-code`, `copilot-cli`, `codex`, `opencode`.

Transports: `streamable-http` (default), `stdio`, and `sse` (legacy).

Wiring writes only the managed entry. For JSON clients the file keeps its original
formatting; for Codex the block is sentinel-delimited and the bearer token is referenced
by environment variable rather than written into the file.

### HTTP transport identity

The Streamable HTTP `Mcp-Session-Id` and legacy SSE session identifier address an MCP
transport session; neither is a credential or a durable Brains Session ID. With
authentication enabled, Brains resolves the presented credential and supplies the SDK's
`AuthenticatedUser` identity carrier. The SDK binds the transport to `client_id`
(credential ID, falling back to actor ID) plus `subject` (actor ID). Reuse by a different
authenticated operator is rejected by the SDK before tool dispatch, rather than running
with the identity captured when the transport opened. Missing credentials are refused.

The carrier's `AccessToken.token` is empty: it stores no additional copy of the secret.
The credential is still present in the incoming authentication header; this does not
remove secrets from the request. Existing credentials keep normal same-operator use,
subject to the SDK identity pair. Credential rotation does not guarantee reuse of an
existing transport: a changed credential ID changes that pair. This binding does not
provide per-CLI isolation for Sessions sharing an operator.

These checks apply to authenticated Streamable HTTP and legacy SSE MCP. They do not
establish global token-liveness or revocation guarantees for all native HTTP routes or
already-open streams. The existing `allow_unauthenticated_api` opt-out bypasses MCP auth
middleware, including its Host check; it is not introduced by this fix. Load the updated
package by restarting the serving process; installing it alone does not update a running
process. See [Operations](OPERATIONS.md#authentication-and-authorization).

## Orientation

Call these first. `get_state` is the one-shot orientation: Workspaces, live Sessions,
open work, and recorded knowledge in a single round trip.

| Tool | Purpose |
|---|---|
| `get_state` | Everything an agent needs to know on arrival |
| `search_repo` | Bounded text lookup across the repository. Not semantic |
| `retrieve_original` | Read bounded, authorized stored evidence or a current-file snapshot by reference |
| `generate_views` | Refresh the optional Markdown projections |

### Bounded reference retrieval

`brains_retrieve_original(ref: str)` accepts `chunk:<id>`, `artifact:<id>`, and
`knowledge:<code>`. Its signature is unchanged. A reference addresses a mutable row or
current file, not versioned content or an immutable capture; it does not guarantee
unbounded, lossless retrieval.

For artifacts and chunks, the control checks `Chunk → Artifact → Source → Workspace`
ancestry and current caller visibility before loading content or descriptive metadata,
or accessing the filesystem. Missing and inaccessible references receive the same
refusal; principal or policy lookup failure denies access. Knowledge reads allow
`shared`/`global` entries or entries in a visible Workspace, with bootstrap-admin
visibility handled by the existing policy.

Current-file reads support only `repo_dir` and `docs_dir` Sources. The Source root must
be within the registered Workspace root; a relative Source URI is Workspace-relative.
The artifact path is Source-relative, or absolute only within both roots. Parent
traversal, root escapes, symlinks and Windows reparse points are denied, including in
directory components. `metadata_json.abs_path` grants no authority and is ignored.
Only regular local files are read; remote Sources are not fetched. Missing ancestry is
refused even for bootstrap admin. An existing Source with `workspace_id: null` permits
bootstrap admin to read stored chunk text or an artifact summary, never an unscoped file;
scoped callers are refused. This cooperative local boundary is not a guest security
sandbox. See [Architecture](ARCHITECTURE.md#knowledge-and-reference-evidence) for race limits.

Results retain `ref`, `kind`, `id`, `content`, and `metadata`, plus kind-specific fields:
artifact `title`/`path`, and knowledge `title`/`body` (`body` equals `content`). The
`evidence` object describes what was actually returned:

| Field | Meaning |
|---|---|
| `origin` | `stored_chunk`, `stored_knowledge`, `current_file`, `summary`, or `missing` |
| `freshness` | `current`, `stale`, `expired`, `superseded`, or `unknown`; lifecycle currency is not proof that a finding is true |
| `snapshot` / `immutable_original` | Snapshot is true for stored rows and current files, false for summary/missing; immutable original is always false |
| `original_verified` | True only for complete current-file bytes matching a valid full SHA-256 in `Artifact.hash` at read time |
| `truncated` / `incomplete` | Content was capped / returned evidence is incomplete; a fallback is incomplete even if its summary fits |
| `reason` | Explains the result, for example `hash_match`, `hash_mismatch`, `stored_row`, `content_limit`, or `file_missing` |
| `content_limit_bytes` | 65536 (64 KiB), measured in UTF-8 bytes, without splitting a character |
| `hash_algorithm`, `expected_hash`, `observed_hash`, `hash_scope` | `sha256`, the valid recorded digest or null, computed digest or null, and `full_file` or `not_computed` |

`origin`, `freshness`, `snapshot`, `immutable_original`, `original_verified`, `truncated`,
and `incomplete` are also mirrored in `metadata`. The cap applies separately to stored
body/chunk text, recorded knowledge evidence, current-file content, and summary fallback.

- **Artifact:** a matching full-file hash means `freshness: current`; a mismatch returns
  the current bytes with `freshness: stale`. A missing/invalid hash leaves freshness
  unknown. A capped file is truncated and incomplete, has no observed hash, and cannot
  verify the full file. Hash agreement verifies bytes against a recorded digest, not the
  integrity of the index or the existence of an immutable backup.
- **Chunk:** stored text is mutable, never verified as an original. `Chunk.hash` records
  a captured **file** digest, not a checksum of the chunk text. Freshness is unknown unless
  valid recorded chunk and artifact digests differ, when it is stale. Evidence includes
  `captured_file_hash`, `source_id`, and `workspace_id`; reading a chunk does not read its file.
- **Unavailable file:** deleted, binary/non-UTF-8, unreadable, unsafe or unsupported files
  return a bounded summary, or empty content with `origin: missing` if no summary exists.
  `incomplete` is true and `original_verified` false. A missing file is stale; other such
  fallbacks have unknown freshness. `reason` distinguishes the failure.
- **Knowledge:** evidence includes `provenance`, `confidence`, `recorded_evidence`,
  `recorded_evidence_truncated`, `created_at`, `updated_at`, `valid_until`, and
  `successor_ref`. Metadata retains effective/stored status and expiry/supersession flags.
  Retrieval freshness prioritizes supersession; resolved/rejected history maps to unknown.
  `truncated` describes the body; `incomplete` also includes recorded-evidence truncation.
  The successor reference is null unless that successor is visible to the caller.

For example, selected fields from a small stored-knowledge response (other fields omitted):

```json
{
  "ref": "knowledge:KNOW-0001",
  "kind": "knowledge",
  "content": "Check migration ordering.",
  "evidence": {
    "origin": "stored_knowledge",
    "immutable_original": false,
    "original_verified": false,
    "truncated": false,
    "incomplete": false,
    "content_limit_bytes": 65536
  }
}
```

## Sessions

A Session is a durable coordination handle, not a process. It survives tool restarts.

Starting or reusing a Session does not schedule graph building or embedding, even when
retained prewarm settings are enabled or an embedding model is configured. Existing index
data is preserved. Welcome previews of historical patterns, memory keys and legacy mail
are informational; legacy unread counts are separate from the durable `mailbox_inbox`.
Repository text lookup through `search_repo` requires no index.

| Tool | Purpose |
|---|---|
| `start_session` | Open or reuse a handle in a Workspace |
| `heartbeat_session` | Renew the lease without journal noise |
| `end_session` | Close a handle with a summary |
| `resume_brain_session` | Re-attach and get a full resume packet |
| `link_tool_session` | Bind a tool-side session id to a Brains Session |
| `find_brain_sessions` | Reverse lookup from a tool-side id |
| `list_tool_session_links` | Every tool incarnation that served a Session |
| `link_session_successor` | Chain an ended handle to its replacement |

## Continuity

| Tool | Purpose |
|---|---|
| `checkpoint` | Drop a resume marker at a breakpoint |
| `list_checkpoints` / `latest_checkpoint` | Read them back |
| `set_handoff` / `pick_handoff` | Leave and take the context for stopping and starting |
| `clear_handoff` / `list_handoffs` | Manage them |

## Work

| Tool | Purpose |
|---|---|
| `create_task` | Create durable work with a code and priority |
| `claim_task` | Take exclusive ownership. A second claim is refused |
| `complete_task` / `release_task` | Finish or give back |
| `handoff_task` | Move work to someone else mid-flight |
| `list_tasks` | Filter by status, priority, or tag |
| `claim_workspace` | Take a Workspace for a scope and duration |
| `release_workspace` / `list_workspace_claims` | Release and inspect |

## Local work assignments

These seven core names are registered with the `brains_` prefix (for example,
`brains_work_assignment_create`). They are local state-only CLI/MCP operations, also
included in the lean MCP selection. There is no assignment native HTTP API or browser
surface. They neither spawn processes nor manage checkouts.

| Core tool name | Required arguments | Purpose / optional arguments |
|---|---|---|
| `work_assignment_create` | `workspace_path`, `title`, `spec`, `session_id`, `idempotency_key` | Store an immutable version-1 specification; return a `ready` assignment. `spec` is a JSON object, not a serialized string. |
| `work_assignment_get` | `code`, `session_id` | Read the assignment and complete attempt history. |
| `work_assignment_list` | `workspace_path`, `session_id` | Read newest-created assignments first; `limit=50`, valid range 1–200. |
| `work_assignment_accept` | `code`, `session_id`, `expected_revision` | Accept ready work as this existing Session; record its actual tool and a new attempt generation. |
| `work_assignment_settle` | `code`, `attempt_id`, `outcome`, `evidence`, `session_id`, `expected_revision` | Report `completed`, `failed`, `cancelled`, or `uncertain`; optional `result=""`. Only the accepting Session may report its current attempt. |
| `work_assignment_cancel` | `code`, `session_id`, `expected_revision` | Cancel ready work or request cancellation of unresolved work; no process stop. |
| `work_assignment_retry` | `code`, `session_id`, `expected_revision` | Explicitly return conclusively failed/cancelled work to ready; preserve attempts. |

All seven require a live Session owned by the authenticated operator and a currently
visible Workspace. Assignments are scoped to that operator and the supplied Session's
Workspace; create/list accept only its registered path or a recorded alias. Anonymous,
Runtime, and foreign-operator callers are refused. Historical ownerless Sessions are
limited to bootstrap admin. A Session ID is not a credential or a per-harness isolation
boundary between Sessions sharing an operator.

### Specification and creation identity

Minimal `spec`:

```json
{"objective": "Review the fixture"}
```

`title` is separately required, nonblank text up to 256 UTF-8 bytes. The nonblank
`idempotency_key` is at most 128 bytes. These and specification text reject NUL bytes.

| Specification field | Contract |
|---|---|
| `version` | Optional integer, defaults to 1; only 1 is accepted. |
| `objective` | Required nonblank string. |
| `context` | Optional string. |
| `deadline` | Optional timezone-aware ISO datetime, normalized to UTC. Omit when unused; null is invalid. |
| `max_runtime_seconds` | Optional integer from 1 to 604800; default acceptance budget is 3600 seconds. Booleans are invalid. |
| `checkout_ref` | Optional string up to 2048 UTF-8 bytes; inert reference, not filesystem authority. |
| `links` | Optional list of at most 32 nonblank strings, each up to 2048 UTF-8 bytes; not fetched. |
| `tool` | Optional string up to 64 UTF-8 bytes; advisory, not harness selection. |

Unknown fields are rejected. The complete canonical JSON specification, including
defaulted `version`, must fit 32768 UTF-8 bytes; `objective` and `context` individually
have that same upper bound. Storage retains `specification`, `specification_hash`, and
`request_hash` in the response. There is no specification-edit operation. Creation keys
are unique per Workspace/operator, not per Session: an identical canonical specification
and title returns the existing assignment, including after creator Session replacement;
a different request under the same key is refused.

### State, observations, and revision fences

Responses include `code`, creator provenance, immutable specification and hashes,
`status`, `revision`, `generation`, `observed_status`, `deadline_exceeded`, timestamps,
cancellation attribution, `current_attempt_id`, and ordered `attempts`. A new assignment
has status `ready`, revision 1, generation 0, and no attempt. Each acceptance increments
generation and creates a distinct attempt ID.

Attempts expose `source_session_id`, actual `tool`, stored/observed status,
`deadline_exceeded`, `source_session_unavailable`, acceptance/deadline/report/settlement
timestamps, runtime budget, cancellation time, `evidence`, `result`, and `usage`.
`usage: null` means unknown; these tools do not collect or accept usage measurements.

The attempt deadline is acceptance time plus its runtime budget, shortened by any earlier
specification deadline. An already-passed deadline refuses acceptance. Expiry or source
Session unavailability makes an unresolved attempt's `observed_status` uncertain without
changing stored status or revision. Get/list do not settle work or renew Session leases.
The budget is cooperative, not OS-enforced, and expiry never implies process termination.
For this observation, a source is unavailable if missing, ended, in `completed`, `failed`,
`cancelled`, or `dormant` Session state, or past an existing lease's expiry. The absence
of a lease row alone is not treated as source unavailability.

Accept/settle/cancel/retry require an integer `expected_revision` matching current state.
Stale revisions always fail, including lost-response replays; read back first. At the
current revision, same-Session acceptance of already accepted work and an identical
reported outcome/evidence/result are no-ops. Creation replay also adds no event or lease
renewal. Reattachment to the same live accepting Session preserves attempt authority;
replacement/successor Sessions do not inherit it.

Settlement requires nonblank `evidence`; it and optional `result` each fit 65536 UTF-8
bytes and reject NUL. Reports are attributed evidence, not independent verification or
requester approval. `completed`, `failed`, and `cancelled` set `settled_at`. A reported
`uncertain` outcome sets `reported_at` but leaves `settled_at` null and remains unresolved;
the tools cannot revise or reconcile that report. Read-time uncertainty alone still
allows the original live Session to report an outcome.

Cancellation of `ready` work is immediately `cancelled` with no attempt. Accepted work
becomes `cancel_requested`; its accepting Session must report the actual outcome with
evidence. A winning cancellation request refuses subsequent `completed` reports, while
`failed`, `cancelled`, or `uncertain` can be reported. Cancellation of reported uncertain
work records the request but retains `uncertain`. Neither request proves a process stopped.

Retry requires stored `failed` or `cancelled` state and all existing attempts conclusively
settled. It returns work to `ready`; only a later explicit acceptance creates another
attempt. Active, cancellation-pending, completed, and uncertain work are blocked. There
are no implicit retries, takeover, checkout ownership, or remote execution guarantees.
See the [guide](GUIDE.md#local-work-assignments) for the CLI journey.

## Existing-peer coordination

These seven core/lean tools use the `brains_` prefix. CLI/MCP expose local protocol state
only, with no native HTTP API, frontend, worker launch, or readiness guarantee. This is
partial local scope for [#38](https://github.com/xibodev/brains-ai/issues/38), not delivery
of worker panels or multi-day checkout management. Assignment state remains the separate
local foundation of [#36](https://github.com/xibodev/brains-ai/issues/36).

| Core tool name | Required arguments | Purpose / optional arguments |
|---|---|---|
| `coordination_propose` | `workspace_path`, `title`, `spec`, `session_id`, `idempotency_key` | Create a proposal; optional `code` plus `expected_revision` replaces its open latest version as the original requester. |
| `coordination_get` | `code`, `session_id` | Read latest state; optional `version` reads history. |
| `coordination_list` | `workspace_path`, `session_id` | List member-visible latest versions, newest-created first; `limit=50`, range 1–200. |
| `coordination_accept` | `code`, `session_id`, `version`, `expected_revision`, `spec_hash` | Acknowledge the exact stored specification. |
| `coordination_advance` | `code`, `session_id`, `version`, `expected_revision` | Requester explicitly starts collection or closes a complete round. |
| `coordination_submit` | `code`, `kind`, `payload`, `session_id`, `version`, `expected_revision`, `idempotency_key` | Append an `initial`, `discussion`, or `final` report. |
| `coordination_cancel` | `code`, `reason`, `session_id`, `version`, `expected_revision` | Requester cancels protocol state, including after expiry. |

Every call requires a live Session owned by the authenticated operator and current
Workspace visibility. Existing-proposal access requires membership (requester,
participant, or result owner); list filters out proposals the caller is not a member of. Proposed
participants must already be live Sessions in that same Workspace under that operator.
Create/list require its registered path or recorded alias. Anonymous, Runtime,
foreign-operator, ownerless, and ended Sessions are refused; bootstrap admin
does not bypass Session ownership. Unknown and inaccessible proposals share a refusal.
This does not prevent a shared operator from acting as another Session they own or reading
the local database. Session acknowledgement is not human approval or proof of an
independent reviewer. Stored tools come from Sessions; model labels are unverified
declarations, not model selection, routing, or verified diversity.

### Proposal specification

`spec` and `payload` are JSON objects in MCP, serialized JSON in CLI `--spec`/`--payload`.
Required specification fields are `objective`, `context`, `evidence_expectations`, and
`participants`. Context and evidence expectations may be empty; `objective` must be nonblank.

| Field | Contract |
|---|---|
| `version` | Optional schema version, integer 1 only; distinct from the proposal's incrementing response `version`. |
| `participants` | 2–8 unique existing Sessions; each input object has exactly `session_id` and `model`. Model is null or nonblank text up to 128 UTF-8 bytes. Do not supply `tool`. |
| `discussion_rounds` | Optional integer 0–3, default 1; booleans are invalid. |
| `result_owner_session_id` | Optional, defaults to requester; must be requester or a participant. |
| `deadline` | Optional timezone-aware ISO datetime, future and at most 30 days away. Omission freezes creation time plus one hour; null is invalid. |
| `links` | Optional list of at most 32 nonblank strings, each up to 2048 UTF-8 bytes. |

Unknown fields and NUL text are refused. Title is required, nonblank, at most 256 UTF-8
bytes; creation/contribution keys are nonblank, at most 128 bytes. Session IDs are at most
32 ASCII letters, digits, underscores or hyphens; Workspace paths fit 1024 UTF-8 bytes.
Proposal codes fit 39 bytes. Mutation `version` is an integer 1–2147483647 and
`expected_revision` is 0–9223372036854775806; booleans are not integers for these checks.
Coordination MCP registration uses strict integer validation, including optional `version`
and list `limit`, so JSON `true` cannot be coerced to revision/version `1` before dispatch.
This validation and the transport identity fix preserve public tool signatures and wire
schemas; the separate addition of `mailbox_wait` brings current main to 89 tools.
Objective, context, and evidence expectations each fit 32768 bytes, and the **whole stored
canonical specification**, including defaults, deadline and recorded tools, must fit that
same 32 KiB cap. Canonicalization sorts participants by Session ID and JSON keys, uses
compact UTF-8 JSON, and normalizes deadline to UTC. `spec_hash` is its SHA-256; accept the
returned hash rather than hashing the input yourself. Context and links are inert: they
are never fetched and do not generate assignments or integrate with assignment state.

Creation returns a `PC-<uuid>` code at proposal version 1, revision 0, status `planned`,
round 0. It automatically records the requester's acceptance of that version's stored
hash. Required acknowledgements are the union of requester, participants and result owner;
every other member must accept before status becomes `accepted`.

### Phases, reports, and visibility

| Stored phase | Next explicit operation |
|---|---|
| `planned` | Remaining members accept the exact hash/version/current revision. |
| `accepted` | Requester advances to `collecting`; all members must still be live. |
| `collecting` | Each participant submits one initial report. Only after all initials exist may requester advance, setting `initial_closed: true`. |
| `discussing` | For 1–3 configured rounds, each participant submits once per round, then requester advances. Zero rounds proceeds directly to final-ready at round 0. |
| `discussing`, `final_ready: true` | Only result owner submits `final`, setting `completed`. With discussion rounds, final-ready round is configured rounds plus one. |

Initial/discussion payloads require string fields `findings`, `evidence`, `uncertainty`,
and `dissent`; optional `clarifications` uses the same list bounds as `links`. Final
requires exactly `summary` and `evidence` strings. Evidence must be nonblank; other
required strings may be empty. Each string and the complete canonical payload fit 65536
UTF-8 bytes; NUL and extra fields are refused. Reports are append-only, with one initial
and one contribution per participant per discussion round, and one final per version.
Evidence is attributed text, not independently verified proof.

**Every protocol API response**, including mutations, replay, get and list, applies the
same blinding filter. Until explicit initial closure, a member sees only their own
initial report; requester and result owner have no exemption. Counts and remaining-Session
IDs reveal progress, not other authors' payloads. All initials arriving alone does not
unblind them. Cancellation or replacement before closure also leaves history blinded.
After closure, members see all reports. All nonblank original initial/discussion dissent
is mechanically retained in `unresolved_dissent` with `resolved: false`, even after final
synthesis. There is no dissent-resolution operation; synthesis cannot erase disagreement.

### Snapshot and retry contract

All operations return the complete member-filtered snapshot; list returns an array of
these snapshots. Fields include code/version/revision, Workspace and creator provenance,
result owner, title, `specification`, `spec_hash`, status/round, timestamps, deadline,
`initial_closed`, `final_ready`, `blinded`, `expired_flag`, `incomplete_flag`, and
`cancellation_reason`. Acceptance helpers are `required_acceptance_session_ids`,
`accepted_session_ids`, and `remaining_acceptance_session_ids`; collection helpers are
`remaining_initial_session_ids` and `remaining_discussion_session_ids`.

`counts` contains `participants`, `required_acceptances`, `acceptances`, `initial`,
`discussion` (current round), `contributions` (all non-acceptance reports), and
`visible_contributions` (after filtering). `contributions` carries contribution ID,
author Session, kind, round, payload, and creation time. Acceptance rows are not report
entries. `final` is the final payload or null; `unresolved_dissent` retains contribution
ID, author, round, original dissent text and `resolved: false`.
`incomplete_flag` is false only for `completed`; cancellation remains incomplete.
`expired_flag` applies only to open phases, so cancellation clears the observed expiry
flag without changing the deadline. `remaining_discussion_session_ids` is empty outside
an active discussion round; `final_ready` becomes false after completion.

Mutations require the exact latest proposal version and revision. Revisions are monotonic
across **one code's versions**, not reset on replacement or shared across different codes.
To change scope, requester re-proposes with `code`, current `expected_revision`, a new key,
and a complete input spec. In one transaction, old version is cancelled at revision r+1
and new version created at r+2. All other acknowledgements and reports must be submitted
anew; only requester acceptance is automatic. Completed, cancelled, or expired proposals
cannot be replaced. History remains readable with get's `version`; completed reports are
not editable. The stored spec includes `tool` on participants: rebuild the input using
only `session_id` and `model`, rather than resubmitting that output unchanged.

Creation keys are unique per Workspace/operator. An identical creation retry returns the
member-filtered latest version without extending its deadline; a changed request under
that key fails. Contribution keys are per code/version/author. Stale fences fail even for
known retries: get current state first. Identical acceptance, contribution or cancellation
retries at the current fence are no-ops where permitted; deadline checks still apply to
acceptance and submission, including final replay. Reads and no-ops neither renew leases
nor emit events.

Expiry of open state sets `expired_flag: true`, `incomplete_flag: true`, and
`final_ready: false` on reads; stored status/revision do not change. Accept, advance,
submit and replacement refuse expired work. Requester may still explicitly cancel it.
Cancellation records `cancelled` with a required nonblank reason (up to 32768 UTF-8 bytes).
It does not cancel work assignments, stop processes, or send/cancel mail. There is no
automatic execution, round advance, deadline settlement, or generated assignment.

### Minimal MCP sequence

Use the [CLI walkthrough](GUIDE.md#existing-peer-deliberation) for the same lifecycle.
In this call notation, `R`, `A`, `B`, and `W` are existing live Session IDs and their
registered Workspace path; `s` is the latest returned snapshot. Replace the synthetic
`ses_peer_a`/`ses_peer_b` strings in `spec` with A/B. `report` contains every required
initial field; replace its synthetic content for each peer's own report.

```text
spec = {"objective":"Review fixture","context":"Synthetic fixture snapshot","evidence_expectations":"Cite fixture lines","participants":[{"session_id":"ses_peer_a","model":null},{"session_id":"ses_peer_b","model":null}],"discussion_rounds":0}
report = {"findings":"Fixture reviewed","evidence":"fixture.txt:1","uncertainty":"Runtime not checked","dissent":""}
s = brains_coordination_propose(workspace_path=W, title="Fixture review", spec=spec, session_id=R, idempotency_key="fixture-1")
s = brains_coordination_get(code=s.code, session_id=A)
s = brains_coordination_accept(code=s.code, session_id=A, version=s.version, expected_revision=s.revision, spec_hash=s.spec_hash)
```

Repeat get/accept as B. Requester then starts collection; each peer submits their report
with a fresh read as that peer before writing:

```text
s = brains_coordination_advance(code=s.code, session_id=R, version=s.version, expected_revision=s.revision)
s = brains_coordination_get(code=s.code, session_id=A)
s = brains_coordination_submit(code=s.code, kind="initial", payload=report, session_id=A, version=s.version, expected_revision=s.revision, idempotency_key="initial")
```

Repeat get/submit as B. Requester closes initial collection, then, as default result owner,
submits final synthesis. Read back first if another call may have changed the revision.

```text
s = brains_coordination_advance(code=s.code, session_id=R, version=s.version, expected_revision=s.revision)
s = brains_coordination_submit(code=s.code, kind="final", payload={"summary":"Review complete; retain peer dissent","evidence":"fixture.txt:1"}, session_id=R, version=s.version, expected_revision=s.revision, idempotency_key="final")
```

## Communication

`inbox_wait` waits for claimable peer-help requests. For unread durable mailbox messages,
use `mailbox_wait` or read through `mailbox_inbox`. Adapter notifications remain a
separate best-effort wakeup path.

| Tool | Purpose |
|---|---|
| `inbox_wait` | Wait for claimable peer help or timeout |
| `mailbox_register` | Register or reattach a Session's mailbox |
| `mailbox_send` / `mailbox_reply` / `mailbox_forward` | Send durable mail |
| `mailbox_broadcast` | Send to a Workspace |
| `mailbox_inbox` / `mailbox_sent` / `mailbox_thread` | Read |
| `mailbox_wait` | Proof-bound wait for unread durable deliveries; no read marking or work acceptance |
| `mailbox_phonebook` / `mailbox_lookup` | Discover addresses |
| `mailbox_notification_take` / `mailbox_notification_settle` | Claim and settle a wake |
| `mailbox_native_id` / `mailbox_binding_reconcile` | Identity and rebinding |
| `mailbox_managed_create` / `mailbox_managed_rotate` / `mailbox_managed_recover` / `mailbox_managed_revoke` | Managed binding lifecycle |

The durable store is authoritative. A live wake is best effort and never loses mail.

### Waiting for durable mail

`brains_mailbox_wait(session_id, binding_file, address=None, timeout_ms=25000,
after_delivery_id=None, limit=50)` is a core/lean tool. It requires the current live agent
Session attachment and adapter-held binding-file proof, revalidated on every poll.
An address alone does not grant access; stale, revoked, detached, or invalid proof fails
closed. The binding file is read under the same rules as `mailbox_inbox`.

`timeout_ms` is an integer from 0 to 25000 inclusive; zero performs one immediate poll.
The budget bounds polling, not database work, so it is not a hard 25-second wall-clock
deadline. Transactions close before sleeping. `limit` is an integer from 1 to 200 and
bounds returned messages, not the authorization scan.

MCP registration uses `StrictInt` for `timeout_ms`, `limit`, and the optional
`after_delivery_id`: JSON booleans are rejected rather than coerced to integers.
The registered MCP handler runs the blocking poll through AnyIO's shared worker-thread
facility, preserving request-local authority and allowing concurrent sends on the event
loop. It uses AnyIO's default capacity limiter, not a per-request pool or unlimited
threads. With `abandon_on_cancel=False`, cancellation waits for the worker's bounded poll
to finish; database work and waiting for worker capacity can extend elapsed time beyond
25 seconds. Client cancellation is not evidence that a job was cancelled. Direct CLI
and core calls block their caller through the same bounded polling loop.

The response retains the inbox envelope (`mailbox`, `cursor`, `unread_count`, `messages`)
and adds:

| Field | Meaning |
|---|---|
| `mail_available` | Whether this response contains unread messages. |
| `wait_timed_out` | True when the poll budget ended without returned messages. |
| `next_after_delivery_id` | Greatest returned `messages[].inbox_delivery.cursor` delivery ID; if empty, the supplied floor or zero. Pass as the next call's `after_delivery_id`. |

The continuation is a delivery-ID floor, not a message ID or an advanced attachment
cursor. The top-level `cursor` stays unchanged. Without a floor, repeated waits can
return the same unread messages; an empty result does not prove the mailbox has no
unread mail below an explicit floor. Waiting never marks read, advances stored cursors,
renews leases, settles notifications, claims/cancels help, or accepts work. Explicit
inbox read marking remains separate. No new HTTP route or browser control is added.

## Peer help

Ask another agent rather than guessing. Answers require evidence.

| Tool | Purpose |
|---|---|
| `file_help_request` | File and return immediately with a code |
| `wait_help_request` | Wait briefly; a timeout leaves it open |
| `claim_help_request` | Accept exactly the supplied code without waiting or claiming another request |
| `wait_for_request` | Block until work is routed to you, then claim it |
| `answer_request` | Answer. Evidence is required |
| `get_help_request` / `list_open_help_requests` | Read |
| `release_help_request` / `cancel_help_request` | Give back or withdraw |

`claim_help_request(code, session_id)` returns the claimed request. Repeating it as the
same live owner returns the existing claim without renewing its deadline or recording
another acceptance event. Unknown, ineligible, expired or terminal codes are refused;
there is no fallback to another queued request. `wait_for_request` retains oldest-eligible
queue claiming, including its optional Workspace-slug override.

Claim eligibility uses the stored Session's harness and current caller's Workspace
visibility. Targets match Session **or** Workspace: supplying both broadens matching,
not pins it to that Session. Use only `to_session_id` for one specific peer, or only
`to_workspace` with a Workspace slug for any eligible peer there.

The peer-help lifecycle tools listed above check ownership when a Session is supplied:
the authenticated operator must own that Session and see its Workspace before liveness
is renewed. The separate `inbox_wait` notification wait checks liveness, not Session
ownership; it does not accept work. For the lifecycle checks, anonymous and Runtime
identities are refused. Only bootstrap admin
may use historical Sessions with no recorded owner; a known different owner is refused
even for bootstrap admin. Request visibility is checked separately. This is not a
per-CLI credential boundary between Sessions owned by the same operator.

Filing `timeout_ms` sets request lifetime (default 30 seconds); waiting `timeout_ms` only
bounds that wait. A claimed request uses claim grace (default 600 seconds), not its
original open deadline. Reading, notification settlement and duplicate claiming do not
renew it. Release clears ownership and starts a new open lifetime. Defaults are unchanged.

Claim, release, cancellation and answer use conditional state transitions; an in-flight
answer cannot overwrite a winning cancellation, expiry or changed claim snapshot. There
is no client-supplied claim-generation token: a new answer submission after the same
Session releases and reclaims is evaluated against its current claim. Do not reuse a
request for changed scope; cancel and file a replacement instead.

Acceptance commits a peer to attempt the recorded scope, not to surrender control of its
CLI. Mail delivery, reading, work acceptance and requester approval of a result are
distinct. Discuss counterproposals, decline or defer in durable mail; no new negotiation
states or automatic worker launch are implied. Evidence is mandatory, not proof of quality.

## Knowledge

| Tool | Purpose |
|---|---|
| `knowledge_add` | Record a finding with type, scope, and confidence |
| `knowledge_search` | Find it before re-deriving it |
| `knowledge_resolve` | Transition to active, confirmed, resolved, rejected, or stale |

`knowledge_search(query, type, status, workspace_path, tags, limit=50)` clamps `limit`
to 1–100. No status filter retains visible history, ordered by importance, then newest
creation time and ID. Explicit `status` filters use effective lifecycle **before** the
limit: `superseded` wins when a successor is recorded or stored status is superseded;
otherwise an active/confirmed entry past `valid_until` is `stale`. Other stored statuses
are retained. Naive timestamps represent UTC; expiry means strictly earlier than now.
Reading does not update knowledge status, timestamps, body or evidence, and correct
filtering does not depend on running the expiry sweeper.

Entry responses expose `status` (effective), `stored_status`, `freshness`, `expired`,
and `superseded`. Search freshness can be `historical` for resolved/rejected rows; an
expired stored active/confirmed row has `freshness: expired` even if also linked to a
successor, while its effective status is superseded. Use the explicit lifecycle flags.
`superseded_by_id` is null for an inaccessible successor; `superseded` remains true.

The shared entry serializer used by add/search caps `body` and `evidence` independently
at 64 KiB UTF-8. `content_bytes` and `evidence_bytes` describe the full stored fields;
`content_limit_bytes` is 65536. `body_truncated` and `evidence_truncated` identify each
cap, and `truncated` is their aggregate. A truncated entry includes a knowledge `ref`.
With context compression enabled, search additionally limits the body to 200 characters,
recomputes body/aggregate truncation, and always includes `ref` and `compressed: true`.
Retrieval via that reference still has the byte cap and reads the current stored row.

`knowledge_add(..., supersedes_code=...)` creates the successor and links the predecessor
in one transaction. A second successor is refused and the attempted new entry rolled
back; it never overwrites the chain. `knowledge_resolve` does not accept `superseded`
as a target status and refuses active/confirmed reactivation of superseded entries.
Lifecycle updates use conditional compare-and-swap checks so a competing status or link
change is refused rather than silently overwritten. Referenced entries must be visible;
hidden and missing references receive the same refusal. Retaining a historical row does
not make its content immutable or versioned.

## Human decisions

| Tool | Purpose |
|---|---|
| `file_decision_request` | Ask a human and keep working |
| `resolve_decision` | Answer. The Session that filed it may not resolve it |
| `route_decision` / `escalate_decision` | Assign or raise |
| `list_open_decisions` | What is waiting |

`file_decision_request(workspace_path, title, body="", proposed_answer=None,
session_id=None)` files a durable ASK. The owner may enable one courtesy email per newly
filed ASK with the default-off, environment-only
`BRAINS_ASK_EMAIL_NOTIFICATIONS_ENABLED` flag and separately configured owner recipient
and SMTP settings. YAML and admin mutation cannot enable consent. Existing SMTP setup
alone does not send after upgrade. Standing notification consent is not approval of the
requested action; no extra per-notification decision is filed.

There is no recipient argument: email goes only to the configured owner and includes
the validated ASK code and a whitespace-normalized title preview plus the server-chosen
`http://127.0.0.1:8787/app` link. The preview is capped at 200 Unicode scalars including
the ellipsis (at most 800 UTF-8 bytes). CR or LF anywhere in the original title rejects
the email before SMTP; the stored ASK title/body are unchanged. Notification codes must
match `ASK-[A-Za-z0-9]{1,28}` (at most 32 ASCII characters); normal generated ASK codes
remain unchanged. Body, proposed answer, Workspace and Session context are excluded.
The bounded preview is still user-supplied and can disclose confidential information.
The fixed loopback link opens the recipient's computer, for the same owner's local-app
host; it is neither public access to the sender's computer nor a credential-bearing
login link. The sender currently hardcodes port 8787 and `/app`; custom gateway ports
are not reflected in this link. The opt-in is loaded from local server/process settings
at startup, with no API or agent tool to enable it or supply an arbitrary recipient.

The filing wrapper returns `code`, `status: open`, `workspace`, and an allowlisted
`notification` object after calling `notify_ask` following commit. For example, with
notifications disabled (illustrative generated code and Workspace slug):

```json
{"code":"ASK-0001","status":"open","workspace":"example","notification":{"status":"disabled","attempted":false,"sent":false}}
```

The notification status allowlist is `disabled`, `attempted`, `failed`, `uncertain`,
and `smtp_accepted`; the normal helper completes with a terminal status, while
`attempted` is its pre-SMTP ledger state. `attempted`/`sent` are booleans for valid
helper outcomes. Optional fields are bounded `error`/`audit_error` markers and boolean
`title_truncated`; the helper emits `title_truncated: true` only when it caps the
normalized preview. Its absence does not prove the title was processed, for example
when notifications are disabled or content is rejected first.

An unexpected hook exception or malformed outcome returns
`{"status":"uncertain","attempted":null,"sent":false,"error":"notification_failed"}`
inside `notification`. Here `attempted: null` means unknown, not no attempt.
`status: open` describes the ASK, not email success. `smtp_accepted` is a status value,
not an additional boolean; `sent: true` means observed SMTP acceptance only, never
recipient delivery or reading. An interrupted send can be uncertain rather than failed,
and no hidden retry follows.
The local `decision_email_notification` ledger records attempted/outcome states when
available. See [Operations](OPERATIONS.md#optional-ask-email-notifications) for exact
markers, setup, disabling, and the historical-outbox boundary.

## Evidence

| Tool | Purpose |
|---|---|
| `append_event` | Record meaningful work in the ledger |
| `event_context` / `event_scope_report` | Categorise and audit scope |
| `list_signals` | Advisory signals for a Workspace |
| `audit_list` | Signed, hash-chained entries |
| `audit_verify` | Recompute the chain and report the first divergence |
| `governed_action_list` | The decision behind every outward effect |

## State

| Tool | Purpose |
|---|---|
| `backup_create` | Online backup to an archive with a manifest |
| `backup_inspect` | Read a manifest without restoring |
| `backup_restore` | Destructive restore. Records its attempt first |

## What is not here

There are no MCP tools for model routing, semantic retrieval, code graphs, runtime
execution, or chat bridges. Those are outside the current supported surface — see the
[product brief](product/PRODUCT_BRIEF.md).

`mail_send` remains withdrawn: optional configured-owner ASK notification is not a public
arbitrary-recipient email tool.

Calling a tool that is not on the allowlist fails closed. It is not hidden behind a flag.

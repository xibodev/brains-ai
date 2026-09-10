# MCP surface

Brains exposes 74 tools over the Model Context Protocol, all prefixed `brains_`. The
registry is filtered against `CORE_MCP_TOOLS` in `src/brains/capabilities.py` at startup.
Tools outside that allowlist are neither registered nor callable through MCP. Actionable
Session welcome hints recommend supported tools only.

## Connecting

```text
brains-ai wire --tool claude-code --transport streamable-http
```

Supported harnesses: `claude-code`, `copilot-cli`, `codex`, `opencode`.

Transports: `streamable-http` (default), `stdio`, and `sse` (legacy).

Wiring writes only the managed entry. For JSON clients the file keeps its original
formatting; for Codex the block is sentinel-delimited and the bearer token is referenced
by environment variable rather than written into the file.

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

## Communication

`inbox_wait` waits for claimable peer-help requests. It does not wait for durable mailbox
messages; read those through `mailbox_inbox` and the adapter's supported notification path.

| Tool | Purpose |
|---|---|
| `inbox_wait` | Wait for claimable peer help or timeout |
| `mailbox_register` | Register or reattach a Session's mailbox |
| `mailbox_send` / `mailbox_reply` / `mailbox_forward` | Send durable mail |
| `mailbox_broadcast` | Send to a Workspace |
| `mailbox_inbox` / `mailbox_sent` / `mailbox_thread` | Read |
| `mailbox_phonebook` / `mailbox_lookup` | Discover addresses |
| `mailbox_notification_take` / `mailbox_notification_settle` | Claim and settle a wake |
| `mailbox_native_id` / `mailbox_binding_reconcile` | Identity and rebinding |
| `mailbox_managed_create` / `mailbox_managed_rotate` / `mailbox_managed_recover` / `mailbox_managed_revoke` | Managed binding lifecycle |

The durable store is authoritative. A live wake is best effort and never loses mail.

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

Calling a tool that is not on the allowlist fails closed. It is not hidden behind a flag.

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
| `retrieve_original` | Fetch a stored original by reference |
| `generate_views` | Refresh the optional Markdown projections |

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
| `knowledge_resolve` | Mark it resolved or superseded |

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

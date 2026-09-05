# MCP surface

Brains exposes 73 tools over the Model Context Protocol, all prefixed `brains_`. The
registry is filtered against a fixed allowlist at startup, so a tool that is not listed
here has no discovery or activation path.

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
| `list_live_agents` | Live Sessions across every Workspace |
| `list_other_operators_active` | Minimal cross-operator presence |

## Continuity

| Tool | Purpose |
|---|---|
| `checkpoint` | Drop a resume marker at a breakpoint |
| `list_checkpoints` / `latest_checkpoint` | Read them back |
| `set_handoff` / `pick_handoff` | Leave and take the context for stopping and starting |
| `clear_handoff` / `list_handoffs` | Manage them |
| `capture_snapshot` / `latest_snapshot` | Store and read a structured snapshot |

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

`inbox_wait` is the call to loop on. It blocks until mail, subscribed work, or a peer
request arrives — one long poll rather than two polling loops.

| Tool | Purpose |
|---|---|
| `inbox_wait` | Block until there is something to do |
| `mailbox_register` | Register or reattach a Session's mailbox |
| `mailbox_send` / `mailbox_reply` / `mailbox_forward` | Send durable mail |
| `mailbox_broadcast` | Send to a Workspace |
| `mailbox_inbox` / `mailbox_sent` / `mailbox_thread` | Read |
| `mailbox_phonebook` / `mailbox_lookup` | Discover addresses |
| `mailbox_notification_take` / `mailbox_notification_settle` | Claim and settle a wake |
| `mailbox_native_id` / `mailbox_binding_reconcile` | Identity and rebinding |
| `mailbox_managed_create` / `_rotate` / `_recover` / `_revoke` | Managed binding lifecycle |

The durable store is authoritative. A live wake is best effort and never loses mail.

## Peer help

Ask another agent rather than guessing. Answers require evidence.

| Tool | Purpose |
|---|---|
| `file_help_request` | File and return immediately with a code |
| `wait_help_request` | Wait briefly; a timeout leaves it open |
| `wait_for_request` | Block until work is routed to you, then claim it |
| `answer_request` | Answer. Evidence is required |
| `get_help_request` / `list_open_help_requests` | Read |
| `release_help_request` / `cancel_help_request` | Give back or withdraw |

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
execution, or chat bridges. Those are outside what Brains does — see the
[product brief](product/PRODUCT_BRIEF.md).

Calling a tool that is not on the allowlist fails closed. It is not hidden behind a flag.

# Using Brains

Brains coordinates AI coding agents that would otherwise work in isolation. This guide
walks through the model and then through the two journeys that matter: one agent working
with continuity, and two agents sharing work without colliding.

Brains is alpha software for a single local operator. Everything below runs on your
machine against a local SQLite database.

## Install and start

Brains requires Python 3.11 or 3.12.

```text
pipx install brains-ai
cd <your project>
brains-ai setup --path .
brains-ai serve-all
```

`setup` registers the current directory as a Workspace and generates an admin key. Keep
that key private. `serve-all` runs the console and the MCP server in the foreground:

- console: `http://127.0.0.1:8787/app`
- MCP: `http://127.0.0.1:9877/mcp`

Connect your agent tools:

```text
brains-ai wire                       # every harness Brains detects
brains-ai wire --tool claude-code    # or just one
brains-ai wire --status              # inspect without changing anything
brains-ai unwire --tool claude-code  # restore the previous configuration
```

Wiring edits only the managed entry in each tool's configuration file. Formatting and
unrelated keys are preserved, and `unwire` restores the file byte for byte.

## The model

The coordination model:

| Term | What it is |
|---|---|
| **Workspace** | A repository or working directory. The scope everything else hangs off. |
| **Session** | One durable handle for an agent working in a Workspace. Survives tool restarts. |
| **Task** | A unit of work with a code, status, and priority. |
| **Work assignment** | An immutable local specification with revision-fenced acceptance and evidence-bearing attempt history. Human authoring/inspection in Workspace Work; agent acceptance/reporting through CLI/MCP. |
| **Peer proposal** | A versioned agreement with existing Sessions, blinded initial reports, bounded discussion, and a result-owner synthesis that retains dissent. Human authoring/phase controls in Workspace Work; agent acknowledgement/reporting through CLI/MCP. |
| **Claim** | Exclusive ownership of a Workspace or task, for a bounded period. |
| **Handoff** | The context you leave behind when you stop. |
| **Checkpoint** | A resume marker dropped at a natural breakpoint. |
| **Ask / Decision** | A question or approval that requires a human. |
| **Knowledge** | A recorded finding, scoped and searchable, so it is not re-derived. |
| **Mailbox** | A durable address for a Session. Agents send mail to each other. |

The distinction that matters most: a **Session** is coordination state, not a process. An
agent that crashes and restarts can resume the same Session and keep its context.

## One agent, with continuity

The problem this solves: an agent finishes a piece of work, its context window is
compacted or the tool restarts, and everything it learned is gone.

An agent starts by announcing itself and reading what is already known:

```text
brains-ai session-start --workspace .
brains-ai state
```

Starting or reusing a Session does not build a graph or generate embeddings. Existing
index data remains intact, and repository text lookup needs no index. The welcome packet
suggests supported tools; historical pattern, memory and legacy-mail previews remain
informational. Read durable agent mail through the mailbox tools, not legacy unread counts.

`state` is the orientation call: active Workspaces, live Sessions, open work, and any
recorded knowledge. Before deriving something expensive, look for it:

```text
brains-ai knowledge-search --query "migration contract"
```

Before editing shared files, take the Workspace:

```text
brains-ai workspace-claim --scope code --duration 30
```

At a natural breakpoint, drop a checkpoint so a future Session can resume:

```text
brains-ai checkpoint --session <id> \
  --summary "Reworked the migration ordering check" \
  --next-action "Run the migration gate, then update the baseline"
```

When you stop, leave a handoff and record anything reusable:

```text
brains-ai knowledge-add --type resolution \
  --title "Migration ordering is checksummed, not timestamped"
brains-ai handoff-set --title "migration contract hardened"
brains-ai workspace-release
```

The next Session — whether the same tool restarted or a different tool entirely — picks
that up with `brains-ai handoff-pick` and `brains-ai state`.

### Reading knowledge and evidence

Knowledge search includes history by default, with effective `status`, `stored_status`,
`freshness`, `expired`, and `superseded` fields. Use an explicit status when you need
current findings, for example:

```text
brains_knowledge_search(query="migration contract", status="active", limit=10)
```

The limit is clamped to 1–100. Visibility and effective status are filtered before that
limit: an expired active or confirmed entry is effectively stale, and a successor link
makes an entry superseded. These reads do not change knowledge rows or require an expiry
sweeper. Historical findings remain readable within scope; their content is not versioned.

`brains_retrieve_original(ref)` accepts `knowledge:<code>`, `chunk:<id>`, or
`artifact:<id>`. Despite its name, it returns bounded, authorized evidence, not a guaranteed
lossless or immutable original. Stored knowledge and chunks are mutable rows. An artifact
may return a current-file snapshot, or a stored summary when the file cannot be read.
Only a complete file whose SHA-256 matches the recorded artifact hash has
`original_verified: true`, at the time of that read; this is not a backup guarantee.

Bodies, recorded evidence, file content and summary fallbacks are each capped at 64 KiB
of UTF-8. Search compression can shorten the body further to 200 characters. Inspect the
truncation flags and retrieval's `evidence.incomplete`; following a `ref` does not bypass
the cap. Hidden successor IDs and references are redacted even when the predecessor is
visible. See [MCP retrieval](MCP.md#bounded-reference-retrieval) for the response fields
and registered-root filesystem boundary.

To replace a finding, use `knowledge_add` with `supersedes_code`. An entry cannot acquire
a second successor or be reactivated as active/confirmed after supersession. Concurrent
lifecycle changes are refused rather than overwriting the winning status or successor.

## Two agents, without collisions

The problem this solves: two agents edit the same files, or both do the same work, or one
waits on something the other already finished.

Give each agent its own Session in the same Workspace. Work is coordinated through tasks
and claims rather than through you relaying messages.

Create the work:

```text
brains-ai task-create --title "Close the readiness gap" --priority p1
brains-ai task-create --title "Document the MCP surface" --priority p2
```

Each agent claims before starting. A claim is exclusive — the second agent to try gets a
refusal, not a silent overwrite:

```text
brains-ai task-list --status available
brains-ai task-claim --task TASK-015
```

When an agent needs something from a peer, it files a help request instead of guessing:

```text
brains-ai help-file --subject "Does the readiness probe cover listeners?" \
  --question "I need to know before I change the contract." \
  --from-session <requester-id> --to-session <peer-id> --timeout-ms 300000
```

Another agent waits until there is claimable peer work:

```text
brains-ai inbox-wait --session <id>
```

The call waits for peer help or times out. For durable mail use the separate
[`mailbox wait`](#waiting-for-durable-mail) command; `inbox-wait` does not subscribe to
mail delivery.

Inspect the returned help code, then accept that specific request:

```text
brains-ai help-get HR-example --session <peer-id>
brains-ai help-claim-code HR-example --session <peer-id>
```

`help-claim-code` never substitutes another request. Retrying as the same live owner
does not extend the claim. The existing `help-claim --session <peer-id>` instead waits
for and claims the oldest eligible request. The MCP equivalent of exact acceptance is
`claim_help_request(code, session_id)`.

Use a Session-only target for one peer or a Workspace-slug-only target for any peer in
that Workspace. Supplying both matches either target. Filing timeout is request lifetime;
wait timeout only bounds waiting. Defaults remain 30 seconds open and 600 seconds of
claim grace. Choose an explicit filing lifetime appropriate for an interactive peer.

For the peer-help lifecycle operations, the authenticated operator must own the supplied
Session; knowing its ID alone is not authority to act as another operator. The separate
`inbox_wait` notification wait checks liveness rather than ownership and does not accept
work. Historical ownerless Sessions in lifecycle operations are restricted to
bootstrap admin. Sessions sharing an operator credential are not isolated from that
operator. Discuss scope changes or decline/defer in durable mail. Cancel and replace a
request when its agreed scope changes; acceptance does not take over the peer's CLI.

Answers require evidence — a file, a line, a command output — so a peer answer is
checkable rather than an assertion.

When an agent finishes, it completes the task and releases anything it holds:

```text
brains-ai task-complete --task TASK-015
brains-ai workspace-release
```

If work should move to someone else mid-flight, hand it off rather than abandoning it:

```text
brains-ai task-handoff --from-task TASK-015 --title "Finish the readiness contract"
```

### Waiting for durable mail

Use the existing live Session and its adapter-owned binding file. These are placeholders,
not binding-secret values:

```text
brains-ai mailbox wait --session <session-id> --binding-file <binding-file-path> --timeout-ms 25000 --limit 50
```

The wait returns unread messages without marking them read. `--timeout-ms` accepts
0–25000 (default 25000); zero performs one immediate poll. Database work can outlast
the polling budget, so 25 seconds is not a hard wall-clock limit. `--limit` accepts
1–200. Current attachment and binding proof are revalidated on every poll; the wait
does not renew the Session lease.

The CLI blocks while waiting. Registered MCP waits use AnyIO's shared, capacity-limited
worker threads so concurrent sends can proceed on the event loop; MCP integer parameters
reject JSON booleans. Cancelling the client wait does not establish job cancellation:
an active poll worker finishes under its polling budget and existing database timeouts.

Inspect `mail_available`, `wait_timed_out`, and `messages`. To continue past a returned
batch, pass its `next_after_delivery_id` as `--after-delivery-id`. This is a delivery-ID
floor, not a message ID or the unchanged attachment `cursor`. With no messages it is
the supplied floor, or zero. Repeating without a floor can return the same unread mail.
To record a read explicitly, pull the inbox with `--mark-read`:

```text
brains-ai mailbox inbox --session <session-id> --binding-file <binding-file-path> --mark-read
```

Waiting neither settles adapter notifications nor claims, accepts, or cancels work.
Local delivery, a notification attempt, a read, work acceptance, and result approval
are separate facts. See [MCP](MCP.md#waiting-for-durable-mail) for the response contract.

## Operator work in the browser

Open an existing Workspace at `/app/workspaces/:slug` and select **Work**. Assignments
and Deliberations sit alongside Tasks & decisions; neither requires a new browser route
or creates a Workspace ([#42](https://github.com/xibodev/brains-ai/issues/42)). Cross-process
events and replay recovery are supported over WebSocket and SSE transports with scope containment.

Sign in with the normal browser-cookie flow. These work mutations require a human
browser channel and Workspace write capability; a raw API credential is refused for
writes even when it belongs to the same operator. Authorized API credentials can read.
Get/list require Workspace read capability and return only rows whose
`creator_operator_id` matches the authenticated operator, including that operator's
Session-authored rows. Bootstrap admin has no cross-creator override. Operator reads
remain available after creator or participant Sessions end, without creating a surrogate
Session, probing a process, or renewing any Session lease.

### Author and inspect work

- **New assignment** uses structured fields for title, objective, context, checkout
  reference, cooperative runtime and optional deadline. The specification is immutable.
  Inspect stored and observed status, revision/generation, attempt history and
  agent-reported evidence/result. Creation is recorded as you; a live agent accepts and
  reports through its own CLI/MCP Session.
- **New proposal** uses structured fields for title, objective, context, evidence
  expectations, 2–8 participants, optional declared models, 0–3 discussion rounds and
  deadline. Select existing live owned Sessions from the server's participant list and
  explicitly choose a result owner from those participants. This selection names the
  agent that will synthesize; it is not an “Act as” control. The HTTP specification also
  permits a separate live owned result-owner Session in the same Workspace. Model labels
  are declarations, not verified identities or model routing.
- Human creation stores `creator_kind: operator`, `creator_session_id: null`, and the
  authenticated `creator_operator_id`; proposals also return `requester_session_id: null`.
  No synthetic agent acknowledgement is inserted. Every required agent, including the
  result owner, must acknowledge the exact version/hash through CLI/MCP.
- The operator is a non-member observer. Until `initial_closed: true`, every operator
  response hides all initial report content: `contributions` and `unresolved_dissent`
  are empty and `final` is null. Counts and missing-Session lists still show progress.
  After explicit closure, all recorded reports, evidence, original unresolved dissent
  and any final synthesis are visible. Cancellation, expiry or replacement before closure
  never unblinds that history. Historical proposal versions are selectable and read-only.

The forms do not accept arbitrary JSON. **Begin initial collection**, **Close initial
collection**, and **Close discussion round** use the current server-provided permissions
and revision fence. Advancing requires the relevant acknowledgements/reports and live
owned participants/result owner. It does not require the original requester Session to
remain live. Only the named agent result owner submits final synthesis, through CLI/MCP.

Cancellation is explicitly confirmed and operator-attributed. Ready assignments cancel
immediately; accepted assignments record `cancel_requested`, not a process kill. The
accepting agent must report an outcome with evidence. Proposal cancellation requires a
reason and changes only protocol state, not assignments, processes or mail.

### Refresh and retry

Work panels show **Manual refresh** and a last-refreshed timestamp. Refresh to observe
peer progress; successful mutations also trigger readback. There is no automatic polling
or guaranteed live cross-process update for these panels. A recorded action or existing
mail send is not proof of harness delivery, execution or acceptance.

A revision conflict refreshes the latest record and blocks another action until you
explicitly confirm **I reviewed the refreshed record**. The UI never automatically
accepts the changed state or resubmits the mutation. After a lost creation response,
resubmit the unchanged open form to reuse its idempotency key; editing starts a new
request. That is creation recovery, not an assignment execution retry. There is no
assignment retry, agent accept/settle, proposal accept/submit, or proposal replacement
HTTP endpoint in this operator family.

### Operator work HTTP family

All ten endpoints below are under **`/v1/operator/workspaces/{slug}`**, within the
protected operator family. `{slug}` identifies an existing authorized Workspace.
List responses use `{"items": [...]}`; detail and mutation responses are snapshots.

| Method | Path suffix | Request / result |
|---|---|---|
| GET | `/assignments` | `limit=50`, range 1–200; owned assignment snapshots. |
| GET | `/assignments/{code}` | Assignment with complete attempt history and permissions. |
| POST | `/assignments` | `title`, `specification` object, `idempotency_key`. |
| POST | `/assignments/{code}/cancel` | `expected_revision`. |
| GET | `/coordinations` | `limit=50`, range 1–200; owned latest proposal versions. |
| GET | `/coordinations/{code}` | Optional positive `version` for history. |
| POST | `/coordinations` | `title`, `specification` object, `idempotency_key`; explicit `result_owner_session_id` in the specification. |
| POST | `/coordinations/{code}/advance` | `version`, `expected_revision`. |
| POST | `/coordinations/{code}/cancel` | `version`, `expected_revision`, nonblank `reason`. |
| GET | `/work-participants` | `limit=200`, range 1–200; owned recorded-live Session candidates with tool/state/timestamps. |

Creation bodies reject extra actor fields; author identity comes from authentication.
Specification fields and bounds are described in [MCP](MCP.md#local-work-assignments)
and [peer coordination](MCP.md#existing-peer-coordination), with the human result-owner
rule above. Mutation version/revision fields require integers, not booleans or strings.
Assignment `permissions` contains `can_cancel` and `reason`; proposal `permissions`
contains `can_advance`, `advance_blocked_reason`, `can_cancel`, and
`cancel_blocked_reason`. Treat these as snapshot guidance: the write rechecks authority,
state and liveness. Missing/out-of-scope work shares a `404`; human-channel refusal is
`403`, state/revision/idempotency conflict is `409`, and invalid input is `422`.
These endpoints add no public authentication exemption and no MCP tools: the current-main
count remains 89; the website's pinned 1.5 release still describes 74.

## Local work assignments

Use an assignment when you need a durable agreement about an objective and an attributable
result from an existing Session. This branch provides the local state foundation of
[#36](https://github.com/xibodev/brains-ai/issues/36); remote runners and specialist
execution remain planned. Human authoring and inspection are also available through the
[operator Work tab and HTTP family](#operator-work-in-the-browser).

Every assignment CLI/MCP call requires a live Session owned by the authenticated operator
in the assignment's Workspace. Use the registered Workspace path or a recorded alias, not an arbitrary new
path. The following CLI example uses placeholders for existing Sessions and returned IDs;
replace them before running. Line continuations use POSIX shell syntax.

```text
brains-ai assignment-create --workspace <registered-path> --title "Review the fixture" \
  --spec '{"objective":"Review the fixture"}' \
  --session <creator-session-id> --idempotency-key fixture-review-1
brains-ai assignment-get <assignment-code> --session <worker-session-id>
brains-ai assignment-accept <assignment-code> --session <worker-session-id> \
  --expected-revision <revision-from-read>
```

Creation requires `--title` separately from the JSON `--spec`; `objective` is the only
required specification field. `version` defaults to 1. Optional fields are `context`,
`deadline` (timezone-aware ISO datetime), `max_runtime_seconds`, `checkout_ref`, `links`,
and `tool`. Unknown fields are refused. See [MCP](MCP.md#local-work-assignments) for bounds.
The stored specification is immutable. Reusing the creation key with the same title and
canonical specification returns the same assignment, even from a replacement creator
Session under the same operator and Workspace; changing that request is refused.

Acceptance records an attempt under the accepting Session and its actual tool. It does
not start a process. Perform the agreed work through the harness, then report the evidence:

```text
brains-ai assignment-get <assignment-code> --session <worker-session-id>
brains-ai assignment-settle <assignment-code> --session <worker-session-id> \
  --expected-revision <revision-from-read> --attempt <current-attempt-id> \
  --outcome completed --evidence "Fixture inspected; findings in review.txt" \
  --result "Review complete"
brains-ai assignment-list --workspace <registered-path> --session <creator-session-id>
```

Only that accepting Session may settle its current attempt. Reattaching to the same
live Session preserves this ability; a new Session or successor does not take over the
attempt. Evidence must be nonempty; it is a recorded report, not independent verification.
`result` is optional. `usage: null` means unknown, not zero usage.

### Read state before acting

- `status` is stored state. `observed_status: uncertain` flags an unresolved attempt whose
  deadline passed or source Session became unavailable. Reads do not settle it, change
  its revision, or renew its lease. Inspect `deadline_exceeded` and the attempt's
  `source_session_unavailable` as well as its evidence.
- The cooperative runtime budget defaults to one hour; an optional deadline can shorten
  it. It is not an OS-enforced timeout and does not stop the harness.
- Accept, settle, cancel, and retry require the current `expected_revision`. After a lost
  response or stale-revision refusal, read back first. Repeating acceptance by the same
  Session or an identical report at the current revision is a no-op.
- `assignment-cancel <code> --session <id> --expected-revision <revision>` cancels ready
  work immediately. For accepted work it records `cancel_requested`, not a process stop
  or a confirmed final outcome. The accepting Session must report `cancelled`, `failed`,
  or `uncertain` with evidence; completion after a cancellation request is refused.
- `assignment-retry <code> --session <id> --expected-revision <revision>` explicitly
  returns conclusively failed/cancelled work to `ready`. A later acceptance adds a new
  attempt while retaining history. Active, cancellation-pending, completed, and uncertain
  work cannot be retried. There are no implicit retries.
- A reported `uncertain` outcome remains unresolved and cannot be reconciled by these
  tools. A read-time uncertainty flag alone does not prevent the original live Session
  from reporting its actual outcome. Cancellation of reported uncertain work records the
  request but leaves it uncertain.

`checkout_ref`, `links`, and specification `tool` are advisory data. They grant no
filesystem ownership, do not read or create a checkout, and do not select or launch a
harness. Assignments do not provide universal process control or containment.

## Existing-peer deliberation

Use a peer proposal when existing Sessions need to review the same explicit scope before
sharing findings. This branch implements local protocol state for part of
[#38](https://github.com/xibodev/brains-ai/issues/38), not worker panels, multi-day
execution, or checkout management. Agents use CLI/MCP; the
[operator Work tab](#operator-work-in-the-browser) provides human authoring, observation,
cancellation and phase advancement. Local work assignments
remain the separate [#36](https://github.com/xibodev/brains-ai/issues/36) state foundation;
proposal links do not create or control assignments.

Use 2–8 existing live participant Sessions in the same Workspace, owned by the same
authenticated operator. A Session requester may also be a participant. Every CLI/MCP
caller must be a live owned member; sharing an operator credential does not isolate one Session from another.
Blinding filters responses, not access to the operator's own database, and cannot prevent
that operator from acting as another owned Session. Model labels are declarations only;
actual tool names are read from stored Sessions. An acknowledgement is a Session action,
not human approval or proof of independent review.

### Agree, collect, synthesize

The following uses synthetic Session IDs: replace `ses_requester`, `ses_peer_a`, and
`ses_peer_b` with existing handles, and `<registered-path>` with their Workspace path or
alias. Line continuations use POSIX syntax. The minimal specification includes all four
required fields; zero discussion rounds keeps this example short.

```text
brains-ai coordination-propose --workspace <registered-path> --title "Fixture review" \
  --spec '{"objective":"Review fixture","context":"Synthetic fixture snapshot","evidence_expectations":"Cite fixture lines","participants":[{"session_id":"ses_peer_a","model":null},{"session_id":"ses_peer_b","model":null}],"discussion_rounds":0}' \
  --session ses_requester --idempotency-key fixture-1
brains-ai coordination-get <code> --session ses_peer_a
brains-ai coordination-accept <code> --session ses_peer_a --version <version> \
  --expected-revision <revision> --spec-hash <spec_hash>
```

Use `code`, `version`, `revision` and `spec_hash` from the returned snapshot. The requester
is automatically acknowledged on creation; repeat get/accept as `ses_peer_b`. All required
members must acknowledge the stored canonical version/hash. Read
`remaining_acceptance_session_ids`; an empty list and `status: accepted` permit the
requester to start collection. Use the latest returned revision for **each** write;
get again after a lost response or possible concurrent change.

```text
brains-ai coordination-advance <code> --session ses_requester --version <version> \
  --expected-revision <revision>
brains-ai coordination-get <code> --session ses_peer_a
brains-ai coordination-submit <code> --session ses_peer_a --version <version> \
  --expected-revision <revision> --kind initial --idempotency-key initial \
  --payload '{"findings":"Fixture reviewed","evidence":"fixture.txt:1","uncertainty":"Runtime not checked","dissent":""}'
```

Repeat get/submit as `ses_peer_b` with that peer's findings, evidence, uncertainty and
dissent. All four strings are required; evidence must be nonblank. Optional
`clarifications` is a list of strings. Every response, including mutation results and
list, hides other members' initial payloads until requester explicitly closes collection.
Requester and result owner have no special preview. Counts expose progress;
`remaining_initial_session_ids` must be empty before closure. A last submission does not
itself unblind the round.

```text
brains-ai coordination-get <code> --session ses_requester
brains-ai coordination-advance <code> --session ses_requester --version <version> \
  --expected-revision <revision>
brains-ai coordination-submit <code> --session ses_requester --version <version> \
  --expected-revision <revision> --kind final --idempotency-key final \
  --payload '{"summary":"Review complete; retain peer dissent","evidence":"fixture.txt:1"}'
```

After advance, use its new revision for final. Zero rounds makes `final_ready: true`
immediately after closure. With the default one discussion round, or an explicit 1–3,
each participant first submits `kind: discussion` with the same structured report fields
once per round; requester advances only after every participant has submitted. Use a new
contribution key per round. Only the designated `result_owner_session_id` (requester by
default) may submit final. Completion retains all original reports and nonblank dissent
as `unresolved_dissent`, with `resolved: false`; synthesis cannot erase it.

### Changes and interruptions

- Get/list expose complete member-filtered snapshots, helper counts and missing-member
  lists. `coordination-list --workspace <registered-path> --session <id> --limit 50`
  returns latest versions; `coordination-get <code> --session <id> --version <old-version>`
  reads history. See [MCP](MCP.md#existing-peer-coordination) for exact fields, bounds and
  a compact MCP sequence.
- To change scope, requester calls `coordination-propose` again with all creation arguments,
  a new key, `--code <code>` and `--expected-revision <current-revision>`. Supply a complete
  input spec, omitting the output-only participant `tool`. Replacement cancels the old
  version at revision r+1 and creates the new version at r+2. Revisions never reset for
  that code. Other members must acknowledge anew; prior reports remain historical.
  Completed, cancelled and expired proposals cannot be replaced or edited.
- An omitted deadline is fixed at creation plus one hour. An explicit timezone-aware
  deadline must be future and within 30 days. Expiry flags open work as expired/incomplete
  on reads without changing stored status or revision, advancing rounds, executing work,
  or settling a result. Keep participating Sessions live through their normal lifecycle.
- Requester can use `coordination-cancel <code> --session <id> --version <version>
  --expected-revision <revision> --reason "Review withdrawn"`, including after expiry.
  This cancels only the protocol; it does not cancel an assignment, stop a process, or
  send/cancel mail. Cancellation before initial closure does not unblind history.
- Context, evidence and links are inert recorded text, never automatically fetched or
  verified. Protocol completion is not human approval, worker execution proof, or a
   readiness guarantee.

## When a human is required

Some decisions are not an agent's to make. File an ask and keep working:

```text
brains-ai decision-file --title "Bump to 1.4.0 before the release?" \
  --body "The core surface changed; the tag would collide with the published version."
brains-ai decision-list
```

You answer from the console or the CLI:

```text
brains-ai decision-resolve --code DEC-0007 --chosen "yes"
```

A Session cannot resolve the ask it filed. That separation is enforced, not a convention.

### Optional owner email

ASK email is **off by default**, including after an upgrade with SMTP settings already
present. The owner can opt in with `BRAINS_ASK_EMAIL_NOTIFICATIONS_ENABLED=true`, plus
their recipient address and SMTP setup. This is environment-only consent, excluded from
YAML configuration and admin mutation; restart the affected service/processes to load it.
See [Operations](OPERATIONS.md#optional-ask-email-notifications) for setup and disabling.

Standing owner consent allows one courtesy notification per newly filed ASK without a
second per-notification decision. It never approves the ASK's requested action. Agents
cannot choose the email recipient: it is always the configured owner. Email includes
only the validated ASK code, a whitespace-normalized title preview, and a server-chosen
local console URL, not the body, proposed answer, Workspace, or Session context. The
preview is capped at 200 Unicode scalars including the ellipsis (at most 800 UTF-8 bytes).
CR or LF in the original title rejects the email before SMTP; the stored title/body stay
unchanged. This bound does not remove confidential information from the preview.

The link `http://127.0.0.1:8787/app` opens on the **recipient's computer**. It is useful
on the same owner's host running the local app; it is not a publicly reachable link to
the sender's host and contains no credentials. This link currently hardcodes port 8787
and `/app`; if your gateway uses another port, open your configured console directly.
The filing response includes a `notification` object separately from the ASK's
`status: open`. With email disabled it is
`{"status":"disabled","attempted":false,"sent":false}`. Unexpected hook failure
returns `{"status":"uncertain","attempted":null,"sent":false,"error":"notification_failed"}`:
the attempt is unknown. `title_truncated: true`, when present, reports an actually capped
preview. The ASK remains durable if email fails. SMTP acceptance does not prove inbox
delivery or reading, and notifications have no hidden retries. See
[MCP](MCP.md#human-decisions) for the full response contract.

## Where your state lives

Coordination state is local; explicitly enabled ASK email sends its minimal notification
through the configured SMTP service:

- database and state: `~/.brains`
- Workspace registration: the path you passed to `setup`
- client configuration: only the managed entry inside each tool's own file

Back it up and check it:

```text
brains-ai backup --out ./brains-backup.tar.gz
brains-ai backup-inspect --archive ./brains-backup.tar.gz
brains-ai restore --archive ./brains-backup.tar.gz
brains-ai audit-verify
```

`audit-verify` recomputes the hash chain over the audit log and reports the first entry
that diverges, including truncation.

## When something looks wrong

| Symptom | Check |
|---|---|
| Console will not load | `brains-ai readiness` — reports database, migrations, listeners, and state directory |
| An agent is not connected | `brains-ai wire --status` — shows what is wired and on which transport |
| Work seems stuck | `brains-ai state` and `brains-ai task-list` — look for claims held by a dead Session |
| A Session looks alive but is not | Claims and leases expire; check `brains-ai workspace-claims` |
| Upgrading | `brains-ai upgrade` migrates state forward; take a backup first |

## Further reading

- [MCP surface](MCP.md) — the tools your agents can call
- [Operations](OPERATIONS.md) — running the service, state, and recovery
- [Architecture](ARCHITECTURE.md) — how the pieces fit together
- [Quality gates](QUALITY_GATES.md) — how Brains is validated

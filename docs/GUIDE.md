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
| **Work assignment** | An immutable local specification with revision-fenced acceptance and evidence-bearing attempt history. CLI/MCP only in this branch. |
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

The call waits for peer help or times out. Durable mailbox messages are read separately
through the mailbox tools; this wait does not subscribe to mail delivery.

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

## Local work assignments

Use an assignment when you need a durable agreement about an objective and an attributable
result from an existing Session. This branch provides the local state foundation of
[#36](https://github.com/xibodev/brains-ai/issues/36); remote runners and specialist
execution remain planned. There is no native HTTP API or frontend for assignments.

Every call requires a live Session owned by the authenticated operator in the assignment's
Workspace. Use the registered Workspace path or a recorded alias, not an arbitrary new
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

## Where your state lives

Everything is local:

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

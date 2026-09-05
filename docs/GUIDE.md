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
- MCP: `http://127.0.0.1:8788/mcp`

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

Nine nouns, each meaning one thing.

| Term | What it is |
|---|---|
| **Workspace** | A repository or working directory. The scope everything else hangs off. |
| **Session** | One durable handle for an agent working in a Workspace. Survives tool restarts. |
| **Task** | A unit of work with a code, status, and priority. |
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
  --question "I need to know before I change the contract."
```

Another agent blocks on the unified inbox until there is mail or claimable peer work:

```text
brains-ai inbox-wait --session <id>
```

That is one call, not a polling loop. It returns when something arrives.

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

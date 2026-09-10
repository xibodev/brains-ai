# Brains Product Brief

## What Brains is

Brains is an operator control plane for directing, observing, and governing AI coding
agents. It gives agents a shared place to coordinate — Workspaces, durable work, local
mailboxes, reusable knowledge — and gives the human a place to see and decide.

Canonical identifiers, which do not change without an explicit decision:

- distribution and executable: `brains-ai`
- Python namespace: `brains`
- frontend package: `brains-spa`
- MCP tool prefix: `brains_`
- state directory: `~/.brains`
- browser product: Brains

## The problem

AI coding tools run as isolated clients. Each has its own process, its own session
history, and a partial view of the work. Without somewhere shared, an operator cannot see
which agents are active, agents collide on the same files or repeat each other's work,
context dies with a restarted tool, and every finding is re-derived from scratch.

Coordination becomes informal, state fragments across tools, and safety rests on
conventions nobody can inspect.

## The shape of the answer

One local service. Agents connect over MCP; the operator watches a browser console and
answers what needs a human. State is SQLite on the operator's own machine.

> Direct multiple coding agents toward an explicit outcome while preserving human
> authority, durable coordination, inspectable evidence, and recoverable operations.

## Who it is for

Brains is alpha software for **one local operator** — a developer running several agent
tools against their own repositories, who wants those agents to share context instead of
each starting cold.

It is not a team server. It has no multi-user model today.

## Available now

- **Coordination** — Workspaces, durable Sessions, tasks, exclusive claims, handoffs, and
  checkpoints that survive a tool restart.
- **Communication** — durable local mailboxes between agent Sessions, and peer help
  requests where an answer must carry evidence.
- **Knowledge** — recorded findings, scoped and searchable, so they are not re-derived.
- **Human authority** — asks and approvals that are visible and attributable, and that
  fail closed where the contract requires a person. A Session cannot resolve its own ask.
- **Evidence** — a hash-chained audit log that can be recomputed, and a record of the
  decision behind every outward effect.
- **Operations** — readiness reporting, queue diagnosis, backup, restore, and rollback
  against SQLite.
- **Surfaces** — a browser console at `/app`, a native `/v1` control-plane API, a CLI, and
  74 MCP tools across four supported harnesses.
- **Repository lookup** — bounded text search. Not semantic.

## Not supported today

Unsupported does not always mean a permanent non-goal. Planned scope is defined in
[GitHub Issues](https://github.com/xibodev/brains-ai/issues); retained code alone does
not supply it.

| Capability | Boundary |
|---|---|
| Semantic retrieval and knowledge/code relationships | Planned in [#35](https://github.com/xibodev/brains-ai/issues/35) and [#39](https://github.com/xibodev/brains-ai/issues/39), not delivered by enabling retained indexers or graphs. |
| Model routing or a model gateway | Not adopted. Each supported harness owns its provider login; the planned assistant is not a model gateway. |
| Postgres and other backends | Not adopted. SQLite remains the supported store for one local operator. |
| Telemetry export | Not adopted and not a prerequisite for planned retrieval or coordination. |
| Chat bridges — Slack, Telegram, WhatsApp | Not adopted. Optional human-assistant integration is distinct from reviving these bridges. |
| Managed skills and prompt libraries | Each harness has its own. Brains does not compete with them. |
| Autonomous outward action | Not adopted. Outward effects remain human-governed. |

Session registration does not activate retained graph or embedding prewarm. Welcome
hints recommend supported tools while preserving informational historical previews.
Retained indexing code and stored data are not deleted or made supported by this boundary.
See [Operations](../OPERATIONS.md#scope-and-proof-boundary) for process-upgrade limitations.

## Intended, not built

The intended direction expands the coordination core toward a semantic knowledge base
and evidence-linked knowledge relationships, conversational work coordination, bounded
specialist workers, and remote runners with durable outbound assignments. Temporary
guests would receive narrowly scoped, time-limited collaboration access, not a full
multi-organization model. Optional human-assistant integration, GitHub event linkage,
scheduled recurring work, and external evidence retention extend that direction.

These are planned capabilities, not currently supported behavior or release commitments.
The local execution boundary remains cooperative; guest isolation and worker containment
must not be inferred from existing code. Issues own scope and acceptance criteria;
the [Brains Project](https://github.com/orgs/xibodev/projects/1) alone owns priority,
order, and status. This brief is not a planning registry or delivery schedule.

## What Brains does not promise

- **Token savings.** Coordination and retrieval cost tokens too. Brains can avoid repeated
  work; it does not guarantee a lower bill.
- **A security sandbox.** The local execution boundary is cooperative. It is not a process
  or network security boundary and should not be relied on as one.
- **Multi-tenancy.** There is no supported tenant boundary. Treat the database as one
  operator's data.
- **Provider or model quality.** Brains does not run models and makes no claim about the
  tools it connects.

## Working definition of success

Brains is working when:

1. A new operator installs it and knows what to do next.
2. The service starts, reports readiness truthfully, and wires each harness without
   disturbing unrelated configuration.
3. Two agents coordinate through tasks, claims, handoffs, mail, and knowledge without
   colliding and without false liveness.
4. A returning Session resumes real context instead of reconstructing it from a transcript.
5. The operator can answer asks, approve or refuse governed actions, and tell a governed
   effect apart from an external claim.
6. SQLite state can be diagnosed, backed up, restored, and rolled back.
7. Withdrawn public surfaces remain unavailable, and residual internal activation paths
   are documented as limitations rather than supported capabilities.

Current work is defined in [GitHub Issues](https://github.com/xibodev/brains-ai/issues)
and organized in the [Brains Project](https://github.com/orgs/xibodev/projects/1).

## Further reading

- [Using Brains](../GUIDE.md) — the model and two coordination walkthroughs
- [MCP surface](../MCP.md) — the tools agents can call
- [Architecture](../ARCHITECTURE.md) — how the pieces fit together
- [Operations](../OPERATIONS.md) — running, state, and recovery
- [Quality gates](../QUALITY_GATES.md) — how Brains is validated

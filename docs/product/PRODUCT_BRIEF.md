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
- **Local assignment state — available in this branch** — create an immutable work
  specification, accept it with an existing Session, and record an outcome with evidence
  and revision-fenced attempt history. The operator can author, inspect and cancel through
  Workspace Work and protected HTTP; agents accept/report through CLI/MCP. No process
  spawning or checkout ownership. Uncertain work remains unresolved rather than being
  implicitly retried.
- **Existing-peer deliberation — available in this branch** — version an explicit
  proposal for 2–8 existing Sessions, collect exact-hash acknowledgements, then blinded
  initial reports, 0–3 discussion rounds and result-owner synthesis. All original dissent
  is retained as unresolved. Workspace Work and protected HTTP provide human authoring,
  observation, cancellation and explicit phase advancement; CLI/MCP provide agent
  acknowledgement and reports. No worker launch or checkout management.
- **Communication** — durable local mailboxes between agent Sessions, proof-bound waiting
  for unread deliveries, and peer help requests where an answer must carry evidence.
- **Knowledge** — recorded findings, scoped and searchable, so they are not re-derived.
- **Human authority** — asks and approvals that are visible and attributable, and that
  fail closed where the contract requires a person. A Session cannot resolve its own ask.
  The owner may explicitly enable default-off ASK email notifications to their configured
  address through optional SMTP. This standing consent permits one notification per newly
  filed ASK, not approval of the requested action; SMTP acceptance is not recipient delivery.
- **Evidence** — a hash-chained audit log that can be recomputed, and a record of the
  decision behind every outward effect.
- **Operations** — readiness reporting, queue diagnosis, backup, restore, and rollback
  against SQLite.
- **Surfaces** — a browser console at `/app`, a native `/v1` control-plane API, a CLI, and
  89 current-main MCP tools across four supported harnesses. Surface coverage differs:
  ten protected operator work endpoints support controls in the existing Workspace Work
  tab, while agent acceptance and evidence submission remain CLI/MCP operations. This
  unreleased branch availability adds no MCP tools or SPA routes; the website's pinned
  1.5 release retains its 74-tool count.
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

The local assignment state foundation is available in this branch, but remote runners
and bounded specialist execution are still planned. It is partial scope for
[#36](https://github.com/xibodev/brains-ai/issues/36), which remains open for remote work
after [#37](https://github.com/xibodev/brains-ai/issues/37) planning. Local acceptance,
reported evidence, and cooperative deadlines do not establish worker launch, OS-enforced
budgets, new-Session takeover, or remote recovery.

The existing-peer protocol is a local implemented part of
[#38](https://github.com/xibodev/brains-ai/issues/38), not the whole worker-panel or
multi-day execution/checkout scope. Its durable proposal store is two standalone SQLite
tables added by migration 155, with explicit operator authorship added by migration 156.
For Session-authored proposals requester acknowledgement is automatic; all other required
members must acknowledge the canonical version/hash before collection. Human proposals
require an explicit named agent result owner and insert no automatic agent acknowledgement.
All selected agents must acknowledge through their own CLI/MCP Sessions. Blinding applies
to every protocol response, including requester reads, until explicit initial closure.
It does not prevent a shared operator from acting as another owned Session or reading
SQLite. Model declarations are unverified, and Session acknowledgement is not human
approval. Scope changes replace an open unexpired version with new acknowledgements and
monotonically increasing revisions; completed versions cannot be edited and history stays
readable. Human proposal replacement is not exposed. Deadline expiry only flags incomplete
state on reads, without automatic execution or settlement. Cancellation affects protocol
state, not assignments, processes
or mail. Context and links are inert, with no autogenerated assignment integration.
This availability makes no proposal-specific readiness promise; see
[MCP](../MCP.md#existing-peer-coordination) for the exact protocol.

The local operator foundation is available for part of
[#42](https://github.com/xibodev/brains-ai/issues/42): structured assignment and deliberation
forms in the existing Workspace Work tab, evidence/history reads, cancellation and phase
controls. Human rows carry `creator_kind: operator`, an authenticated creator operator
and no creator Session. They never manufacture or renew agent Sessions. Operator get/list
also retain access to their Session-authored history after those Sessions end, subject to
Workspace read capability and matching creator operator; admin cannot override another
creator's scope. HTTP writes require human browser-cookie authentication and Workspace
write capability; raw API credentials are read-only for this family.

The human requester/observer sees progress counts but no initial report content until
explicit initial closure. Afterwards, all recorded reports, evidence, synthesis and
unresolved dissent are visible. Cancellation or expiry before closure does not unblind
history. The UI chooses live owned participants and an explicit result-owner agent, not
an “Act as” identity. Revision conflicts refresh state and require human re-review;
unchanged-form creation retries are idempotent, not execution retries. Accepted assignment
cancellation is a request, not a process stop. No agent accept/submit or assignment retry
HTTP endpoint is added. Migration 156 preserves existing Session authors, hashes and
history with compatible rollback/replay behavior rather than rewriting historical work.

#42 remains partial: broader cross-process events/replay and transport comparison are
not delivered. Work panels use manual refresh and post-action readback, with no automatic
polling or guaranteed realtime update. Remote #36 work and #38 specialist workers remain
deferred to #37 planning. See the [operator guide](../GUIDE.md#operator-work-in-the-browser)
for the available local journey and HTTP family.

The remaining intended capabilities are not supported behavior or release commitments.
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

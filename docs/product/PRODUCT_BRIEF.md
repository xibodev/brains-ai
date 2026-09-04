# Brains Product Brief

## Identity

**Brains** is the product and repository identity. It is an operator control plane for directing, observing, and governing AI coding agents.

The canonical implementation identifiers are:

- distribution and executable: `brains-ai`
- Python namespace: `brains`
- frontend package: `brains-spa`
- MCP tool prefix: `brains_`
- default state directory: `~/.brains`
- browser product: Brains

Documentation, source comments, package metadata, tests, workflows, and operator-facing labels must use that single identity. A separate experimental product does not define this repository.

## Problem

AI coding tools are usually operated as isolated clients. Each tool has its own process,
session history, and partial view of work. Operators lack one place to:

- see which Workspaces and agent sessions are active;
- assign and claim shared work without collisions;
- preserve handoffs, checkpoints, messages, and reusable knowledge;
- resolve asks and approval requests;
- retain attributable operational and audit evidence.

Without a control plane, coordination becomes informal, state fragments across tools, and safety claims depend on conventions that are difficult to inspect or enforce.

## Vision

An operator can open one console, inspect a portfolio of Workspaces, connect supported
agent harnesses through MCP, coordinate durable work, resolve human decisions, and
determine whether the service and its evidence satisfy the product's acceptance
contract.

The final product goal is not "run more agents." It is:

> Direct multiple coding agents toward an explicit product outcome while preserving human authority, durable coordination, inspectable evidence, and recoverable operations.

## Product promise

Brains aims to provide:

1. **A coherent operating model.** Workspaces, coordination Sessions, tasks,
   claims, handoffs, messages, knowledge, and human decisions have stable meanings.
2. **Workspace and tool visibility.** Operators can see bounded portfolio, presence,
   wiring, and service posture without inferring execution that Brains did not govern.
3. **Durable coordination.** Agents can share ownership and resume context through MCP,
   CLI, and typed browser surfaces.
4. **Human control.** Asks, approvals, and governed effects are visible, attributable,
   and fail closed where the contract requires a decision.
5. **Truthful capabilities.** Missing, unsupported, experimental, and withdrawn behavior
   is not presented as normal availability.
6. **Honest evidence.** Static code, test presence, local execution, isolated
   validation, and deployed observation are distinct evidence levels.
7. **Recoverable operation.** SQLite state, backup, restore, health, readiness, and
   rollback have explicit contracts.

The [Feature Contract](FEATURE_CONTRACT.md) distinguishes advertised, partial, target,
frozen, and withdrawn behavior.

## Current Advertised Product

The normal installation currently claims only:

- Command Center and Workspace portfolio views;
- durable coordination Sessions, tasks, claims, handoffs, checkpoints, mailbox,
  peer help, and knowledge;
- human asks, decisions, governed-effect records, and audit verification;
- Operations readiness, queue diagnosis, service posture, and recovery policy;
- bounded Workspace knowledge and non-semantic repository lookup;

Execution-model, model-gateway, semantic/graph, automation, alternate-storage,
telemetry-export, messaging-bridge, and legacy-browser implementations have been
withdrawn as product claims. Their routes, commands, tools, flags, runtime extras, and
browser activation paths are absent. Historical modules and tables may remain only for
persisted-data compatibility.

## Primary users

| ID | User | Primary need |
|---|---|---|
| P1 | Solo operator/developer | Connect agent harnesses, coordinate Workspace work, and retain continuity. |
| P2 | Reserved multi-operator owner | Frozen future actor; not part of the supported local product. |
| P3 | Reserved multi-operator member | Frozen future actor; not part of the supported local product. |
| P4 | Service host operator | Control the local service, CLI wiring, credentials, state, and working roots. |
| P5 | Human approver | Review asks and outward-action requests with sufficient context and attribution. |
| P6 | Release/operations operator | Run gates, validate in isolation, back up, observe, and roll back. |
| P7 | AI agent session | Coordinate scoped work, preserve continuity, and request peer or human input. |

P7 is a product actor, not a stronger authentication principal than the credential and
Session boundaries the implementation provides.

## Value

Brains' value is coordinated, human-governed, local-first operation across supported
agent tools. It does **not** promise universal token savings: coordination and retrieval
can add cost as well as avoid repeated work.

## Product boundaries

### In scope

- operator console and native `/v1` control-plane API;
- Workspace-first coordination Sessions and human-governed work;
- asks, approvals, handoffs, messages, tasks, claims, and shared knowledge;
- CLI and MCP coordination surfaces;
- SQLite storage;
- backup, audit, observability, wiring, service definitions, and isolated validation
  tools; native service lifecycle proof remains an explicit backlog requirement;

### Target-only or withdrawn

- Runtime enrollment/execution, Personas, Pods, Projects, Issues, execution-model
  onboarding, and execution Session supervision;
- Automation UI, managed Skills, and scheduled Autopilot execution;
- model gateway and LiteLLM provider behavior;
- semantic retrieval and code graph;
- running-agent message delivery;
- Postgres, OpenTelemetry export, Telegram, Slack, WhatsApp, and WhatsApp Web;
- legacy dashboard and admin HTML.
- GitHub linkage and public defect publication;
- Org multi-user/multi-operator administration and cross-process scale;
- feedback intelligence, automatic coordination-pattern routing, ephemeral peer review,
  optional SMTP copies, and external evidence-retention services.

These concepts retain stable contract identifiers where needed for traceability, but
they are not advertised or schedulable core work. Re-entry requires an explicit
replacement or thaw decision.

### Non-goals

- claiming that any current deployment exists;
- treating Git tags, screenshots, old reports, or test files as proof of a current release;
- guaranteeing provider availability, model quality, cost savings, or external-service uptime;
- treating the current PATH-shim gate as a complete process or network security boundary;
- treating compatibility Org rows or API keys as a supported multi-tenant boundary;
- making Skills, recurring execution, or tool spawning autonomous without an explicit human-governed contract;
- replacing Git history with an in-repository chronology or evidence archive;
- changing the canonical Brains package, CLI, namespace, MCP prefix, state directory, repository, or browser identity without an explicit product decision.

## Vocabulary

| Term | Product meaning | Current implementation note |
|---|---|---|
| Org | Compatibility container for local persisted scope. | Multi-user membership and administration are frozen. |
| Workspace | Repository or working-directory coordination scope for the local operator. | Used by sessions, claims, knowledge, and visibility controls. |
| Runtime | Target concept for one machine multiplied by one detected CLI tool. | Current implementation withdrawn; persisted rows are compatibility data, not availability. |
| Persona | Target concept for a named reusable agent identity. | Current implementation withdrawn; not an authentication principal. |
| Pod | Target concept for a team of Personas. | Current implementation withdrawn; legacy Squad rows remain compatibility data. |
| Project | Target concept for an Org-scoped work container. | Current implementation withdrawn. |
| Issue | Target concept for assignable product work. | Current implementation withdrawn; GitHub linkage must not imply a normal Issue UI. |
| Session | One durable coordination handle for an agent in a Workspace. Historical execution links may also exist. | Stored in `agent_sessions`; leases, checkpoints, and events preserve coordination continuity. |
| Skill | Target concept for Org-scoped reusable work instructions. | Managed Skill implementation withdrawn; harness-native skills are separate. |
| Autopilot | Target concept for governed recurring work. | Current implementation withdrawn. |
| Ask | A human question requiring an answer. | Shares parts of the approval and decision mechanism. |
| Approval | A human decision about an action or choice. | Persistence exists; enforcement coverage is incomplete. |
| Evidence | A statement tied to a known source, command, environment, and result. | Levels are defined in [QUALITY_GATES.md](../QUALITY_GATES.md). |

## Success definition

Brains succeeds when the following can be demonstrated for one exact candidate:

1. A fresh operator enters the product and understands the next action.
2. The supported local service starts, reports truthful readiness, and wires each
   supported harness without overwriting unrelated configuration.
3. Two or more agent sessions can coordinate scoped work through tasks, claims,
   handoffs, messages, knowledge, and checkpoints without false liveness.
4. A returning session can resume durable context without duplicating ownership or
   reconstructing work from private transcripts.
5. The operator can answer asks, approve or reject governed actions, and distinguish
   governed effects from external claims.
6. Local authentication and Workspace scoping prevent unauthorized access and realtime
   subscription.
7. SQLite state can be diagnosed, backed up, restored, and rolled back under the
   declared recovery contract.
8. Withdrawn capabilities have no discovery or activation path in the supported
   installation.
9. Validation identifies the artifact and environment it actually exercised.
10. The product outcome satisfies the feature and journey acceptance criteria, not
    merely a build or process check.

Current HEAD does not meet that complete definition. Current actionable work is in
[BACKLOG.md](BACKLOG.md). Deferred product expansion is in
[FROZEN_BACKLOG.md](FROZEN_BACKLOG.md); lifecycle labels are in
[FEATURE_CONTRACT.md](FEATURE_CONTRACT.md).

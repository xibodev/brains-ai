<!--
last_verified: 2026-08-29T11:26:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 2630f04e31ca47ff93eda1e2b616b3e657b0c877 plus static inspection of advertised surfaces and approved current/experimental/withdrawn lifecycle decisions; withdrawal implementation not verified; deployment not verified
-->

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
- coordinate concurrent agents without collisions;
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

1. **A coherent operating model.** Orgs, Workspaces, coordination Sessions, tasks,
   claims, handoffs, messages, knowledge, and human decisions have stable meanings.
2. **Workspace and tool visibility.** Operators can see bounded portfolio, presence,
   wiring, and service posture without inferring execution that Brains did not govern.
3. **Durable coordination.** Agents can share ownership and resume context through MCP,
   CLI, and typed browser surfaces.
4. **Human control.** Asks, approvals, and governed effects are visible, attributable,
   and fail closed where the contract requires a decision.
5. **Truthful capabilities.** Missing, unsupported, experimental, and withdrawn behavior
   is not presented as normal availability.
6. **Honest evidence.** Static code, test presence, local execution, isolated UAT, and
   deployed observation are distinct evidence levels.
7. **Recoverable operation.** SQLite state, backup, restore, health, readiness, and
   rollback have explicit contracts.

Current HEAD implements parts of this promise. [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md) distinguishes present, partial, and missing behavior.

## Current Advertised Product

The normal installation currently claims only:

- Command Center and Workspace portfolio views;
- durable coordination Sessions, tasks, claims, handoffs, checkpoints, mail, topics,
  peer help, knowledge, and coordination patterns;
- human asks, decisions, governed-effect records, and audit verification;
- Operations readiness, queue diagnosis, service/tool posture, recovery policy, Org
  access, supported configuration, and scoped usage;
- signed GitHub event linkage;
- bounded Workspace knowledge and non-semantic repository lookup;
- the active experiments explicitly listed in
  [EXPERIMENTAL_BACKLOG.md](EXPERIMENTAL_BACKLOG.md).

Execution-model, model-gateway, semantic/graph, automation, alternate-storage,
telemetry-export, messaging-bridge, and legacy-browser implementations have been
withdrawn as product claims. Some routes, commands, tools, flags, extras, tables, and
source modules still exist until BL-P0-09 removes their exposure; existence is not an
invitation to enable them.

## Primary users

| ID | User | Primary need |
|---|---|---|
| P1 | Solo operator/developer | Connect agent harnesses, coordinate Workspace work, and retain continuity. |
| P2 | Org owner | Define Org membership, product configuration, and risk boundaries. |
| P3 | Org admin/member | Collaborate across authorized Workspaces and shared coordination state. |
| P4 | Service host operator | Control the local service, CLI wiring, credentials, state, and working roots. |
| P5 | Human approver | Review asks and outward-action requests with sufficient context and attribution. |
| P6 | Release/operations operator | Run gates, isolated UAT, backup, deploy, observe, and roll back. |
| P7 | AI agent session | Coordinate scoped work, preserve continuity, and request peer or human input. |

P7 is a product actor, not a stronger authentication principal than the credential and
Session boundaries the implementation provides.

## Value

Brains' defensible value is coordinated and governed operation:

- one durable work and session plane across multiple agent tools;
- fewer collisions through claims, tasks, handoffs, and presence;
- visible human decision points;
- a traceable path from product promise to acceptance evidence;
- local-first operation with SQLite as the supported source of truth;
- signed GitHub linkage without treating external activity as trusted by default.

Brains does **not** promise universal token savings. Retrieval, routing, and coordination can add cost as well as remove repeated work. Value depends on task shape, model, repository size, and whether useful context is delivered at the right time.

## Product boundaries

### In scope

- operator console and native `/v1` control-plane API;
- Workspace-first coordination Sessions and human-governed work;
- asks, approvals, handoffs, messages, tasks, claims, and shared knowledge;
- CLI and MCP coordination surfaces;
- SQLite storage;
- backup, audit, observability, wiring, services, containers, and isolated harnesses;
- signed GitHub linkage and human-approved public defect proposals.

### Target-only or withdrawn

- Runtime enrollment/execution, Personas, Pods, Projects, Issues, execution-model
  onboarding, and execution Session supervision;
- Automation UI, managed Skills, and scheduled Autopilot execution;
- model gateway and LiteLLM provider behavior;
- semantic retrieval and code graph;
- running-agent message delivery;
- Postgres, OpenTelemetry export, Telegram, Slack, WhatsApp, and WhatsApp Web;
- legacy dashboard and admin HTML.

These concepts retain stable contract identifiers where needed for traceability, but
their current implementations are frozen faulty or retired, are not schedulable
backlog, and require an explicit replacement/graduation decision before re-entry.

### Non-goals

- claiming that any current deployment exists;
- treating Git tags, screenshots, old reports, or test files as proof of a current release;
- guaranteeing provider availability, model quality, cost savings, or external-service uptime;
- treating the current PATH-shim gate as a complete process or network security boundary;
- treating Org roles or API keys as enforced tenant isolation before route-level authorization exists;
- making Skills, recurring execution, or tool spawning autonomous without an explicit human-governed contract;
- replacing Git history with an in-repository chronology or evidence archive;
- changing the canonical Brains package, CLI, namespace, MCP prefix, state directory, repository, or browser identity without an explicit product decision.

## Vocabulary

| Term | Product meaning | Current implementation note |
|---|---|---|
| Org | Top-level product and UI scope. | Stored in `orgs`; route-level role enforcement is incomplete. |
| Workspace | Repository or working-directory coordination scope inside or alongside an Org. | Older Brains engine concept; used by sessions, claims, knowledge, and visibility controls. |
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
6. Authorization prevents cross-Org and private-Workspace access and realtime
   subscription.
7. SQLite state can be diagnosed, backed up, restored, and rolled back under the
   declared recovery contract.
8. Withdrawn capabilities have no discovery or activation path in the supported
   installation.
9. Code, tests, isolated UAT, documentation, backup, and rollback evidence all identify
   the same candidate.
10. The product outcome satisfies the feature and journey acceptance criteria, not
    merely a build or process check.

Current HEAD does not meet that complete definition. Current schedulable gaps are in
[ACTIVE_BACKLOG.md](ACTIVE_BACKLOG.md); implemented field trials are in
[EXPERIMENTAL_BACKLOG.md](EXPERIMENTAL_BACKLOG.md); stable ID and withdrawal
disposition are indexed in [BACKLOG.md](BACKLOG.md).

<!--
last_verified: 2026-08-01T19:29:19.185-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD 6eb071bba49a5e678fb6ee8a35a3b21199136374; static inspection of source, tests, configuration, and canonical product contracts; deployment not verified
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

AI coding tools are usually operated as isolated clients. Each tool has its own process, provider configuration, session history, and partial view of work. Operators lack one place to:

- see which machines and CLIs are available;
- bind reusable agent identities to capable runtimes;
- assign and dispatch product work;
- observe durable execution state;
- coordinate concurrent agents without collisions;
- resolve asks and approval requests;
- retain attributable operational evidence.

Without a control plane, coordination becomes informal, state fragments across tools, and safety claims depend on conventions that are difficult to inspect or enforce.

## Vision

An operator can open one console, connect a machine, bind a Persona, create or select work, dispatch it, follow the resulting Session, intervene when needed, and determine whether the outcome satisfies the product's acceptance contract.

The final product goal is not "run more agents." It is:

> Direct multiple coding agents toward an explicit product outcome while preserving human authority, durable coordination, inspectable evidence, and recoverable operations.

## Product promise

Brains aims to provide:

1. **A coherent operating model.** Orgs, Workspaces, Runtimes, Personas, Pods, Projects, Issues, Sessions, Skills, and Autopilots have stable meanings.
2. **Machine and tool visibility.** A Runtime represents one machine and one detected CLI capability.
3. **Reusable agent identities.** A Persona binds instructions, model, tool, and an optional default Runtime.
4. **Work-driven execution.** Issues can be assigned and dispatched into observable Sessions.
5. **Human control.** Asks, approvals, and stop or steering controls are visible and attributable.
6. **Shared coordination.** Agents can use tasks, claims, handoffs, messages, knowledge, patterns, and checkpoints through MCP and CLI surfaces.
7. **Honest evidence.** Static code, test presence, local execution, isolated UAT, and deployed observation are distinct evidence levels.
8. **Recoverable operation.** State, backup, restore, health, readiness, and rollback have explicit contracts.

Current HEAD implements parts of this promise. [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md) distinguishes present, partial, and missing behavior.

## Primary users

| ID | User | Primary need |
|---|---|---|
| P1 | Solo operator/developer | Connect tools, define Personas, dispatch work, and supervise Sessions. |
| P2 | Org owner | Define Org membership, product configuration, and risk boundaries. |
| P3 | Org admin/member | Collaborate on Projects and Issues within authorized scope. |
| P4 | Runtime host operator | Control the machine, CLI credentials, working roots, and Runtime lifecycle. |
| P5 | Human approver | Review asks and outward-action requests with sufficient context and attribution. |
| P6 | Release/operations operator | Run gates, isolated UAT, backup, deploy, observe, and roll back. |
| P7 | AI Persona | Execute assigned work using a selected runtime, emit events, and request human input. |

P7 is a product actor, not a stronger authentication principal than the implementation provides.

## Value

Brains' defensible value is coordinated and governed operation:

- one durable work and session plane across multiple agent tools;
- fewer collisions through claims, tasks, handoffs, and presence;
- visible human decision points;
- a traceable path from product promise to acceptance evidence;
- local-first operation with an optional shared database topology;
- provider and model compatibility through the Brains gateway.

Brains does **not** promise universal token savings. Retrieval, routing, and coordination can add cost as well as remove repeated work. Value depends on task shape, model, repository size, and whether useful context is delivered at the right time.

## Product boundaries

### In scope

- operator console and native `/v1` control-plane API;
- Runtime enrollment, discovery, heartbeat, draining, assignment, and event ingestion;
- Persona, Pod, Project, Issue, Session, Skill, and Autopilot workflows;
- asks, approvals, handoffs, messages, tasks, claims, and shared knowledge;
- OpenAI- and Anthropic-compatible gateway behavior;
- CLI and MCP coordination surfaces;
- SQLite by default and optional Postgres support;
- backup, audit, observability, wiring, services, containers, and isolated harnesses;
- human-governed integrations and bridges.

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
| Runtime | One machine multiplied by one detected CLI tool. | Stored in `runtimes`; registered and heartbeated by the daemon. |
| Persona | Named agent identity with instructions, model, tool, and optional default Runtime. | Stored in `personas`; not an authentication principal. |
| Pod | A team with a leader and members. | Product term over the current `Squad` storage/control alias; membership semantics are incomplete. |
| Project | Org-scoped work container, optionally linked to a Workspace and Pod. | Stored in `projects`. |
| Issue | Work item with status, priority, comments, assignment, and dispatch history. | Stored in `issues` and `issue_comments`. |
| Session | One execution record, linked where available to Runtime, Persona, Issue, Workspace, and operator. | Stored in `agent_sessions`; event rows are durable, chat is not. |
| Skill | Org-scoped reusable `SKILL.md` content. | Stored in `skills`; Persona/Project attachment is missing. |
| Autopilot | Recurring task definition and run history. | Uses `recurring_task_definitions` and `recurring_runs`; schedule grammar is limited, not general cron. |
| Ask | A human question requiring an answer. | Shares parts of the approval and decision mechanism. |
| Approval | A human decision about an action or choice. | Persistence exists; enforcement coverage is incomplete. |
| Evidence | A statement tied to a known source, command, environment, and result. | Levels are defined in [QUALITY_GATES.md](../QUALITY_GATES.md). |

## Success definition

Brains succeeds when the following can be demonstrated for one exact candidate:

1. A fresh operator enters the product and understands the next action.
2. A machine connects and advertises usable CLI capabilities.
3. A Persona is bound to a Runtime and a supported model/tool choice.
4. A Project and Issue express real product work with stable acceptance criteria.
5. Dispatch produces one durable, attributable Session.
6. The operator can observe, recover after reconnect, answer asks, approve or reject actions, steer where supported, and stop execution.
7. Authorization prevents cross-Org access and realtime subscription.
8. Outward execution cannot bypass the approved gate and audit boundary.
9. Code, tests, isolated UAT, documentation, backup, and rollback evidence all identify the same candidate.
10. The product outcome satisfies the feature and journey acceptance criteria, not merely a build or process check.

Current HEAD does not meet that complete definition. The gaps are explicit in [BACKLOG.md](BACKLOG.md).

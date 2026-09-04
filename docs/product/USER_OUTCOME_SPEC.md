# Brains User Outcome Specification

## Purpose and boundary

This specification expresses the Brains contract from the user's point of
view: what a person is trying to achieve, the minimum intended interaction,
the system surfaces that must cooperate, and the evidence required to prove
the outcome.

It does not create new feature IDs or override the normative contracts in
[FEATURE_CONTRACT.md](FEATURE_CONTRACT.md),
[PERSONAS_AND_JOURNEYS.md](PERSONAS_AND_JOURNEYS.md), or
[TRACEABILITY.md](TRACEABILITY.md). Personal repositories, local database
contents, operator history, and project-specific observations are explicitly
excluded from this product specification.

Each outcome retains its user goal, minimal path, code contract, evidence contract,
and acceptance criteria. Unfinished core outcomes also identify core backlog items.

Validation names below identify observable contracts, not a record of a particular run.

## Core user outcomes

| ID | User promise | Minimal path | Code contract | Expected evidence contract | Core backlog items | Acceptance anchors |
|---|---|---|---|---|---|---|
| F0 | A user can operate Brains from one coherent Workspace-first console. | Sign in -> inspect Command Center -> enter a Workspace -> choose a typed action or receive an explicit blocker. | `/app/command-center`, Workspaces, Coordination, Governance, Operations, Act; typed operator HTTP adapters over shared controls. | Attributable task/claim/handoff/message/knowledge/decision events and audit rows with Workspace scope. | — | AC-F0-01..05; J1, J7, J11 |
| F1 | Withdrawn target: connect a machine and expose tools as Runtimes. | No supported user path; verify enrollment, Runtime, daemon, and activation surfaces are absent from discovery and fail closed. | Frozen Runtime/daemon routes, models, and tests are source-compatibility inventory only. | Advertisement inventory plus direct-call refusal and persisted-data compatibility evidence. | — | AC-F1-01..06; J2 |
| F2 | Withdrawn target: define reusable execution Personas. | No supported user path; verify Persona, binding, managed Skill, and Spawn surfaces are undiscoverable and non-activatable. | Frozen Persona/Skill/spawn source and tables are compatibility inventory only. | Advertisement inventory, direct-call refusal, and stable-store compatibility evidence. | — | AC-F2-01..06; J3 |
| F3 | A user can preserve durable coordination state, resolve human decisions, and resume work. | Open Workspace/Coordination/Governance -> inspect scoped Session context -> answer or decide -> checkpoint, hand off, resume, or end. | Workspace, Coordination, Governance; Session/event/decision APIs; local realtime delivery; CLI/MCP Session, decision, checkpoint, and resume families. | Coordination lifecycle transitions; persisted events; decision correlation; local reconnect backfill; unsupported execution control refused explicitly. | — | AC-F3-01..07; J7, J8, J11 |
| F4 | Withdrawn target: express and dispatch Projects and Issues. | No supported user path; verify Project, Issue, assignment, dispatch, and execution evidence surfaces are undiscoverable and non-activatable. | Frozen Project/Issue APIs, controls, UI, and tables are compatibility inventory only. | Advertisement inventory, direct-call refusal, and historical-row compatibility evidence. | — | AC-F4-01..07; J5, J6, J7 |
| F5 | Withdrawn target: assemble execution Pods. | No supported user path; verify Pod/Squad execution, roster, and dispatch surfaces are undiscoverable and non-activatable. | Frozen Pod/Squad source and rows are compatibility inventory only. | Advertisement inventory, direct-call refusal, and historical-row compatibility evidence. | — | AC-F5-01..04; J4, J6 |
| F6 | Withdrawn target: execution-model first-run onboarding. | No supported user path; a fresh install must start at Command Center and never redirect into execution onboarding. | Frozen onboarding route/state and composed F1/F2/F4 APIs are compatibility inventory only. | Clean-state route inventory and direct-call refusal without an execution flag. | — | AC-F6-01..05; J1 |
| F7 | A user can inspect supported Brains configuration and change only settings with an approved write contract. | Operations -> Configuration -> inspect redacted local service, MCP, SQLite, and harness posture -> use only a supported write/restart path. | Operations Config; positive supported config summary; validated non-secret writes. Other configuration is containment inventory only. | Redacted effective state; attributable audit and explicit restart result; zero withdrawn activation controls. | — | AC-F7-01..04; J9 |
| F8 | Frozen target: GitHub event intake and public defect relay. | No supported user path; confirm the capability remains unavailable. | Compatibility state does not authorize activation. | Public-surface absence and direct-call refusal. | — | AC-F8-01..04; J6, J9 |
| F9 | Frozen target: multi-user organization administration. | No supported user path; confirm the capability remains unavailable. | Workspace scoping for one local operator remains supported; compatibility state does not authorize multi-user activation. | Public-surface absence and direct-call refusal. | — | AC-F9-01..05; J11 |
| F10 | Withdrawn target: managed Skills, Autopilots, and scheduled execution. | No supported user path; verify Automation, recurring, generic webhook, and managed Skill surfaces are undiscoverable and non-activatable. | Frozen automation UI/APIs/CLI/MCP/tables are compatibility inventory only. | Advertisement inventory, direct-call refusal, and historical-row compatibility evidence. | — | AC-F10-01..06; J10 |

## Supporting user and operator outcomes

| ID | User promise | Minimal path | Code contract | Expected evidence contract | Core backlog items | Acceptance anchors |
|---|---|---|---|---|---|---|
| B1 | Withdrawn target: external request routing. | No supported user path; confirm the capability remains unavailable. | Compatibility state does not authorize activation. | Public-surface absence and direct-call refusal. | — | AC-B1-01..04; J9, J11 |
| B2 | A local human operator and agents can coordinate durable Workspace work across Sessions. | Start/register mailbox -> task/claim/address mail/handoff/help -> Inbox/Sent/thread/read -> checkpoint -> proof-bound resume/end. | Supported CLI/MCP coordination, mailbox identity/delivery/local-notification controls, a consented Claude Code stop hook, truthful pull fallback for every other harness, and the Coordination mailbox desk. | Session, task, claim, handoff, mailbox identity/attachment, direct/offline delivery, explicit broadcast, body-free local notification with bounded reclaim and uncertainty, exact hook-configuration restoration, pull fallback, thread/reply/forward, per-recipient read, browser selection/compose/deep-link, help, checkpoint, and resume transitions with valid ownership and references. | — | AC-B2-01..04; J5-J8 |
| B3 | A user or agent can retrieve bounded Workspace knowledge and non-semantic repository matches. | Search knowledge or bounded repository text/symbols -> inspect source and ok/empty/limited/unavailable state. | Knowledge controls and stable local lookup. Semantic indexing, embeddings, graph, and external freshness are withdrawn. | Workspace scope, source reference, bounded result, explicit complete/partial/unavailable outcome, and response binding across Workspace switches. | — | AC-B3-01..04; J6, J7, J11 |
| B4 | A human can retain authority over supported consequential actions and verify the decision trail. | Action requested -> review exact bounded context -> approve/reject -> execute/deny -> verify audit. | Supported governed actions, decisions, and local audit trail. | Request/resolution/execution correlation; actor/action; denial and failure evidence. | — | AC-B4-01..04; J8, J11 |
| B5 | An operator can evolve, back up, and restore supported SQLite data without losing integrity. | Initialize/upgrade -> inspect schema -> back up -> verify manifest -> restore/rollback. | SQLite storage, migrations, integrity, and backup/restore CLI/MCP. | Applied migration checksum/outcome; FK check; backup manifest/hash/schema; restore compatibility and result. | — | AC-B5-01..05; J11 |
| B6 | An operator can install, wire, start, inspect, and stop Brains consistently. | Install -> setup/wire -> start service -> inspect status/logs -> stop/unwire. | `brains-ai`, supported wire adapters, service renderers, and supervisor. | Exact source/package/OCI-image, executable, config, and state identity; listener/protocol health; PID/log ownership; cleanup and rollback result. | — | AC-B6-01..04; J1, J2, J9, J11 |
| B7 | Frozen target: GitHub delivery and public defect relay. | No supported user path; confirm the capability remains unavailable. | Compatibility state does not authorize activation. | Public-surface absence and direct-call refusal. | — | AC-B7-01..04; J8, J9 |
| B8 | An operator can distinguish liveness from supported-product readiness and diagnose failures. | Probe -> inspect service, storage, queue, durable-mail, wiring, and recovery posture -> recover -> recheck. | `/health`, readiness, supervisor, queue diagnosis, and recovery policy. | Dependency-specific readiness fields; count-only mailbox registration/attachment/unread/local-notification classes including bounded reclaim and uncertainty; process role, listener/protocol state, queue health, and recovery result. | — | AC-B8-01..04; J7, J11 |
| B9 | A user encounters one deliberate modern browser contract while deleted HTML stays absent. | Enter `/app` -> authenticate -> use a supported route or receive explicit not-found behavior. | `/app` plus its `/admin/login` and `/admin/logout` cookie endpoints. | Source, route, command, wheel/sdist inventory, consistent local-operator/Workspace decision, and zero deleted-surface activation. | — | AC-B9-01..03; J9-J11 |

## End-to-end outcome specifications

### O1 - Complete a unit of AI-assisted work

- **Given:** an authorized operator, Workspace, and one or more agent sessions.
- **When:** the work is recorded, claimed, coordinated, and handed off through Brains.
- **Then:** ownership remains attributable and idempotent, progress survives reconnect,
  and human questions or governed actions remain pending until resolved.
- **Evidence:** F0, F3; B2, B4, B8; J7, J8, J11.

### O2 - Coordinate several AI specialists

- **Given:** several live, authorized agent sessions in one or more Workspaces.
- **When:** they use claims, tasks, direct mail, peer help, and handoffs.
- **Then:** ownership and routing are explicit; each contribution remains attributable;
  stale or unavailable participants leave recoverable queue state.
- **Evidence:** F0, F3; B2, B4, B8; J7, J8, J11.

### O3 - Repeat a successful method safely

- **Given:** a reusable knowledge entry within a local Workspace.
- **When:** the operator or agent retrieves and applies it to a matching task.
- **Then:** its source remains visible and no withdrawn automation executes.
- **Evidence:** F3, F10; B2, B3, B8; J11.

### O4 - Intervene in running or blocked work

- **Given:** a coordination Session waiting for guidance, approval, or recovery.
- **When:** an authorized human answers, approves, rejects, defers, or ends the
  coordination handle.
- **Then:** the decision is one-time and attributable, persists before publication,
  and never implies running-agent delivery or process stop where those capabilities
  are withdrawn.
- **Evidence:** F3; B2, B4; J7, J8, J11.

### O5 - Leave and resume without reconstructing context

- **Given:** interrupted or previously completed Workspace work.
- **When:** a user returns through a coordination Session, checkpoint, handoff, or
  notification.
- **Then:** Brains restores the authorized durable state, identifies stale or
  missing dependencies, and supports retry/resume without duplicating work.
- **Evidence:** F3; B2, B3, B8; J7, J8, J11.

### O6 - Operate Brains locally

- **Given:** one local human operator, multiple Workspaces, and multiple agent Sessions.
- **When:** the operator manages Workspace-scoped coordination and supported local
  configuration through one supervised gateway.
- **Then:** reads and writes remain attributable and Workspace-scoped, while frozen
  multi-user, Org-management, GitHub, and cross-process capabilities stay unavailable.
- **Evidence:** F0, F3, F7; B2, B6, B8, B9; J7-J11.

### O7 - Diagnose and recover the product

- **Given:** a failed child service, stale coordination handle, or damaged SQLite state.
- **When:** an operator checks readiness, logs, migrations and backup evidence.
- **Then:** the failed dependency is explicit, repair or rollback is bounded,
  data integrity is verified, and critical journeys pass before service is
  declared ready.
- **Evidence:** B5, B6, B8; J11.

## Cross-cutting acceptance rules

Every outcome above must satisfy:

1. **Identity and scope:** one attributable local operator; explicit Workspace
   and entity policy; non-enumerating denial. Multi-user Org policy is frozen.
2. **Durability:** authoritative state commits before success or realtime
   publication; retries are idempotent.
3. **Human governance:** approval-required effects fail closed across each advertised
   governed path; external or withdrawn paths must not be represented as governed.
4. **Truthful errors:** failure, empty, loading, stale and unauthorized states
   remain distinct and actionable.
5. **Recovery:** retry, resume, repair and rollback preserve valid evidence and
   do not fabricate successful history.
6. **Privacy:** secrets and personal data do not appear in URLs, logs, traces,
   screenshots, fixtures or error messages.
7. **Accessibility:** keyboard, focus, labels, contrast and responsive behavior
   are part of acceptance rather than visual polish.
8. **Validation:** implementation, tests, and end-to-end checks must cover the changed
   boundary as defined in [QUALITY_GATES.md](../QUALITY_GATES.md).

## Change rule

A change to a promise, user path, route, component, API, control, model,
migration, CLI/MCP family, structured evidence signal, test or core backlog item
must update this specification and [TRACEABILITY.md](TRACEABILITY.md) in the
same change.

<!--
last_verified: 2026-08-02T11:15:22.361-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD da480d684d312ac05fb1f709901c4f2e0652098a; static source/test inspection plus privacy-preserving evidence-contract analysis; deployment not verified
-->

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

Each outcome must retain five links:

```text
user goal -> minimal path -> code contract -> evidence contract
          -> acceptance criteria and backlog gaps
```

Evidence names below are structured system identifiers and fields, not a
promise that the current candidate produced them successfully.

## Core user outcomes

| ID | User promise | Minimal path | Code contract | Expected evidence contract | Current gap ownership | Acceptance anchors |
|---|---|---|---|---|---|---|
| F0 | A user can operate Brains from one coherent console and start attributable work. | Sign in -> inspect Command Center -> enter a Workspace -> choose a typed action or receive an explicit blocker. | `/app/command-center`, Workspaces, Coordination, Governance, Operations, Act; typed operator HTTP adapters over shared controls. | Attributable task/claim/handoff/message/knowledge/decision events and audit rows with Workspace scope. | BL-P0-05, BL-P3-01 | AC-F0-01..05; J1, J3, J7, J11 |
| F1 | A user can connect a machine and make supported AI tools available as Runtimes. | Connect machine -> run enrollment command -> redeem -> confirm online -> drain/offline when needed. | Runtimes screens; enrollment, register, heartbeat, status and assignment APIs; daemon commands. | Token expiry/redemption fields; `runtime_registered`, `runtime_status`, `runtime_offline`; health and heartbeat timestamps. | BL-P0-01, BL-P0-06, BL-P1-13 | AC-F1-01..06; J2 |
| F2 | A user can define a reusable AI Persona with a compatible model, tool and Runtime. | Create Persona -> select Runtime/model -> save -> choose an Issue/work target or receive an explicit blocker -> Spawn or archive. | Personas screens; Persona CRUD/spawn; Persona, Runtime and Session controls. | `persona_created`, `persona_bound`, `persona_archived`; stored capability binding and attributable Session link. | BL-P0-05, BL-P1-08 | AC-F2-01..06; J3 |
| F3 | A user can supervise durable work, intervene, decide, stop and resume when the execution channel supports it. | Open Workspace/Governance -> answer or decide -> inspect contextual Session evidence -> stop or resume through a typed contract. | Workspaces, Coordination, Governance; Session/event/decision APIs; gated Session screen; WS/SSE; CLI/MCP Session, decision, checkpoint and resume families. | Explicit lifecycle transitions; persisted events; `decision_filed` and `decision_resolved`; delivery acknowledgement for message/stop; scoped reconnect backfill. | BL-P0-02, BL-P0-03, BL-P0-05, BL-P0-07, BL-P1-12 | AC-F3-01..07; J7, J8, J11 |
| F4 | A user can turn an objective into a Project and Issue, assign it and inspect execution evidence. | Create Project -> create Issue -> assign -> comment/transition -> dispatch -> inspect Session/result. | Projects, Issues and Board; Project/Issue/assignment/comment/dispatch APIs and controls. | `issue_created`, `issue_assigned`, `issue_transitioned`, `issue_comment`, `spawn_enqueued`; Issue mutation, Session and usage links. | BL-P0-05, BL-P1-02 | AC-F4-01..07; J5, J6, J7 |
| F5 | A user can assemble a Pod of complementary Personas and route work through it. | Create Pod -> add members -> choose leader -> assign Issue -> dispatch. | Pods screens; Pod/Squad roster, leadership, assignment and routing controls. | `squad_created`, member add/remove, task assign/delegate; attributable leader/member and resulting Session records. | BL-P1-03 | AC-F5-01..04; J4, J6 |
| F6 | A new user can move from empty state to one real, supervised result. | Create Org -> connect/defer machine -> create Persona -> create work -> dispatch or see an explicit blocker. | Onboarding route and composed F1/F2/F4 APIs. | Dedicated onboarding attempt/completion state; step outcomes; retry/resume state; final Session or explicit blocked reason. | BL-P1-04 | AC-F6-01..05; J1 |
| F7 | A user can understand effective Brains configuration and change only settings exposed by an approved write contract. | Operations -> Configuration -> inspect redacted state -> test provider -> use only an explicitly supported write/reload path. | Operations Config screen; config summary/provider-test APIs; config loader and provider registry; conditional approved admin writes. | Redacted effective-state result; provider readiness result; for supported writes, attributable audit plus reload outcome. | BL-P1-05, BL-P1-11, BL-P2-01 | AC-F7-01..04; J9 |
| F8 | A user can connect GitHub delivery to an Issue without trusting unverified events. | Configure integration -> receive signed event -> link PR -> reconcile verified merge. | GitHub webhook and Issue integration controls; configuration/failure UI. | Signature/repository/replay decision; idempotency key; delivery result; linked Issue transition and failure reason. | BL-P1-06 | AC-F8-01..04; J6, J9 |
| F9 | An owner can manage an Org, member roles, scope and attributable usage. | Operations -> Access -> select Org -> add member -> assign role -> inspect scoped usage. | Operations Access screens; Org/member/usage APIs and identity controls. | Principal and role attribution; Org/Workspace authorization decision; membership events; product-entity usage dimensions. | BL-P0-01, BL-P0-02, BL-P0-07, BL-P1-07 | AC-F9-01..05; J10, J11 |
| F10 | A user can automate repeatable work through the same governance boundary as manual work. | Create Skill/Autopilot -> set scope/schedule -> enable/fire -> approve when required -> inspect run. | Automation screen; Autopilot, Skill, recurring, job and webhook APIs/CLI/MCP. | Definition state; durable run with source/status/task/Session; gate and audit correlation; Skill attachment/provenance; failure reason. | BL-P0-03, BL-P0-04, BL-P1-08 | AC-F10-01..06; J10 |

## Supporting user and operator outcomes

| ID | User promise | Minimal path | Code contract | Expected evidence contract | Current gap ownership | Acceptance anchors |
|---|---|---|---|---|---|---|
| B1 | A client can use one gateway while preserving exact model intent or explicitly requesting policy routing. | List models -> request exact/tier/`brains/auto` model -> inspect actual provider/model result. | OpenAI/Anthropic-compatible APIs; resolver, classifier and provider policy. | Trace, route decision and usage records with requested/routed model, provider, endpoint, task type, token and cost fields. | BL-P1-05, BL-P1-11 | AC-B1-01..04; J9, J11 |
| B2 | Humans and agents can coordinate durable work across Sessions and machines. | Start -> task/claim/message/handoff/help -> checkpoint -> resume/end. | CLI/MCP coordination families and `control/*`. | Session, task, claim, handoff, mailbox, help, checkpoint and resume transitions with valid ownership and references. | BL-P0-01, BL-P0-07, BL-P1-08, BL-P1-12, BL-P2-03 | AC-B2-01..04; J5-J8, J10 |
| B3 | A user or agent can retrieve bounded, attributable and fresh project context. | Register/index source -> orient/search/query graph -> inspect provenance/freshness. | Context CLI/MCP, repository/semantic indexers, code graph, freshness, memory and knowledge controls. | Source/build identity, artifact/chunk hashes, graph-to-source linkage, freshness/readiness state and retrieval provenance. | BL-P2-03, BL-P2-04 | AC-B3-01..04; J6, J7, J11 |
| B4 | A human can retain authority over consequential actions and verify the decision trail. | Action requested -> review context -> approve/reject -> execute/deny -> verify audit. | Execution gate, decisions, relay, bridges and audit chain. | Gate request/resolution/execution correlation; actor/action; transactional append; previous/entry hashes; denial and failure evidence. | BL-P0-03, BL-P0-04, BL-P1-08, BL-P1-12 | AC-B4-01..04; J8, J10, J11 |
| B5 | An operator can evolve, back up and restore data without losing integrity. | Initialize/upgrade -> inspect schema -> back up -> verify manifest -> restore/rollback. | Storage, migrations and backup/restore CLI/MCP. | Applied migration checksum/backend/outcome; FK check; backup manifest/hash/schema; restore compatibility and result. | BL-P0-04, BL-P0-07, BL-P0-08, BL-P1-09 | AC-B5-01..05; J10, J11 |
| B6 | An operator can install, wire, start, inspect and stop Brains consistently. | Install -> setup/wire -> start service -> inspect status/logs -> stop/unwire. | `brains-ai`, wire adapters, service renderers and supervisor. | Command result; exact executable/config/state identity; process start time; child status; PID/log ownership and cleanup result. | BL-P0-06, BL-P0-08, BL-P1-05, BL-P1-09, BL-P1-13 | AC-B6-01..04; J1, J2, J9, J11 |
| B7 | External systems can trigger or receive governed Brains work safely. | Configure integration -> authenticate delivery -> dedupe -> authorize -> fire/respond. | Webhooks, relay, bridges and wa-web. | Trigger/delivery identity, credential decision, dedupe key, status, task/run correlation and delivery failure. | BL-P1-06 | AC-B7-01..04; J8, J9 |
| B8 | An operator can distinguish liveness from readiness and diagnose failures. | Probe -> inspect dependency state/logs/traces/metrics -> recover -> recheck. | `/health`, target readiness surface, supervisor, observability and Runtime sweep. | Dependency-specific readiness fields, process role, route template, exception class, Runtime freshness, queue health and recovery result. | BL-P0-02, BL-P0-06, BL-P1-09, BL-P1-12, BL-P2-04 | AC-B8-01..04; J7, J11 |
| B9 | A user encounters one deliberate support contract across modern and legacy interfaces. | Enter supported surface -> authenticate -> perform supported task or receive explicit retirement guidance. | `/app`, `/dashboard`, `/admin`, shared auth/config/data boundaries. | Supported-surface inventory, consistent principal/scope decision, route outcome and explicit retirement/migration state. | BL-P0-01, BL-P0-06, BL-P2-01 | AC-B9-01..03; J9-J11 |

## End-to-end outcome specifications

### O1 - Complete a unit of AI-assisted work

- **Given:** an authorized operator, Org, eligible Runtime and compatible Persona.
- **When:** the operator creates or selects an Issue and dispatches it.
- **Then:** one attributable logical execution is created, assignment and claim
  retries remain idempotent, progress is observable, human questions and
  governed actions remain pending until resolved, and the Issue reflects the
  durable terminal outcome.
- **Evidence:** F0-F4, F9; B2, B4, B8; J6-J8; BL-P0-01..05, BL-P0-07,
  BL-P1-02.

### O2 - Coordinate several AI specialists

- **Given:** authorized Personas with compatible Runtime capabilities.
- **When:** the operator assembles a Pod and assigns work to it.
- **Then:** membership, leadership and routing are explicit; every delegated
  Session and handoff remains attributable; failure of one member remains
  visible and recoverable.
- **Evidence:** F2-F5; B2, B4, B8; J3, J4, J6, J7; BL-P1-02, BL-P1-03,
  BL-P1-12.

### O3 - Repeat a successful method safely

- **Given:** an approved Skill or Autopilot definition with Org, schedule and
  execution scope.
- **When:** it fires manually, by schedule or webhook.
- **Then:** it passes through the common authorization gate, produces one
  durable run and linked work record, and exposes completion, skip or failure
  without fabricated provenance.
- **Evidence:** F10; B2, B4, B7, B8; J10; BL-P0-03, BL-P0-04, BL-P1-06,
  BL-P1-08.

### O4 - Intervene in running or blocked work

- **Given:** a Session waiting for guidance, approval or recovery.
- **When:** an authorized human answers, approves, rejects, redirects or stops.
- **Then:** the decision is one-time, attributable, delivered to the correct
  Runtime, persisted before publication, and reflected in Session and Issue
  state.
- **Evidence:** F3, F4, F9; B2, B4; J7, J8, J11; BL-P0-01..05, BL-P1-12.

### O5 - Leave and resume without reconstructing context

- **Given:** interrupted or previously completed work.
- **When:** a user returns through an Issue, Session, checkpoint or notification.
- **Then:** Brains restores the authorized durable state, identifies stale or
  missing dependencies, and supports retry/resume without duplicating work.
- **Evidence:** F3, F4; B2, B3, B8; J7, J8, J11; BL-P0-02, BL-P0-05,
  BL-P1-12, BL-P2-04.

### O6 - Operate Brains for a team

- **Given:** an Org owner and multiple users.
- **When:** membership, roles, Workspaces, Projects and integrations are managed.
- **Then:** every read/write/realtime/background action is principal- and
  Org-scoped, usage is attributable, and unauthorized entities are not
  enumerable.
- **Evidence:** F1, F4, F8, F9; B2, B7, B9; J2, J5, J9-J11; BL-P0-01,
  BL-P0-02, BL-P0-07, BL-P1-06, BL-P1-07.

### O7 - Diagnose and recover the product

- **Given:** a failed dependency, stale Runtime, damaged state or incompatible
  candidate.
- **When:** an operator checks readiness, logs, migrations and backup evidence.
- **Then:** the failed dependency is explicit, repair or rollback is bounded,
  data integrity is verified, and critical journeys pass before service is
  declared ready.
- **Evidence:** B5, B6, B8; J11; BL-P0-06..08, BL-P1-01, BL-P1-09.

## Cross-cutting acceptance rules

Every outcome above must satisfy:

1. **Identity and scope:** one attributable principal; explicit Org, Workspace
   and entity policy; non-enumerating denial.
2. **Durability:** authoritative state commits before success or realtime
   publication; retries are idempotent.
3. **Human governance:** approval-required effects fail closed across manual,
   scheduled, interpreter, network and webhook paths.
4. **Truthful errors:** failure, empty, loading, stale and unauthorized states
   remain distinct and actionable.
5. **Recovery:** retry, resume, repair and rollback preserve valid evidence and
   do not fabricate successful history.
6. **Privacy:** secrets and personal data do not appear in URLs, logs, traces,
   screenshots, fixtures or error messages.
7. **Accessibility:** keyboard, focus, labels, contrast and responsive behavior
   are part of acceptance rather than visual polish.
8. **Evidence level:** code/test presence is E1/E2 only; acceptance requires the
   E3/E4 evidence defined in [QUALITY_GATES.md](../QUALITY_GATES.md).

## Change rule

A change to a promise, user path, route, component, API, control, model,
migration, CLI/MCP family, structured evidence signal, test or backlog owner
must update this specification and [TRACEABILITY.md](TRACEABILITY.md) in the
same change.

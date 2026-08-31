<!--
last_verified: 2026-08-30T19:30:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD ea15b51a3868434f2f2081b71da48126818007b3 plus one-way SMTP candidate inspection and isolated Docker lint, type, migration, authorization, redaction, retry, and browser evidence; real SMTP provider and deployment not verified
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
| F0 | A user can operate Brains from one coherent Workspace-first console. | Sign in -> inspect Command Center -> enter a Workspace -> choose a typed action or receive an explicit blocker. | `/app/command-center`, Workspaces, Coordination, Governance, Operations, Act; typed operator HTTP adapters over shared controls. | Attributable task/claim/handoff/message/knowledge/decision events and audit rows with Workspace scope. | BL-P3-01, BL-P2-01 | AC-F0-01..05; J1, J7, J11 |
| F1 | Withdrawn target: connect a machine and expose tools as Runtimes. | No supported user path; verify enrollment, Runtime, daemon, and activation surfaces are absent from discovery and fail closed. | Frozen Runtime/daemon routes, models, and tests are source-compatibility inventory only. | Advertisement inventory plus direct-call refusal and persisted-data compatibility evidence. | BL-P0-09, BL-P1-13 | AC-F1-01..06; J2 |
| F2 | Withdrawn target: define reusable execution Personas. | No supported user path; verify Persona, binding, managed Skill, and Spawn surfaces are undiscoverable and non-activatable. | Frozen Persona/Skill/spawn source and tables are compatibility inventory only. | Advertisement inventory, direct-call refusal, and stable-store compatibility evidence. | BL-P0-09, BL-P1-08 | AC-F2-01..06; J3 |
| F3 | A user can preserve durable coordination state, resolve human decisions, and resume work. | Open Workspace/Coordination/Governance -> inspect scoped Session context -> answer or decide -> checkpoint, hand off, resume, or end. | Workspace, Coordination, Governance; Session/event/decision APIs; WS/SSE; CLI/MCP Session, decision, checkpoint, and resume families. | Coordination lifecycle transitions; persisted events; decision correlation; scoped reconnect backfill; unsupported execution control refused explicitly. | BL-P0-02, BL-P0-03, BL-P0-05, BL-P1-12, BL-P1-14 | AC-F3-01..07; J7, J8, J11 |
| F4 | Withdrawn target: express and dispatch Projects and Issues. | No supported user path; verify Project, Issue, assignment, dispatch, and execution evidence surfaces are undiscoverable and non-activatable. | Frozen Project/Issue APIs, controls, UI, and tables are compatibility inventory only. | Advertisement inventory, direct-call refusal, and historical-row compatibility evidence. | BL-P0-09, BL-P1-02 | AC-F4-01..07; J5, J6, J7 |
| F5 | Withdrawn target: assemble execution Pods. | No supported user path; verify Pod/Squad execution, roster, and dispatch surfaces are undiscoverable and non-activatable. | Frozen Pod/Squad source and rows are compatibility inventory only. | Advertisement inventory, direct-call refusal, and historical-row compatibility evidence. | BL-P0-09, BL-P1-03 | AC-F5-01..04; J4, J6 |
| F6 | Withdrawn target: execution-model first-run onboarding. | No supported user path; a fresh install must start at Command Center and never redirect into execution onboarding. | Frozen onboarding route/state and composed F1/F2/F4 APIs are compatibility inventory only. | Clean-state route inventory and direct-call refusal without an execution flag. | BL-P0-09, BL-P1-04 | AC-F6-01..05; J1 |
| F7 | A user can inspect supported Brains configuration and change only settings with an approved write contract. | Operations -> Configuration -> inspect redacted service, MCP, GitHub, email, and secret posture -> use only a supported write/reload path. | Operations Config; supported config summaries and probes; validated non-secret/encrypted writes. Withdrawn gateway and bridge configuration is containment inventory. | Redacted effective state; attributable audit and explicit reload/restart result; zero withdrawn activation controls. | BL-P1-05, BL-P1-11, BL-P2-01 | AC-F7-01..04; J9 |
| F8 | A user can accept signed GitHub development events without trusting unverified delivery. | Configure repository scope -> receive signed event -> deduplicate -> associate with attributable Brains work or report an explicit blocker. | GitHub webhook, configuration posture, delivery controls, and the planned human-approved public defect relay. | Signature/repository/replay decision; idempotency key; delivery result; governed outbound payload when BL-P1-19 lands. | BL-P1-06, BL-P1-19 | AC-F8-01..04; J6, J9 |
| F9 | An owner can manage an Org, member roles, Workspace scope, and attributable usage. | Operations -> Access -> select Org -> manage membership -> inspect scoped usage. | Operations Access; Org/member/usage APIs and identity controls. | Principal/role attribution; Org/Workspace authorization decision; membership events; explicit usage dimensions. | BL-P0-01, BL-P1-07, BL-P3-01 | AC-F9-01..05; J10, J11 |
| F10 | Withdrawn target: managed Skills, Autopilots, and scheduled execution. | No supported user path; verify Automation, recurring, generic webhook, and managed Skill surfaces are undiscoverable and non-activatable. | Frozen automation UI/APIs/CLI/MCP/tables are compatibility inventory only. | Advertisement inventory, direct-call refusal, and historical-row compatibility evidence. | BL-P0-09, BL-P1-08 | AC-F10-01..06; J10 |

## Supporting user and operator outcomes

| ID | User promise | Minimal path | Code contract | Expected evidence contract | Current gap ownership | Acceptance anchors |
|---|---|---|---|---|---|---|
| B1 | Withdrawn target: model gateway and policy routing. | No supported user path; verify model proxy, provider, routing, and launcher surfaces are undiscoverable and non-activatable. | Frozen OpenAI/Anthropic facades, router, providers, and usage tables are compatibility inventory only. | Advertisement inventory, direct-call refusal, and no normal-readiness dependency. | BL-P0-09, BL-P1-11 | AC-B1-01..04; J9, J11 |
| B2 | Humans and agents can coordinate durable Workspace work across Sessions. | Start/register mailbox -> task/claim/address mail/handoff/help -> optional fixed nudge or verified one-way SMTP copy -> Inbox/Sent/thread/read -> checkpoint -> proof-bound resume/end. | Supported CLI/MCP coordination, mailbox identity/delivery/notification controls, human-bound SMTP copy controls, and the Coordination mailbox desk. | Session, task, claim, handoff, mailbox identity/attachment, direct/offline delivery, explicit broadcast, body-free notification claim/settle with pull fallback, encrypted verified SMTP destination, notification-only default/full-body consent, leased retry/uncertain state, thread/reply/forward, per-recipient read, browser selection/compose/deep-link, help, checkpoint, and resume transitions with valid ownership and references; concrete harness hook/plugin installation and real-provider SMTP remain open. | BL-P0-05, BL-P1-12, BL-P1-14, BL-P1-17 | AC-B2-01..04; J5-J8, J10 |
| B3 | A user or agent can retrieve bounded Workspace knowledge and non-semantic repository matches. | Search knowledge or bounded repository text/symbols -> inspect source and unavailable/empty state. | Knowledge controls and stable local lookup. Semantic indexing, embeddings, graph, and external freshness are withdrawn. | Workspace scope, source reference, bounded result, and explicit empty/unavailable outcome. | BL-P0-09, BL-P1-18, BL-P2-04 | AC-B3-01..04; J6, J7, J11 |
| B4 | A human can retain authority over consequential actions and verify the decision trail. | Action requested -> review exact bounded context -> approve/reject -> execute/deny -> verify audit. | Governed actions, decisions, audit chain, and future exact-payload defect relay. | Request/resolution/execution correlation; actor/action; transactional append; previous/entry hashes; denial and failure evidence. | BL-P0-03, BL-P0-04, BL-P1-19, BL-P2-03 | AC-B4-01..04; J8, J10, J11 |
| B5 | An operator can evolve, back up, and restore supported SQLite data without losing integrity. | Initialize/upgrade -> inspect schema -> back up -> verify manifest -> restore/rollback. | SQLite storage, migrations, integrity, and backup/restore CLI/MCP. | Applied migration checksum/outcome; FK check; backup manifest/hash/schema; restore compatibility and result. | BL-P0-04, BL-P0-07, BL-P0-08, BL-P1-09 | AC-B5-01..05; J10, J11 |
| B6 | An operator can install, wire, start, inspect, and stop Brains consistently. | Install -> setup/wire -> start service -> inspect status/logs -> stop/unwire. | `brains-ai`, supported wire adapters, service renderers, and supervisor. | Exact executable/config/state identity; listener/protocol health; PID/log ownership; cleanup and rollback result. | BL-P0-06, BL-P0-08, BL-P1-05, BL-P1-09 | AC-B6-01..04; J1, J2, J9, J11 |
| B7 | GitHub can deliver authenticated, deduplicated events, and a human may later approve an exact public defect payload. | Configure GitHub scope -> authenticate delivery -> deduplicate -> link locally; for outbound defects, preview -> approve -> create/comment. | Signed GitHub webhook and planned governed public defect relay. Generic webhooks, relay, and messaging bridges are withdrawn. | Delivery identity, signature/scope decision, dedupe key, local link, exact approval, and governed result. | BL-P0-09, BL-P1-06, BL-P1-19 | AC-B7-01..04; J8, J9 |
| B8 | An operator can distinguish liveness from supported-product readiness and diagnose failures. | Probe -> inspect service, storage, queue, wiring, and recovery posture -> recover -> recheck. | `/health`, readiness, supervisor, queue diagnosis, recovery policy, and privacy-safe experimental analytics. | Dependency-specific readiness fields, process role, listener/protocol state, queue health, and recovery result. | BL-P0-02, BL-P0-06, BL-P1-09, BL-P1-12, BL-P1-16 | AC-B8-01..04; J7, J11 |
| B9 | A user encounters one deliberate modern browser contract while legacy HTML remains retired. | Enter `/app` -> authenticate -> use a supported route or receive explicit not-found/retirement behavior. | `/app` plus withdrawn `/dashboard` and legacy `/admin` source inventory. | Supported-route inventory, consistent principal/scope decision, and zero retired-surface activation. | BL-P0-09, BL-P2-01, BL-P3-01 | AC-B9-01..03; J9-J11 |

## End-to-end outcome specifications

### O1 - Complete a unit of AI-assisted work

- **Given:** an authorized operator, Workspace, and one or more agent sessions.
- **When:** the work is recorded, claimed, coordinated, and handed off through Brains.
- **Then:** ownership remains attributable and idempotent, progress survives reconnect,
  and human questions or governed actions remain pending until resolved.
- **Evidence:** F0, F3, F9; B2, B4, B8; J7, J8, J11; BL-P0-01..05,
  BL-P1-12, BL-P1-14.

### O2 - Coordinate several AI specialists

- **Given:** several live, authorized agent sessions in one or more Workspaces.
- **When:** they use claims, tasks, direct mail, topics, peer help, and handoffs.
- **Then:** ownership and routing are explicit; each contribution remains attributable;
  stale or unavailable participants leave recoverable queue state.
- **Evidence:** F0, F3, F9; B2, B4, B8; J7, J8, J11; BL-P0-02,
  BL-P0-05, BL-P1-12, BL-P1-14.

### O3 - Repeat a successful method safely

- **Given:** an approved coordination pattern or reusable knowledge entry.
- **When:** Brains offers it for a matching task and the agent uses or declines it.
- **Then:** the source/version and offer reason are visible, the receipt is
  privacy-safe, and no managed Skill or scheduler executes automatically.
- **Evidence:** F3, F10; B2, B4, B8; J10, J11; BL-P0-03, BL-P1-17,
  BL-P1-08.

### O4 - Intervene in running or blocked work

- **Given:** a coordination Session waiting for guidance, approval, or recovery.
- **When:** an authorized human answers, approves, rejects, defers, or ends the
  coordination handle.
- **Then:** the decision is one-time and attributable, persists before publication,
  and never implies running-agent delivery or process stop where those capabilities
  are withdrawn.
- **Evidence:** F3, F9; B2, B4; J7, J8, J11; BL-P0-01..05, BL-P1-12.

### O5 - Leave and resume without reconstructing context

- **Given:** interrupted or previously completed Workspace work.
- **When:** a user returns through a coordination Session, checkpoint, handoff, or
  notification.
- **Then:** Brains restores the authorized durable state, identifies stale or
  missing dependencies, and supports retry/resume without duplicating work.
- **Evidence:** F3; B2, B3, B8; J7, J8, J11; BL-P0-02, BL-P0-05,
  BL-P1-12, BL-P1-14, BL-P1-18.

### O6 - Operate Brains for a team

- **Given:** an Org owner and multiple users.
- **When:** membership, roles, Workspaces, supported configuration, and GitHub linkage
  are managed.
- **Then:** every read/write/realtime/background action is principal- and
  Org-scoped, usage is attributable, and unauthorized entities are not
  enumerable.
- **Evidence:** F0, F8, F9; B2, B7, B9; J7-J11; BL-P0-01,
  BL-P0-02, BL-P1-06, BL-P1-07, BL-P3-01.

### O7 - Diagnose and recover the product

- **Given:** a failed child service, stale coordination handle, damaged SQLite state,
  or incompatible candidate.
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
8. **Evidence level:** code/test presence is E1/E2 only; acceptance requires the
   E3/E4 evidence defined in [QUALITY_GATES.md](../QUALITY_GATES.md).

## Change rule

A change to a promise, user path, route, component, API, control, model,
migration, CLI/MCP family, structured evidence signal, test or backlog owner
must update this specification and [TRACEABILITY.md](TRACEABILITY.md) in the
same change.

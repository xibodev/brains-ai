<!--
last_verified: 2026-08-29T11:26:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 2630f04e31ca47ff93eda1e2b616b3e657b0c877 plus static reconciliation of source inventories with advertised, experimental, and withdrawn lifecycle state; withdrawal implementation not verified; deployment not verified
-->

# Brains Traceability

## Traceability rule

Every product claim must connect:

```text
feature -> persona -> journey/AC -> user action -> UI -> machine interface
        -> control/service -> durable state -> automated evidence -> evidence gap
```

Test names and source files below show E1/E2 presence only. They do not assert that a command passed, a browser journey completed, or a deployment exists.

The user-centered outcome and evidence view of this matrix is
[USER_OUTCOME_SPEC.md](USER_OUTCOME_SPEC.md).

## Core feature matrix

| Feature | Personas | Journeys and ACs | User action | SPA route/component | API / WS | Control/service | Data / migration | Test/evidence presence and gap |
|---|---|---|---|---|---|---|---|---|
| F0 Console foundation | P1, P2, P3 | J1, J11; AC-F0-01..05 | Sign in, inspect Command Center, enter a Workspace, launch a typed action | `/app/command-center`, `/app/workspaces*`, `/app/act`; `AppShell`, `OperatorProvider`, `CommandCenter`, `Workspaces`, `Act` | `/admin/login`; `/v1/operator/overview`; Workspace projections; capability catalog | authz plus Workspace/task/claim/handoff controls | operators, Orgs, Workspaces, coordination rows | Advertised. `test_operator_console_api.py` and `j11-console-clean.spec.ts` exist; exact-candidate browser evidence remains pending. |
| F1 Connect machine | P1, P4 | J2; AC-F1-01..06 | Withdrawn target; no supported action | Current source still declares `/app/labs/runtimes*`, `ConnectMachineModal`, and `Runtimes` | Enrollment, register, heartbeat, status, and assignment routes remain mounted | frozen enrollment, Runtime, and daemon controls | `registered_tools`, `runtimes`, `enrolment_tokens`; 122 | Withdrawn/source compatibility. BL-P0-09 must remove discovery and activation while preserving needed store compatibility. |
| F2 Personas | P1, P2, P7 | J3; AC-F2-01..06 | Withdrawn target; no supported action | Current source still declares `/app/labs/personas*` and `Personas` | Persona CRUD, Sessions, Spawn, and Skill attachment remain mounted | frozen Persona, assignment, Runtime, and Skill controls | `personas`, `agent_sessions`, `persona_skills`; 120/121/138 | Withdrawn/source compatibility. Existing backend/browser tests prove code presence only; BL-P0-09 owns containment. |
| F3 Coordination Sessions and HITL | P1, P4, P5, P7 | J7, J8, J11; AC-F3-01..07 | Coordinate durable work, route/resolve decisions, report friction, checkpoint/resume/end | `/app/governance`, `/app/coordination`, `/app/workspaces/:slug`; withdrawn execution Session UI remains in source | operator feedback/governance/coordination; approval route/escalate/resolve; typed event scope; `/v1/ws`; `/v1/events` | leased coordination Session lifecycle, event taxonomy/scope, mailbox/successor continuity, feedback, decisions, realtime | `agent_sessions`, `events`, `event_contexts`, continuity/routing/feedback rows; 121/123/132/133/142-149 | Advertised coordination plus BL-P1-15/16/20 experiments. Running-agent message/stop and execution supervision are withdrawn; cross-harness E4 remains open. |
| F4 Projects and Issues | P1, P3, P7 | J5, J6, J7; AC-F4-01..07 | Withdrawn target; no supported action | Current source still declares `/app/labs/projects*`, `/app/labs/issues*`, `Projects`, `Issues`, and `Board` | Project, Issue, assignment, comment, and dispatch APIs remain mounted | frozen project/issue/session evidence controls | Project, Issue, Session, and usage rows | Withdrawn/source compatibility. BL-P0-09 owns discovery/activation removal and historical-row compatibility. |
| F5 Pods | P1, P2, P3, P7 | J4, J6; AC-F5-01..04 | Withdrawn target; no supported action | Current source still declares `/app/labs/pods*` and `Pods` | Pod APIs remain mounted | frozen Pod, assignment, and Issue evidence controls | `squads`, `squad_members`, `pod_profiles`, `pod_members`; 104/110/134 | Withdrawn/source compatibility. Legacy rows may remain only where persisted-data compatibility requires them. |
| F6 Onboarding | P1, P2 | J1; AC-F6-01..05 | Withdrawn execution-model target; normal first run opens Command Center | Current source still declares `/app/labs/onboarding`, `Onboarding`, and `Stepper` | Onboarding attempt APIs and composed execution APIs remain mounted | frozen onboarding plus F1/F2/F4 controls | `onboarding_attempts`, `onboarding_steps`; 135 | Withdrawn/source compatibility. BL-P0-09 must replace old browser expectations with Workspace-first clean-state entry. |
| F7 Config | P1, P2, P6 | J9; AC-F7-01..04 | Inspect supported config; edit only approved encrypted/non-secret settings | `/app/operations/config/:section`; `Config` | supported config summaries/writes plus withdrawn provider/bridge fields still in source | config loader, encrypted secure settings, mailer | `secure_settings`; 141 | Advertised/partial. Redaction/write tests exist; multi-process restart and removal of withdrawn activation controls remain open. |
| F8 GitHub linkage | P1, P2, P6 | J6: AC-F8-01..02; J9: AC-F8-03..04 | Configure scope, accept signed delivery, deduplicate and link locally | `/app/operations/config/integrations`; `Config` | public `/hooks/github`; protected compatibility alias; config and delivery APIs | GitHub scope/signature and delivery controls | historical Issue links, `integration_deliveries`; 137 | Advertised/partial. External E4 is absent; BL-P1-19 owns the separate human-approved public defect relay. |
| F9 Org, members, usage | P2, P3, P6 | J10, J11; AC-F9-01..05 | Manage Org/members/roles and inspect scoped usage | `/app/operations/access/:section`; `Settings`, `OrgContext` | Org/member CRUD and scoped usage | Orgs, memberships, usage | identity and usage tables; 050/060/090-092/120/129/130/131/136 | Advertised/partial. API deny matrix exists; browser-session E4 remains absent. |
| F10 Automation | P2, P3, P6, P7 | J10; AC-F10-01..06 | Withdrawn target; no supported action | Current source still declares `/app/labs/automation` and attachment screens | Autopilot/Skill, recurring, job, and generic webhook APIs remain mounted | frozen recurring, Skill, and governed-spawn controls | recurring, Skills, governed actions, webhook rows; 104/110/111/112/125/126/138 | Withdrawn/source compatibility. Existing tests are not an activation contract; BL-P0-09 owns containment. |

## Supporting capability matrix

| Feature | Personas | Journeys and ACs | User/system action | Surface | Control/service | Data | Test/evidence presence and gap |
|---|---|---|---|---|---|---|---|
| B1 Gateway/routing | P1, P4, P7 | J9, J11; AC-B1-01..04 | Withdrawn target; no supported model-proxy action | Model routes, Config fields, and bare Copilot aliases remain in source | frozen router, classifier, provider registry/policy | `traces`, `route_decisions`, `usage_ledger` | Withdrawn/source compatibility. Existing protocol tests prove code presence only; BL-P0-09 must remove discovery and activation. |
| B2 Coordination/MCP | P1, P3, P7 | J5-J8, J10; AC-B2-01..04 | Start/end, task/claim/handoff/message, feedback, topic, help, checkpoint/resume | MCP SSE/stdio, CLI, `/app/coordination`, `/app/workspaces*`, `/app/act` | feedback inbox, Session leases/successors, interest-scoped topics, fenced ephemeral review, typed coordination adapters | coordination, feedback, knowledge, tasks/claims/mailbox/topics/successors/leases/review executions; 139/140/142-149 | Advertised plus BL-P1-15/20 experiments. Async help/topic/continuity suites exist; real-provider review and stdio trust remain incomplete. |
| B3 Context/retrieval | P1, P7 | J6, J7, J11; AC-B3-01..04 | Search Workspace knowledge and bounded repository text/symbols | supported knowledge/local lookup plus semantic/graph MCP/CLI source awaiting containment | knowledge controls and bounded search; frozen `context/*` semantic/graph code | knowledge plus compatibility sources/artifacts/chunks/graph/freshness/memories | Advertised only for knowledge and non-semantic lookup. Semantic, embedding, graph, and external freshness implementations are withdrawn under BL-P0-09/BL-P2-04. |
| B4 Governance/audit | P5, P6, P7 | J8, J10, J11; AC-B4-01..04 | Request, route/escalate, resolve approval, execute supported action, verify audit | `/app/governance`, typed HTTP, MCP/CLI governance families | human-bound approval routing, canonical governed-action contract, decisions, audit | approvals, `approval_routing`, `governed_actions`, `audit_log`, `audit_chain_head`; 070, 126-128, 145 | Advertised/partial. The boundary remains in-process; external harness effects are explicitly outside it. |
| B5 Storage/recovery | P6 | J10, J11; AC-B5-01..05 | Initialize, migrate, back up, verify, diagnose, repair, restore SQLite | `/app/operations` read posture; CLI/MCP mutation contracts | SQLite storage, migrations, integrity, backup, encrypted settings | frozen baseline + numbered deltas, checksummed ledger, `secure_settings`, manifest-2 archives; 141 | Advertised/partial for SQLite. Browser destructive actions and E4 recovery drill remain absent; alternate backend code is withdrawn compatibility. |
| B6 CLI/wiring/service | P1, P4, P6 | J1, J2, J9, J11; AC-B6-01..04 | Install, setup, serve, wire, manage service | `brains-ai`; PyPI; Copilot/Claude/Codex/OpenCode wire adapters; `/app/operations` posture | exact-interpreter verifier, persisted endpoints/probes, native MCP renderers, supervisor bind preflight | package metadata, agent config, service endpoint config, PID state | Advertised/partial. Config-preservation and service tests exist; listener-aware recovery and clean-host E4 remain open. |
| B7 Authenticated external events | P2, P5, P6 | J8, J9; AC-B7-01..04 | Accept signed GitHub delivery; later approve exact public defect payload | `/hooks/github` and Config; generic triggers, relay, bridges, and wa-web remain source containment debt | GitHub scope/signature/delivery controls; planned governed defect relay | `integration_deliveries` plus withdrawn trigger/bridge compatibility rows | Advertised only for GitHub ingress. BL-P1-19 is active outbound-relay work; other webhooks/bridges are withdrawn. |
| B8 Observability/readiness | P4, P6 | J7, J11; AC-B8-01..04 | Probe liveness/readiness, inspect bounded posture, diagnose queues and stale presence | `/app/operations`, `/health`, protected admin/operator projections, logs | health, readiness, queue diagnosis/repair, recovery policy, BL-P1-16 analytics, supervisor | typed events, usage, process log files, compatibility traces | Advertised/partial. `/health` is liveness only; child protocol/listener, scheduler, registry, and cross-process failure coverage remain open. |
| B9 Legacy surfaces | P1, P2, P6 | J9-J11; AC-B9-01..03 | Use `/app`; verify retired HTML is unreachable | `/dashboard*`, legacy `/admin*`, templates, and static assets remain in source | retired dashboard/admin apps plus shared `authz` | shared compatibility DB/config | Withdrawn. Authentication consistency is source evidence, not support; BL-P0-09/BL-P2-01 require zero launch, route, and static exposure. |

## Modern SPA route inventory

All routes are declared in `frontend/src/App.tsx` and served under the `/app` basename.

| Route | Component/behavior | Feature/journey | Current gap |
|---|---|---|---|
| `/app` | Redirect to `/app/command-center` | F0, J1, J11 | The normal console starts from cross-Workspace durable state, not onboarding or an entity list. |
| `/app/command-center` | `CommandCenter` | F0, F3, B2, B8, J11 | Readiness and audit posture are install-admin-only. |
| `/app/workspaces` | `Workspaces` portfolio and first visible control room | F0, F3, B2, J5-J8 | Workspace import has no typed HTTP contract and is disabled. |
| `/app/workspaces/:slug` | `Workspaces` | F0, F3, B2, J5-J8 | `:slug` is consumed; unauthorized and unknown Workspaces are both 404. |
| `/app/coordination` | `OperatorCoordination` | F3, B2, J5-J8 | Task, claim, handoff, comms, knowledge, and patterns are Workspace-attributable; some transitions require Session identity through Act/contextual screens. |
| `/app/governance` | `Governance` | F3, B4, J8, J11 | Resolution and audit verification are native; audit-chain detail is install-admin-only. |
| `/app/operations` | `Operations` | F7, F9, B5, B6, B8, J9-J11 | Host mutations, logs, backup, and restore remain disabled pending typed contracts. |
| `/app/operations/config` | Redirect to `/app/operations/config/general` | F7, J9 | None beyond section contract. |
| `/app/operations/config/:section` | `Config` | F7, F8, B8, J9 | Only approved settings are writable; process-reload state remains explicit. |
| `/app/operations/access` | Redirect to `/app/operations/access/org` | F9, J10 | None beyond section contract. |
| `/app/operations/access/:section` | `Settings` | F9, J10 | Org role enforcement remains a server capability check, not a visual-only gate. |
| `/app/act` | `Act` typed capability launcher | F0, F3, B2, B4-B6, J11 | No generic shell or MCP-call endpoint; missing adapters are labeled and disabled. |
| `/app/labs` | `LabsHome` behind `LabsGate` | F1-F6, F10 | Withdrawn route still declared in source; BL-P0-09 must remove the route and activation switch. |
| `/app/labs/onboarding` | `Onboarding` behind `LabsGate` | F6, J1 | Withdrawn execution-model route; no supported activation. |
| `/app/labs/sessions` | `Sessions` behind `LabsGate` | F3, J7, J8 | Withdrawn execution-supervision route; Workspace coordination remains on normal surfaces. |
| `/app/labs/sessions/:id` | `Sessions` behind `LabsGate` | F3, J7 | `:id` remains unconsumed by the legacy screen. |
| `/app/labs/personas` | `Personas` behind `LabsGate` | F2, J3 | Withdrawn route; no supported activation. |
| `/app/labs/personas/:slug` | `Personas` behind `LabsGate` | F2, J3 | Withdrawn route; `:slug` is also unconsumed by retained source. |
| `/app/labs/pods` | `Pods` behind `LabsGate` | F5, J4 | Withdrawn route; no supported activation. |
| `/app/labs/pods/:slug` | `Pods` behind `LabsGate` | F5, J4 | Withdrawn route still declared in source. |
| `/app/labs/projects` | `Projects` behind `LabsGate` | F4, J5 | Withdrawn route; no supported activation. |
| `/app/labs/projects/:code` | `Projects` behind `LabsGate` | F4, J5, F10 | Withdrawn route still declared in source. |
| `/app/labs/issues` | `Issues` behind `LabsGate` | F4, J6 | Withdrawn route; no supported activation. |
| `/app/labs/issues/:code` | `Issues` behind `LabsGate` | F4, J6 | Withdrawn route still declared in source. |
| `/app/labs/automation` | `Automation` behind `LabsGate` | F10, J10 | Withdrawn route; no supported activation. |
| `/app/labs/runtimes` | `Runtimes` behind `LabsGate` | F1, J2 | Withdrawn route; no supported activation. |
| `/app/labs/runtimes/:slug` | `Runtimes` behind `LabsGate` | F1, J2 | Withdrawn route; `:slug` is also unconsumed by retained source. |
| `/app/inbox` | Redirect to `/app/governance` | F3, J8 | Compatibility redirect; Inbox is no longer primary navigation. |
| `/app/sessions` | Redirect to `/app/labs/sessions` | F3, J7 | Withdrawn redirect still declared; BL-P0-09 must replace it with retirement/not-found behavior. |
| `/app/sessions/:id` | Parameter-preserving redirect to Labs | F3, J7 | Withdrawn compatibility source only. |
| `/app/personas` | Redirect to `/app/labs/personas` | F2, J3 | Withdrawn redirect still declared in source. |
| `/app/personas/:slug` | Parameter-preserving redirect to Labs | F2, J3 | Withdrawn compatibility source only. |
| `/app/pods` | Redirect to `/app/labs/pods` | F5, J4 | Withdrawn redirect still declared in source. |
| `/app/pods/:slug` | Parameter-preserving redirect to Labs | F5, J4 | Withdrawn compatibility source only. |
| `/app/projects` | Redirect to `/app/labs/projects` | F4, J5 | Withdrawn redirect still declared in source. |
| `/app/projects/:code` | Parameter-preserving redirect to Labs | F4, J5 | Withdrawn compatibility source only. |
| `/app/issues` | Redirect to `/app/labs/issues` | F4, J6 | Withdrawn redirect still declared in source. |
| `/app/issues/:code` | Parameter-preserving redirect to Labs | F4, J6 | Withdrawn compatibility source only. |
| `/app/automation` | Redirect to `/app/labs/automation` | F10, J10 | Withdrawn redirect still declared in source. |
| `/app/runtimes` | Redirect to `/app/labs/runtimes` | F1, J2 | Withdrawn redirect still declared in source. |
| `/app/runtimes/:slug` | Parameter-preserving redirect to Labs | F1, J2 | Withdrawn compatibility source only. |
| `/app/onboarding` | Redirect to `/app/labs/onboarding` | F6, J1 | Withdrawn redirect still declared in source. |
| `/app/config` | Redirect to `/app/operations/config/general` | F7, J9 | Compatibility only. |
| `/app/config/:section` | Parameter-preserving redirect to Operations | F7, J9 | Compatibility only. |
| `/app/settings` | Redirect to `/app/operations/access/org` | F9, J10 | Compatibility only. |
| `/app/settings/:section` | Parameter-preserving redirect to Operations | F9, J10 | Compatibility only. |
| `/app/*` | Redirect to Command Center | F0, J11 | Unknown top-level URLs recover to the canonical start; entity not-found behavior remains inside parameterized routes. |

## Native API and realtime family inventory

All native product routers are mounted on the gateway process. Router prefixes make the listed paths `/v1/*` unless noted.

This is a source inventory, not an advertisement list. Rows marked **withdrawn** remain
mounted at HEAD only as BL-P0-09 containment debt and have no supported activation
contract.

| Family | Principal routes | Auth boundary | Feature mapping |
|---|---|---|---|
| Health | `GET /health` | open | B8 |
| Model gateway | withdrawn: models, chat/completions, responses, messages, count_tokens | `require_api_key`; still mounted pending containment | B1 |
| Copilot aliases | withdrawn: `/models`, `/chat/completions`, `/completions`, `/responses` | downstream gateway auth; still rewritten pending containment | B1 |
| Identity/authorization | credential store, principal resolution, capability policy, FastAPI gates (`src/brains/authz`) | not a route family; every native route resolves through it | F1, F9, B2, B9 |
| Operator console | `/v1/operator/*` overview, Workspace control rooms, coordination, governance, operations, capabilities, scoped mutations, audit verification | resolved operator principal plus per-Workspace `org.read`/`org.write`; install operations/global approvals require bootstrap admin | F0, F3, F7, F9, B2, B4-B6, B8 |
| Orgs/members | Org CRUD, member list/add/remove, onboarding aliases | principal + `org.read`/`org.write`/`org.admin`/`org.owner` | F0, F6, F9 |
| Pods | withdrawn: Org Pod list/create; Pod get/dispatch-plan/add member/remove member/set leader/archive | still mounted with prior principal/scope checks pending containment | F5 |
| Onboarding | withdrawn: `GET /v1/onboarding/state`; attempt start; step record; abandon | still mounted with prior operator checks pending containment | F6 |
| Autopilots/Skills | withdrawn: Org list/create; enable/fire; Skill list/create | still mounted with prior principal/scope checks pending containment | F10 |
| Personas | withdrawn: Org list/create; get/patch/archive; sessions/spawn | still mounted with prior principal/scope checks pending containment | F2 |
| Projects | withdrawn: Org list/create; get/patch/archive; board | still mounted with prior principal/scope checks pending containment | F4 |
| Issues | withdrawn: list/create/get/patch/cancel; sessions; evidence; dispatch-plan; assign; transition; comments; dispatch | still mounted with prior principal/scope checks pending containment | F4 |
| GitHub | public `POST /hooks/github`; protected `POST /v1/integrations/github/webhook` compatibility alias | public ingress requires HMAC-SHA256, delivery/event headers and exact repository-to-Org binding; `/v1` alias additionally requires an operator principal | F8 |
| Inbox/coordination | asks, handoffs, approvals, usage, config summary/test, Sessions, Session `message`/`stop`/`commands` | principal + Workspace/Org scope; approval resolution adds separation of duty; usage/config are bootstrap-admin only; Session control refuses Runtime credentials and answers `404` for another Org or a `private` Workspace | F3, F7, F9 |
| Operational health | `GET /v1/admin/readiness`; `GET /v1/admin/queue-health`; `POST /v1/admin/queue-health/repair`; `GET /v1/admin/recovery-policy` | bootstrap-admin only (`principal.is_bootstrap_admin`, same in-handler gate as `/v1/config/summary` and `/v1/usage`) | B5, B6, B8 |
| Runtimes | withdrawn except BL-P1-20 compatibility: register, heartbeat, list/get/patch/offline, enroll, assignments, ephemeral help-review list/claim/complete, Session/event ingest, Session-command poll/claim/ack/release, Session reconcile | Runtime execution/enrollment is withdrawn; current checks remain source safety while BL-P0-09 separates any BL-P1-20 worker compatibility | F1, F3, B2 |
| Realtime | `WS /v1/ws`, `GET /v1/events` | principal from key/cookie, then server-derived topic authorization re-checked per message and on a timer; Runtime credentials refused | F0, F3, J11 |
| Trigger webhooks | withdrawn: `POST /hooks/{slug}` | per-trigger bearer; still mounted pending containment | F10, B7 |
| Relay | `POST /relay/reply`, `/relay/triage` | relay bearer or 503 when unset | B7 |
| Modern browser | `/app`, `/app/{path}`, assets, favicon | SPA index/fallback auth; favicon open | F0-F10 |
| Legacy static | withdrawn: `/static/brains/*` packaged dashboard and admin assets | still open at HEAD pending BL-P0-09/BL-P2-01 removal | B9 |
| Admin | withdrawn HTML: `/admin*`, `/admin/api/*` | still mounted with prior auth pending containment; supported operations must move through `/app` or typed APIs | F7, B9 |
| Framework defaults | `/docs`, `/redoc`, `/openapi.json` | open at HEAD | B8, B9 |

## Client/server mismatches and missing contracts

| ID | Client or product expectation | Current server fact | Affected IDs |
|---|---|---|---|
| UM-01 | `POST /v1/sessions/{id}/message` | Route/source exists, but running-agent delivery is withdrawn. Durable mailbox/topic communication is the advertised path; BL-P0-09 must remove any UI or guidance that implies this command reaches a shipped CLI. | F3, J8, AC-F3-05 |
| UM-02 | `POST /v1/sessions/{id}/stop` | Route/source exists, but Runtime process stop is withdrawn. Coordination Session end/dormancy is separate; BL-P0-09 owns containment. | F3, J8, AC-F3-06 |
| UM-03 | Chat should steer a running agent and survive reload. | Withdrawn. `session_commands` source persistence is compatibility inventory and must not be presented as an unavailable-but-activatable steering feature. | F3, J8, AC-F3-05 |
| UM-04 | Deep entity routes select the route entity. | Workspace deep links are advertised. Execution-model entity routes are withdrawn; any remaining param behavior is source inventory until their routes are removed. | F0, J3-J7, AC-F0-05 |
| UM-05 | Pod CRUD and Persona team semantics. | Withdrawn. Existing membership/routing code and tests do not create a supported user path. | F5, J4 |
| UM-06 | Fresh-state onboarding completes the north-star loop. | Withdrawn. The advertised fresh-state outcome is Command Center/Workspace-first entry, not execution-model onboarding. | F6, J1 |
| UM-07 | Modern Config edits supported state. | Advertised only for approved service/email/secret writes with explicit reload semantics. Provider, bridge, alternate-storage, telemetry, and legacy-admin activation controls are withdrawn containment debt. | F7, J9 |
| UM-08 | GitHub webhook validates GitHub events. | Resolved at E1/E2/E3: the public route requires HMAC-SHA256, delivery/event headers, an exact configured repository-to-Org binding, and durable replay refusal; external E4 operation remains open. | F8, J9 |
| UM-09 | Roles restrict native API access. | Resolved: `owner`/`admin`/`member` are enforced per route against one resolved Org, including the Org-scoped `GET /v1/orgs/{org}/usage`; the residual gap is browser-session E4 evidence for AC-F9-05. | F9, J10, J11 |
| UM-10 | Skills affect Persona or Project execution. | Withdrawn. Skill attachment/context code and migration 138 remain compatibility inventory; reusable advertised guidance is Workspace knowledge and coordination patterns. | F10, J10 |
| UM-11 | Scheduled execution uses the same approval gate. | Withdrawn. No scheduled auto-fire is a supported path, regardless of source-level governed-action integration. | F10, B4, J10 |
| UM-12 | Readiness indicates candidate operability. | Partly resolved: `/health` remains liveness-only; protected readiness reports bounded storage/migration, queue, compatibility-state, and recovery posture. BL-P1-09 must add child protocol/listener, scheduler, registry, package/schema, and supported-transport checks while excluding withdrawn dependencies. | B8, J11 |

## MCP and CLI surface mapping

| Capability | MCP families | CLI families | Feature mapping |
|---|---|---|---|
| Plan/retrieve | advertised bounded repo search and knowledge; semantic, graph, embedding, and freshness tools remain mounted pending containment | advertised plan/repo search; docs index, orient, graph, and external freshness commands are withdrawn | B3 |
| Session/state | advertised start/heartbeat/end, state, event, snapshot, checkpoint/resume; execution message/stop commands are withdrawn | advertised coordination Session/state/event/snapshot; execution message/stop commands are withdrawn | B2, F3 |
| Work coordination | tasks, claims, handoffs, messages, feedback report/enrich/get/list, help lifecycle, presence, topic lifecycle | task, claim, handoff, message, feedback/help/topic lifecycle commands | B2, F3, F4 |
| Knowledge/patterns/tools | knowledge, learn, coordination patterns, tool registry/adoption | learn, pattern, tool, views commands | B2, B3 |
| Automation/webhooks | recurring and generic webhook tools remain mounted pending containment | recurring and jobs commands are withdrawn | F10, B7 |
| Governance/recovery | human feedback triage/promotion, decision route/escalate/resolve, audit, governed actions, backup/restore | feedback and decision lifecycle, audit/governed/backup/restore families | F3, B4, B5 |
| Runtime/operations | supported MCP server transport; Runtime APIs remain mounted pending containment | setup, serve/up, wire, service; `run`, daemon, and withdrawn-feature activation commands are containment debt | F1, B6, B8 |

Current MCP naming is `brains_*`; internal legacy dotted names are not the public
documentation contract. Supported defaults must exclude withdrawn tools regardless of
full/lean/allowlist source modes; BL-P0-09 closes the current mismatch.

## Data and migration mapping

| Domain | Principal tables/state | Migration coverage | Known gap |
|---|---|---|---|
| Identity/scope | `operators`, `orgs`, `org_members`, `workspaces`, `workspace_aliases`, `workspace_memberships`, `api_credentials` | 050, 060, 101, 120, 129, 130, 131, 148 | Every accepted credential is one hashed row bound to a principal; `130` backfills the previously implicit default-Org membership for pre-existing operators and deliberately excludes `daemon-*` operators, which keep authenticating, see nothing, and are reported by `brains-ai credentials doctor`; `131` records each credential's provenance so a rotated admin key or a deleted operator key file revokes exactly the credential it superseded. `148` makes normalized paths aliases of a durable Workspace and converges linked Git worktrees only inside one Org; archived duplicate history remains attributable. |
| Coordination and withdrawn execution compatibility | `registered_tools`, `agent_sessions`, `events`, plus withdrawn `runtimes`, `enrolment_tokens`, `personas`, `projects`, `issues`, `issue_comments`, `skills`, attachment, and `session_commands` rows | 100, 120-125, 133, 138 | Coordination Session/tool/event rows remain advertised. Runtime/Persona/Project/Issue/Skill/execution-command rows are compatibility inventory; source behavior and historical attribution do not authorize activation. BL-P0-09 must preserve only the data needed to open existing stores. |
| Realtime | `realtime_events` | 132 | Session, Issue, approval and Runtime *state* events commit before they are announced, carry a monotonic `event_id` clients resume from, are idempotent for a publisher that supplies a `dedupe_key` (the unique key is enforced by the store; the Session command publisher supplies one derived from the command id and state, so a retried mutation is delivered once logically; the Session lifecycle, Issue, approval and Runtime publishers do not, so their delivery is at-least-once), and record the Org/Workspace the topic resolved to so delivery is filtered on the event's own scope; a replay cursor advances with delivery (ack, then frames, then `replay_complete`) rather than with the store and a batch that read only part of a connection's topics hands over no cursor at all (`covers_connection: false`, `cursor: null`, a reporting-only `batch_cursor`) so the live frames queued below it are still recovered on the next resume, a catch-up batch is written whole ahead of the live frames it overlapped and one publish commits and announces in a single critical section so announcement order matches id order, retention is by row count, and a cursor older than the oldest retained row is answered with an explicit reset. Transcript chunks, the chat echo and `runtime.heartbeat` stay notification-only; the chat echo is a live mirror whose durable counterpart is the `session_commands` row the console backfills over REST, retention and gap detection are install-wide rather than per topic, and live fan-out is per gateway process. |
| Governance | approval/routing, feedback reports/enrichments/promotions, typed `event_contexts`, coordination queues, `help_request_executions`, governed actions and audit chain | 010, 030, 040, 070, 126-128, 139, 140, 142-149 | Feedback promotion is exactly-once and audit-correlated; event category/scope provenance is stored per row and unresolved extension scope is explicit; approval routing never resolves; comms retains async help, fenced read-only ephemeral review, and Session continuity. |
| Encrypted local config | `secure_settings` | 141 | AES-256-GCM ciphertext with Scrypt admin-key derivation, environment precedence, protected redacted APIs, and re-key-before-admin-key-rotation. |
| Teams/automation compatibility | `squads`, `squad_members`, `pod_profiles`, `pod_members`, recurring and generic webhook tables | 104, 110-112, 134 | Withdrawn. Rows remain only to preserve historical references and schema compatibility until separately reviewed removal; no Pod, managed-Skill, recurring, or generic-trigger activation is supported. |
| Onboarding compatibility | `onboarding_attempts`, `onboarding_steps` | 135 | Withdrawn execution-model state. Existing attempts are historical compatibility data, not a resumable current first-run path. |
| Knowledge and retrieval compatibility | knowledge plus sources, artifacts, chunks, chunks_meta, graph nodes/edges | 020, 080, 102, 103 | Workspace knowledge remains advertised. Semantic/embedding/graph/freshness artifacts are withdrawn compatibility data; stable local lookup requires none of them. |
| Usage and routing compatibility | usage ledger and attributions plus withdrawn traces, route decisions, memories, and freshness | 090-092, 136 | Scoped usage remains advertised where attribution is explicit. Model-routing and semantic-freshness rows are compatibility inventory and must not drive normal-product readiness or adoption denominators. |
| Integrations | `integration_deliveries` plus withdrawn `webhook_triggers` and `webhook_deliveries` | 110-112, 137 | Signed GitHub delivery identity/dedupe remains advertised. Generic triggers, relay, approval bridges, and companion delivery are withdrawn compatibility; BL-P1-19 requires a new exact-payload governed contract rather than reactivating them. |
| File state | admin/operator/audit keys, `secrets.env`, runtime overlay, daemon config, OAuth cache, exec transcripts, service PID/log | not SQL-migrated | PID identity/liveness, multi-process reload, ownership, backup scope, and retention need explicit policy. |

Fresh and legacy SQLite stores reach the current schema through one ordered, checksummed
migration contract: the frozen baseline followed by numbered deltas, recorded with
order, checksum, origin, backend, status, attempts, timings, and error. `create_all` is
not on the startup path. Alternate-backend baseline and migration code remains at HEAD
only as withdrawn compatibility inventory; BL-P0-09 must define what is required to
inspect or migrate existing stores without advertising Postgres support.

## Browser and backend evidence inventory

| Journey | Browser file at HEAD | Backend/static evidence | Gap |
|---|---|---|---|
| J1 | `j01-first-run.spec.ts` | Existing F6/onboarding plus sign-in assertions | Spec still encodes withdrawn execution onboarding; BL-P0-09 must replace it with Workspace-first entry and zero withdrawn exposure. |
| J2 | `j02-connect-machine.spec.ts` | F1 enrollment/Runtime/daemon source tests | Entire journey is withdrawn; retain the file only until containment tests replace activation expectations. |
| J3 | `j03-personas.spec.ts` | F2 Persona and Skill-attachment source tests | Entire journey is withdrawn; existing tests are compatibility evidence only. |
| J4 | `j04-pods.spec.ts` | F5 Pod roster/routing source tests | Entire journey is withdrawn; containment and data compatibility are the current acceptance needs. |
| J5 | `j05-project-workspace.spec.ts` | Project plus Workspace scope source tests | Project portion is withdrawn; advertised Workspace identity needs independent journey evidence. |
| J6 | `j06-issues.spec.ts` | F4 Issue/dispatch source tests | Entire Issue/dispatch journey is withdrawn; GitHub linkage must no longer depend on a normal Issue UI. |
| J7 | `j07-sessions.spec.ts`; simulated Runtime in `sandbox/pivot/try` | F3 Session/event source tests | Advertised coordination Session E4 must replace Runtime dispatch/execution expectations. |
| J8 | `j08-governance-session-control.spec.ts` | approval/ask/gate/session-command/WS tests | Stop/running-agent command portions are withdrawn; durable communications and explicit refusal remain to prove. |
| J9 | `j09-config-settings.spec.ts` | F7/F8 plus withdrawn admin/provider source tests | Supported config/GitHub expectations must be separated from withdrawn activation controls. |
| J10 | `j10-automation.spec.ts`; Settings assertion in J9 | F9 plus withdrawn F10/recurring/webhook/Skill source tests | Automation is withdrawn; advertised access/usage/pattern E4 remains absent. |
| J11 | `j11-console-clean.spec.ts` | auth, error, WS, privacy tests | Blocking CI; authorization/accessibility/route contract incomplete. |

The backend acceptance module contains active F0-F10 tests and no live `xfail` decorators at this HEAD. Test presence is E2 only and is not evidence that the tests passed.

## Platform capability inventory

| Capability | Principal code | Product value | Canonical owner | Current limitation |
|---|---|---|---|---|
| Modern SPA | `frontend`, `brains.web.spa` | Advertised Workspace-first operator experience plus withdrawn screens awaiting removal | F0-F10 | Bundle provenance is checked; BL-P0-09 must remove Labs/entity-first/legacy exposure. |
| Gateway/providers | `api/openai.py`, `api/anthropic.py`, `router`, `providers` | Withdrawn compatibility code | B1 | No product activation; BL-P0-09 owns route/help/config removal. |
| Native product API | `api/orgs,runtimes,personas,projects,issues,coordination,ws`, `authz`, `events` | Advertised Org/Workspace coordination plus withdrawn execution endpoints | F0-F10 | RBAC and durable realtime exist; mounted withdrawn families remain containment debt. |
| Runtime execution | `daemon`, `control/assignments`, `exec`, `authz/credentials` | Withdrawn compatibility code | F1, F3, F4 | No enrollment, assignment, process-control, or activation claim. |
| Coordination/MCP | `control`, `mcp` | Multi-agent continuity and collision avoidance | B2 | Advertised surface plus BL-P1-15/16/20 trials; withdrawn tools must be removed from defaults. |
| Retrieval/graph | `context` | Advertised knowledge/local lookup plus withdrawn semantic/graph code | B3 | Semantic, graph, embedding, and external freshness are not activatable product capabilities. |
| Governance/audit | `govern`, decisions, audit; withdrawn exec/bridge integrations remain | Human authority and evidence | B4 | In-process cooperative boundary; external harness effects remain outside it. |
| Storage/recovery | `storage`, `backup` | Supported SQLite state and recovery plus alternate-backend compatibility | B5 | E4 recovery and default FK enforcement remain open; Postgres is withdrawn. |
| CLI/wiring/service | `cli`, `wire`, `service`, supervisor | Install, wire Copilot/Claude/Codex/OpenCode, and operate Brains | B6 | Browser host mutation contracts remain absent. |
| Integrations | GitHub webhook plus withdrawn generic webhooks, bridges, and `services/wa-web` | Signed GitHub linkage; future exact human-approved defect relay | B7, F8 | External GitHub E4 absent; every non-GitHub integration is withdrawn. |
| Observability | health, readiness, typed events, logs; withdrawn OTEL code remains | Diagnose supported operation | B8 | Child protocol/listener, scheduler, registry, and cross-process checks remain open. |
| Legacy web | dashboard/admin/templates | Withdrawn compatibility code | B9 | BL-P0-09/BL-P2-01 require zero route, launch, and static exposure. |
| Containers/ingress/CI | Dockerfiles, compose, deploy, workflows | Reproducible candidate and operations | B5, B6, B8 | Every CI job is blocking, the runtime image constrains the MCP SDK to its supported major version, and its healthcheck covers gateway, dashboard, and MCP; deploy scaffold consistency and candidate-specific hosted evidence remain separate concerns. |

## Explicit traceability gaps

1. No E3/E4 evidence is embedded in canonical docs; run evidence belongs outside this tree.
2. J1-J11 retain dedicated browser specs, but J2-J6 and F10 execution expectations are withdrawn source evidence; BL-P0-09 must replace activation assertions with containment while preserving stable IDs.
3. Running-agent message and stop routes remain matched in source (UM-01..03), but both capabilities are withdrawn and must disappear from advertised UI/CLI/MCP guidance.
4. Native HTTP authorization satisfies F9's enforcement criteria at E2, including Org-scoped usage (`GET /v1/orgs/{org}/usage`); the open part is browser-session E4 evidence for AC-F9-05. Realtime topics are server-derived and persist-before-publish; the residual gap is per-process live fan-out (BL-P0-02).
5. Frontend routes and route parameters, API client calls, mounted server routes and their families, SQLAlchemy entities, the migration corpus, journey specs, acceptance tests, and `AC-*` references are generated from source by `scripts/check_traceability.py` and fail the gate on drift. What is still hand-written is the prose in these tables - gap statements, control/service columns, and the MCP/CLI family mapping - so those can still drift without failing a check.
6. No exact candidate has verified GitHub, backup/restore, or deployment operation. Provider, bridge, and Postgres operation are withdrawn rather than evidence gaps for the advertised product.

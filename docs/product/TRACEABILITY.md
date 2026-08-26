<!--
last_verified: 2026-08-06T19:13:03.000-06:00
verified_by: GitHub Copilot CLI
verification_basis: clean-state corrective candidate based on HEAD 96c2b66fe8adddd9ea29f59f2944e8e702453f27; source inspection and regression coverage for first-run SQLite state creation; public package/GHCR/GitHub publication pipeline verified; corrective publication, external integration operation, and live deployment not verified
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
| F0 Console foundation | P1, P2, P3 | J1, J11; AC-F0-01..05 | Sign in, inspect the brain, enter a Workspace, launch a typed action | `/app/command-center`, `/app/workspaces*`, `/app/act`; `AppShell`, `OperatorProvider`, `CommandCenter`, `Workspaces`, `Act` | `/admin/login`; `/v1/operator/overview`; Workspace projections; capability catalog | authz plus workspace/task/claim/handoff controls | operators, Orgs, Workspaces, coordination rows | `test_operator_console_api.py` and `j11-console-clean.spec.ts`; exact-candidate browser evidence remains pending. |
| F1 Connect machine | P1, P4 | J2; AC-F1-01..06 | Mint command, redeem, inspect/drain Runtime | Labs only: `/app/labs/runtimes*`; `ConnectMachineModal`, `Runtimes` | `/v1/runtimes/enrol`; public redeem; Runtime lifecycle routes | enrolment, runtimes, daemon detection/heartbeat | `registered_tools`, `runtimes`, `enrolment_tokens`; 122 | Backend evidence exists; the browser screen requires `BRAINS_UI_LABS=1`, and a real daemon path remains unproven. |
| F2 Personas | P1, P2, P7 | J3; AC-F2-01..06 | Create, bind, edit, archive, Spawn | Labs only: `/app/labs/personas*`; `Personas` | Persona CRUD, sessions, spawn; Skill attachment | personas, assignments, runtimes, skills | `personas`, `agent_sessions`, `persona_skills`; 120/121/138 | Backend and gated browser specs exist; normal-console outcome and empty-body executable Spawn remain gaps. |
| F3 Sessions and HITL | P1, P4, P5, P7 | J7, J8, J11; AC-F3-01..07 | Inspect contextual Sessions, resolve decisions, coordinate continuity, stop when supported | `/app/governance`, `/app/coordination`, `/app/workspaces/:slug`; Labs Session evidence at `/app/labs/sessions*` | operator governance/coordination projections; Session commands; approvals/asks; `/v1/ws`; `/v1/events` | sessions, mailbox, successor continuity, decisions, governance, realtime | `agent_sessions`, `session_successors`, mailbox, commands/events/approvals; 121/123/132/133/142 | Focused control/API tests exist. No permanent chat dock is promised; shipped CLI interactive input remains unsupported. |
| F4 Projects and Issues | P1, P3, P7 | J5, J6, J7; AC-F4-01..07 | Create work, assign, dispatch, inspect evidence | Labs only: `/app/labs/projects*`, `/app/labs/issues*`; `Projects`, `Issues`, `Board` | Project and Issue APIs | project/issue/session evidence controls | project, issue, Session and usage rows | Backend and gated browser specs exist; complete real-Runtime Pod execution remains unproven. |
| F5 Pods | P1, P2, P3, P7 | J4, J6; AC-F5-01..04 | Create Pod, manage Persona roster/leader, assign, archive | Labs only: `/app/labs/pods*`; `Pods` | Pod APIs | pods, assignments, issue evidence | `squads`, `squad_members`, `pod_profiles`, `pod_members`; 104/110/134 | Backend and gated browser specs exist; normal-console and real multi-Persona execution evidence are absent. |
| F6 Onboarding | P1, P2 | J1; AC-F6-01..05 | Org -> machine -> Persona -> work -> dispatch | Labs only: `/app/labs/onboarding`; `Onboarding`, `Stepper` | onboarding attempt APIs plus composed execution APIs | onboarding plus F1/F2/F4 controls | `onboarding_attempts`, `onboarding_steps`; 135 | Durable flow evidence exists, but automatic normal-console routing was removed; clean-host and real-Runtime evidence remain gaps. |
| F7 Config | P1, P2, P6 | J9; AC-F7-01..04 | Inspect effective config; edit approved encrypted/non-secret settings | `/app/operations/config/:section`; `Config` | config summary/probes; `/v1/admin/configuration/*` | config loader, encrypted secure settings, mailer | `secure_settings`; 141 | `test_secure_configuration.py` covers redaction and writes; multi-process restart remains required. |
| F8 GitHub linkage | P1, P2, P6 | J6: AC-F8-01..02; J9: AC-F8-03..04 | Configure scope, deliver signed event, link and reconcile | `/app/operations/config/integrations`; `Config` | public `/hooks/github`; protected compatibility alias; config and delivery APIs | GitHub scope/signature and delivery controls | Issue state, `integration_deliveries`; 137 | Controlled external and browser E4 evidence remain absent. |
| F9 Org, members, usage | P2, P3, P6 | J10, J11; AC-F9-01..05 | Manage Org/members/roles and inspect scoped usage | `/app/operations/access/:section`; `Settings`, `OrgContext` | Org/member CRUD and scoped usage | orgs, memberships, usage | identity and usage tables; 050/060/090-092/120/129/130/131/136 | API deny matrix exists; browser-session E4 evidence remains absent. |
| F10 Automation | P2, P3, P6, P7 | J10; AC-F10-01..06 | Define and fire repeatable work | Labs only: `/app/labs/automation`; gated Persona/Project attachment screens | Autopilot/Skill APIs; recurring MCP; webhooks | recurring, skills, governed spawn | recurring, skills, governed actions, webhook rows; 104/110/111/112/125/126/138 | Backend and gated browser specs exist; the normal console does not promise this unfinished model. |

## Supporting capability matrix

| Feature | Personas | Journeys and ACs | User/system action | Surface | Control/service | Data | Test/evidence presence and gap |
|---|---|---|---|---|---|---|---|
| B1 Gateway/routing | P1, P4, P7 | J9, J11; AC-B1-01..04 | List models, send OpenAI/Anthropic requests, select exact/tier/auto model | `/v1/models`, chat, responses, messages, count_tokens; Config readiness/tier view; bare Copilot aliases | router resolver/classifier, provider registry/policy, admin provider status | `traces`, `route_decisions`, `usage_ledger` | Router/provider/facade and F7 acceptance tests distinguish direct/tier/auto and simulated/configured/reachable/degraded state; `/v1/responses` is not a full Responses contract and live-provider recovery remains unproven. |
| B2 Coordination/MCP | P1, P3, P7 | J5-J8, J10; AC-B2-01..04 | Start/end, discover, task, claim, handoff, message, topic, checkpoint, resume/link successor | MCP SSE/stdio, CLI, `/app/coordination`, `/app/workspaces*`, `/app/act` | coordination controls with scoped typed HTTP adapters | coordination, knowledge, task, claim, mailbox, topics, `session_successors`; 139/140/142 | Agent-comms, successor, and operator API suites cover scope and continuity; stdio trust and per-tool destructive governance remain incomplete. |
| B3 Context/retrieval | P1, P7 | J6, J7, J11; AC-B3-01..04 | Index, orient, semantic search, graph query, freshness check | MCP/CLI only | `context/*`, prewarm | sources/artifacts/chunks, graph, freshness, memories | Repo/semantic/graph/freshness tests; Python-only graph and optional embedding dependency. |
| B4 Governance/audit | P5, P6, P7 | J8, J10, J11; AC-B4-01..04 | Request/resolve approval, execute, verify audit | `/app/governance`, typed HTTP, MCP/CLI governance families, relay | canonical governed-action contract, decisions, bridges, audit | approvals, `governed_actions`, `audit_log`, `audit_chain_head`; 070, 126, 127, 128 | Governance tests cover the decision spine and chain; the boundary remains in-process, so out-of-band third-party effects stay outside it. |
| B5 Storage/recovery | P6 | J10, J11; AC-B5-01..05 | Initialize, migrate, back up, verify, diagnose, repair, restore | `/app/operations` read posture; CLI/MCP mutation contracts | storage, migrations, integrity, backup, encrypted settings | frozen baseline + numbered deltas, checksummed ledger, `secure_settings`, manifest-2 archives; 141 | Browser backup/restore remains disabled until typed destructive contracts exist. |
| B6 CLI/wiring/service | P1, P4, P6 | J1, J2, J9, J11; AC-B6-01..04 | Install, setup, serve, wire, manage service | `brains-ai`; PyPI; Copilot/Claude/Codex/OpenCode wire adapters; `/app/operations` posture | exact-interpreter verifier, native MCP renderers, probes, supervisor | package metadata, agent config and PID state | `test_wire.py` covers native schema, preservation, conflict, permissions, idempotency, status and unwire; browser host mutations remain disabled until typed preview/confirmation contracts exist. |
| B7 Webhooks/bridges | P2, P5, P6 | J8, J9; AC-B7-01..04 | Deliver trigger/reply/triage, send bridge message | `/hooks/{slug}`, `/relay/*`, bridge plugins, wa-web `/send` | webhooks, relay, bridges, wa-web | trigger/delivery rows plus bridge state | Webhook/bridge tests; external operation and third-party behavior unverified. |
| B8 Observability/readiness | P4, P6 | J7, J11; AC-B8-01..04 | Probe liveness/readiness, inspect bounded operational posture, diagnose queues | `/app/operations`, `/health`, protected admin/operator projections, logs, optional OTLP | health, readiness, queue diagnosis/repair, recovery policy, supervisor, runtime sweep | traces, usage, process log files | `/health` remains liveness only; service logs and deployment-specific recovery evidence are not browser contracts yet. |
| B9 Legacy surfaces | P1, P2, P6 | J9-J11; AC-B9-01..03 | Use or retire dashboard/admin workflows | `/dashboard*`, `/admin*`, templates | dashboard and admin apps, `authz` | shared DB and config files | Dashboard/admin tests plus cookie-binding, Runtime/scopeless console refusal and install-admin restriction cases in `test_authz_identity_scope.py`; every mounted surface now resolves the same credential store to the same principal, `/admin` configuration is restricted to the install administrator, and the remaining question is the support/retirement boundary. |

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
| `/app/labs` | `LabsHome` behind `LabsGate` | F1-F6, F10 | Requires `BRAINS_UI_LABS=1`; otherwise fails closed to Command Center. |
| `/app/labs/onboarding` | `Onboarding` behind `LabsGate` | F6, J1 | Experimental execution-model journey, not the normal console start. |
| `/app/labs/sessions` | `Sessions` behind `LabsGate` | F3, J7, J8 | Session-centric supervision remains experimental and contextual. |
| `/app/labs/sessions/:id` | `Sessions` behind `LabsGate` | F3, J7 | `:id` remains unconsumed by the legacy screen. |
| `/app/labs/personas` | `Personas` behind `LabsGate` | F2, J3 | Experimental execution model. |
| `/app/labs/personas/:slug` | `Personas` behind `LabsGate` | F2, J3 | `:slug` remains unconsumed by the legacy screen. |
| `/app/labs/pods` | `Pods` behind `LabsGate` | F5, J4 | Experimental execution model. |
| `/app/labs/pods/:slug` | `Pods` behind `LabsGate` | F5, J4 | `:slug` selects the Pod and unknown values render not-found. |
| `/app/labs/projects` | `Projects` behind `LabsGate` | F4, J5 | Experimental execution model. |
| `/app/labs/projects/:code` | `Projects` behind `LabsGate` | F4, J5, F10 | `:code` selects the Project and unknown values render not-found. |
| `/app/labs/issues` | `Issues` behind `LabsGate` | F4, J6 | Experimental execution model. |
| `/app/labs/issues/:code` | `Issues` behind `LabsGate` | F4, J6 | `:code` opens the Issue and unknown values render not-found. |
| `/app/labs/automation` | `Automation` behind `LabsGate` | F10, J10 | Scheduled execution remains outside the normal operator surface. |
| `/app/labs/runtimes` | `Runtimes` behind `LabsGate` | F1, J2 | Runtime enrollment/lifecycle journey remains experimental. |
| `/app/labs/runtimes/:slug` | `Runtimes` behind `LabsGate` | F1, J2 | `:slug` remains unconsumed by the legacy screen. |
| `/app/inbox` | Redirect to `/app/governance` | F3, J8 | Compatibility redirect; Inbox is no longer primary navigation. |
| `/app/sessions` | Redirect to `/app/labs/sessions` | F3, J7 | Compatibility redirect still fails closed when Labs is off. |
| `/app/sessions/:id` | Parameter-preserving redirect to Labs | F3, J7 | Compatibility only. |
| `/app/personas` | Redirect to `/app/labs/personas` | F2, J3 | Compatibility redirect still fails closed when Labs is off. |
| `/app/personas/:slug` | Parameter-preserving redirect to Labs | F2, J3 | Compatibility only. |
| `/app/pods` | Redirect to `/app/labs/pods` | F5, J4 | Compatibility redirect still fails closed when Labs is off. |
| `/app/pods/:slug` | Parameter-preserving redirect to Labs | F5, J4 | Compatibility only. |
| `/app/projects` | Redirect to `/app/labs/projects` | F4, J5 | Compatibility redirect still fails closed when Labs is off. |
| `/app/projects/:code` | Parameter-preserving redirect to Labs | F4, J5 | Compatibility only. |
| `/app/issues` | Redirect to `/app/labs/issues` | F4, J6 | Compatibility redirect still fails closed when Labs is off. |
| `/app/issues/:code` | Parameter-preserving redirect to Labs | F4, J6 | Compatibility only. |
| `/app/automation` | Redirect to `/app/labs/automation` | F10, J10 | Compatibility redirect still fails closed when Labs is off. |
| `/app/runtimes` | Redirect to `/app/labs/runtimes` | F1, J2 | Compatibility redirect still fails closed when Labs is off. |
| `/app/runtimes/:slug` | Parameter-preserving redirect to Labs | F1, J2 | Compatibility only. |
| `/app/onboarding` | Redirect to `/app/labs/onboarding` | F6, J1 | Compatibility redirect still fails closed when Labs is off. |
| `/app/config` | Redirect to `/app/operations/config/general` | F7, J9 | Compatibility only. |
| `/app/config/:section` | Parameter-preserving redirect to Operations | F7, J9 | Compatibility only. |
| `/app/settings` | Redirect to `/app/operations/access/org` | F9, J10 | Compatibility only. |
| `/app/settings/:section` | Parameter-preserving redirect to Operations | F9, J10 | Compatibility only. |
| `/app/*` | Redirect to Command Center | F0, J11 | Unknown top-level URLs recover to the canonical start; entity not-found behavior remains inside parameterized routes. |

## Native API and realtime family inventory

All native product routers are mounted on the gateway process. Router prefixes make the listed paths `/v1/*` unless noted.

| Family | Principal routes | Auth boundary | Feature mapping |
|---|---|---|---|
| Health | `GET /health` | open | B8 |
| Model gateway | models, chat/completions, responses, messages, count_tokens | `require_api_key` | B1 |
| Copilot aliases | `/models`, `/chat/completions`, `/completions`, `/responses` | downstream gateway auth | B1 |
| Identity/authorization | credential store, principal resolution, capability policy, FastAPI gates (`src/brains/authz`) | not a route family; every native route resolves through it | F1, F9, B2, B9 |
| Operator console | `/v1/operator/*` overview, Workspace control rooms, coordination, governance, operations, capabilities, scoped mutations, audit verification | resolved operator principal plus per-Workspace `org.read`/`org.write`; install operations/global approvals require bootstrap admin | F0, F3, F7, F9, B2, B4-B6, B8 |
| Orgs/members | Org CRUD, member list/add/remove, onboarding aliases | principal + `org.read`/`org.write`/`org.admin`/`org.owner` | F0, F6, F9 |
| Pods | Org Pod list/create; Pod get/dispatch-plan/add member/remove member/set leader/archive | principal + `org.read`/`org.write`/`org.admin`; membership and leadership are Personas in the Pod's own Org | F5 |
| Onboarding | `GET /v1/onboarding/state`; attempt start; step record; abandon | principal + operator identity; an attempt belongs to the operator that started it | F6 |
| Autopilots/Skills | Org list/create; enable/fire; Skill list/create | principal + `org.read`/`org.write`; enable/fire require `org.admin` | F10 |
| Personas | Org list/create; get/patch/archive; sessions/spawn | principal + `org.read`/`org.write`; bound Runtime/Issue must share the Org | F2 |
| Projects | Org list/create; get/patch/archive; board | principal + `org.read`/`org.write`; Workspace/Pod must share the Org | F4 |
| Issues | list/create/get/patch/cancel; sessions; evidence; dispatch-plan; assign; transition; comments; dispatch | principal + `org.read`/`org.write`; cross-Org list filtered, cross-Org entity 404; evidence filters Sessions the principal cannot read and reports how many | F4 |
| GitHub | public `POST /hooks/github`; protected `POST /v1/integrations/github/webhook` compatibility alias | public ingress requires HMAC-SHA256, delivery/event headers and exact repository-to-Org binding; `/v1` alias additionally requires an operator principal | F8 |
| Inbox/coordination | asks, handoffs, approvals, usage, config summary/test, Sessions, Session `message`/`stop`/`commands` | principal + Workspace/Org scope; approval resolution adds separation of duty; usage/config are bootstrap-admin only; Session control refuses Runtime credentials and answers `404` for another Org or a `private` Workspace | F3, F7, F9 |
| Operational health | `GET /v1/admin/readiness`; `GET /v1/admin/queue-health`; `POST /v1/admin/queue-health/repair`; `GET /v1/admin/recovery-policy` | bootstrap-admin only (`principal.is_bootstrap_admin`, same in-handler gate as `/v1/config/summary` and `/v1/usage`) | B5, B6, B8 |
| Runtimes | register, heartbeat, list/get/patch/offline, enroll, assignments, Session/event ingest, Session-command poll/claim/ack/release, Session reconcile | Runtime-narrow credential for its own machine, or operator `org.read`/`org.write`/`org.admin`; a Session command may only be listed, claimed and settled by the Runtime its Session is bound to - sharing a machine is not ownership, the machine recorded on the row is a diagnostic rather than the ownership test, and a command whose Session is unbound belongs to the local process; release accepts only the current holder; token redeem unauthenticated | F1, F3 |
| Realtime | `WS /v1/ws`, `GET /v1/events` | principal from key/cookie, then server-derived topic authorization re-checked per message and on a timer; Runtime credentials refused | F0, F3, J11 |
| Trigger webhooks | `POST /hooks/{slug}` | per-trigger bearer | F10, B7 |
| Relay | `POST /relay/reply`, `/relay/triage` | relay bearer or 503 when unset | B7 |
| Modern browser | `/app`, `/app/{path}`, assets, favicon | SPA index/fallback auth; favicon open | F0-F10 |
| Legacy static | `/static/brains/*` packaged dashboard and admin assets | open | B9 |
| Admin | `/admin*`, `/admin/api/*` | public login form; sign-in requires a key that resolves to an active credential; pages require a resolved principal | F7, B9 |
| Framework defaults | `/docs`, `/redoc`, `/openapi.json` | open at HEAD | B8, B9 |

## Client/server mismatches and missing contracts

| ID | Client or product expectation | Current server fact | Affected IDs |
|---|---|---|---|
| UM-01 | `POST /v1/sessions/{id}/message` | Resolved: the route exists, authorizes Org/Workspace scope, records a `session_commands` row before delivery, and is idempotent per `operation_id`. A message to a shipped CLI is settled `failed`/`unsupported` because none is launched with an open input channel. | F3, J8, AC-F3-05 |
| UM-02 | `POST /v1/sessions/{id}/stop` | Resolved: the route exists, is idempotent per Session while an attempt is open or after one succeeded, mints a new durable attempt when an attempt failed terminally and the Session is still running, reaches the exact process handle its owning consumer holds, and only stamps terminal state when the process is proven gone. | F3, J8, AC-F3-06 |
| UM-03 | Chat should steer a running agent and survive reload. | Partly resolved: `session_commands` is durable, ordered, leased and replayed by `GET /v1/sessions/{id}/commands`, and the daemon claims and acknowledges it. Steering a *shipped* CLI is still not possible, because their launch shapes close stdin; the console shows that as a blocked composer with the stated reason. | F3, J8, AC-F3-05 |
| UM-04 | Deep entity routes select the route entity. | Pods and Issues consume their route params; Sessions, Personas, Projects, and Runtimes still generally ignore them. | F0, J3-J7, AC-F0-05 |
| UM-05 | Pod CRUD and Persona team semantics. | Resolved at E1/E2: native membership and leadership use Personas, removal/archive exist, and deterministic dispatch exposes its candidate reasoning; E4 remains open. | F5, J4 |
| UM-06 | Fresh-state onboarding completes the north-star loop. | Resolved at E1/E2: automatic durable routing resumes to a real Session or an explicit blocked state; clean-state E4 remains open. | F6, J1 |
| UM-07 | Modern Config edits supported state. | Resolved by exclusion: the modern console explicitly promises read-only inspection and bounded probes; legacy writes remain separate and require every process to restart before the change is treated as active. | F7, J9 |
| UM-08 | GitHub webhook validates GitHub events. | Resolved at E1/E2/E3: the public route requires HMAC-SHA256, delivery/event headers, an exact configured repository-to-Org binding, and durable replay refusal; external E4 operation remains open. | F8, J9 |
| UM-09 | Roles restrict native API access. | Resolved: `owner`/`admin`/`member` are enforced per route against one resolved Org, including the Org-scoped `GET /v1/orgs/{org}/usage`; the residual gap is browser-session E4 evidence for AC-F9-05. | F9, J10, J11 |
| UM-10 | Skills affect Persona or Project execution. | Resolved: `persona_skills`/`project_skills` (138) attach with idempotent re-attach and cross-Org refusal; `control.skills.resolve_context_for_session` composes deduplicated, provenance-carrying context that `exec.runner.run_session` prepends to a spawned agent's actual prompt (not merely returned in an API response). | F10, J10 |
| UM-11 | Scheduled execution uses the same approval gate. | Recurring spawn is classified, approved and recorded through the governed-action contract, but the boundary is cooperative and in-process. | F10, B4, J10 |
| UM-12 | Readiness indicates candidate operability. | Partly resolved: `/health` remains open and liveness-only (200 always); `GET /v1/admin/readiness` is a separate, protected, bootstrap-admin-only surface reporting one overall ready/degraded verdict plus bounded storage/migration, queue, Runtime-lifecycle, and recovery-policy component state. Live provider readiness is deliberately excluded from this contract (BL-P1-11). | B8, J11 |

## MCP and CLI surface mapping

| Capability | MCP families | CLI families | Feature mapping |
|---|---|---|---|
| Plan/retrieve | plan, context pack, repo/semantic search, graph, freshness, memory | plan, repo/docs index/search, orient, graph, check-source | B3 |
| Session/state | start/end, state, event, snapshot, checkpoint/resume, session message/stop/commands | session-start/end, sessions, state, event-append/events, snapshot, session-message/session-stop/session-commands | B2, F3 |
| Work coordination | tasks, claims, handoffs, messages, help, presence | task, workspace-claim, handoff, message commands | B2, F4 |
| Knowledge/patterns/tools | knowledge, learn, patterns, tool registry/adoption | learn, pattern, tool, views commands | B2, B3 |
| Automation/webhooks | recurring and webhook tools | recurring and jobs commands | F10, B7 |
| Governance/recovery | decisions, audit, governed actions, backup/restore | decision, audit, governed-list, governed-sweep, audit-adopt, backup/restore, `db` migration + integrity family, exec-session | F3, B4, B5 |
| Runtime/operations | limited through native API; MCP server transport | setup, serve/up, wire, run, service, daemon, features | F1, B6, B8 |

Current MCP naming is `brains_*`; internal legacy dotted names are not the public documentation contract. Full mode exposes a broad mutation surface, lean mode exposes a curated subset, and an explicit allowlist is supported.

## Data and migration mapping

| Domain | Principal tables/state | Migration coverage | Known gap |
|---|---|---|---|
| Identity/scope | `operators`, `orgs`, `org_members`, `workspaces`, `workspace_memberships`, `api_credentials` | 050, 060, 101, 120, 129, 130, 131 | Every accepted credential is one hashed row bound to a principal; `130` backfills the previously implicit default-Org membership for pre-existing operators and deliberately excludes `daemon-*` operators, which keep authenticating, see nothing, and are reported by `brains-ai credentials doctor`; `131` records each credential's provenance so a rotated admin key or a deleted operator key file revokes exactly the credential it superseded. |
| Runtime/product | `registered_tools`, `runtimes`, `enrolment_tokens`, `personas`, `projects`, `issues`, `issue_comments`, `agent_sessions`, `events`, `skills`, `persona_skills`, `project_skills`, `session_commands` | 100, 120-125, 133, 138 | Operator messages and stops are durable, ordered, idempotent per operation key, leased to one consumer and settled with the observed outcome; delivery to a shipped agent CLI is still impossible because none is launched with an open input channel, so those messages settle `failed`/`unsupported`. Product links are optional; historical Session state was defaulted to `running` and is reconciled only where a lifecycle event proves it; `persona_skills`/`project_skills` (138) attach a Skill to a Persona or Project (unique per pair, idempotent re-attach) and `control.skills.resolve_context_for_session` composes a deduplicated, provenance-carrying context that `exec.runner.run_session` prepends to the spawned agent's actual prompt. |
| Realtime | `realtime_events` | 132 | Session, Issue, approval and Runtime *state* events commit before they are announced, carry a monotonic `event_id` clients resume from, are idempotent for a publisher that supplies a `dedupe_key` (the unique key is enforced by the store; the Session command publisher supplies one derived from the command id and state, so a retried mutation is delivered once logically; the Session lifecycle, Issue, approval and Runtime publishers do not, so their delivery is at-least-once), and record the Org/Workspace the topic resolved to so delivery is filtered on the event's own scope; a replay cursor advances with delivery (ack, then frames, then `replay_complete`) rather than with the store and a batch that read only part of a connection's topics hands over no cursor at all (`covers_connection: false`, `cursor: null`, a reporting-only `batch_cursor`) so the live frames queued below it are still recovered on the next resume, a catch-up batch is written whole ahead of the live frames it overlapped and one publish commits and announces in a single critical section so announcement order matches id order, retention is by row count, and a cursor older than the oldest retained row is answered with an explicit reset. Transcript chunks, the chat echo and `runtime.heartbeat` stay notification-only; the chat echo is a live mirror whose durable counterpart is the `session_commands` row the console backfills over REST, retention and gap detection are install-wide rather than per topic, and live fan-out is per gateway process. |
| Governance | approval, handoff, task, claim, mailbox, snapshot, pattern, help, checkpoint, `topic_posts`, `session_successors`, `governed_actions`, `audit_log`, `audit_chain_head` | 010, 030, 040, 070, 126-128, 139, 140, 142 | Audit/governance semantics remain transactional/fenced. Agent comms adds harness help, topic inbox blast and explicit successor continuity; migration 140 narrowly converges one pre-release draft checksum, and every other edited migration fails closed. |
| Encrypted local config | `secure_settings` | 141 | AES-256-GCM ciphertext with Scrypt admin-key derivation, environment precedence, protected redacted APIs, and re-key-before-admin-key-rotation. |
| Teams/automation | `squads`, `squad_members`, `pod_profiles`, `pod_members`, recurring and webhook tables | 104, 110-112, 134 | Pod membership and leadership are Personas: `pod_profiles` records the Pod's Org and its one leader Persona, `pod_members` is the roster, and the legacy `squads` row is retained because `issues.assignee_pod_id`/`projects.assignee_pod_id` reference it and the legacy workspace task routing still reads its operator columns. `squads.leader_operator_id` is therefore the legacy row's owner, reported as `legacy_leader_operator`, not the Pod's leader. The 134 backfill converts a legacy operator membership only when that operator resolves to exactly one active Persona in the Pod's Org; the rest stay in `squad_members` and are reported as legacy operator members with the reason they could not be resolved, and are never dispatched. Governed scheduling remains incomplete. |
| Onboarding | `onboarding_attempts`, `onboarding_steps` | 135 | The attempt is server state derived from real rows on every read, `(attempt_id, step)` is unique so a retry updates one row, a deferred machine is a recorded outcome, and `completed` is stamped only when a Session exists for the attempt's Issue. Attempts are per operator and are not shared across operators. |
| Retrieval/graph | sources, artifacts, chunks, chunks_meta, graph nodes/edges, knowledge | 020, 080, 102, 103 | Per-artifact/chunk hashes exist; build-level provenance, graph-to-source linkage, freshness, and operator-visible readiness are absent. |
| Usage/routing | traces, route decisions, memories, freshness, usage ledger, `usage_attributions` | 090-092, 136 | `usage_attributions.usage_entry_id` is unique, so a gateway call is attributed to a Session/Issue/Persona/Org exactly once and an Issue rollup cannot double-count it. Attribution is opt-in: only a caller that sends `X-Brains-Session` is attributed, an unknown Session id attributes nothing, and the rollup reports unattributed calls as unattributed rather than spreading them. `GET /v1/orgs/{org}/usage` joins the ledger through `usage_attributions` filtered on `org_id`, so it is readable by any principal with `org.read` on that Org (not bootstrap-admin-only like the install-wide `/v1/usage`) and never returns another Org's or an unattributed call. |
| Integrations | `webhook_triggers`, `webhook_deliveries`, `integration_deliveries` | 110-112, 137 | Generic triggers use hashed bearer tokens and per-trigger dedupe. GitHub and relay deliveries reserve a durable key and expiring lease before effects, reclaim only explicit failures atomically, and fence settlement by attempt; an expired `processing` attempt remains fenced until a bootstrap admin confirms the worker is gone and releases it to `failed`. Approval bridge sends retain one outcome per approval and bridge. Relay callers should supply `X-Dedupe-Key`; otherwise identical bodies dedupe only within a five-minute window. External operation, durable companion-device dedupe, and broader outbound governance remain open. |
| File state | admin/operator/audit keys, `secrets.env`, runtime overlay, daemon config, OAuth cache, exec transcripts, service PID/log | not SQL-migrated | PID identity/liveness, multi-process reload, ownership, backup scope, and retention need explicit policy. |

Fresh databases and legacy stores reach the current schema through one ordered, checksummed migration contract: the frozen per-backend baseline DDL followed by the numbered deltas, recorded in `schema_versions` with order, checksum, checksum origin, backend, status, attempts, timings, and error. `create_all` is not on the startup path. Non-SQLite backends execute the baseline and record every SQLite-only delta as `skipped` with a reason instead of as applied; a migration with no backend implementation that is not baseline-covered is refused. A pre-checksum ledger row is adopted backend-aware: it becomes `skipped`/`legacy-unproven` when this backend has no implementation, so a later backend delta still runs, and it is never re-executed when an implementation exists. The Postgres baseline's deferred foreign keys are guarded on `pg_constraint` relation/column identity rather than constraint name, so a legacy `create_all` store gains no duplicate constraints. The remaining gap is backend parity proven by executed per-delta equivalents on a live Postgres.

## Browser and backend evidence inventory

| Journey | Browser file at HEAD | Backend/static evidence | Gap |
|---|---|---|---|
| J1 | `j01-first-run.spec.ts` | F6 acceptance/onboarding tests for guard, resume, retry, scope, blocking, and Session-linked completion | Clean-host install and real external Runtime remain unproven. |
| J2 | `j02-connect-machine.spec.ts` | F1 acceptance, enrollment/runtime/daemon tests | Real daemon path not proven. |
| J3 | `j03-personas.spec.ts` | F2 acceptance/persona tests, `test_skill_attachments.py` | Empty-body executable Spawn gap. |
| J4 | `j04-pods.spec.ts` | F5 acceptance and Persona-oriented Pod roster/routing/state tests | Complete archive/removal and Pod-assigned real-Runtime journey evidence absent. |
| J5 | `j05-project-workspace.spec.ts` | Project, Workspace scope, and API tests | Pause/archive journey remains unproven. |
| J6 | `j06-issues.spec.ts` | F4 acceptance and Issue evidence/dispatch tests | Complete reload/failure and Pod real-Runtime evidence absent. |
| J7 | `j07-sessions.spec.ts`; simulated Runtime in `sandbox/pivot/try` | F3 acceptance/session/event and harness-safety tests | Real Runtime lifecycle reconciliation remains unproven. |
| J8 | `j08-governance-session-control.spec.ts` | approval/ask/gate/session-command/WS tests | Interactive follow-up to shipped Copilot remains unsupported. |
| J9 | `j09-config-settings.spec.ts` | F7/F8 tests and admin/provider tests | External integrations and write semantics unproven. |
| J10 | `j10-automation.spec.ts`; Settings assertion in J9 | F9/F10, recurring, webhook, Skill tests, `test_skill_attachments.py` | Browser-session E4 evidence for scoped usage and Skill-attachment UI remains absent. |
| J11 | `j11-console-clean.spec.ts` | auth, error, WS, privacy tests | Blocking CI; authorization/accessibility/route contract incomplete. |

The backend acceptance module contains active F0-F10 tests and no live `xfail` decorators at this HEAD. Test presence is E2 only and is not evidence that the tests passed.

## Platform capability inventory

| Capability | Principal code | Product value | Canonical owner | Current limitation |
|---|---|---|---|---|
| Modern SPA | `frontend`, `brains.web.spa` | Main Brains operator experience | F0-F10 | The committed bundle is rebuilt and compared byte-for-byte by the SPA gate; the residual gaps are per-screen behavior, not build provenance. |
| Gateway/providers | `api/openai.py`, `api/anthropic.py`, `router`, `providers` | Model compatibility and faithful routing | B1 | Default `echo`; external readiness unverified. |
| Native product API | `api/orgs,runtimes,personas,projects,issues,coordination,ws`, `authz`, `events` | Product workflows | F0-F10 | RBAC, server-derived realtime topics and persist-before-publish are enforced; the residual gaps are the unmatched Session routes and per-process live fan-out. |
| Runtime execution | `daemon`, `control/assignments`, `exec`, `authz/credentials` | Distributed agent work | F1, F3, F4 | Credentials are Runtime-narrow, Org-bound and revocable; session reconciliation remains open. |
| Coordination/MCP | `control`, `mcp` | Multi-agent continuity and collision avoidance | B2 | Broad mutation surface and incomplete auth. |
| Retrieval/graph | `context` | Faster orientation and shared knowledge | B3 | Optional dependencies and language scope. |
| Governance/audit | `govern`, `exec`, decisions, bridges, audit | Human authority and evidence | B4 | In-process cooperative boundary; transactional, chain-anchored audit. Out-of-band execution and outbound network calls are outside it. |
| Storage/recovery | `storage`, `backup` | Durable shared state and recovery | B5 | Schema evolution is an ordered, checksummed contract with backend-honest outcomes; live-Postgres parity and recovery policy remain gaps, and FK enforcement is available but opt-in until a store is proven clean. |
| CLI/wiring/service | `cli`, `wire`, `service`, supervisor | Install, wire Copilot/Claude/Codex/OpenCode, and operate Brains | B6 | Browser host mutation contracts remain absent. |
| Integrations | webhooks, bridges, `services/wa-web` | External triggers and mobile human loop | B7, F8 | Credential and external-runtime proof gaps. |
| Observability | health, traces, OTEL, logs | Diagnose operation | B8 | No readiness contract. |
| Legacy web | dashboard/admin/templates | Existing Brains operations | B9 | Duplicate product surfaces. |
| Containers/ingress/CI | Dockerfiles, compose, deploy, workflows | Reproducible candidate and operations | B5, B6, B8 | Every CI job is blocking, the runtime image constrains the MCP SDK to its supported major version, and its healthcheck covers gateway, dashboard, and MCP; deploy scaffold consistency and candidate-specific hosted evidence remain separate concerns. |

## Explicit traceability gaps

1. No E3/E4 evidence is embedded in canonical docs; run evidence belongs outside this tree.
2. J1-J11 have dedicated browser specs; real external Runtime/provider execution is not implied by the simulated harness.
3. The client Session control calls are matched (UM-01, UM-02); the J8 browser contract proves approval, truthful unsupported chat, and durable idempotent stop, while message delivery to the shipped Copilot launch shape remains unsupported (UM-03).
4. Native HTTP authorization satisfies F9's enforcement criteria at E2, including Org-scoped usage (`GET /v1/orgs/{org}/usage`); the open part is browser-session E4 evidence for AC-F9-05. Realtime topics are server-derived and persist-before-publish; the residual gap is per-process live fan-out (BL-P0-02).
5. Frontend routes and route parameters, API client calls, mounted server routes and their families, SQLAlchemy entities, the migration corpus, journey specs, acceptance tests, and `AC-*` references are generated from source by `scripts/check_traceability.py` and fail the gate on drift. What is still hand-written is the prose in these tables - gap statements, control/service columns, and the MCP/CLI family mapping - so those can still drift without failing a check.
6. No exact candidate has verified provider, bridge, GitHub, Postgres, backup/restore, or deployment operation.

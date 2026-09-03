<!--
last_verified: 2026-09-01T22:00:00.000-06:00
verified_by: Codex
verification_basis: HEAD 4ecba6a23aa4e6e287f926f4ef3992072d750f8a plus the worktree actionable-backlog and containment-reference rewrite; documentation, traceability, and targeted Docker gates verified; deployment not verified
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
| F0 Console foundation | P1, P2, P3 | J1, J11; AC-F0-01..05 | Sign in, inspect Command Center, enter a Workspace, launch a typed action | `/app`, `/app/command-center`, `/app/workspaces*`, `/app/act`; `AppShell`, `OperatorProvider`, typed async boundaries, `NotFound` | `/admin/login`; `/v1/operator/overview`; Workspace projections; capability catalog | local auth plus Workspace/task/claim/handoff controls | local operator, Workspaces, coordination rows; retained Org rows are compatibility scope | Advertised at E1/E2. J11 covers exclusive refresh states, route-specific nested content, scope-hidden not-found behavior, connection degradation, contrast, keyboard/focus, and responsive control reachability. |
| F1 Connect machine | P1, P4 | J2; AC-F1-01..06 | Withdrawn target; no supported action | Absent from shipped SPA | Runtime routes not composed | frozen enrollment, Runtime, and daemon controls | `registered_tools`, `runtimes`, `enrolment_tokens`; 122 | Historical store compatibility only. |
| F2 Personas | P1, P2, P7 | J3; AC-F2-01..06 | Withdrawn target; no supported action | Absent from shipped SPA | Persona/Spawn routes not composed | frozen Persona, assignment, Runtime, and Skill controls | `personas`, `agent_sessions`, `persona_skills`; 120/121/138 | Historical store compatibility only. |
| F3 Coordination Sessions and HITL | P1, P4, P5, P7 | J7, J8, J11; AC-F3-01..07 | Coordinate durable local work, route/resolve decisions, checkpoint/resume/end | `/app/governance`, `/app/coordination`, `/app/workspaces/:slug`; withdrawn execution Session UI remains in source | local governance/coordination; approval route/escalate/resolve; typed event scope; `/v1/ws`; `/v1/events` | leased coordination Session lifecycle with strict terminal cleanup, atomic ownership, idempotent checkpoints/handoffs, event taxonomy/scope, mailbox/successor continuity, decisions, local realtime | `agent_sessions`, `events`, `event_contexts`, continuity rows; feedback/routing rows are frozen inventory | Advertised local coordination. Feedback intelligence, automatic patterns, peer-review admission, cross-process fanout, running-agent control, and execution supervision are frozen or withdrawn. |
| F4 Projects and Issues | P1, P3, P7 | J5, J6, J7; AC-F4-01..07 | Withdrawn target; no supported action | Absent from shipped SPA | Project/Issue routes not composed | frozen project/issue/session evidence controls | Project, Issue, Session, and usage rows | Historical store compatibility only. |
| F5 Pods | P1, P2, P3, P7 | J4, J6; AC-F5-01..04 | Withdrawn target; no supported action | Absent from shipped SPA | Pod routes not composed | frozen Pod, assignment, and Issue evidence controls | `squads`, `squad_members`, `pod_profiles`, `pod_members`; 104/110/134 | Historical store compatibility only. |
| F6 Onboarding | P1, P2 | J1; AC-F6-01..05 | Withdrawn execution-model target; normal first run opens Command Center | Absent from shipped SPA | Onboarding routes not composed | frozen onboarding plus F1/F2/F4 controls | `onboarding_attempts`, `onboarding_steps`; 135 | Historical store compatibility only. |
| F7 Config | P1, P2, P6 | J9; AC-F7-01..04 | Inspect supported local config; edit only approved non-secret settings | `/app/operations/config/local`; `Config` | `GET/PUT /v1/operator/configuration`; legacy `/admin/config` remains deletion inventory | `brains.control.configuration`; config loader and local service controls | `tests/test_core_configuration.py`; `j09-config-settings.spec.ts` | Advertised/partial. The modern manifest is positive, redacted, bootstrap-admin-only, attributable, atomic, CAS-protected, and restart-explicit; legacy writable configuration prevents complete containment. |
| F8 GitHub linkage | P1, P2, P6 | J6: AC-F8-01..02; J9: AC-F8-03..04 | No supported path; keep GitHub ingress and relay undiscoverable and non-activatable | Frozen config/integration source | frozen webhook aliases and delivery APIs | retained signature/scope defenses | historical Issue links, `integration_deliveries`; 137 | Frozen/source compatibility; not an active evidence gap. |
| F9 Org, members, usage | P2, P3, P6 | J10, J11; AC-F9-01..05 | No supported multi-user administration path | Frozen access UI/API source; Workspace scoping remains supported locally | retained Org/member/usage APIs | compatibility Org and membership rows | identity and usage tables; 050/060/090-092/120/129/130/131/136 | Frozen/source compatibility; not an active evidence gap. |
| F10 Automation | P2, P3, P6, P7 | J10; AC-F10-01..06 | Withdrawn target; no supported action | Absent from shipped SPA | Automation routes/tools not composed | frozen recurring, Skill, and governed-spawn controls | recurring, Skills, governed actions, webhook rows; 104/110/111/112/125/126/138 | Historical store compatibility only. |

## Supporting capability matrix

| Feature | Personas | Journeys and ACs | User/system action | Surface | Control/service | Data | Test/evidence presence and gap |
|---|---|---|---|---|---|---|---|
| B1 Gateway/routing | P1, P4, P7 | J9, J11; AC-B1-01..04 | No supported model-proxy action | No mounted HTTP, MCP, CLI, browser, or configuration surface | frozen router, classifier, provider registry/policy | `traces`, `route_decisions`, `usage_ledger` | Withdrawn/source compatibility. The core-surface gate rejects discovery and activation. |
| B2 Coordination/MCP | P1, P3, P7 | J5-J8, J10; AC-B2-01..04 | Start/end, task/claim/handoff/message, local notification claim/settle, help, checkpoint/resume | MCP Streamable HTTP `/mcp` by default, stdio, explicit legacy SSE compatibility, CLI, `/app/coordination`, `/app/workspaces*`, `/app/act` | Session leases/successors, explicit native-ID validation/extraction controls, CLI/MCP managed versioned mailbox bindings with hash-only recovery intents, durable address delivery/read/thread controls, proof-bound body-free local notification protocol, browser mailbox desk, typed coordination adapters | coordination, knowledge, tasks/claims/mailbox/thread/message/delivery/notification/successors/leases; topic/feedback/SMTP/review rows are frozen inventory | Advertised for durable local coordination. Identity storage and manual lifecycle controls persist raw adapter provenance and fail closed across invalid input, conflicts, restart/resume, Workspace aliases, binding rotation/recovery/revocation, and abrupt exit. Owner-only binding files use POSIX modes or Windows DPAPI plus an exact current-user DACL. BL-P1-14 remains open because wire does not yet install proven native-ID lifecycle hooks/plugins for every harness. Topic boards, feedback intelligence, pattern routing, SMTP, and peer-review admission are frozen. |
| B3 Context/retrieval | P1, P7 | J6, J7, J11; AC-B3-01..04 | Search Workspace knowledge and bounded repository text/symbols | `brains-ai search-repo`; `brains_search_repo`; authenticated `/v1/operator/workspaces/{slug}/lookup`; Workspace Knowledge tab | one read-only `context.lookup` control with deterministic limits, relative line-numbered snippets, symbol-first matching, and `ok\|empty\|limited\|unavailable` reasons; caps and traversal/read errors make results explicitly partial; frozen `context/*` semantic/graph code | knowledge only; lookup creates no rows or source files; compatibility sources/artifacts/chunks/graph/freshness/memories remain data-only | Advertised only for knowledge and the shared non-semantic lookup envelope. Fresh never-indexed source, every-cap/error-state, source-fingerprint, and deferred Workspace-switch tests prove no preparation/write side effect, false no-match, or cross-Workspace late result. Semantic, embedding, graph, and external freshness implementations have no registered or routed activation surface. |
| B4 Governance/audit | P5, P6, P7 | J8, J10, J11; AC-B4-01..04 | Request, route/escalate, resolve approval, execute supported action, verify audit | `/app/governance`, typed HTTP, MCP/CLI governance families | human-bound approval routing, canonical governed-action contract, decisions, audit | approvals, `approval_routing`, `governed_actions`, `audit_log`, `audit_chain_head`; 070, 126-128, 145 | Advertised/partial. The boundary remains in-process; external harness effects are explicitly outside it. |
| B5 Storage/recovery | P6 | J10, J11; AC-B5-01..05 | Initialize, migrate, back up, verify, diagnose, repair, restore SQLite | `/app/operations` read posture; CLI/MCP mutation contracts; `recovery-drill` | SQLite storage, migrations, integrity, backup, encrypted settings | frozen baseline + numbered deltas, checksummed ledger, `secure_settings`, manifest-2 archives; 141 | Advertised/partial for SQLite because default FK enforcement remains open. Restore refuses an incompatible candidate before mutation, creates a verified rollback point for live replacement, verifies restored integrity, and records successful isolated drills; alternate backend code is withdrawn compatibility. |
| B6 CLI/wiring/service | P1, P4, P6 | J1, J2, J9, J11; AC-B6-01..04 | Install, setup, serve, wire, manage service | `brains-ai`; PyPI; Copilot/Claude/Codex/OpenCode wire adapters; `/app/operations` posture | verified windowless Windows launcher, exact-interpreter verifier, persisted endpoints/probes, native MCP renderers, supervisor bind preflight and per-child listener watchdog | package metadata, agent config, service endpoint config, PID state | Advertised/partial. E3 includes a hermetic Windows/macOS/Linux service-manager lifecycle crossed with every wire adapter. CI installs the built wheel on each native OS/Python combination and checks clean-home setup, native definition rendering, persistence, and reversible wiring. Actual native scheduler start/login/restart/stop/uninstall execution is still E4 and BL-P0-06 remains open until those jobs exist and pass. |
| B7 Authenticated external events | P2, P5, P6 | J8, J9; AC-B7-01..04 | No supported path; keep external ingress and relay unavailable | Frozen GitHub, trigger, relay, bridge, and wa-web source | retained defensive validation only | `integration_deliveries` plus trigger/bridge compatibility rows | Frozen/source compatibility; not an active evidence gap. |
| B8 Observability/readiness | P4, P6 | J7, J11; AC-B8-01..04 | Probe liveness/readiness, inspect durable-mail failure posture, diagnose queues and stale presence | `/app/operations`, `/health`, protected admin/operator projections, logs | SQLite migration/integrity, retained HTTP control-gateway identity/auth boundary, authenticated MCP handshake, count-only local durable-mail readiness, queue diagnosis/repair, verified recovery posture, supervisor | typed operational events, durable mailbox lifecycle rows, audited recovery drills, real isolated state-driven dependency failure drills, process log files | Advertised/partial. `/health` is liveness only; protected readiness isolates supported dependency failures with stable secret-free reasons. The model/provider gateway remains withdrawn; cross-process and external evidence are frozen. |
| B9 Legacy surfaces | P1, P2, P6 | J9-J11; AC-B9-01..03 | Use `/app`; verify deleted HTML is unreachable | `/app`; `/admin/login` and `/admin/logout` only | SPA auth plus generated source/route/distribution inventories | local auth | Deleted. Source and built-artifact gates reject dashboard/admin browser code, routes, commands, templates, helpers, and static assets; former opt-in cannot reactivate them. |

## Modern SPA route inventory

All shipped routes are declared in `frontend/src/App.tsx` and served under the `/app`
basename. Withdrawn execution-model and Labs routes are absent rather than gated.

| Route | Component/behavior | Feature/journey | Current gap |
|---|---|---|---|
| `/app` | `CommandCenter` without URL rewriting | F0, J1, J11 | The normal console starts from cross-Workspace durable state, not onboarding or an entity list. |
| `/app/command-center` | `CommandCenter` | F0, F3, B2, B8, J11 | Readiness and audit posture are install-admin-only. |
| `/app/workspaces` | `Workspaces` portfolio and first visible control room | F0, F3, B2, J5-J8 | Workspace import has no typed HTTP contract and is disabled. |
| `/app/workspaces/:slug` | `Workspaces`, including Knowledge-tab source lookup | F0, F3, B2, B3, J5-J8, J11 | `:slug` is consumed; unauthorized and unknown Workspaces are both 404. Lookup distinguishes complete no-match, incomplete scan, and unreadable/missing root without exposing absolute result paths. Requests are aborted and response-bound to the selected Workspace so a late result cannot cross a Workspace switch. |
| `/app/coordination` | `OperatorCoordination`, `MailboxWorkspace` | F3, B2, J5-J8 | Tasks, claims, handoffs, durable mail, and knowledge remain Workspace-attributable. The mailbox desk is human-bound; agent mailboxes are browser read-only without adapter proof. |
| `/app/governance` | `Governance` | F3, B4, J8, J11 | Resolution and audit verification are native; audit-chain detail is install-admin-only. |
| `/app/operations` | `Operations` | F7, F9, B5, B6, B8, J9-J11 | Host mutations, logs, backup, and restore remain disabled pending typed contracts. |
| `/app/operations/config` | `Config` local section without URL rewriting | F7, J9 | The default configuration entry renders the supported local manifest. |
| `/app/operations/config/:section` | `Config` | F7, F8, B8, J9 | Local service exposes the positive supported manifest and approved writes; MCP guidance and operational health remain read-only. |
| `/app/act` | `Act` typed capability launcher | F0, F3, B2, B4-B6, J11 | No generic shell or MCP-call endpoint; missing adapters are labeled and disabled. |
| `/app/inbox` | `NotFound` | F0, J11 | The retired alias does not silently select a different supported screen. |
| `/app/config` | `NotFound` | F0, J11 | The retired alias does not silently select a different supported screen. |
| `/app/*` | `NotFound` | F0, J11 | Unknown and withdrawn URLs remain in place and disclose neither route inventory nor resource existence. |

## Native API and realtime family inventory

Only core routers are composed into the gateway. Historical implementation modules and
database rows are not route inventory and provide no activation contract.

| Family | Principal routes | Auth boundary | Feature mapping |
|---|---|---|---|
| Health | `GET /health` | open | B8 |
| Admin | `/admin/login`, `/admin/logout` | sign-in bootstrap and cookie lifecycle only | B9 |
| Identity/authorization | credential store, principal resolution, capability policy, FastAPI gates (`src/brains/authz`) | not a route family; every native route resolves through it | F1, F9, B2, B9 |
| Operator console | `/v1/operator/*` overview, Workspace control rooms and read-only source lookup, coordination, governance, operations, capabilities, scoped mutations, mailbox access/registration/phonebook/lookup/send/broadcast/reply/forward/Inbox/Sent/thread/read and mailbox SMTP status/destination/verify/mode, audit verification | resolved operator principal plus per-Workspace `org.read`/`org.write`; source lookup requires `org.read` and returns only relative source paths; mailbox access and SMTP configuration are human-channel-only and mailbox-owner-bound; agent mailbox operations additionally require the current caller-owned Session and binding header; registration may declare a bounded adapter notification mode, while take/settle remains CLI/MCP-only; raw API credentials are send-only to operator inboxes while browser/local human channels may read owned human mail; install operations/global approvals require bootstrap admin | F0, F3, F7, F9, B2-B6, B8 |
| Orgs/members | Org CRUD, member list/add/remove, onboarding aliases | principal + `org.read`/`org.write`/`org.admin`/`org.owner` | F0, F6, F9 |
| Inbox/coordination | asks, handoffs, approvals, usage, config summary/test, Sessions, Session `message`/`stop`/`commands` | principal + Workspace/Org scope; approval resolution adds separation of duty; usage/config are bootstrap-admin only; Session control refuses Runtime credentials and answers `404` for another Org or a `private` Workspace | F3, F7, F9 |
| Operational health | `GET /v1/admin/readiness`; `GET /v1/admin/queue-health`; `POST /v1/admin/queue-health/repair`; `GET /v1/admin/recovery-policy` | bootstrap-admin only (`principal.is_bootstrap_admin`, same in-handler gate as `/v1/config/summary` and `/v1/usage`) | B5, B6, B8 |
| Realtime | `WS /v1/ws`, `GET /v1/events` | principal from key/cookie, then server-derived topic authorization re-checked per message and on a timer; Runtime credentials refused | F0, F3, J11 |
| Modern browser | `/app`, `/app/{path}`, assets, favicon | SPA index/fallback auth; favicon open | F0-F10 |

## Client/server mismatches and missing contracts

| ID | Client or product expectation | Current server fact | Affected IDs |
|---|---|---|---|
| UM-01 | `POST /v1/sessions/{id}/message` | No mounted route or supported running-agent delivery exists. Durable mailbox communication is the advertised path. | F3, J8, AC-F3-05 |
| UM-02 | `POST /v1/sessions/{id}/stop` | No mounted route or supported Runtime process stop exists. Coordination Session end/dormancy is separate. | F3, J8, AC-F3-06 |
| UM-03 | Chat should steer a running agent and survive reload. | Withdrawn. `session_commands` source persistence is compatibility inventory and must not be presented as an unavailable-but-activatable steering feature. | F3, J8, AC-F3-05 |
| UM-04 | Deep entity routes select the route entity. | Workspace deep links are advertised. Execution-model entity routes are withdrawn and unmounted; remaining parameter behavior is source inventory only. | F0, J3-J7, AC-F0-05 |
| UM-05 | Pod CRUD and Persona team semantics. | Withdrawn. Existing membership/routing code and tests do not create a supported user path. | F5, J4 |
| UM-06 | Fresh-state onboarding completes the north-star loop. | Withdrawn. The advertised fresh-state outcome is Command Center/Workspace-first entry, not execution-model onboarding. | F6, J1 |
| UM-07 | Modern Config edits supported state. | Advertised for approved rate-limit and SQLite writes with explicit restart outcomes and recovery. Frozen fields are absent, and the legacy writable configuration surface is deleted. | F7, J9 |
| UM-08 | GitHub webhook validates GitHub events. | Defensive validation exists in frozen source; GitHub operation is not advertised and creates no active E4 gap. | F8, J9 |
| UM-09 | Roles restrict native API access. | Defensive role checks exist in retained source; multi-user Org operation is frozen and creates no active browser-evidence gap. | F9, J10, J11 |
| UM-10 | Skills affect Persona or Project execution. | Withdrawn. Skill attachment/context code and migration 138 remain compatibility inventory; reusable advertised guidance is Workspace knowledge and bounded non-semantic repository lookup. | F10, J10 |
| UM-11 | Scheduled execution uses the same approval gate. | Withdrawn. No scheduled auto-fire is a supported path, regardless of source-level governed-action integration. | F10, B4, J10 |
| UM-12 | Readiness indicates candidate operability. | Resolved for the supported local topology: `/health` remains liveness-only; protected readiness independently reports SQLite migration/integrity, retained HTTP control-gateway identity/auth boundary, authenticated MCP initialize and tools/list, queue, durable-mail, and verified recovery posture while excluding the withdrawn model/provider gateway and other frozen dependencies. | B8, J11 |

## MCP and CLI surface mapping

| Capability | MCP families | CLI families | Feature mapping |
|---|---|---|---|
| Plan/retrieve | advertised bounded repo search and knowledge | advertised repo search; semantic, docs-index, graph, embedding, and external-freshness commands are withdrawn | B3 |
| Session/state | advertised start/heartbeat/end, state, event, snapshot, checkpoint/resume, mailbox register/phonebook/lookup/send/broadcast/reply/forward/inbox/sent/thread/notification-take/notification-settle; execution message/stop commands are withdrawn | advertised coordination Session/state/event/snapshot and matching mailbox families, including body-free notification claim/settle; execution message/stop commands are withdrawn | B2, F3 |
| Work coordination | tasks, claims, handoffs, durable mailbox, existing-peer help lifecycle, and presence | task, claim, handoff, mailbox, and existing-peer help lifecycle commands | B2, F3, F4 |
| Knowledge | knowledge add/search/resolve/retrieve and optional views | knowledge and views commands | B2, B3 |
| Automation/webhooks | no recurring or generic-webhook activation surface ships in core | recurring and jobs commands are withdrawn | F10, B7 |
| Governance/recovery | decision route/escalate/resolve, audit, governed actions, SQLite backup/restore | decision lifecycle, audit/governed/backup/restore families | F3, B4, B5 |
| Runtime/operations | supported MCP server transport and retained local operational probes | setup, serve/up, wire, service; Runtime, `run`, daemon, and optional-feature activation surfaces are withdrawn | F1, B6, B8 |

Current MCP naming is `brains_*`; internal legacy dotted names are not the public
documentation contract. Supported defaults must exclude withdrawn tools regardless of
full/lean/allowlist source modes; `scripts/check_core_surface.py` enforces that boundary.

## Data and migration mapping

| Domain | Principal tables/state | Migration coverage | Known gap |
|---|---|---|---|
| Identity/scope | `operators`, `orgs`, `org_members`, `workspaces`, `workspace_aliases`, `workspace_memberships`, `api_credentials` | 050, 060, 101, 120, 129, 130, 131, 148 | Every accepted credential is one hashed row bound to a principal; `130` backfills the previously implicit default-Org membership for pre-existing operators and deliberately excludes `daemon-*` operators, which keep authenticating, see nothing, and are reported by `brains-ai credentials doctor`; `131` records each credential's provenance so a rotated admin key or a deleted operator key file revokes exactly the credential it superseded. `148` makes normalized paths aliases of a durable Workspace and converges linked Git worktrees only inside one Org; archived duplicate history remains attributable. |
| Coordination, durable mail, and withdrawn execution compatibility | `registered_tools`, `agent_sessions`, `events`, durable mailbox/thread/message/delivery/notification/SMTP/legacy-inventory rows, plus withdrawn `runtimes`, `enrolment_tokens`, `personas`, `projects`, `issues`, `issue_comments`, `skills`, attachment, and `session_commands` rows | 100, 120-125, 133, 138, 150-153 | Coordination Session/tool/event rows, mailbox identity, local delivery/read/thread/fixed-nudge state, and manual adapter-binding lifecycle remain advertised. Migration 153 persists adapter provenance plus hash-only binding transition intents. SMTP, Runtime, Persona, Project, Issue, Skill, and execution-command rows are compatibility inventory only; source behavior and historical attribution do not authorize activation. |
| Realtime | `realtime_events` | 132 | The supported subscription grammar accepts only Org inbox/Session collections and Session state streams; withdrawn Issue and Runtime families are rejected. Events commit before announcement, carry monotonic resumable IDs, remain scope-filtered, and preserve idempotent publication, ordered catch-up, and explicit stale-cursor reset behavior. Retention and gap detection are install-wide rather than per subscription; live fan-out is per gateway process. |
| Governance | approval/routing, typed `event_contexts`, coordination queues, historical `help_request_executions`, governed actions and audit chain | 010, 030, 040, 070, 126-128, 139, 140, 142-149 | Event category/scope provenance is stored per row; approval routing never resolves; help dispatch is existing-peer-only and cannot launch or reschedule an automatic review. |
| Encrypted local config compatibility | `secure_settings` | 141, 152 | Historical encrypted settings remain readable for migration; provider, bridge, SMTP, and generic configuration APIs are withdrawn. |
| Teams/automation compatibility | `squads`, `squad_members`, `pod_profiles`, `pod_members`, recurring and generic webhook tables | 104, 110-112, 134 | Withdrawn. Rows remain only to preserve historical references and schema compatibility until separately reviewed removal; no Pod, managed-Skill, recurring, or generic-trigger activation is supported. |
| Onboarding compatibility | `onboarding_attempts`, `onboarding_steps` | 135 | Withdrawn execution-model state. Existing attempts are historical compatibility data, not a resumable current first-run path. |
| Knowledge and retrieval compatibility | knowledge plus sources, artifacts, chunks, chunks_meta, graph nodes/edges | 020, 080, 102, 103 | Workspace knowledge remains advertised. Semantic/embedding/graph/freshness artifacts are withdrawn compatibility data; stable local lookup requires none of them. |
| Usage and routing compatibility | usage ledger and attributions plus withdrawn traces, route decisions, memories, and freshness | 090-092, 136 | Scoped usage remains advertised where attribution is explicit. Model-routing and semantic-freshness rows are compatibility inventory and must not drive normal-product readiness. |
| Integrations | `integration_deliveries` plus withdrawn `webhook_triggers` and `webhook_deliveries` | 110-112, 137 | GitHub delivery identity/dedupe, generic triggers, relay, approval bridges, and companion delivery are frozen or withdrawn compatibility inventory, not advertised product surfaces. |
| File state | admin/operator/audit keys, `secrets.env`, runtime overlay, daemon config, OAuth cache, exec transcripts, service PID/log | not SQL-migrated | PID identity/liveness, multi-process reload, ownership, backup scope, and retention need explicit policy. |

Fresh and legacy SQLite stores reach the current schema through one ordered, checksummed
migration contract: the frozen baseline followed by numbered deltas, recorded with
order, checksum, origin, backend, status, attempts, timings, and error. `create_all` is
not on the startup path. Alternate-backend baseline and migration code remains at HEAD
only as withdrawn compatibility inventory required to inspect or migrate existing
stores. Runtime backend selection accepts SQLite only and rejects Postgres.

## Browser and backend evidence inventory

| Journey | Browser file at HEAD | Backend/static evidence | Gap |
|---|---|---|---|
| J1 | `j01-first-run.spec.ts` | Sign-in and Workspace registration controls | Workspace-first entry plus withdrawn onboarding fail-closed is asserted; long-run E4 recovery evidence remains open. |
| J2 | `j02-connect-machine.spec.ts` | F1 enrollment/Runtime/daemon source tests | Withdrawn journey now asserts containment only: no discovery and direct Runtime URLs fail closed to Command Center. |
| J3 | `j03-personas.spec.ts` | F2 Persona and Skill-attachment source tests | Withdrawn journey now asserts containment only: no discovery and direct Persona URLs fail closed. |
| J4 | `j04-pods.spec.ts` | F5 Pod roster/routing source tests | Withdrawn journey now asserts containment only: no discovery and direct Pod URLs fail closed. |
| J5 | `j05-project-workspace.spec.ts` | Project plus Workspace scope source tests | Browser evidence keeps advertised Workspace control-room routing while asserting withdrawn Project fail-closed behavior. |
| J6 | `j06-issues.spec.ts` | F4 Issue/dispatch source tests | Withdrawn journey now asserts containment only: no Issue activation controls and direct Issue URLs fail closed. |
| J7 | `j07-sessions.spec.ts`; `test_nonembedding_lookup.py`; `test_operator_console_api.py` | F3 coordination and B3 lookup tests | Browser source lookup shares the CLI/MCP envelope; fresh-source, no-write, result-state, scope, and non-disclosure behavior is blocking E3 coverage. Durable task/handoff browser coverage remains on Act, Coordination, and Workspace surfaces. |
| J8 | `j08-governance-session-control.spec.ts` | approval/ask/gate plus durable mailbox authorization/delivery/SMTP tests | Browser coverage asserts one-time governance resolution, fail-closed legacy session-control navigation, and container-only mailbox selector/read/compose/reply/forward/SMTP-status/reload/responsive journeys. |
| J9 | `j09-config-settings.spec.ts` | F7/F8 configuration and access source tests | Browser coverage targets the supported local Operations configuration contract without Labs or frozen access activation. |
| J10 | `j10-automation.spec.ts` | F9 plus withdrawn F10/recurring/webhook/Skill source tests | Withdrawn automation and multi-user access journeys assert containment only. |
| J11 | `j11-console-clean.spec.ts`, `j11-operator-web-hardening.spec.ts` | auth, error, WS, privacy tests | Route-specific and nested-boundary success/loading/empty/error/authorization, exclusive refresh failure, scope-hidden not-found, connection degradation, two-width viewport/overflow reachability, rendered-text contrast, and APG tab/combobox/modal keyboard/focus contracts are present; multi-process realtime evidence remains frozen. |

The backend acceptance module covers advertised F0/F3 local behavior and direct
containment outcomes for withdrawn F1/F2/F4-F10 activation. Test presence is E2
only and is not evidence that the tests passed.

## Platform capability inventory

| Capability | Principal code | Product value | Canonical owner | Current limitation |
|---|---|---|---|---|
| Modern SPA | `frontend`, `brains.web.spa` | Advertised Workspace-first operator experience | F0-F10 | Bundle provenance and the absence of Labs/entity-first/legacy exposure are checked. |
| Gateway/providers | `api/openai.py`, `api/anthropic.py`, `router`, `providers` | Withdrawn compatibility code | B1 | No mounted route, help, configuration, or product activation surface. |
| Native product API | retained organization identity, coordination, operator, health, and realtime routes | Advertised local Workspace coordination; historical models and migrations remain data-only compatibility boundaries | F0-F10 | The machine-readable core-surface inventory rejects frozen or withdrawn route activation. |
| Runtime execution | `daemon`, `control/assignments`, `exec`, `authz/credentials` | Withdrawn compatibility code | F1, F3, F4 | No enrollment, assignment, process-control, or activation claim. |
| Coordination/MCP | `control`, `mcp` | Local multi-agent continuity and collision avoidance | B2 | Advertised local surface. Feedback intelligence, automatic routing, and automatic peer-review admission are frozen and absent from the tool registry. |
| Retrieval/graph | `context.lookup`, knowledge controls; retained compatibility `context` code | Advertised knowledge plus bounded read-only local lookup; semantic/graph code is withdrawn | B3 | CLI, MCP, and authenticated browser use the same no-write lookup envelope. Semantic, graph, embedding, and external freshness are not activatable product capabilities. |
| Governance/audit | `govern`, decisions, audit | Human authority and evidence | B4 | In-process cooperative boundary; approval notifications remain local and cannot activate withdrawn bridges. |
| Storage/recovery | `storage`, `backup` | Supported SQLite state and recovery plus alternate-backend compatibility | B5 | Candidate refusal, rollback creation, restore verification, and isolated drill are covered; default FK enforcement remains open and Postgres is withdrawn. |
| CLI/wiring/service | `cli`, `wire`, `service`, supervisor | Install, wire Copilot/Claude/Codex/OpenCode, and operate Brains | B6 | Browser host mutation contracts remain absent. |
| Integrations | historical GitHub/webhook, bridge, and companion-service code/data | Frozen external integration inventory | B7, F8 | No external integration is an advertised or activatable surface. |
| Observability | health, readiness, typed events, logs; withdrawn OTEL code remains | Diagnose supported operation | B8 | Supported local dependencies have independent readiness; cross-process checks remain frozen. |
| Legacy web | no implementation remains | Deleted browser surface | B9 | Source, route, command, package-data, wheel, and sdist checks prevent reintroduction; only modern SPA authentication endpoints remain. |
| Containers/ingress/CI | Dockerfiles, compose, deploy, workflows | Reproducible candidate and operations | B5, B6, B8 | Every CI job is blocking and the runtime image constrains the MCP SDK to its supported major version; deploy scaffold consistency and candidate-specific hosted evidence remain separate concerns. |

## Explicit traceability gaps

1. No E3/E4 evidence is embedded in canonical docs; run evidence belongs outside this tree.
2. J1-J11 retain dedicated browser specs, and J2-J6 plus J10 now assert withdrawn containment rather than activation while preserving stable IDs.
3. Historical running-agent message and stop code may remain importable, but its HTTP/CLI/MCP activation surfaces are absent.
4. Native HTTP authorization and retained Org checks remain defensive source evidence, not a multi-user product promise. Realtime subscriptions are server-derived and persist-before-publish inside the supported local gateway; cross-process fan-out is frozen.
5. Frontend routes and route parameters, API client calls, mounted server routes and their families, SQLAlchemy entities, the migration corpus, journey specs, acceptance tests, and `AC-*` references are generated from source by `scripts/check_traceability.py` and fail the gate on drift. What is still hand-written is the prose in these tables - gap statements, control/service columns, and the MCP/CLI family mapping - so those can still drift without failing a check.
6. No exact candidate has verified backup/restore or deployment operation. GitHub, provider, bridge, Postgres, and other frozen/withdrawn operation are not evidence gaps for the advertised product.

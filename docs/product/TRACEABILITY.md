# Brains Traceability

This map connects public product contracts to their implementation and acceptance
surfaces. It is deliberately compact: source and tests are the live evidence, while the
backlog lists only unfinished actions.

## Core outcomes

| Contract | Lifecycle/source | Acceptance family |
|---|---|---|
| F0 Console foundation and coherent product state | Workspace services and authenticated application surfaces | `AC-F0-*` |
| F1 Connect a machine and register Runtimes | withdrawn compatibility source | `AC-F1-*` |
| F2 Personas and capability binding | withdrawn compatibility source | `AC-F2-*` |
| F3 Coordination Sessions, events, and human control | Session, event, decision, mailbox, and realtime services | `AC-F3-*` |
| F4 Projects, Issues, assignment, and dispatch | withdrawn compatibility source | `AC-F4-*` |
| F5 Pods | withdrawn compatibility source | `AC-F5-01..04` |
| F6 First-run execution onboarding | withdrawn compatibility source | `AC-F6-*` |
| F7 Supported configuration truth | supported configuration summary and validated writes | `AC-F7-*` |
| F8 GitHub linkage | frozen target and compatibility source | `AC-F8-*` |
| F9 Orgs, members, roles, and usage | frozen target; local Workspace scope remains supported | `AC-F9-*` |
| F10 Autopilots and managed Skills | withdrawn compatibility source | `AC-F10-*` |

## Boundary outcomes

| Contract | Enforced boundary | Acceptance family |
|---|---|---|
| B1 External request routing | withdrawn compatibility source | `AC-B1-*` |
| B2 Coordination plane and MCP | supported local coordination | `AC-B2-*` |
| B3 Workspace knowledge and repository lookup | supported bounded, non-semantic lookup | `AC-B3-*` |
| B4 Human governance and audit | supported governed actions and local audit | `AC-B4-*` |
| B5 SQLite storage, migrations, backup, and recovery | supported SQLite lifecycle | `AC-B5-*` |
| B6 CLI, wiring, and service management | supported foreground lifecycle and wiring; native manager qualification open | `AC-B6-*` |
| B7 Authenticated external events | frozen target and compatibility source | `AC-B7-*` |
| B8 Observability, health, and readiness | supported local operations | `AC-B8-*` |
| B9 Deleted legacy browser surfaces | supported modern application; legacy surfaces absent | `AC-B9-*` |

The detailed acceptance identifiers live in `FEATURE_CONTRACT.md`: `AC-F0-`,
`AC-F1-`, `AC-F2-`, `AC-F3-`, `AC-F4-`, `AC-F5-`, `AC-F6-`, `AC-F7-`, `AC-F8-`,
`AC-F9-`, `AC-F10-`, `AC-B1-`, `AC-B2-`, `AC-B3-`, `AC-B4-`, `AC-B5-`, `AC-B6-`,
`AC-B7-`, `AC-B8-`, and `AC-B9-`.

## Personas and journeys

`P1`, `P2`, `P3`, `P4`, `P5`, `P6`, and `P7` describe the actors. `J1`, `J2`, `J3`,
`J4`, `J5`, `J6`, `J7`, `J8`, `J9`, `J10`, and `J11` are stable journey contracts;
withdrawn or frozen journeys validate containment rather than a supported action path.
The authoritative narratives and their feature mappings live in
`PERSONAS_AND_JOURNEYS.md`.

## Modern SPA route inventory

The supported single-page application owns these routes:

| Route | Contract |
|---|---|
| `/app` | Command Center |
| `/app/command-center` | Command Center |
| `/app/workspaces` | Workspace portfolio |
| `/app/workspaces/:slug` | Workspace control room |
| `/app/coordination` | Coordination and mailboxes |
| `/app/governance` | Asks, decisions, and audit |
| `/app/operations` | Operational health |
| `/app/operations/config` | Supported configuration |
| `/app/operations/config/:section` | Supported configuration section |
| `/app/act` | Typed governed actions |
| `/app/inbox` | Retired alias fails closed |
| `/app/config` | Retired alias fails closed |
| `/app/*` | Unknown routes fail closed |

Route registration, frontend navigation, and generated distribution assets must agree.
The core-surface manifest and browser tests enforce that agreement.

## Native API and realtime family inventory

The protected API and realtime registrations under `src/brains/api/` define the current
native service boundary. Authentication, authorization, and reconnect tests bind those
families to F0, F3, B2, B4, and B8.

| Family | Principal routes | Auth boundary | Feature mapping |
|---|---|---|---|
| Health | `GET /health` | open | B8 |
| Admin | `/admin/login`, `/admin/logout` | sign-in bootstrap and cookie lifecycle only | B9 |
| Identity/authorization | credential store, principal resolution, capability policy, FastAPI gates (`src/brains/authz`) | every native route resolves through it | F1, F9, B2, B9 |
| Operator console | `/v1/operator/*` | resolved local operator plus per-Workspace capability checks; install-wide operations require bootstrap admin | F0, F3, F7, F9, B2-B6, B8 |
| Orgs/members | compatibility route family | not supported for local-product activation | F0, F6, F9 |
| Inbox/coordination | asks, handoffs, approvals, usage, configuration summary, Sessions, and mailbox operations | principal plus Workspace scope; install-wide views require bootstrap admin | F3, F7, F9 |
| Operational health | `GET /v1/admin/readiness`, `GET /v1/admin/queue-health`, `POST /v1/admin/queue-health/repair`, `GET /v1/admin/recovery-policy` | bootstrap admin | B5, B6, B8 |
| Realtime | `WS /v1/ws`, `GET /v1/events` | principal plus server-derived scoped subscription authorization | F0, F3, J11 |
| Modern browser | `/app`, `/app/{path}`, assets, favicon | authenticated application shell; favicon open | F0-F10 |

## Data and migration mapping

| Domain | Current source | Migration coverage |
|---|---|---|
| SQLite schema | schema and migration modules under `src/brains/` | 010, 020, 030, 040, 050, 060, 070, 080, 090-092, 100-104, 110-112, 120-131, 133-153 |
| Realtime event log | durable event migration | 132 |

The source migrations, not this summary, define the schema and transition details.

## Browser and backend evidence inventory

| Journey | Browser specification |
|---|---|
| J1 | `j01-first-run.spec.ts` |
| J2 | `j02-connect-machine.spec.ts` |
| J3 | `j03-personas.spec.ts` |
| J4 | `j04-pods.spec.ts` |
| J5 | `j05-project-workspace.spec.ts` |
| J6 | `j06-issues.spec.ts` |
| J7 | `j07-sessions.spec.ts` |
| J8 | `j08-governance-session-control.spec.ts` |
| J9 | `j09-config-settings.spec.ts` |
| J10 | `j10-automation.spec.ts` |
| J11 | `j11-console-clean.spec.ts`, `j11-operator-web-hardening.spec.ts` |

Backend acceptance covers the same contracts through registered API, CLI, and MCP
surfaces. Tests and the generated core-surface manifest are the live evidence inventory.

## Current source, target contract, and evidence gaps

- Current source is what the registered routes, commands, tools, schema, frontend, and
  generated core-surface manifest expose now.
- Target contract is the behavior stated in `FEATURE_CONTRACT.md`; it does not become a
  current claim until implementation and acceptance evidence agree.
- Persistent native service lifecycle qualification, native Claude recovery
  qualification, exact-candidate aggregation across source, wheel, sdist, and OCI image
  manifest digests, and publication reuse of those exact qualified artifacts are
  unfinished implementation actions in `BACKLOG.md`. Container and hermetic tests do
  not establish native persistence or platform-specific recovery guarantees.
- Deferred ideas remain in `FROZEN_BACKLOG.md` and are not current product promises.

Run `python scripts/check_traceability.py` for mapping drift. The full quality runner
builds the distribution and checks the generated core-surface manifest.

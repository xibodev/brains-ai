<!--
last_verified: 2026-08-31T18:30:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 35ce5ff1b4a2eb8bce2777ca7e3cff4d7ceece99 plus the worktree experimental-label, ordinary-feedback, and durable-mail evidence reconciliation; installed-service recovery and deployment not verified
-->

# Brains Backlog Registry

## Delivery Model

This registry preserves stable backlog IDs and maps them to product outcomes. Full
schedulable requirements live in two feature-oriented documents:

- [Active backlog](ACTIVE_BACKLOG.md) contains normal-install features and
  cross-cutting foundations eligible for feature-branch delivery into `staging`.
- [Experimental backlog](EXPERIMENTAL_BACKLOG.md) marks implemented behavior whose
  normal-use ergonomics or edge cases remain uncertain and need ordinary feedback.

Known-faulty withdrawn implementations appear in neither backlog. Their lifecycle,
containment boundary, and re-entry rule are recorded in
[FEATURE_CONTRACT.md](FEATURE_CONTRACT.md). Withdrawal does not claim that current
source has already removed every route, command, tool, extra, or configuration key;
that containment work is active backlog item BL-P0-09.

## Rules

- Priority describes risk; feature ownership determines development and branch scope.
- Active work uses one short-lived feature branch from current `staging`, merges to
  `staging` after its own acceptance gates, and reaches `main` only through promotion
  of an exact integrated candidate.
- Experimental features pass the same automated and isolated-UAT gates as other release
  candidates; the label does not create an analytics or field-trial requirement.
- A withdrawn implementation is not schedulable feature work. Only containment,
  shared-data compatibility, approved removal, or replacement research may touch it.
- A replacement enters the experimental backlog only after its implementation, UAT,
  feedback path, rollback, and revision boundaries are reviewed.
- Dated measurements remain outside canonical docs; backlog items record repeatable
  probes, unhealthy conditions, required outcomes, and evidence.

## Active Backlog Registry

### BL-P0-01 - Identity and authorization
- **Maps to:** F0, F3, F8, F9, B2, B4, B7, B9; J1, J6, J8-J11.
- **Requirement:** [Security, identity, and authority](ACTIVE_BACKLOG.md#security-identity-and-human-authority).

### BL-P0-02 - Realtime consistency
- **Maps to:** F0, F3, F9, B2, B8; J7, J8, J11.
- **Requirement:** [Realtime and distributed consistency](ACTIVE_BACKLOG.md#realtime-and-distributed-consistency).

### BL-P0-03 - Enforceable action boundary
- **Maps to:** F3, B4; J8, J11.
- **Requirement:** [Security, identity, and authority](ACTIVE_BACKLOG.md#security-identity-and-human-authority).

### BL-P0-04 - Audit and database integrity
- **Maps to:** F3, B4, B5; J8, J10, J11.
- **Requirement:** [Security, identity, and authority](ACTIVE_BACKLOG.md#security-identity-and-human-authority) and [Storage and recovery](ACTIVE_BACKLOG.md#storage-migrations-and-recovery).

### BL-P0-05 - Durable coordination Session control
- **Maps to:** F0, F3, B2; J7, J8, J11.
- **Requirement:** [Workspace coordination](ACTIVE_BACKLOG.md#workspace-coordination-and-sessions). Running-agent message delivery is withdrawn.

### BL-P0-06 - Service, packaging, wiring, and transport
- **Maps to:** F0, F7, B6, B8, B9; J1, J9, J11.
- **Requirement:** [Installation, service, and wiring](ACTIVE_BACKLOG.md#installation-service-and-wiring).

### BL-P0-07 - SQLite integrity repair
- **Maps to:** F3, F9, B2, B5; J7, J10, J11.
- **Requirement:** [Storage and recovery](ACTIVE_BACKLOG.md#storage-migrations-and-recovery).

### BL-P0-08 - Reproducible supported schema evolution
- **Maps to:** B5, B6; J9, J11.
- **Requirement:** [Storage and recovery](ACTIVE_BACKLOG.md#storage-migrations-and-recovery). Postgres is withdrawn; this item protects the supported SQLite path and archive compatibility.

### BL-P0-09 - Withdraw frozen capability exposure
- **Maps to:** F1, F2, F4-F6, F10, B1, B3, B6-B9; J1-J11.
- **Requirement:** [Frozen capability containment](ACTIVE_BACKLOG.md#frozen-capability-containment).

### BL-P1-01 - Blocking quality and staging promotion
- **Maps to:** F0-F10, B1-B9; J1-J11.
- **Requirement:** [Quality and release](ACTIVE_BACKLOG.md#quality-release-and-traceability).

### BL-P1-05 - Configuration truth
- **Maps to:** F7, B6, B8; J9, J11.
- **Requirement:** [Access and configuration](ACTIVE_BACKLOG.md#access-usage-and-configuration). Gateway and bridge configuration are withdrawn.

### BL-P1-06 - GitHub integration security
- **Maps to:** F8, B7; J6, J9.
- **Requirement:** [GitHub linkage](ACTIVE_BACKLOG.md#github-linkage). Messaging bridges are withdrawn.

### BL-P1-07 - Org access and scoped usage
- **Maps to:** F9; J10, J11.
- **Requirement:** [Access and configuration](ACTIVE_BACKLOG.md#access-usage-and-configuration).

### BL-P1-09 - Readiness and recovery
- **Maps to:** B5, B6, B8; J1, J7, J10, J11.
- **Requirement:** [Operations and readiness](ACTIVE_BACKLOG.md#operations-readiness-and-recovery).

### BL-P1-10 - Surface and traceability checks
- **Maps to:** F0-F10, B1-B9; J1-J11.
- **Requirement:** [Quality and release](ACTIVE_BACKLOG.md#quality-release-and-traceability).

### BL-P1-12 - Durable mail and asynchronous collaboration
- **Maps to:** F3, B2, B8; J7, J8, J11.
- **Requirement:** [Agent communications](ACTIVE_BACKLOG.md#agent-communications-and-durable-mailboxes).

### BL-P1-14 - Mailbox identity, presence, and successor continuity
- **Maps to:** F3, B2, B8; J7, J8, J11.
- **Requirement:** [Workspace portfolio and presence](ACTIVE_BACKLOG.md#workspace-portfolio-and-presence).

### BL-P1-15 - Agent-experience feedback
- **Maps to:** F3, B2, B4, B8; J7, J8, J11.
- **Requirement:** [Agent feedback](ACTIVE_BACKLOG.md#agent-feedback).

### BL-P1-17 - Coordination-pattern and workflow routing
- **Maps to:** F3, B2, B6, B8; J7, J10, J11.
- **Requirement:** [Knowledge and patterns](ACTIVE_BACKLOG.md#knowledge-and-coordination-patterns). Managed Persona/Project Skills are withdrawn.

### BL-P1-18 - Truthful default lookup guidance
- **Maps to:** B2, B3, B6, B8; J7, J11.
- **Requirement:** [Stable local lookup](ACTIVE_BACKLOG.md#stable-local-lookup).

### BL-P1-19 - Human-approved public defect relay
- **Maps to:** F3, F8, B2, B4, B7, B8; J8, J9, J11.
- **Requirement:** [Community defect relay](ACTIVE_BACKLOG.md#community-defect-relay).

### BL-P2-01 - Retire legacy browser surfaces
- **Maps to:** F0, F7, B9; J9-J11.
- **Requirement:** [Frozen capability containment](ACTIVE_BACKLOG.md#frozen-capability-containment).

### BL-P2-02 - External evidence retention
- **Maps to:** B5, B8; J11.
- **Requirement:** [Quality and release](ACTIVE_BACKLOG.md#quality-release-and-traceability).

### BL-P2-03 - Import-cycle maintainability
- **Maps to:** B2, B4, B6; J7, J11.
- **Requirement:** [Maintainability](ACTIVE_BACKLOG.md#maintainability).

### BL-P3-01 - Normal-console routes, errors, and accessibility
- **Maps to:** F0, F3, F7-F9, B8, B9; J1, J7-J11.
- **Requirement:** [Workspace-first console](ACTIVE_BACKLOG.md#workspace-first-console).

## Experimental Backlog Registry

### BL-P1-16 - Removed adoption/outcome analytics interpretation
- **Maps to:** F3, B2, B8; J7, J8, J11.
- **Disposition:** [Feedback and evidence boundary](EXPERIMENTAL_BACKLOG.md#feedback-and-evidence-boundary).

### BL-P1-20 - Ephemeral peer-review admission blocker
- **Maps to:** F3, B2, B4, B8; J7, J8, J11.
- **Requirement:** [Ephemeral peer review](EXPERIMENTAL_BACKLOG.md#ephemeral-peer-review-admission-blocker).

## Withdrawn Historical IDs

These IDs remain defined so existing documentation, commits, and external references
do not become ambiguous. They own no schedulable backlog.

### BL-P1-02 - Withdrawn Project and Issue execution
- **Maps to:** F4; J5-J7.
- **Disposition:** Withdrawn faulty implementation; no activation or backlog work.

### BL-P1-03 - Withdrawn Pod execution
- **Maps to:** F5; J4, J6.
- **Disposition:** Withdrawn faulty implementation; no activation or backlog work.

### BL-P1-04 - Withdrawn execution-model onboarding
- **Maps to:** F6; J1.
- **Disposition:** Withdrawn faulty implementation; no activation or backlog work.

### BL-P1-08 - Withdrawn Automation UI and managed Skills
- **Maps to:** F2, F10; J3, J10.
- **Disposition:** Withdrawn faulty implementation; replacement research is isolated and does not reactivate this ID.

### BL-P1-11 - Withdrawn model gateway
- **Maps to:** B1, F7; J9, J11.
- **Disposition:** Withdrawn faulty implementation; no activation or backlog work.

### BL-P1-13 - Withdrawn Runtime execution
- **Maps to:** F1, F2, F4, F5, B6, B8; J2-J7.
- **Disposition:** Withdrawn faulty implementation; no activation or backlog work.

### BL-P2-04 - Withdrawn semantic retrieval and code graph
- **Maps to:** B3; J6, J7, J11.
- **Disposition:** Withdrawn faulty implementation; replacement research is isolated and does not reactivate this ID.

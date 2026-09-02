# Brains Frozen Backlog

This file contains deferred future TODOs only. These items are not advertised,
scheduled, validated, or used as current outcome owners. An item may move to
`BACKLOG.md` only after `BACKLOG.md` is empty and a human explicitly approves thawing it.
Remove an item when it is completed or rejected; Git history holds disposition.

### BL-P0-01 - Validate multi-operator authorization

- **Action:** Validate principal, Org, role, Workspace, and private-scope authorization across every protected human surface.
- **Done when:** A generated inventory and two-user/two-Org matrix prove non-enumerating denial for reads, mutations, subscriptions, mailboxes, usage, and configuration.
- **Maps to:** F0, F8, F9, B4, B7; J6, J8-J11.

### BL-P0-02 - Implement cross-process realtime consistency

- **Action:** Add cross-process live fan-out, idempotent publisher keys, scoped retention and gap detection, and authorized reconnect behavior.
- **Done when:** A two-process test proves ordered publish, disconnect, gap/reset handling, reconnect, and exactly-once logical recovery within each authorized topic scope.
- **Maps to:** F3, F9, B2, B8; J7, J8, J11.

### BL-P0-03 - Complete the enforceable external-action boundary

- **Action:** Enforce approved process and network effect shapes beyond the cooperative local boundary and expose residual out-of-band paths truthfully.
- **Done when:** Denied, approved, expired, duplicate, and externally observed effect journeys fail closed or report the correct governance classification without implying control Brains did not exercise.
- **Maps to:** F3, B4; J8, J11.

### BL-P0-04 - Validate extended audit integrity

- **Action:** Validate approval, action, result, and audit correlation under multi-operator mutation and concurrency and close demonstrated gaps.
- **Done when:** Race, deletion, insertion, truncation, and mutation probes prove single-use decisions, attributable results, and fail-closed audit/database integrity.
- **Maps to:** F3, F9, B4, B5; J8, J10, J11.

### BL-P1-06 - Complete Workspace-first GitHub linkage

- **Action:** Add an explicit Workspace-first destination for authenticated GitHub events without depending on withdrawn Issue surfaces.
- **Done when:** Signed, invalid, replayed, and out-of-scope deliveries reconcile durably in an authorized Workspace and a browser journey shows the result.
- **Maps to:** F8, B7; J6, J9.

### BL-P1-07 - Complete Org access and scoped usage

- **Action:** Implement the multi-user owner, admin, member, last-owner, browser-session, scoped-usage, and unattributed-call contract.
- **Done when:** A multi-user two-Org journey proves allowed administration, denied escalation and disclosure, owner preservation, and correctly labelled usage.
- **Maps to:** F9; J10, J11.

### BL-P1-15 - Validate the agent-feedback workflow

- **Action:** Validate privacy-safe agent reporting, enrichment, human triage, discard, and exactly-once promotion and recovery.
- **Done when:** A two-agent journey proves redaction, duplicate handling, authorization, human-only disposition, attributable promotion, and recovery without granting roadmap authority.
- **Maps to:** F3, B2, B4, B8; J7, J8, J11.

### BL-P1-17 - Implement automatic coordination-pattern routing

- **Action:** Match approved coordination patterns against bounded task intent and record versioned offer/omit reasons plus privacy-safe usage receipts.
- **Done when:** A supported-harness task-class matrix proves relevant guidance is offered, irrelevant guidance is omitted, and used, declined, unavailable, and not-applicable outcomes are attributable.
- **Maps to:** F3, B2, B6, B8; J7, J10, J11.

### BL-P1-19 - Implement the human-approved public defect relay

- **Action:** Add existing-issue search, exact outgoing preview, discard/link/create choices, governed GitHub execution, rate limits, and durable fingerprint/public-link state.
- **Done when:** A disposable-repository journey proves redaction, deduplication, separation of duty, exact-payload approval, governed retry, rate limiting, and no automatic publication.
- **Maps to:** F3, F8, B2, B4, B7, B8; J8, J9, J11.

### BL-P1-20 - Complete ephemeral peer-review admission controls

- **Action:** Make existing-peer help the default, gate and disable automatic or ephemeral launch independently, and separate worker transport from withdrawn Runtime activation.
- **Done when:** Controlled real-provider journeys cover unavailable tools, timeout, mutation attempts, source changes, malformed answers, retry, cleanup, cost, and explicit disablement.
- **Maps to:** F3, B2, B4, B8; J7, J8, J11.

### BL-P1-21 - Implement optional verified SMTP mailbox copies

- **Action:** Add an explicitly consented, encrypted, per-destination SMTP copy path with bounded retry and uncertainty handling.
- **Done when:** Real-provider journeys prove destination verification, consent changes, redacted diagnostics, stable message identity, bounded retry, uncertainty without blind resend, and no effect on durable local mailbox state.
- **Maps to:** F3, F7, B2, B8; J7-J9, J11.

### BL-P2-02 - Validate external exact-candidate evidence retention

- **Action:** Validate operator-owned external retention and retrieval for quality, UAT, backup, restore, promotion, and rollback results by exact candidate and environment.
- **Done when:** A repeatable probe retrieves hash-verifiable evidence under a declared access and retention policy and reports missing or expired evidence distinctly.
- **Maps to:** B5, B8; J11.

### BL-P2-03 - Validate deferred dependency isolation

- **Action:** Check whether withdrawn modules create concrete import cycles or startup risks in supported paths and isolate only demonstrated defects.
- **Done when:** A dependency probe either proves no actionable defect or identifies and verifies a bounded isolation change without speculative compatibility scaffolding.
- **Maps to:** B2, B4, B6; J7, J11.

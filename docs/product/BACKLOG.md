# Brains Core Backlog

This is the sole schedulable product backlog. It contains unfinished work required for
one local human operator to coordinate multiple Workspaces and agent Sessions through
one supervised local Brains service.

Every item names an action and an observable completion condition. Remove an item when
that condition is satisfied. Deferred work belongs in
[FROZEN_BACKLOG.md](FROZEN_BACKLOG.md) and cannot be scheduled until this backlog is
empty and a human explicitly approves thawing it.

## P0 — Core integrity

### BL-P0-06 - Validate supported local installation, wiring, and transport

- **Action:** Validate package, executable, endpoint, and wire identity, safe harness transport selection, and service lifecycle on the declared supported host/harness matrix.
- **Done when:** Clean-host install, wire, start, stop, restart, persistence, uninstall, and configuration-preservation journeys pass for every declared combination.
- **Maps to:** F0, F7, B6, B8, B9; J1, J9, J11.

## P1 — Core completion

### BL-P1-09 - Validate local readiness and recovery

- **Action:** Make readiness distinguish gateway/MCP protocol state, SQLite integrity, queue/mailbox progress, and declared backup, restore, and rollback capability in the supported local topology.
- **Done when:** Local failure drills identify the affected dependency without secrets and the documented probe restores a compatible candidate and re-establishes truthful readiness.
- **Maps to:** B5, B6, B8; J1, J7, J11.

### BL-P1-10 - Implement core-surface advertisement checks

- **Action:** Generate CLI, MCP, alias, wire, route, navigation, configuration, extra, and documentation inventories that enforce the core/frozen boundary.
- **Done when:** Documentation and CI fail whenever a frozen, withdrawn, or undocumented surface becomes discoverable or activatable in a normal installation.
- **Maps to:** F0-F10, B1-B9; J1-J11.

### BL-P1-12 - Complete supported harness wakeup adapters

- **Action:** Install consented body-free hook or plugin wakeups where a supported harness permits them and expose truthful pull fallback everywhere else.
- **Done when:** Supported-harness journeys prove wakeup or pull fallback, bounded retry and uncertainty, and zero message-body or credential disclosure.
- **Maps to:** F3, B2, B8; J7, J8, J11.

### BL-P1-14 - Complete harness identity and presence lifecycle

- **Action:** Preserve raw adapter provenance and close native-ID extraction, renewal, detach, conflict, binding recovery, restart, and resume gaps across supported harnesses.
- **Done when:** Idle, abrupt-exit, restart, Workspace movement, conflict, and resume journeys keep identity, activity, ownership, and reachability distinct and recoverable.
- **Maps to:** F3, B2, B8; J7, J8, J11.

### BL-P1-18 - Complete truthful default lookup guidance

- **Action:** Remove semantic and graph recommendations from default wire guidance and use the existing non-embedding knowledge and substring/symbol lookup path.
- **Done when:** A fresh normal installation performs a Workspace lookup without embeddings and distinguishes empty results from unavailable capabilities across CLI, MCP, wire copy, and browser guidance.
- **Maps to:** B2, B3, B6, B8; J7, J11.

## P3 — Core experience

### BL-P3-01 - Complete normal-console behavior and accessibility

- **Action:** Add non-disclosing unknown-route handling, finish API-error-versus-empty behavior, remove redirects into frozen or withdrawn screens, and close keyboard, focus, labels, contrast, responsive, and connection-degradation gaps.
- **Done when:** Blocking component tests and isolated core-route sweeps pass with no Labs flag, no frozen or withdrawn navigation, and truthful ready, empty, degraded, error, unauthorized, and not-found states.
- **Maps to:** F0, F3, F7, B8, B9; J1, J7-J11.

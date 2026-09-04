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

### BL-P1-12 - Validate native Claude wakeup recovery

- **Action:** Validate consented Claude hook configuration exchange, owner-only recovery state, and interruption recovery on isolated native Windows and macOS homes.
- **Done when:** The exact candidate passes real Windows `ReplaceFileW`, DPAPI, and current-user-DACL recovery plus real macOS `renamex_np(RENAME_SWAP)` and owner-only-mode recovery, including abrupt install and removal interruption at every durable transaction phase, exact prior-configuration restoration, and a blocking pinned-Claude discovery and continuation probe.
- **Maps to:** F3, B2, B8; J7, J8, J11.

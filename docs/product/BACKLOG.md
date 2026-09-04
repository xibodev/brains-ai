# Brains Core Backlog

This is the sole schedulable product backlog. It contains unfinished work required for
one local human operator to coordinate multiple Workspaces and agent Sessions through
one supervised local Brains service.

Every item names an action and an observable completion condition. Remove an item when
that condition is satisfied. Deferred work belongs in
[FROZEN_BACKLOG.md](FROZEN_BACKLOG.md) and cannot be scheduled until this backlog is
empty and a human explicitly approves thawing it.

## P0 — Core integrity

### BL-P0-06 - Complete persistent native service-manager lifecycle qualification

- **Action:** Implement a persistent disposable-host journey that resumes the exact installed candidate across an actual login or machine-observed reboot on Windows Task Scheduler, macOS launchd, and Linux systemd-user.
- **Done when:** Every supported native combination proves owned start, stop, restart, manager recovery, persistence, uninstall, listener teardown, and exact prior-configuration restoration.
- **Maps to:** F0, F7; B6, B8; J1, J9, J11.

## P1 — Core completion

### BL-P1-12 - Complete native Claude recovery exact-candidate qualification

- **Action:** Complete exact-candidate qualification of consented Claude hook configuration exchange, owner-only recovery state, and interrupted transaction recovery on isolated native Windows and macOS homes.
- **Done when:** One candidate and package provenance passes real Windows `ReplaceFileW`, DPAPI, and current-user-DACL recovery plus real macOS `renamex_np(RENAME_SWAP)` and owner-only-mode recovery; abrupt install and removal interruption at the prepared, swapped, validated, and metadata phases; exact prior-configuration restoration; and the blocking pinned-Claude discovery and continuation probe.
- **Maps to:** F3; B2, B8; J7, J8, J11.

### BL-P1-22 - Implement exact-candidate qualification and publication gating

- **Action:** Implement one fail-closed qualification aggregate that consumes BL-P0-06, BL-P1-12, the native installation and wiring matrix, isolated Docker harness probes, container runtime smoke against the exact candidate OCI image manifest digest, and the full repository gate under one candidate source, wheel, sdist, and OCI-image provenance, then gate publication on that result under protected approval.
- **Done when:** One sanitized result binds the candidate source, wheel, sdist, and OCI image manifest digests; proves required matrix completeness and teardown; distinguishes required from inapplicable checks; rejects every missing, skipped, stale, rebuilt, or mismatched artifact or input; and publication reuses only the exact qualified wheel, sdist, and OCI image after protected approval.
- **Maps to:** F0, F3, F7; B2, B6, B8; J1, J7, J9, J11.

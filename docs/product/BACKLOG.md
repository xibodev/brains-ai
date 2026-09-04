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

- **Action:** Execute the machine-readable workflow matrix `task-scheduler/windows-2022 | launchd/macos-14 | systemd-user/ubuntu-24.04` × `CPython 3.11 | CPython 3.12` × `copilot-cli | claude-code | codex | opencode` × `streamable-http`; validate one provenance-bound package, executable, endpoint, wire identity, native manager lifecycle, and login/reboot boundary per combination.
- **Done when:** Every declared matrix cell proves clean-host install, wire, start, stop, restart, owned-process recovery, login and reboot persistence, uninstall, listener teardown, and exact configuration restoration from sanitized structured evidence bound to the candidate commit and installed wheel.
- **Maps to:** F0, F7, B6, B8, B9; J1, J9, J11.

## P1 — Core completion

### BL-P1-12 - Complete supported harness wakeup adapters

- **Action:** Install consented body-free hook or plugin wakeups where a supported harness permits them and expose truthful pull fallback everywhere else.
- **Done when:** Supported-harness journeys prove wakeup or pull fallback, bounded retry and uncertainty, and zero message-body or credential disclosure.
- **Maps to:** F3, B2, B8; J7, J8, J11.

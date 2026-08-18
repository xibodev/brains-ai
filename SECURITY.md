<!--
last_verified: 2026-08-01T19:29:19.185-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD 6eb071bba49a5e678fb6ee8a35a3b21199136374; static inspection of auth, realtime, execution, audit, storage, provider, bridge, and deployment source; deployment not verified
-->

# Security Policy

## Reporting a vulnerability

Do not open a public issue for a security vulnerability.

Use GitHub private vulnerability reporting:

<https://github.com/xibodev/brains-ai/security/advisories/new>

Include:

- affected commit SHA;
- entry point and required privileges;
- reproduction steps;
- expected and actual behavior;
- impact and suggested containment;
- whether the finding involves live credentials or data.

Do not include real secrets in the report.

## Security scope

In scope:

- authentication or authorization bypass on `/v1`, `/app`, `/dashboard`, `/admin`, WS, SSE, MCP, hooks, relay, or Runtime paths;
- cross-Org or cross-Workspace access;
- Runtime enrollment/token race or credential overreach;
- action-gate bypass and pre-approval effects;
- audit-chain integrity or missing governed-action records;
- secret leakage through errors, traces, logs, models, config, browser state, backups, or wiring;
- SSRF, injection, path traversal, unsafe deserialization, command execution, or supply-chain flaws;
- migration, backup, restore, or foreign-key behavior that causes unauthorized access or data loss;
- webhook forgery, replay, or bridge credential failure;
- prompt-injection paths that cross the intended process, network, file, or approval boundary.

Reports against optional upstream providers are normally reported upstream unless Brains' adapter, configuration, or trust boundary creates the vulnerability.

## Current security boundaries

Current HEAD provides:

- API-key enforcement for protected model routes;
- key or signed-cookie authentication for native console routes;
- authenticated WS/SSE and MCP SSE entry points;
- per-trigger and relay credentials;
- hash-only expiring enrollment tokens;
- redaction helpers and bounded gateway error handling;
- faithful explicit-model routing;
- HMAC-chained audit records;
- backup manifest and payload hash checks;
- loopback defaults for primary processes.

These are source-level capabilities, not deployment proof.

## Known limitations

- Accepted API/operator/daemon keys are not consistently bound to one operator with route-level RBAC.
- Org roles are stored but not fully enforced.
- WS topic authorization is missing.
- Runtime daemon credentials are not Runtime-narrow.
- The action gate uses selected PATH shims and is not a universal process/network boundary.
- Optional recurring auto-spawn directly launches a process outside that gate.
- Audit append is best-effort and is not transactional with the primary action.
- SQLite foreign keys are not enabled in connection setup.
- Postgres disk migrations are recorded rather than executed.
- Browser chat is ephemeral; Session message and stop routes are absent.
- Framework OpenAPI/docs routes are open on bound interfaces.
- Query-string credentials remain accepted on some browser/WS paths and may be exposed by surrounding logs.
- The box deployment scaffold is inconsistent and unverified.
- The wa-web companion device can read and send all messages for the linked WhatsApp account; dedicated-chat handling is an application convention, not a permission boundary.

See [Architecture](docs/ARCHITECTURE.md), [Operations](docs/OPERATIONS.md), and the [Backlog](docs/product/BACKLOG.md) for exact mappings.

## Operator hardening

- Keep gateway, dashboard, MCP, and sidecars on loopback or a reviewed private network.
- Use distinct, scoped credentials for database, providers, relay, triggers, and bridges.
- Treat all accepted operator and daemon keys as high authority under current HEAD.
- Disable unused providers, bridges, public binds, recurring spawn, and framework docs at exposed ingress.
- Mount secrets read-only and keep state/backups outside the repository.
- Restrict Runtime working roots and external credentials.
- Prefer a small MCP allowlist.
- Verify backup restoration and rollback in isolation.
- Do not claim hard outward-action enforcement until BL-P0-03 is complete.

## Security acceptance

A security-sensitive change is not accepted until it maps to Feature/Journey/AC IDs, includes negative authorization tests, exercises failure/recovery behavior, and passes the security portions of [QUALITY_GATES.md](docs/QUALITY_GATES.md).

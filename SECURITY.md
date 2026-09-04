# Security Policy

## Reporting a vulnerability

Do not open a public issue for a security vulnerability. Use
[GitHub private vulnerability reporting](https://github.com/xibodev/brains-ai/security/advisories/new).

Include the affected version or commit, entry point, required privileges, minimal
reproduction, impact, and suggested containment. Redact credentials, personal data,
private paths, hostnames, and repository secrets. If a real credential may have been
exposed, revoke or rotate it before sharing diagnostic material.

## Supported security boundary

Security reports are in scope when they affect the advertised Brains core, including:

- authentication or authorization on the local browser, supported `/v1/*` endpoints,
  realtime connections, or MCP transport;
- Workspace isolation, mailbox ownership, credential handling, or configuration
  redaction;
- approval and governed-action integrity;
- audit-chain integrity or attribution;
- injection, path traversal, unsafe deserialization, unintended command execution, or
  supply-chain behavior reachable from a supported surface;
- SQLite migration, backup, restore, or repair behavior that risks disclosure or data
  loss;
- secret disclosure through errors, logs, browser state, backups, packages, or generated
  client configuration.

Code retained only for historical data compatibility is not a supported activation
surface. A vulnerability is still reportable if supported code can reach it or if it
weakens containment of a withdrawn capability.

## Operating safely

- Keep HTTP and MCP listeners on loopback unless a separately reviewed boundary protects
  them.
- Protect the admin key, operator credentials, state directory, configuration, and
  backups with operating-system access controls.
- Use separate synthetic state and credentials for tests.
- Review generated client configuration before installing it and remove obsolete
  bindings when a client is retired.
- Verify backups and restores in an isolated destination before relying on them.
- Treat the action boundary as cooperative application enforcement, not universal
  operating-system or network confinement.
- Do not enable or expose retained, frozen, or withdrawn subsystems merely because code
  or schema objects remain present.

The current product boundary and explicit limitations are documented in
[Architecture](docs/ARCHITECTURE.md), [Operations](docs/OPERATIONS.md), and the
[Product Brief](docs/product/PRODUCT_BRIEF.md). Deferred security work is listed in the
[Frozen Backlog](docs/product/FROZEN_BACKLOG.md), not represented as implemented here.

Security-sensitive changes should include negative authorization tests, failure and
recovery behavior, and the relevant checks from
[Quality Gates](docs/QUALITY_GATES.md).

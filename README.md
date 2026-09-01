<!--
last_verified: 2026-08-31T18:30:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 35ce5ff1b4a2eb8bce2777ca7e3cff4d7ceece99 plus the worktree contract correction and isolated Docker full quality, packaged browser, and real OpenCode/Claude/Codex mailbox UAT; installed-service recovery and deployment not verified
-->

# Brains

Brains is a local-first operator control plane for coordinating AI coding agents through shared Workspaces, durable work, and human approvals.

Current maturity: Brains is an alpha release. The normal product is the Workspace-first coordination, governance, operations, access/configuration, GitHub-linkage, and local-lookup surface. Withdrawn implementations are not product claims even where containment removal from current source remains open. Live deployment and external provider behavior are not certified by repository evidence.

Brains is the canonical product and repository identity. It is distributed as `brains-ai`, uses the `brains` Python namespace, `brains_` MCP prefix, `~/.brains` state directory, `brains-spa` frontend package, and Brains browser identity.

## Install

Brains requires Python 3.11 or 3.12. Install the CLI in an isolated environment:

```text
python -m pip install --user pipx
python -m pipx ensurepath
pipx install brains-ai
```

Initialize Brains for the project you want it to coordinate and install the supervised
user service:

```text
cd <project>
brains-ai setup --path . --service
```

The service starts without a terminal window, restarts on failure, and starts again at
login. Verify it with `brains-ai service status`, then open
`http://127.0.0.1:8787/app`. The setup command prints the generated admin-key location;
reveal it only when needed with `brains-ai admin-key show --reveal`.

Use `brains-ai serve-all` only when foreground logs are useful for diagnosis or
development. Upgrade an existing isolated installation with `pipx upgrade brains-ai`.

## Canonical documentation

- [Product brief](docs/product/PRODUCT_BRIEF.md)
- [Feature contract](docs/product/FEATURE_CONTRACT.md)
- [User outcome specification](docs/product/USER_OUTCOME_SPEC.md)
- [Personas and journeys](docs/product/PERSONAS_AND_JOURNEYS.md)
- [Traceability](docs/product/TRACEABILITY.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Quality gates](docs/QUALITY_GATES.md)
- [Backlog registry](docs/product/BACKLOG.md)
- [Active feature backlog](docs/product/ACTIVE_BACKLOG.md)
- [Experimental feature backlog](docs/product/EXPERIMENTAL_BACKLOG.md)

## Repository guidance

- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Agent instructions](AGENTS.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [MIT License](LICENSE)

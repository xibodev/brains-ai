<!--
last_verified: 2026-08-06T19:13:03.000-06:00
verified_by: GitHub Copilot CLI
verification_basis: clean-state corrective candidate based on HEAD 96c2b66fe8adddd9ea29f59f2944e8e702453f27; source inspection and regression coverage for first-run SQLite state creation; public package/GHCR/GitHub publication pipeline verified; corrective publication and live deployment not verified
-->

# Brains

Brains is a local-first operator control plane for coordinating AI coding agents across machines, tools, work, and human approvals.

Current maturity: Brains is an alpha release. Core local workflows are covered by blocking CI and sealed local UAT. Live deployment and external provider or bridge behavior depend on operator configuration and are not certified by repository evidence.

Brains is the canonical product and repository identity. It is distributed as `brains-ai`, uses the `brains` Python namespace, `brains_` MCP prefix, `~/.brains` state directory, `brains-spa` frontend package, and Brains browser identity.

## Install

Brains requires Python 3.11 or 3.12. Install the CLI in an isolated environment:

```text
python -m pip install --user pipx
python -m pipx ensurepath
pipx install brains-ai
```

Initialize Brains for the project you want it to coordinate, then start the supervised gateway, browser console, and MCP server:

```text
cd <project>
brains-ai setup --path .
brains-ai serve-all
```

Open `http://127.0.0.1:8787/app`. The setup command prints the generated admin-key location; reveal it only when needed with `brains-ai admin-key show --reveal`.

To start Brains automatically at login, use `brains-ai setup --path . --service`. Upgrade an existing isolated installation with `pipx upgrade brains-ai`.

## Canonical documentation

- [Product brief](docs/product/PRODUCT_BRIEF.md)
- [Feature contract](docs/product/FEATURE_CONTRACT.md)
- [User outcome specification](docs/product/USER_OUTCOME_SPEC.md)
- [Personas and journeys](docs/product/PERSONAS_AND_JOURNEYS.md)
- [Traceability](docs/product/TRACEABILITY.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Quality gates](docs/QUALITY_GATES.md)
- [Current backlog](docs/product/BACKLOG.md)

## Repository guidance

- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Agent instructions](AGENTS.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [MIT License](LICENSE)

# Brains

Brains is a local-first control plane for coordinating AI coding agents through shared
Workspaces, durable work, local mailboxes, and human approvals.

Brains is currently an alpha product for one local human operator. The supported core
is Workspace-first coordination, governance, operations, bounded repository lookup,
and local configuration. Deferred and withdrawn implementations are not supported
product capabilities even when compatibility code or historical database tables remain.

The project is distributed as `brains-ai`, uses the `brains` Python namespace, exposes
MCP tools with the `brains_` prefix, stores state under `.brains`, and serves the Brains
browser application from `/app`.

## Install and run

Brains requires Python 3.11 or 3.12. Install it in an isolated environment:

```text
python -m pip install --user pipx
python -m pipx ensurepath
pipx install brains-ai
```

Initialize a Workspace and run Brains in the foreground:

```text
cd <project>
brains-ai setup --path .
brains-ai serve-all
```

Open `http://127.0.0.1:8787/app`. Keep the generated admin key private.

Native service installation commands are implemented, but clean-host lifecycle and
persistence validation remains an open backlog item. See
[Operations](docs/OPERATIONS.md) before relying on a background service.

## Documentation

- [Product brief](docs/product/PRODUCT_BRIEF.md)
- [Feature contract](docs/product/FEATURE_CONTRACT.md)
- [User outcome specification](docs/product/USER_OUTCOME_SPEC.md)
- [Personas and journeys](docs/product/PERSONAS_AND_JOURNEYS.md)
- [Traceability](docs/product/TRACEABILITY.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Quality gates](docs/QUALITY_GATES.md)
- [Core backlog](docs/product/BACKLOG.md)
- [Frozen backlog](docs/product/FROZEN_BACKLOG.md)

See [Contributing](CONTRIBUTING.md), [Security](SECURITY.md), the
[Code of Conduct](CODE_OF_CONDUCT.md), and the [MIT License](LICENSE).

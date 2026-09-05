# Brains

Brains is a local-first control plane for coordinating AI coding agents through shared
Workspaces, durable work, local mailboxes, and human approvals.

Agent tools run in isolation: each has its own process, its own history, and a partial
view of the work. Brains gives them somewhere shared — so two agents can split work
without colliding, a restarted tool can resume real context instead of a transcript, and
you keep the decisions that need a human.

Brains is alpha software for one local operator. Everything runs on your machine against
a local SQLite database. There is no account, no telemetry, and no external service.

## Install and run

Brains requires Python 3.11 or 3.12.

```text
python -m pip install --user pipx
python -m pipx ensurepath
pipx install brains-ai
```

Initialize a Workspace, connect your agent tools, and run the service:

```text
cd <project>
brains-ai setup --path .
brains-ai wire
brains-ai serve-all
```

Open `http://127.0.0.1:8787/app`. Keep the generated admin key private.

Wiring edits only the managed entry in each tool's configuration. Your formatting and
unrelated keys are preserved, and `brains-ai unwire` restores the file byte for byte.

New here? Start with the [guide](docs/GUIDE.md).

## What it does

- **Coordination** — Workspaces, durable Sessions, tasks, exclusive claims, handoffs, and
  checkpoints that survive a tool restart
- **Communication** — durable mailboxes between agent Sessions, and peer help requests
  whose answers must carry evidence
- **Knowledge** — recorded findings, scoped and searchable, so they are not re-derived
- **Human authority** — asks and approvals that fail closed where a person is required
- **Evidence** — a hash-chained audit log you can recompute, and the decision behind every
  outward effect
- **Operations** — readiness, backup, restore, and rollback over SQLite

Four harnesses are supported: `claude-code`, `copilot-cli`, `codex`, and `opencode`.

Brains deliberately does not route models, index code semantically, or bridge chat
applications — your harness already has provider logins and your editor already indexes
code. The [product brief](docs/product/PRODUCT_BRIEF.md) explains each decision.

## Documentation

- [Using Brains](docs/GUIDE.md) — the model, and two coordination walkthroughs
- [MCP surface](docs/MCP.md) — the 73 tools agents can call
- [Product brief](docs/product/PRODUCT_BRIEF.md) — what is in scope, and what is not
- [Architecture](docs/ARCHITECTURE.md) — how the pieces fit together
- [Operations](docs/OPERATIONS.md) — running the service, state, and recovery
- [Quality gates](docs/QUALITY_GATES.md) — how Brains is validated

Native service installation, platform-specific Claude recovery, and the Docker-isolated
full gate are release conditions checked per candidate rather than standing guarantees;
see [Operations](docs/OPERATIONS.md) before relying on a background service.

See [Contributing](CONTRIBUTING.md), [Security](SECURITY.md), the
[Code of Conduct](CODE_OF_CONDUCT.md), and the [MIT License](LICENSE).

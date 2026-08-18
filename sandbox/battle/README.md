<!--
last_verified: 2026-08-01T19:29:19.185-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD 6eb071bba49a5e678fb6ee8a35a3b21199136374; static inspection of battle image, compose, scenario, seed, and rendering harness files; deployment not verified
-->

# Shared-database coordination harness

This directory defines a sealed development harness for two Brains processes sharing one Postgres database.

It is retained to exercise Brains coordination behavior. Historical result narratives and screenshot evidence are intentionally not part of the current repository documentation.

## Topology

```text
Postgres 16
  |-- brain-a: gateway/dashboard/MCP on shifted loopback ports
  `-- brain-b: gateway/dashboard/MCP on separate shifted loopback ports
```

Both application containers:

- use container-local HOME/state;
- mount the repository read-only;
- install Postgres dependencies;
- can contain Claude Code and Copilot CLI tools;
- use distinct operator labels;
- share the Postgres schema.

## Retained files

| File | Purpose |
|---|---|
| `Dockerfile.battle` | Development image with project, Postgres drivers, Node, and agent CLIs |
| `compose.battle.yml` | Postgres plus two application processes and shifted ports |
| `scenarios.py` | Machine-readable coordination scenarios |
| `seed.py` | Optional development data |
| `compress_demo.py` | Context-compression demonstration code |
| `render_preview.py` | Isolated graph rendering helper |

## Source-defined start and teardown

```text
docker build -f sandbox/battle/Dockerfile.battle -t brains-battle:dev .
docker compose -f sandbox/battle/compose.battle.yml up -d pg brain-a
docker compose -f sandbox/battle/compose.battle.yml up -d brain-b
docker compose -f sandbox/battle/compose.battle.yml down -v
```

Individual scenarios are invoked inside a container through `scenarios.py`. Read its dispatch table before use.

## Safety and proof boundary

- Do not pass real provider credentials unless the operator has approved the exact test.
- Do not treat shared database access as tenant isolation.
- Do not treat baked third-party CLIs as authenticated.
- Do not publish the shifted ports beyond loopback without a separate review.
- A harness file or `RESULT PASS` line is not accepted evidence without exact SHA, environment, command, and retained machine-readable output.

This verification did not build or run the harness.

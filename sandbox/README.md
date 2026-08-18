<!--
last_verified: 2026-08-01T19:29:19.185-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD 6eb071bba49a5e678fb6ee8a35a3b21199136374; static inspection of sandbox Dockerfile, compose, state, mounts, ports, and commands; deployment not verified
-->

# Isolated Brains sandbox

This harness is intended to exercise Brains through `brains-ai` without using the host's normal Brains state or agent configuration.

## Isolation defined in source

- host repository mounted read-only at `/opt/brains`;
- container HOME at `/home/agent`;
- state and SQLite database under `/home/agent/.brains`;
- empty agent-tool configuration directories in the image;
- loopback-only shifted host ports:
  - gateway `18787`;
  - dashboard `19876`;
  - MCP `19877`;
- named container/network state removed by `down -v`.

The compose file permits MCP to bind all container interfaces while host publication remains loopback-only and MCP authentication remains enabled.

## Source-defined lifecycle

```text
docker compose -f sandbox/docker-compose.yml up -d --build
docker compose -f sandbox/docker-compose.yml exec sandbox bash
docker compose -f sandbox/docker-compose.yml exec -w /opt/brains sandbox python -m pytest -q -p no:cacheprovider
docker compose -f sandbox/docker-compose.yml down -v
```

Gateway liveness is configured at `http://127.0.0.1:18787/health`.

## Proof boundary

The harness definition is present, but this documentation verification did not build or run it. It is not evidence of:

- a clean image build;
- a passing test suite;
- provider readiness;
- complete J1-J11 UAT;
- deployment readiness.

Use [OPERATIONS.md](../docs/OPERATIONS.md) and [QUALITY_GATES.md](../docs/QUALITY_GATES.md) to turn a harness run into accepted evidence.

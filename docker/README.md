<!--
last_verified: 2026-08-01T19:29:19.185-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD 6eb071bba49a5e678fb6ee8a35a3b21199136374; static inspection of root and development Dockerfiles, compose, supervisor defaults, and health route; deployment not verified
-->

# Brains container development

The repository's runtime container runs Brains through the `brains-ai` executable.

## Root runtime image

The root `Dockerfile` defines:

- Python 3.12 slim runtime;
- non-root UID/GID 1000;
- `/data` HOME and `BRAINS_STATE_DIR=/data/.brains`;
- `brains-ai` entrypoint with `serve-all` default;
- gateway liveness check at `http://127.0.0.1:8787/health`;
- exposed ports 8787, 9876, and 9877.

The default services bind loopback inside the container. Host publication requires explicit internal bind settings, and MCP public bind requires its separate opt-in. Image build, registry publication, and deployment are not verified by this document.

## Development compose

`docker/docker-compose.dev.yml` intends to provide:

- editable source at `/app/src`;
- isolated named state volume;
- loopback-only shifted host ports:
  - gateway `18787`;
  - dashboard `19876`;
  - MCP `19877`.

## Broken path

`docker/Dockerfile.dev` uses `ENTRYPOINT ... "brains"`, but `pyproject.toml` installs only `brains-ai`. The development image is therefore not a supported runnable path until BL-P0-06 repairs it.

Do not copy old launch commands or present this harness as working. Use the root image or [sandbox harness](../sandbox/README.md) only after validating it for the exact HEAD.

Operational contracts and other container blockers are in [OPERATIONS.md](../docs/OPERATIONS.md).

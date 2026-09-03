<!--
last_verified: 2026-08-30T09:45:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 4e4819f02c621db5ceb75a13328a741208abdf42 plus candidate inspection of Docker-only quality and browser UAT runner contracts; deployment not verified
-->

# Contributing to Brains

Brains is the product and implementation identity across this repository.

## Start with the product contract

Before coding:

1. Read the [Product Brief](docs/product/PRODUCT_BRIEF.md).
2. Identify affected [Feature](docs/product/FEATURE_CONTRACT.md), Persona, Journey, and acceptance IDs.
3. Inspect [Traceability](docs/product/TRACEABILITY.md) for the UI, API, control, data, and test surfaces.
4. Apply the [Quality Gates](docs/QUALITY_GATES.md).
5. Check the [Current Backlog](docs/product/BACKLOG.md) for dependencies and known gaps.

## Development setup

Recommended:

```text
uv sync --extra dev
```

Plain editable installation:

```text
python -m venv .venv
pip install -e ".[dev]"
```

The project requires Python 3.11 or newer. Optional provider, database, bridge, and telemetry subsystems use extras declared in `pyproject.toml`.

## Targeted validation

Run the smallest commands that cover the change:

```text
python scripts/check_docs.py
python scripts/check_traceability.py
uv run ruff check <changed-python-paths>
uv run ruff format --check <changed-python-paths>
uv run mypy
uv run pytest -q <relevant-tests>
```

For modern console changes:

```text
cd frontend
npm ci
npm run typecheck
npm run build
```

`npm run build` writes the bundle CI compares against, so a console source
change must be committed together with the rebuilt `src/brains/web/spa`. To
check the committed bundle without touching it, run
`python scripts/check_spa_bundle.py`.

For browser journeys, follow [tests/e2e/README.md](tests/e2e/README.md).

## Docker-only validation

Before opening a pull request, run the candidate quality gate in Docker:

```text
pwsh -File scripts/run_docker_quality.ps1
```

It bakes the complete candidate source and locked Python/Node dependencies into a
disposable image, then runs without a network, host mount, Linux capabilities, or
published port. It covers documentation, traceability, Ruff, mypy, acceptance and full
pytest, both TypeScript checks, the committed SPA bundle, package build, and distribution
contents.

Run browser UAT separately:

```text
pwsh -File scripts/run_docker_e2e.ps1
```

The UAT runner publishes no host port, uses an internal Docker network and tmpfs state,
passes only a synthetic seed manifest to Playwright, and removes only artifacts it
created. Both runners refuse pre-existing artifact names. `scripts/run_quality_gates.py`
remains the CI-command enumerator and compatibility fallback; it is not the isolated
operator-machine path.

Every job in `.github/workflows/ci.yml` is blocking: documentation and
traceability contracts, Ruff, mypy, pytest on Python 3.11 and 3.12, the
migration and frozen-baseline contract, the SPA typecheck/build/bundle
comparison, the wheel and sdist build, the privacy scan, the container health
smoke, and the Playwright journey suite. The `quality gate` job fails when any
of them failed, was cancelled, or was skipped. State which gates you actually
ran, and on which platform, when you describe evidence.

## Repository layout

| Path | Responsibility |
|---|---|
| `src/brains/api` | Model gateway and native product HTTP/WS surfaces |
| `src/brains/control` | Product and coordination domain controls |
| `src/brains/daemon` | Runtime discovery, heartbeat, assignment, execution |
| `src/brains/exec` | Agent process runner, gate, relay, transcript state |
| `src/brains/mcp` | MCP server and tool registry |
| `src/brains/context` | Indexing, semantic retrieval, graph, freshness |
| `src/brains/router`, `providers` | Model resolution and provider adapters |
| `src/brains/storage` | SQLAlchemy models, database setup, migrations |
| `src/brains/backup`, `audit` | Recovery and audit capabilities |
| `frontend` | Brains React SPA source |
| `src/brains/web/spa` | Checked-in built SPA included in the Python package |
| `src/brains/admin/routes.py` | Modern `/app` sign-in/sign-out cookie endpoints only |
| `tests` | Python contracts and Brains acceptance tests |
| `tests/e2e` | Playwright journey contracts |
| `sandbox`, `sandbox/battle` | Isolated harness definitions, not proof records |

## Change rules

- Keep one concern per pull request.
- Map behavior changes to stable Feature, Journey, and AC IDs.
- Add or update the smallest automated contract that proves the behavior.
- Preserve exact model selection unless the client explicitly requests `brains/auto`.
- Do not bypass protected-route authentication.
- Apply operator/Org/Workspace authorization to every new HTTP, WS, SSE, MCP, and background path.
- Do not add autonomous firing or spawning that bypasses the human-governed contract.
- Add compatible schema migration and recovery behavior for persistent data changes.
- Do not commit credentials, provider tokens, operator keys, personal identifiers, or generated runtime state.
- Do not treat the checked-in SPA bundle as current unless it was rebuilt and compared.
- Update canonical documentation and traceability when any mapped surface changes.
- Keep current docs free of chronology, run diaries, screenshots, pass counts, and tag-based status.

## Pull request flow

1. Work from current `main` on a short-lived branch.
2. State the product outcome and affected IDs.
3. Implement code, tests, failure behavior, and recovery behavior.
4. Update traceability and docs.
5. Run targeted validation and record commands/results in the pull request.
6. Request review against the Definition of Done.
7. Merge only through `main`.

The repository does not use tags or release notes as current product truth. Acceptance is defined by [QUALITY_GATES.md](docs/QUALITY_GATES.md).

## Security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Community and license

Contributors must follow the [Code of Conduct](CODE_OF_CONDUCT.md). Contributions are licensed under the [MIT License](LICENSE).

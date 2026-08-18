<!--
last_verified: 2026-08-04T08:00:00.000-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD c21a15db3859e6b9f147260a38a7a0d6fe2533b2 plus the local blocking-quality-gates change; static inspection of project manifest, workflows, source layout, tests, and canonical quality contract, with a local Windows execution of every documented gate command including the container health smoke and the Playwright journey suite; a GitHub-hosted workflow run and deployment not verified
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

## Full local gate

Before opening a pull request, run the exact local equivalent of the workflow:

```text
python scripts/run_quality_gates.py
```

It runs, in CI order: the documentation contract, the generated traceability
contract, Ruff lint and format, mypy, the acceptance subset, the full pytest
suite, `npm ci`, the SPA typecheck, the committed-bundle comparison, the
distribution build, and the shipped-data assertions. Use `--fast` to swap the
full sweep for the contract self-tests, `--no-spa` to skip the Node gates, and
`--list` to print the commands without running them.

The Docker smoke and Playwright gates are not run by that script because they
need a Docker daemon, browsers, and an ephemeral hub. Run them explicitly when
your change touches those surfaces.

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
| `src/brains/dashboard`, `admin` | Legacy server-rendered surfaces |
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

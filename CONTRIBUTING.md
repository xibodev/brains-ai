# Contributing to Brains

## Start with the product contract

Before changing behavior:

1. Read the [Product Brief](docs/product/PRODUCT_BRIEF.md).
2. Identify the affected Feature, Persona, Journey, and acceptance identifiers.
3. Inspect [Traceability](docs/product/TRACEABILITY.md) for the corresponding UI, API,
   control, data, and test surfaces.
4. Check the [Core Backlog](docs/product/BACKLOG.md). Deferred work remains in the
   [Frozen Backlog](docs/product/FROZEN_BACKLOG.md) and is not a current capability.

## Development setup

Brains supports Python 3.11 and 3.12. The only optional dependency group is `dev`.

```text
uv sync --extra dev
```

Or use a conventional virtual environment:

```text
python -m venv .venv
python -m pip install -e ".[dev]"
```

## Validation

Run the smallest checks that cover the change:

```text
python scripts/check_docs.py
python scripts/check_traceability.py
uv run ruff check <changed-python-paths>
uv run ruff format --check <changed-python-paths>
uv run mypy
uv run pytest -q <relevant-tests>
```

The complete gate and its isolated runners are documented in
[Quality Gates](docs/QUALITY_GATES.md). Report only commands that actually ran and their
environment; the existence of a test is not proof that a candidate passed it.

### Browser application

The React, TypeScript, and Vite source is under `frontend`. The built application is
served by FastAPI from `/app` and checked into `src/brains/web/spa` for packaging.

```text
cd frontend
npm ci
npm run typecheck
npm run build
```

Use `npm ci` so the lockfile remains authoritative. A `frontend/src` change must include
the rebuilt bundle. From the repository root, this command checks bundle parity without
replacing the committed output:

```text
python scripts/check_spa_bundle.py
```

### Browser journeys

Playwright journey specifications are under `tests/e2e/specs`. Run the isolated browser
suite with:

```text
pwsh -File scripts/run_docker_e2e.ps1
```

Use synthetic state and credentials. Assert the visible product outcome, expected error
and authorization states, reconnect/recovery behavior, and the relevant `J*`/`AC-*`
contract. Do not commit screenshots or reports as current product proof.

## Supported code map

| Path | Responsibility |
|---|---|
| `src/brains/api` | Supported local HTTP and realtime endpoints plus retained compatibility code |
| `src/brains/control` | Coordination, governance, mailbox, configuration, and recovery controls |
| `src/brains/mcp` | MCP server and advertised tool registry |
| `src/brains/storage` | SQLite models, setup, and migrations |
| `src/brains/backup`, `src/brains/audit` | Recovery and audit capabilities |
| `frontend` | Brains browser application source |
| `src/brains/web/spa` | Built browser assets included in the package |
| `tests` | Python contracts and acceptance tests |
| `tests/e2e` | Playwright journey contracts |

Other modules may remain solely to open, inspect, or migrate historical state. Their
presence is not an activation or support promise.

## Change rules

- Keep one concern per pull request and explain the user-visible outcome.
- Map behavior changes to stable Feature, Journey, and acceptance identifiers.
- Add the smallest automated contract that proves both success and failure behavior.
- Preserve authentication and Workspace scope on protected HTTP, realtime, MCP, and
  background paths.
- Do not add autonomous execution or outward effects that bypass human governance.
- Include migration and recovery behavior for persistent changes.
- Never commit credentials, personal identifiers, private configuration, or runtime
  state. Use synthetic fixtures and placeholder-only example files.
- Keep documentation free of release chronology, run diaries, screenshots, dated pass
  counts, and tag-based status.

Report vulnerabilities privately as described in [Security](SECURITY.md). Contributors
must follow the [Code of Conduct](CODE_OF_CONDUCT.md); contributions use the
[MIT License](LICENSE).

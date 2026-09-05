# Repository guidance

Brains is a public repository. Never commit credentials, private configuration,
personal data, private infrastructure identifiers, or generated runtime state. Use
placeholder-only `*.example` files and synthetic test data.

## Product contract

- Keep the `brains-ai` distribution and CLI, `brains` Python namespace, `brains_` MCP
  prefix, `.brains` state directory, and Brains browser identity aligned.
- Start with [PRODUCT_BRIEF.md](docs/product/PRODUCT_BRIEF.md), then map behavior through
  [FEATURE_CONTRACT.md](docs/product/FEATURE_CONTRACT.md),
  [PERSONAS_AND_JOURNEYS.md](docs/product/PERSONAS_AND_JOURNEYS.md), and
  [TRACEABILITY.md](docs/product/TRACEABILITY.md).
- [BACKLOG.md](docs/product/BACKLOG.md) is the schedulable core backlog.
  [FROZEN_BACKLOG.md](docs/product/FROZEN_BACKLOG.md) contains deferred work and is not
  a current product promise.
- Distinguish implemented behavior, target contracts, and evidence gaps. Retained
  compatibility code does not make a capability supported.

## Implementation rules

- Keep changes minimal, functional, and mapped to stable `F*`, `B*`, `P*`, `J*`, and
  `AC-*` identifiers where applicable.
- SQLite is the supported source of truth. Markdown projections under `.brains/views`
  are optional.
- Do not bypass authentication on protected `/v1/*` routes.
- Approval, recurring execution, process spawning, and outward effects remain
  human-governed. Do not claim stronger enforcement than the code provides.
- Persistent changes require compatible migration, failure, and recovery behavior.
- Rebuild `src/brains/web/spa` when `frontend/src` changes.
- Update canonical documentation and traceability when routes, components, APIs,
  controls, models, migrations, CLI/MCP families, tests, or operational contracts
  change.

## Validation

Run the smallest relevant tests plus:

```text
python scripts/check_docs.py
python scripts/check_traceability.py
```

Use [QUALITY_GATES.md](docs/QUALITY_GATES.md) for the complete validation contract.
Do not add changelogs, milestone diaries, dated pass counts, screenshot proof packs, or
other files that turn transient evidence into product truth.

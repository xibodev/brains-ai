# Repository guidance

Brains is a public repository. Never commit credentials, private configuration,
personal data, private infrastructure identifiers, or generated runtime state. Use
placeholder-only `*.example` files and synthetic test data.

## Product contract

- Keep the `brains-ai` distribution and CLI, `brains` Python namespace, `brains_` MCP
  prefix, `.brains` state directory, and Brains browser identity aligned.
- Start with [PRODUCT_BRIEF.md](docs/product/PRODUCT_BRIEF.md). It states what is
  available, what is deliberately not in scope, and what is intended but unbuilt.
- Behavior is documented in [GUIDE.md](docs/GUIDE.md) and [MCP.md](docs/MCP.md). The
  supported surface itself is defined in code, by `CORE_MCP_TOOLS` and the `WITHDRAWN_*`
  sets in `src/brains/capabilities.py`, and enforced by `scripts/check_core_surface.py`.
- Open work is tracked in the repository's issues, not in a backlog document.
- Distinguish implemented behavior from intent. Retained compatibility code does not make
  a capability supported, and an unbuilt capability is not advertised.

## Implementation rules

- Keep changes minimal and functional.
- SQLite is the supported source of truth. Markdown projections under `.brains/views`
  are optional.
- Do not bypass authentication on protected `/v1/*` routes.
- Approval, recurring execution, process spawning, and outward effects remain
  human-governed. Do not claim stronger enforcement than the code provides.
- Persistent changes require compatible migration, failure, and recovery behavior.
- Rebuild `src/brains/web/spa` when `frontend/src` changes.
- Update [GUIDE.md](docs/GUIDE.md), [MCP.md](docs/MCP.md), and the SPA route list in
  `scripts/check_traceability.py` when routes, components, APIs, controls, models,
  migrations, or CLI/MCP families change.

## Validation

Run the smallest relevant tests plus:

```text
python scripts/check_docs.py
python scripts/check_traceability.py
```

Use [QUALITY_GATES.md](docs/QUALITY_GATES.md) for the complete validation contract.
Do not add changelogs, milestone diaries, dated pass counts, screenshot proof packs, or
other files that turn transient evidence into product truth.

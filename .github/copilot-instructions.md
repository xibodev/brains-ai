<!--
last_verified: 2026-08-04T08:00:00.000-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD c21a15db3859e6b9f147260a38a7a0d6fe2533b2 plus the local blocking-quality-gates change; static inspection of repository instructions, MCP naming, and canonical documentation; deployment not verified
-->

# Brains repository instructions

1. Start a Brains coordination session and read its welcome packet when the tools are available.
2. Before non-trivial code or documentation work, call `brains_plan_request`.
3. Treat **Brains** as the product, repository, package, namespace, CLI, MCP, state, and browser identity.
4. Read the canonical authorities:
   - `docs/product/PRODUCT_BRIEF.md`
   - `docs/product/FEATURE_CONTRACT.md`
   - `docs/product/PERSONAS_AND_JOURNEYS.md`
   - `docs/product/TRACEABILITY.md`
   - `docs/ARCHITECTURE.md`
   - `docs/OPERATIONS.md`
   - `docs/QUALITY_GATES.md`
   - `docs/product/BACKLOG.md`
5. Map changes to stable `F*`, `B*`, `P*`, `J*`, and `AC-*` IDs.
6. Use public MCP names with the `brains_` prefix, not dotted names.
7. Preserve SQLite as the default source of truth and treat generated Markdown views as optional.
8. Never bypass protected-route authentication or invent RBAC, hard-gate, audit, readiness, UAT, or deployment claims.
9. Keep pattern approval, recurring execution, tool spawning, and outward actions human-governed.
10. Update traceability for routes, components, APIs, controls, models, migrations, CLI/MCP families, tests, and operations.
11. Do not create changelogs, release notes, roadmaps, milestone diaries, logbooks, saga reports, screenshot proof packs, dated test counts, or tag-based truth.
12. Run `python scripts/check_docs.py`, `python scripts/check_traceability.py`, and targeted tests before handoff; `python scripts/run_quality_gates.py` runs the full local gate in CI order.
13. Rebuild and commit `src/brains/web/spa` with any `frontend/src` change: CI compares the committed bundle with a fresh build.

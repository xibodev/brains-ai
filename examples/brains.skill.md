---
name: brains
description: Use Brains to plan, retrieve, coordinate, and hand off work.
---

<!--
last_verified: 2026-08-01T19:29:19.185-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD 6eb071bba49a5e678fb6ee8a35a3b21199136374; static inspection against the current Brains MCP prefix and canonical contracts; deployment not verified
-->

# Brains agent workflow

Brains is the product. Public MCP tools use the `brains_` prefix.

## Start

1. Call `brains_start_session`.
2. Read the returned welcome packet.
3. If unread messages are present, call `brains_read_messages`.
4. Call `brains_plan_request` before non-trivial work.
5. Read the relevant canonical contracts:
   - `docs/product/PRODUCT_BRIEF.md`
   - `docs/product/FEATURE_CONTRACT.md`
   - `docs/product/PERSONAS_AND_JOURNEYS.md`
   - `docs/product/TRACEABILITY.md`
   - `docs/QUALITY_GATES.md`
6. Search shared knowledge before re-deriving a known fact.
7. Claim the workspace before editing shared scope.

## Work

- Map the task to `F*`, `B*`, `P*`, `J*`, and `AC-*`.
- Prefer exact current code and tests over old prose.
- Use tasks, claims, handoffs, messages, checkpoints, and knowledge to avoid collisions.
- Treat generated views as optional; SQLite is the coordination source of truth.
- Do not bypass protected-route authentication.
- Do not describe RBAC, realtime authorization, hard gating, audit completeness, UAT, or deployment as stronger than current evidence.
- Keep patterns, recurring execution, tool spawning, and outward actions human-governed.

## Verify and hand off

1. Run `python scripts/check_docs.py` when documentation is affected.
2. Run the smallest relevant automated tests.
3. Record meaningful work with `brains_append_event`.
4. Leave `brains_set_handoff` when natural next work remains.
5. Call `brains_end_session`.

## Scheduling note

Current recurring schedule grammar is `manual`, `hourly`, `daily`, or `every:<N><s|m|h|d>`. It is not general cron.

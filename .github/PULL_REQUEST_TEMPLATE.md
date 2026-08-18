<!--
last_verified: 2026-08-04T08:00:00.000-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD c21a15db3859e6b9f147260a38a7a0d6fe2533b2 plus the local blocking-quality-gates change; static inspection against the canonical Brains product and quality contracts; deployment not verified
-->

## Product outcome

<!-- What final Brains user or operator outcome changes? -->

## Contract mapping

- Features (`F*` / `B*`):
- Personas (`P*`):
- Journeys (`J*`):
- Acceptance criteria (`AC-*`):
- Backlog item, if any:

## Current behavior and target behavior

<!-- Keep observed current facts separate from the target contract. -->

## Implementation

<!-- UI/API/control/data/migration/operations changes and failure/recovery behavior. -->

## Evidence

<!-- Exact commands run, environment, and results. Do not paste secrets or add repository evidence packs. -->

- [ ] `python scripts/check_docs.py`
- [ ] `python scripts/check_traceability.py`
- [ ] Targeted lint/format/type checks
- [ ] Targeted Python tests
- [ ] Frontend typecheck/build and `python scripts/check_spa_bundle.py` if applicable
- [ ] Playwright journey tests if applicable
- [ ] Migration/backup/restore checks if applicable
- [ ] Isolated UAT if user-visible or operational
- [ ] `python scripts/run_quality_gates.py` (state which gates ran and on which platform)

## Review checklist

- [ ] Protected `/v1/*` routes keep authentication.
- [ ] Operator, Org, Workspace, WS/SSE, MCP, and background authorization is explicit.
- [ ] Human-governed actions do not gain a bypass.
- [ ] Frontend client calls match server routes.
- [ ] The committed `src/brains/web/spa` bundle was rebuilt with any console source change.
- [ ] Persistent changes include migration and recovery behavior.
- [ ] Secrets and personal identifiers are absent.
- [ ] Canonical docs and traceability are updated.
- [ ] No chronology, milestone diary, dated pass count, screenshot proof, or tag-based truth was added.
- [ ] The Definition of Done in `docs/QUALITY_GATES.md` is satisfied or the PR is clearly blocked.

<!--
last_verified: 2026-08-04T08:00:00.000-06:00
verified_by: GitHub Copilot CLI
verification_basis: candidate tree based on HEAD c21a15db3859e6b9f147260a38a7a0d6fe2533b2 plus the local blocking-quality-gates change; local Windows execution of scripts/check_docs.py, scripts/check_traceability.py, ruff check/format, mypy, the full SQLite pytest suite and the acceptance subset, npm ci, the SPA typecheck, the committed-bundle comparison, uv build, scripts/check_distribution.py, the runtime image build with its container health smoke, and the Playwright journey suite against an isolated local hub; a GitHub-hosted workflow run, live Postgres, and deployment not verified
-->

# Brains Quality Gates

## Delivery contract

Every change must preserve a visible chain:

```text
product promise
  -> feature ID
  -> acceptance criterion
  -> persona and journey
  -> code and data surface
  -> automated contract
  -> isolated UAT observation
  -> acceptance decision
```

A build, test file, screenshot, branch, tag, or deployment process is never a substitute for the product outcome at the start of the chain.

## Evidence levels

| Level | Name | Meaning |
|---|---|---|
| E0 | Contract | A stable feature, journey, and acceptance criterion state the intended behavior. |
| E1 | Static presence | Source, configuration, migration, or UI code for the behavior exists at an exact SHA. |
| E2 | Automated contract present | A unit, integration, acceptance, or browser assertion exists at that SHA. It has not necessarily run or passed. |
| E3 | Local execution | Named commands passed in a controlled local environment against the exact SHA. |
| E4 | Isolated UAT | The exact candidate ran in an isolated environment with journey assertions, captured logs, and recovery checks. |
| E5 | Operational observation | A named deployed environment was observed with health, readiness, rollback, and monitoring evidence. |

Current canonical feature status uses only E1 and E2 unless a newer evidence record explicitly identifies the exact candidate and environment. Deployment is not verified by this documentation reset.

## Required gates

### 1. Planning gate

- Name the product outcome being changed.
- Identify affected `F*` or `B*` feature IDs.
- Identify affected `AC-*`, Persona, and `J*` journey IDs.
- Identify security, data, operations, and recovery implications.
- Update [TRACEABILITY.md](product/TRACEABILITY.md) when a route, component, API, model, migration, CLI, MCP tool family, or test family changes.

### 2. Code gate

- Keep Brains product, package, namespace, CLI, MCP, state, and browser language consistent.
- Do not bypass `require_api_key` or `require_console_auth` on protected `/v1/*` routes.
- Do not add an execution, recurring, pattern, or tool-spawn path that evades required human control.
- Preserve SQLite as the default source of truth; generated Markdown views are optional projections.
- Add migrations and rollback or compatibility behavior for persistent schema changes.
- Keep failure behavior explicit and fail closed where the acceptance contract requires authorization or approval.

### 3. Review gate

Reviewers verify:

- the feature and acceptance IDs are correct;
- current and target behavior are not mixed;
- authorization and Org/Workspace scope are applied to HTTP, WS, SSE, MCP, and background execution as applicable;
- frontend client routes exist on the server;
- data writes, events, audit, and retries have a durability contract;
- logs and responses do not expose secrets;
- operational instructions identify unverified or broken paths;
- documentation contains no chronology, milestone diary, dated pass count, screenshot proof, or tag-based truth.

### 4. Automated test gate

The target hard gate includes:

- documentation checker;
- Python lint and format;
- Python type checking;
- targeted unit and integration tests;
- Brains acceptance tests mapped to `AC-*`;
- frontend typecheck and production build;
- source-to-committed-SPA bundle comparison;
- Playwright journey tests;
- container start and health smoke;
- route/client contract checks;
- migration and backup/restore compatibility checks.

Current workflow facts at HEAD:

- Every job in `.github/workflows/ci.yml` is blocking. No required gate carries `continue-on-error`, and the `quality gate` job fails when any dependency failed, was cancelled, or was skipped.
- The blocking jobs are: documentation and generated traceability contract; Ruff lint and format; mypy; pytest (Python 3.11 and 3.12, acceptance subset then the full unit/integration suite); migration and frozen-baseline contract; SPA typecheck, production build and committed-bundle comparison; wheel/sdist build with shipped-data assertions; the privacy scan; the runtime image build and container health smoke; and the Playwright journey suite.
- The generated traceability checker derives SPA routes, API client calls, mounted server routes, SQLAlchemy entities, migrations, and stable-ID test markers from source, and fails on any orphan, unmatched, or duplicate surface. Intentional legacy, external, or dynamic exceptions are explicit allowlists that fail when they stop describing a real exception.
- The bundle gate rebuilds `frontend/src` into a scratch directory and compares it byte-for-byte with the committed `src/brains/web/spa`. It never writes to the tracked bundle, and CI additionally asserts the worktree is unchanged afterwards.
- Failing jobs upload their diagnostics: pytest and migration JUnit XML plus coverage, the rebuilt SPA bundle, container logs, and the Playwright report and hub log.
- A blocking gate is not an evidence claim. J1-J11 all have Playwright contracts, but simulated Runtime/provider paths remain distinct from real external execution and stay recorded as gaps in [TRACEABILITY.md](product/TRACEABILITY.md) and [BACKLOG.md](product/BACKLOG.md).

### Local gate command

The exact local equivalent of the workflow, in CI order:

```text
python scripts/run_quality_gates.py
```

It runs the documentation contract, the generated traceability contract, Ruff lint and format, mypy, the acceptance subset, the full pytest suite, `npm ci`, the SPA typecheck, the committed-bundle comparison, the distribution build, and the shipped-data assertions. `--fast` swaps the full sweep for the contract self-tests; `--no-spa` skips the Node gates; `--list` prints the commands without running them.

The Docker smoke and Playwright gates are deliberately not run by that script: they need a Docker daemon, browsers, and an ephemeral hub. Run them explicitly when the change touches those surfaces, and record which of them actually ran when reporting evidence.

Candidate evidence must state the exact SHA, which gates ran, and on what platform. A local run is E3 evidence for the gates it actually executed and for nothing else.

### 5. Isolated UAT gate

UAT must:

- use an isolated HOME, state directory, database, ports, and credentials;
- use simulated tool execution for browser journeys unless real Runtime behavior is explicitly under test;
- run against a disposable or read-only source tree and fail if the candidate worktree changes;
- stop and verify the complete process tree created by the harness;
- identify the exact SHA and built artifact;
- exercise J1-J11 as applicable without relying on old screenshots or seeded success claims;
- include negative authorization, disconnect/reconnect, failure, recovery, backup, restore, and rollback cases;
- preserve logs and machine-readable results outside the canonical documentation tree;
- distinguish simulated external systems from real integrations.

No UAT result is implied by the presence of `sandbox/`, `sandbox/battle/`, or Playwright files.

### 6. Acceptance gate

Acceptance requires:

- every in-scope AC has E3 or E4 evidence;
- P0 backlog items affecting the candidate are closed or explicitly block acceptance;
- no unmatched frontend route remains;
- no cross-Org authorization or realtime subscription escape is open;
- backup and rollback have been rehearsed for the exact candidate;
- documentation freshness and traceability checks pass;
- the operator confirms the final product goal was met.

## Definition of Done

A change is done only when:

1. Feature, Persona, Journey, and AC mappings are present.
2. Code and data changes implement the stated contract.
3. Failure and recovery behavior are implemented.
4. Targeted automated tests pass.
5. Required hard gates pass.
6. Isolated UAT passes for user-visible or operational changes.
7. Security and authorization implications are reviewed.
8. Canonical and supporting docs describe current facts at the new verification point.
9. Evidence identifies exact code and environment without becoming a repository diary.
10. No runtime, package identity, or deployment claim is made beyond observed evidence.

## Documentation rules

- Canonical authorities are linked from the root README.
- Every claim-bearing canonical and supporting document begins with `last_verified`, `verified_by`, and `verification_basis`.
- `verification_basis` includes a full SHA and states whether deployment was verified.
- Current facts, target contracts, and evidence gaps are visibly distinct.
- Git history and external immutable artifacts hold chronology and run evidence.
- Do not add changelogs, release notes, roadmaps, milestone diaries, saga reports, screenshot proof packs, or test-count ledgers to the current tree.
- Update links whenever a document is renamed or removed.
- Run `python scripts/check_docs.py` and `python scripts/check_traceability.py` before review.

## Branch policy

`main` is the sole integration and truth branch for the product. Work may use short-lived branches, but documentation and acceptance decisions describe the candidate by exact SHA and must converge on `main`.

Until an explicit first release decision is recorded outside these current-state docs:

- no tag defines product truth;
- no tag or version implies readiness;
- no release chronology is maintained in the repository;
- acceptance is based on the quality chain above.

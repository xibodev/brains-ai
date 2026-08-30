<!--
last_verified: 2026-08-02T00:24:58.901-06:00
verified_by: GitHub Copilot CLI
verification_basis: candidate tree based on HEAD 7903eb55ce5fbe6e115169a90263d209e59e4fa4; static inspection and local simulated-harness verification; deployment not verified
-->

# Brains browser journey contracts

This Playwright suite exercises the Workspace-first `/app` console in a normal install (Labs disabled). Withdrawn journeys retain one spec file per stable `J*` ID, and those specs assert containment/fail-closed behavior rather than activation. Test files are contract presence (E2), not proof that the current SHA passed.

## Current spec coverage

| Journey | Spec |
|---|---|
| J1 | `specs/j01-first-run.spec.ts` |
| J2 | `specs/j02-connect-machine.spec.ts` |
| J3 | `specs/j03-personas.spec.ts` |
| J4 | `specs/j04-pods.spec.ts` |
| J5 | `specs/j05-project-workspace.spec.ts` |
| J6 | `specs/j06-issues.spec.ts` |
| J7 | `specs/j07-sessions.spec.ts` |
| J8 | `specs/j08-governance-session-control.spec.ts` |
| J9 | `specs/j09-config-settings.spec.ts` |
| J10 | `specs/j10-automation.spec.ts` |
| J11 | `specs/j11-console-clean.spec.ts` |

J2-J6 and J10 are withdrawn and prove no normal discovery/navigation/activation path. J1 and J7-J11 cover advertised Command Center, Workspaces, Coordination, Governance, Operations/Access/Configuration, Act, and cross-cutting hygiene states. The full mapping and evidence gaps are in [Traceability](../../docs/product/TRACEABILITY.md).

## Run against an isolated stack

```text
cd tests/e2e
npm ci
npm run typecheck
npm run install:browsers
$env:BRAINS_E2E_AUTO_STACK="1"
npm test
```

Configuration:

| Variable | Default | Meaning |
|---|---|---|
| `BRAINS_E2E_BASE_URL` | `http://127.0.0.1:8810` | Isolated gateway origin |
| `BRAINS_E2E_KEY` | `try-brains` | Sign-in key expected by fixtures |
| `BRAINS_E2E_AUTO_STACK` | unset | When `1`, uses the Windows PowerShell global setup/teardown |
| `BRAINS_E2E_STACK_NAME` | `trystack` | Lowercase slug for the owned temporary stack |
| `BRAINS_E2E_SEED_CONTAINER` | unset | Docker container in which direct setup helpers run |
| `BRAINS_E2E_SEED_STATE_DIR` | local temporary state | State path as seen inside that container |

Auto-stack mode rejects non-loopback URLs and passes the configured port, key,
and stack name to setup/teardown as one contract.

When testing an already-running Docker stack, set both seed variables so setup
helpers use the container's filesystem view. Do not open a bind-mounted SQLite
WAL database from the host while the container is running.

The repository's Windows auto-stack scripts:

- resolve the repository from the script location;
- create temporary state and isolated compatibility seed data;
- launch only the gateway with Labs disabled; specs seed required Workspace data;
- never launch a real agent CLI or read the operator's `~/.brains` state;
- stop only the recorded hub process tree;
- compare Git status before and after the run and fail if the worktree changed;
- remove temporary state on teardown.

Do not edit the worktree while the auto-stack is running because the mutation
guard intentionally treats any repository change as a failed test. The harness
is deterministic browser evidence, not proof of Runtime execution-model lifecycle behavior.

## Acceptance rules

- Map every assertion to `J*` and `AC-*`.
- Use isolated state and credentials.
- Assert intended UI state and product outcome, not only route response.
- Treat console errors and failed `/v1` requests as failures unless explicitly expected.
- Cover error, authorization, disconnect/reconnect, retry, and recovery states.
- Do not commit screenshots or reports as current product proof.

The `brains-e2e` workflow job is part of the blocking quality gate described in [QUALITY_GATES.md](../../docs/QUALITY_GATES.md).

<!--
last_verified: 2026-08-30T09:45:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD 4e4819f02c621db5ceb75a13328a741208abdf42 plus Docker-only J1-J11 browser evidence over a private internal network with synthetic state; deployment not verified
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

## Run in isolated Docker containers

```text
pwsh -File scripts/run_docker_e2e.ps1
```

The runner builds the exact candidate app and a lockfile-defined Playwright image,
connects them only through a private internal Docker network, publishes no host port,
uses tmpfs for Brains state, and removes its owned containers, network, and images in
`finally`. It refuses pre-existing artifact names rather than deleting or reusing them.

Container configuration:

| Variable | Default | Meaning |
|---|---|---|
| `BRAINS_E2E_BASE_URL` | `http://<app-container>:8787` | Private Docker-network gateway origin. |
| `BRAINS_E2E_KEY` | generated per run | Synthetic sign-in key shared only by the disposable app and browser containers. |
| `BRAINS_E2E_SEED_MANIFEST` | generated | Synthetic IDs and addresses prepared inside the disposable app container. |

The older `BRAINS_E2E_AUTO_STACK=1` PowerShell harness remains compatibility inventory
for environments that explicitly choose a host process. It is not the default isolated
UAT path. Auto-stack mode rejects non-loopback URLs; Docker mode accepts only a plain
internal HTTP origin without embedded credentials.

The compatibility Windows auto-stack scripts:

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

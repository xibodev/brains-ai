<!--
last_verified: 2026-08-30T19:30:00.000-06:00
verified_by: OpenCode
verification_basis: HEAD ea15b51a3868434f2f2081b71da48126818007b3 plus Coordination mailbox SMTP source inspection, TypeScript checks, and isolated Docker browser evidence; real SMTP provider, external harness notification, and deployment not verified
-->

# Brains operator SPA

This directory contains the React, TypeScript, and Vite source for the modern Brains console.

The package and built asset paths use the canonical Brains identity:

- npm package: `brains-spa`
- build output: `src/brains/web/spa`
- FastAPI mount: `/app`

No Node process is required at runtime. FastAPI serves the checked-in built assets.

## Local commands

```text
cd frontend
npm ci
npm run typecheck
npm run build
npm run dev
```

`npm run build` runs `tsc -b && vite build` and replaces `src/brains/web/spa`. Vite uses `/app/` as its base. That directory is committed and shipped in the wheel, so a source change here must be committed together with the rebuilt bundle.

To check the committed bundle without overwriting it, run `python scripts/check_spa_bundle.py` from the repository root. It builds into a scratch directory, compares the result byte-for-byte, and removes the scratch directory again. CI runs the same check as a blocking gate.

Use `npm ci`, not `npm install`: the lockfile is the install contract, and CI refuses a `package.json` that disagrees with it.

The dev server proxies `/v1` to `http://127.0.0.1:8080`. That differs from the gateway's default `:8787`; start a gateway on `:8080` or override the dev configuration before relying on the proxy.

## Structure

| Path | Responsibility |
|---|---|
| `src/api` | Typed `/v1` fetch client and product types |
| `src/realtime` | Multiplexed `/v1/ws` client, the wire contract in `protocol.ts` (server-derived topic map, cursor, duplicate window), and topic hooks |
| `src/store` | Operator capability/Labs state, active Org for Access/Labs, and async state |
| `src/components` | Workspace-first shell, operator primitives, dialogs, legacy Labs components, navigation |
| `src/screens` | Command Center, Workspaces, Coordination, Governance, Operations, Act, plus explicitly gated Labs screens |
| `src/styles` | Tokens and application styles |

The complete route and client/server contract is in [Traceability](../docs/product/TRACEABILITY.md).

## Current limitations

- Labs Session, Persona, and Runtime deep-route parameters remain declared but unconsumed; the generated traceability gate holds that list explicit.
- Chat is not delivered to a shipped agent CLI, because none is launched with an open input channel.
- The Coordination mailbox desk commits and reads durable local mail and configures a
  verified one-way SMTP copy; live harness notification and real-provider SMTP evidence
  remain separate gaps.
- Host-level Operations actions remain disabled until typed preview/confirmation contracts exist.
- Some request failures can appear as empty lists.

Frontend work must map to Feature/Journey/AC IDs and follow [Quality Gates](../docs/QUALITY_GATES.md).

<!--
last_verified: 2026-08-04T08:00:00.000-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD c21a15db3859e6b9f147260a38a7a0d6fe2533b2 plus the local blocking-quality-gates change; static source inspection with SQLite pytest (full suite), ruff check/format, mypy, scripts/check_docs.py, scripts/check_traceability.py, npm ci, the frontend TypeScript type-check and production build, and the committed-bundle comparison; cross-process realtime fan-out, the live-Postgres migration matrix, browser-session authorization evidence, the E4 disconnect/reconnect browser journey, and deployment not verified
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
| `src/store` | Active Org, chat dock, and async state |
| `src/components` | Shell, cards, board, dialogs, chat, approvals, navigation |
| `src/screens` | Inbox, Sessions, Personas, Pods, Projects, Issues, Automation, Runtimes, Config, Settings, Onboarding |
| `src/styles` | Tokens and application styles |

The complete route and client/server contract is in [Traceability](../docs/product/TRACEABILITY.md).

## Current limitations

- Entity deep-route parameters are generally declared but not consumed by their screens; the generated traceability gate holds that list explicit rather than letting it drift.
- Chat is not delivered to a shipped agent CLI, because none is launched with an open input channel.
- Config is read-mostly and several sections are informational.
- Some request failures can appear as empty lists.

Frontend work must map to Feature/Journey/AC IDs and follow [Quality Gates](../docs/QUALITY_GATES.md).

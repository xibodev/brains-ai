<!--
last_verified: 2026-08-01T19:29:19.185-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD 6eb071bba49a5e678fb6ee8a35a3b21199136374; static inspection of wa-web package and service source plus relay route contract; deployment not verified
-->

# Brains WhatsApp Web sidecar

This Node service links a WhatsApp account as a companion device through Baileys. It can send Brains approval messages to one bound chat and forward replies from that chat to the Brains relay endpoint.

The implementation and environment variables retain `BRAINS_*` names.

## Security boundary

A WhatsApp companion device can read and send **all messages on the linked account**. The bound-chat filter controls what this application acts on; it is not a WhatsApp permission boundary.

- Run only on a trusted host.
- Keep `WA_AUTH_DIR` private and outside Git.
- Set `WA_SEND_TOKEN` in every operational environment.
- Use a distinct `BRAINS_RELAY_TOKEN`.
- Restrict network access to `/send`.
- Treat WhatsApp Web automation as unofficial and subject to platform terms.

The code allows unauthenticated `/send` when `WA_SEND_TOKEN` is empty. Operational policy requires a non-empty token.

## Configuration

| Variable | Source-defined meaning |
|---|---|
| `WA_PORT` | HTTP port; default `8788` |
| `WA_AUTH_DIR` | Baileys credential and bind-state directory; default `./auth` |
| `WA_SEND_TOKEN` | Optional in code, required by policy for `/send` bearer auth |
| `BRAINS_RELAY_URL` | Brains relay URL, normally `/relay/reply` |
| `BRAINS_RELAY_TOKEN` | Relay bearer added to forwarded replies |
| `WA_BIND_KEYWORD` | Chat binding phrase; default `brains: bind` |
| `WA_TARGET_JID` | Explicit chat JID that bypasses interactive binding |
| `WA_PAIRING_CODE` | E.164 digits for phone-number pairing instead of QR |
| `WA_LOG_LEVEL` | Pino log level |

## Control surface

- `GET /health` and `GET /status`
  - HTTP 200 when linked;
  - HTTP 503 when not linked;
  - returns link and bound-chat metadata.
- `POST /send`
  - body can be `{ "text": "..." }` or raw text;
  - requires bearer auth when `WA_SEND_TOKEN` is set;
  - returns 503 when WhatsApp is not linked or no chat is bound.

Inbound messages are forwarded only from the bound chat. If `BRAINS_RELAY_URL` is unset, the service logs and drops the command.

## Source-defined local start

```text
cd services/wa-web
npm install
npm start
```

Node 20 or newer is declared. Linking, bridge delivery, and external operation are not verified.

See [Security](../../SECURITY.md) and [Operations](../../docs/OPERATIONS.md).

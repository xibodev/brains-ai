// brains WhatsApp-Web sidecar (Baileys).
//
// Links to YOUR WhatsApp as a companion device (QR / pairing code) and uses a
// single dedicated chat (a group or a contact DM) as the command channel:
//
//   brains  --POST /send--> [this sidecar] --whatsapp-web--> the bound chat
//   you reply "approve ASK-0005" in that chat
//   the bound chat --whatsapp-web--> [this sidecar] --POST /relay/reply--> brains
//
// No Meta Cloud API, no public webhook, no message templates. The sidecar adds
// the brains relay bearer itself, so the inbound side "just works".
//
// SECURITY: a companion device can read/send ALL your WhatsApp messages. Run
// this on a box you trust, keep WA_AUTH_DIR private, and protect /send with
// WA_SEND_TOKEN. The sidecar only ever ACTS on the one bound chat.

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import pino from "pino";
import qrcode from "qrcode-terminal";
import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} from "@whiskeysockets/baileys";

const PORT = parseInt(process.env.WA_PORT || "8788", 10);
const AUTH_DIR = process.env.WA_AUTH_DIR || "./auth";
const SEND_TOKEN = (process.env.WA_SEND_TOKEN || "").trim();
const RELAY_URL = (process.env.BRAINS_RELAY_URL || "").trim(); // e.g. http://brains:8787/relay/reply
const RELAY_TOKEN = (process.env.BRAINS_RELAY_TOKEN || "").trim();
const BIND_KEYWORD = (process.env.WA_BIND_KEYWORD || "brains: bind").trim().toLowerCase();
const USE_PAIRING_CODE = (process.env.WA_PAIRING_CODE || "").trim(); // set to your number (E.164, digits only) for code pairing instead of QR
let TARGET_JID = (process.env.WA_TARGET_JID || "").trim(); // optional explicit override

const log = pino({ level: process.env.WA_LOG_LEVEL || "info" });
const BIND_FILE = path.join(AUTH_DIR, "bind.json");

// Message ids WE sent — so the bot never reacts to its own outbound (a
// companion device shares your account, so our sends are fromMe too).
const sentIds = new Set();

function loadBoundJid() {
  if (TARGET_JID) return TARGET_JID;
  try {
    const raw = JSON.parse(fs.readFileSync(BIND_FILE, "utf8"));
    return (raw && raw.jid) || "";
  } catch {
    return "";
  }
}
function saveBoundJid(jid) {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  fs.writeFileSync(BIND_FILE, JSON.stringify({ jid, boundAt: new Date().toISOString() }, null, 2));
}

let boundJid = "";
let sock = null;
let linked = false;
let meId = "";

function textOf(msg) {
  const m = msg.message || {};
  return (
    m.conversation ||
    (m.extendedTextMessage && m.extendedTextMessage.text) ||
    (m.imageMessage && m.imageMessage.caption) ||
    (m.videoMessage && m.videoMessage.caption) ||
    ""
  );
}

async function forwardToBrains(text) {
  if (!RELAY_URL) {
    log.warn("inbound command but BRAINS_RELAY_URL is unset — dropping");
    return;
  }
  try {
    const res = await fetch(RELAY_URL, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(RELAY_TOKEN ? { authorization: `Bearer ${RELAY_TOKEN}` } : {}),
      },
      body: JSON.stringify({ message: text }),
    });
    const body = await res.text();
    log.info({ status: res.status, body: body.slice(0, 200) }, "forwarded inbound to brains");
    return { status: res.status, body };
  } catch (err) {
    log.error({ err: String(err) }, "failed to forward inbound to brains");
  }
}

async function sendToBound(text) {
  if (!sock || !linked) throw new Error("not linked to WhatsApp yet");
  if (!boundJid) throw new Error("no chat bound yet — send the bind keyword in your dedicated chat");
  const sent = await sock.sendMessage(boundJid, { text });
  if (sent && sent.key && sent.key.id) sentIds.add(sent.key.id);
  return sent && sent.key ? sent.key.id : null;
}

async function handleMessages({ messages, type }) {
  if (type !== "notify") return; // ignore history / append replays
  for (const msg of messages) {
    if (!msg.message) continue;
    const id = msg.key && msg.key.id;
    if (id && sentIds.has(id)) continue; // our own outbound echo
    const chat = msg.key && msg.key.remoteJid;
    if (!chat || chat === "status@broadcast") continue;
    const text = (textOf(msg) || "").trim();
    if (!text) continue;

    // bind flow: lock onto the chat where the keyword is sent
    if (!boundJid && text.toLowerCase() === BIND_KEYWORD) {
      boundJid = chat;
      saveBoundJid(chat);
      log.info({ chat }, "bound command channel");
      try {
        const sent = await sock.sendMessage(chat, {
          text: "✅ brains bound to this chat. I'll send approval requests here; reply `approve ASK-####` or `deny ASK-#### <reason>`.",
        });
        if (sent && sent.key && sent.key.id) sentIds.add(sent.key.id);
      } catch (err) {
        log.error({ err: String(err) }, "failed to send bind confirmation");
      }
      continue;
    }

    // only act on the bound chat
    if (!boundJid || chat !== boundJid) {
      if (!boundJid) log.info({ chat }, `unbound — send "${BIND_KEYWORD}" in your dedicated chat to bind`);
      continue;
    }
    log.info({ text }, "inbound command on bound chat");
    await forwardToBrains(text);
  }
}

async function start() {
  boundJid = loadBoundJid();
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger: log.child({ mod: "baileys" }),
    markOnlineOnConnect: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      if (USE_PAIRING_CODE && !sock.authState.creds.registered) {
        try {
          const code = await sock.requestPairingCode(USE_PAIRING_CODE.replace(/[^0-9]/g, ""));
          log.info(`\n\n  WhatsApp pairing code: ${code}\n  Enter it in WhatsApp > Linked devices > Link with phone number.\n`);
        } catch (err) {
          log.error({ err: String(err) }, "pairing code request failed; falling back to QR");
          qrcode.generate(qr, { small: true });
        }
      } else {
        log.info("\n\n  Scan this QR in WhatsApp > Linked devices > Link a device:\n");
        qrcode.generate(qr, { small: true });
      }
    }
    if (connection === "open") {
      linked = true;
      meId = (sock.user && sock.user.id) || "";
      log.info({ me: meId, bound: boundJid || "(unbound)" }, "WhatsApp connection open");
      if (!boundJid) log.info(`Send "${BIND_KEYWORD}" in your dedicated brains chat to bind it.`);
    }
    if (connection === "close") {
      linked = false;
      const code = lastDisconnect && lastDisconnect.error && lastDisconnect.error.output
        ? lastDisconnect.error.output.statusCode
        : 0;
      const loggedOut = code === DisconnectReason.loggedOut;
      log.warn({ code, loggedOut }, "WhatsApp connection closed");
      if (!loggedOut) setTimeout(() => start().catch((e) => log.error({ e: String(e) }, "restart failed")), 3000);
      else log.error("logged out — delete WA_AUTH_DIR and re-link");
    }
  });

  sock.ev.on("messages.upsert", (ev) => handleMessages(ev).catch((e) => log.error({ e: String(e) }, "handleMessages failed")));
}

// --- tiny HTTP control surface ------------------------------------------

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "content-type": "application/json" });
  res.end(body);
}

const server = http.createServer((req, res) => {
  if (req.method === "GET" && (req.url === "/health" || req.url === "/status")) {
    return json(res, linked ? 200 : 503, {
      ok: linked,
      linked,
      bound: boundJid || null,
      me: meId || null,
      bindKeyword: BIND_KEYWORD,
    });
  }
  if (req.method === "POST" && req.url === "/send") {
    if (SEND_TOKEN) {
      const auth = (req.headers["authorization"] || "").replace(/^Bearer\s+/i, "");
      if (auth !== SEND_TOKEN) return json(res, 401, { error: "invalid token" });
    }
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", async () => {
      let text = "";
      try {
        const parsed = JSON.parse(raw || "{}");
        text = typeof parsed === "string" ? parsed : parsed.text || "";
      } catch {
        text = raw;
      }
      if (!text) return json(res, 400, { error: "text required" });
      try {
        const id = await sendToBound(text);
        return json(res, 200, { sent: true, id });
      } catch (err) {
        return json(res, 503, { error: String(err && err.message ? err.message : err) });
      }
    });
    return;
  }
  json(res, 404, { error: "not found" });
});

server.listen(PORT, () => log.info(`wa-web control surface on :${PORT} (POST /send, GET /health)`));
start().catch((e) => {
  log.error({ e: String(e) }, "fatal start error");
  process.exit(1);
});

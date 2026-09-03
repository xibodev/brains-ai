"""Pinned real-Claude stop-hook probe with a local synthetic Messages endpoint.

Run only in a disposable container with an empty HOME. The probe creates its own
SQLite state, binds a known Claude session UUID, generates settings through the
committed wire renderer, and proves Claude honored the generated Stop output by
making a second model request. No external credential or provider is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _MessagesHandler(BaseHTTPRequestHandler):
    requests = 0

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        type(self).requests += 1
        body = json.dumps(
            {
                "id": f"msg_{type(self).requests}",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "synthetic completion"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _seed(home: Path, workspace: Path, native_id: str) -> Path:
    os.environ["BRAINS_DB_URL"] = f"sqlite:///{(home / 'state.db').as_posix()}"
    os.environ["BRAINS_STATE_DIR"] = str(home / ".brains")
    os.environ["BRAINS_MCP_BEARER_TOKEN"] = "synthetic-client-key"
    from brains import wire
    from brains.control.durable_mail import send_mailbox_message
    from brains.control.durable_mailbox import (
        create_managed_agent_mailbox,
        register_agent_mailbox,
    )
    from brains.control.operators import ensure_admin_operator
    from brains.control.sessions import start_session
    from brains.storage.migrations import init_db

    init_db()
    ensure_admin_operator()
    recipient = start_session(str(workspace), tool="claude-code")
    managed = create_managed_agent_mailbox(
        str(workspace),
        "claude-code",
        native_id,
        recipient["session_id"],
        notification_mode="turn_boundary",
    )
    sender = start_session(str(workspace), tool="codex")
    binding = f"binding-{uuid.uuid4().hex}"
    sender_box = register_agent_mailbox(
        str(workspace),
        "codex",
        f"codex-{uuid.uuid4()}",
        sender["session_id"],
        binding,
    )
    send_mailbox_message(
        str(workspace),
        [managed["address"]],
        "Synthetic subject",
        f"probe-{uuid.uuid4().hex}",
        body="synthetic body that must never reach hook output",
        sender_address=sender_box["address"],
        sender_session_id=sender["session_id"],
        binding_secret=binding,
    )
    report = wire.wire(
        home,
        wire.WireContext(
            transport="streamable-http",
            url="http://127.0.0.1:9877/mcp",
            api_key="synthetic-client-key",
        ),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=True,
    )
    if not report["ok"]:
        raise RuntimeError("wire-refused")
    return home / ".claude" / "settings.json"


def main() -> int:
    home = Path(os.environ["HOME"]).resolve()
    if any(home.iterdir()):
        raise RuntimeError("probe-home-not-empty")
    workspace = home / "workspace"
    workspace.mkdir()
    native_id = str(uuid.uuid4())
    settings_path = _seed(home, workspace, native_id)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for group in settings["hooks"]["Stop"]
        for hook in group["hooks"]
        if hook.get("type") == "command"
    ]
    if commands != ["brains-ai mailbox harness-wakeup --adapter claude-code"]:
        raise RuntimeError("generated-command-mismatch")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _MessagesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "ANTHROPIC_API_KEY": "synthetic-provider-key",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--session-id",
                native_id,
                "--setting-sources",
                "user",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--tools",
                "",
            ],
            cwd=workspace,
            env=env,
            input="Return one short synthetic response.\n",
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    if result.returncode != 0:
        diagnostic = f"{result.stdout}\n{result.stderr}".lower()
        signals = [
            signal
            for signal in (
                "requires node",
                "node.js",
                "authentication",
                "connection",
                "econnrefused",
                "invalid",
                "stream",
                "base_url",
            )
            if signal in diagnostic
        ]
        raise RuntimeError(
            f"claude-exit-{result.returncode};requests={_MessagesHandler.requests};"
            f"signals={','.join(signals) or 'none'}"
        )
    if _MessagesHandler.requests < 2:
        raise RuntimeError("generated-stop-output-not-honored")
    print(json.dumps({"ok": True, "requests": _MessagesHandler.requests}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

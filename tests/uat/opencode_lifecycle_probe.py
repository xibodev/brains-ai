"""Real OpenCode 1.18.25 lifecycle probe in one disposable container."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class MockProvider(BaseHTTPRequestHandler):
    requests = 0
    hold = True

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        payload = json.dumps({"object": "list", "data": [{"id": "mock", "object": "model"}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802
        type(self).requests += 1
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        while type(self).hold:
            time.sleep(0.05)
        chunks = [
            {
                "id": "synthetic",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}],
            },
            {
                "id": "synthetic",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def _wait(query, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = query()
        if value is not None:
            return value
        time.sleep(0.1)
    raise RuntimeError("lifecycle observation timed out")


def main() -> None:
    expected_sha = os.environ.get("BRAINS_RENDERER_COMMIT", "")
    if len(expected_sha) != 40:
        raise RuntimeError("committed renderer identity is unavailable")
    root = Path(tempfile.mkdtemp(prefix="brains-opencode-uat-"))
    home = root / "home"
    state = root / "state"
    workspace = root / "workspace"
    moved = root / "moved"
    for path in (home, state, workspace, moved):
        path.mkdir(parents=True)
    os.environ.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "BRAINS_STATE_DIR": str(state),
            "BRAINS_DB_URL": f"sqlite:///{(state / 'brains.db').as_posix()}",
            "BRAINS_PREWARM_INDEX_ON_SESSION": "0",
        }
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config_path = home / ".config/opencode/opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "model": "local/mock",
                "provider": {
                    "local": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Synthetic local provider",
                        "options": {
                            "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                            "apiKey": "synthetic-only",
                        },
                        "models": {"mock": {"name": "Synthetic"}},
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    from brains.control.common import utc_now
    from brains.control.durable_mailbox import (
        MailboxUnavailableError,
        read_mailbox_binding_file,
        resume_agent_mailbox,
    )
    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import AgentSession, Mailbox, MailboxAttachment, SessionLease
    from brains.wire import WireContext, wire

    init_db()
    report = wire(
        home,
        WireContext(transport="stdio"),
        tools=["opencode"],
        force=True,
        verify_harness_versions=True,
    )
    if not report["ok"]:
        raise RuntimeError("OpenCode compatibility or plugin wiring failed")

    environment = dict(os.environ)
    command = [
        "opencode",
        "run",
        "--format",
        "json",
        "--model",
        "local/mock",
        "--dir",
        str(workspace),
        "synthetic first turn",
    ]
    first = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def registration():
        with SessionLocal() as session:
            row = session.query(Mailbox).filter(Mailbox.tool == "opencode").one_or_none()
            if row is None:
                return None
            attachment = (
                session.query(MailboxAttachment)
                .filter_by(mailbox_id=row.id, active_slot=1)
                .one_or_none()
            )
            return (row, attachment.session_id) if attachment is not None else None

    try:
        mailbox, first_brains_id = _wait(registration)
    except RuntimeError as exc:
        first.terminate()
        first.wait(timeout=5)
        raise RuntimeError("OpenCode did not attach") from exc
    native_id = mailbox.native_tool_session_id
    if not native_id:
        raise RuntimeError("authoritative OpenCode Session ID was not attached")
    from brains.control import durable_mailbox as mailbox_ctl
    from brains.storage.models import Workspace

    with SessionLocal() as session:
        persisted = session.get(Mailbox, mailbox.id)
        workspace_row = session.get(Workspace, persisted.workspace_id)
        proof_path = mailbox_ctl._managed_binding_path(workspace_row, "opencode", native_id)
    old_proof = read_mailbox_binding_file(proof_path, managed_only=True)

    first.kill()
    first.wait(timeout=10)
    with SessionLocal() as session:
        lease = session.get(SessionLease, first_brains_id)
        lease.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    MockProvider.hold = False
    resumed = subprocess.run(
        [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            "local/mock",
            "--dir",
            str(workspace),
            "--session",
            native_id,
            "synthetic resumed turn",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if resumed.returncode != 0:
        raise RuntimeError("OpenCode resume failed")
    _mailbox, recovered_brains_id = _wait(registration)
    if recovered_brains_id == first_brains_id:
        raise RuntimeError("expired Brains incarnation was not replaced")

    moved_result = subprocess.run(
        [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            "local/mock",
            "--dir",
            str(moved),
            "--session",
            native_id,
            "synthetic moved turn",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    del moved_result
    with SessionLocal() as session:
        rows = session.query(Mailbox).filter_by(native_tool_session_id=native_id).all()
        if len(rows) != 1 or rows[0].workspace_id != mailbox.workspace_id:
            raise RuntimeError("workspace conflict moved the durable identity")

    deleted = subprocess.run(
        ["opencode", "session", "delete", native_id],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if deleted.returncode != 0:
        raise RuntimeError("OpenCode native deletion failed")

    def deletion_observed():
        with SessionLocal() as session:
            persisted = session.get(Mailbox, mailbox.id)
            lifecycle = session.get(AgentSession, recovered_brains_id)
            if (
                persisted is None
                or persisted.status != "retired"
                or lifecycle is None
                or lifecycle.state != "completed"
            ):
                return None
            return True

    _wait(deletion_observed)
    try:
        resume_agent_mailbox(
            str(workspace),
            "opencode",
            native_id,
            recovered_brains_id,
            old_proof,
        )
    except MailboxUnavailableError:
        pass
    else:
        raise RuntimeError("revoked or old proof was accepted")

    journal = (state / "adapter-lifecycle/opencode.jsonl").read_text(encoding="utf-8")
    forbidden = (native_id, old_proof, "synthetic first turn", "synthetic resumed turn")
    if any(value in journal for value in forbidden):
        raise RuntimeError("lifecycle journal disclosed protected input")
    with SessionLocal() as session:
        current = session.get(AgentSession, recovered_brains_id)
        if current is None:
            raise RuntimeError("recovered lifecycle state disappeared")
    server.shutdown()
    print(
        json.dumps(
            {
                "ok": True,
                "autoload": True,
                "recovered": True,
                "workspace_conflict": True,
                "deleted": True,
                "revoked": True,
                "renderer_commit": expected_sha,
                "provider_requests": MockProvider.requests,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

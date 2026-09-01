"""Credential-isolated actor and database oracle for real-CLI mailbox UAT."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

HOME = Path("/home/node")
WORKSPACE = Path("/workspace/uat")
BINDING_ROOT = Path("/data/.brains/mailbox-bindings")
TOOLS = ("claude", "copilot", "opencode", "codex")
CANONICAL_TOOLS = {
    "claude": "claude-code",
    "copilot": "copilot-cli",
    "opencode": "opencode",
    "codex": "codex",
}
TOOL_NAMES = (
    "brains_start_session",
    "brains_end_session",
    "brains_mailbox_send",
    "brains_mailbox_inbox",
    "brains_mailbox_reply",
)


def _emit(payload: dict[str, Any], code: int = 0) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    raise SystemExit(code)


def _decode(value: str) -> str:
    return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")


def _link(source: Path, target: Path) -> None:
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError("credential mount is unavailable")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(source)


def _copy_secret(source: Path, target: Path) -> None:
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError("credential mount is unavailable")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o600)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _configure(tool: str) -> dict[str, Any]:
    from brains.wire import (
        WireContext,
        _claude_entry,
        _codex_block,
        _copilot_entry,
        _opencode_entry,
    )

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    primary = Path("/run/credentials/primary")
    if tool == "claude":
        _link(primary, HOME / ".claude/.credentials.json")
    elif tool == "copilot":
        _copy_secret(primary, HOME / ".copilot/config.json")
    elif tool == "opencode":
        _link(primary, HOME / ".local/share/opencode/auth.json")
    elif tool == "codex":
        _link(primary, HOME / ".codex/auth.json")
    else:  # pragma: no cover - argparse constrains this
        raise ValueError(tool)

    mcp_env = {
        "BRAINS_DB_URL": os.environ["BRAINS_DB_URL"],
        "BRAINS_STATE_DIR": os.environ["BRAINS_STATE_DIR"],
        "BRAINS_MCP_TOOLS": ",".join(name.removeprefix("brains_") for name in TOOL_NAMES),
        "BRAINS_PREWARM_INDEX_ON_SESSION": "0",
    }
    ctx = WireContext(
        transport="stdio",
        python="/opt/brains-venv/bin/python",
        db_url=mcp_env["BRAINS_DB_URL"],
    )
    if tool == "claude":
        entry = _claude_entry(ctx)
        entry["env"].update(mcp_env)
        _write_json(HOME / "mcp.json", {"mcpServers": {"brains": entry}})
    elif tool == "copilot":
        entry = _copilot_entry(ctx)
        entry["env"].update(mcp_env)
        _write_json(HOME / "mcp.json", {"mcpServers": {"brains": entry}})
    elif tool == "opencode":
        entry = _opencode_entry(ctx)
        entry["environment"].update(mcp_env)
        _write_json(
            HOME / ".config/opencode/opencode.json",
            {
                "$schema": "https://opencode.ai/config.json",
                "autoupdate": False,
                "share": "disabled",
                "model": "github-copilot/gpt-5-mini",
                "permission": {"*": "deny", "brains_*": "allow"},
                "mcp": {"brains": entry},
            },
        )
    else:
        block = _codex_block(ctx)
        tool_policy = 'default_tools_approval_mode = "approve"\n'
        tool_policy += (
            "enabled_tools = [" + ", ".join(json.dumps(name) for name in TOOL_NAMES) + "]\n"
        )
        block = block.replace(
            "\n\n[mcp_servers.brains.env]",
            f"\n{tool_policy}\n[mcp_servers.brains.env]",
        )
        block += "\n" + "\n".join(
            f"{key} = {json.dumps(value)}"
            for key, value in mcp_env.items()
            if key != "BRAINS_DB_URL"
        )
        config = (
            'sandbox_mode = "read-only"\n'
            "check_for_update_on_startup = false\n\n"
            'developer_instructions = "For this isolated UAT, call an enabled Brains MCP tool exactly when the user requests it; never replace a requested tool call with prose."\n\n'
            + block
            + "\n"
        )
        path = HOME / ".codex/config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config, encoding="utf-8")
        path.chmod(0o600)

    version = subprocess.run(
        [tool, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(tool),
    )
    if version.returncode != 0:
        raise RuntimeError("CLI version probe failed")
    first_line = (version.stdout or version.stderr).splitlines()[0].strip()
    return {"ok": True, "tool": tool, "version": first_line, "transport": "stdio"}


def _environment(tool: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(HOME),
            "XDG_CONFIG_HOME": str(HOME / ".config"),
            "XDG_DATA_HOME": str(HOME / ".local/share"),
            "CODEX_HOME": str(HOME / ".codex"),
            "NO_COLOR": "1",
            "CI": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
    )
    if tool != "codex":
        env.pop("CODEX_HOME", None)
    if tool == "copilot":
        env["COPILOT_HOME"] = str(HOME / ".copilot")
    return env


def _command(tool: str, prompt: str, session_id: str | None) -> tuple[list[str], str | None]:
    if tool == "claude":
        command = [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--strict-mcp-config",
            "--mcp-config",
            str(HOME / "mcp.json"),
            "--restricted",
            "--permission-mode",
            "dontAsk",
            "--disable-slash-commands",
            "--no-chrome",
            "--tools",
            *[f"mcp__brains__{name}" for name in TOOL_NAMES],
            "--allowedTools",
            *[f"mcp__brains__{name}" for name in TOOL_NAMES],
            "--model",
            "haiku",
            "--max-budget-usd",
            "0.20",
        ]
        if session_id:
            command.extend(["--resume", session_id])
        input_payload = json.dumps(
            {"type": "user", "message": {"role": "user", "content": prompt}},
            separators=(",", ":"),
        )
        return command, input_payload + "\n"
    if tool == "copilot":
        command = [
            "copilot",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            "gpt-5-mini",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--additional-mcp-config",
            f"@{HOME / 'mcp.json'}",
            "--available-tools=brains(*)",
            "--allow-tool=brains(*)",
            "--no-ask-user",
            "--no-auto-update",
            "--no-remote",
            "--no-remote-export",
            "--log-level",
            "error",
            "-C",
            str(WORKSPACE),
        ]
        if session_id:
            command.extend(["--session-id", session_id])
        return command, None
    if tool == "opencode":
        command = [
            "opencode",
            "run",
            "--pure",
            "--format",
            "json",
            "--model",
            "github-copilot/gpt-5-mini",
            "--dir",
            str(WORKSPACE),
        ]
        if session_id:
            command.extend(["--session", session_id])
        command.append(prompt)
        return command, None

    if session_id:
        return (
            [
                "codex",
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--dangerously-bypass-approvals-and-sandbox",
                "-c",
                "features.shell_tool=false",
                "-c",
                "features.unified_exec=false",
                "-c",
                "features.apps=false",
                session_id,
                prompt,
            ],
            None,
        )
    return (
        [
            "codex",
            "exec",
            "--json",
            "--color",
            "never",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ignore-rules",
            "-c",
            "features.shell_tool=false",
            "-c",
            "features.unified_exec=false",
            "-c",
            "features.apps=false",
            "-C",
            str(WORKSPACE),
            prompt,
        ],
        None,
    )


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _recursive_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for candidate, child in value.items():
            if candidate == key:
                found.append(child)
            found.extend(_recursive_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_recursive_values(child, key))
    return found


def _native_session_id(tool: str, events: list[dict[str, Any]]) -> str | None:
    key = {
        "claude": "session_id",
        "copilot": "sessionId",
        "opencode": "sessionID",
        "codex": "thread_id",
    }[tool]
    for event in events:
        for value in _recursive_values(event, key):
            if isinstance(value, str) and len(value) >= 12:
                return value
    return None


def _event_types(events: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for event in events:
        kind = event.get("type")
        subtype = event.get("subtype")
        label = "/".join(str(part) for part in (kind, subtype) if part)
        if label and label not in values:
            values.append(label[:80])
    return values[:20]


def _failure_category(output: str, *, timed_out: bool = False) -> str:
    if timed_out:
        return "timeout"
    lowered = output.lower()
    for category, words in (
        ("authentication", ("auth", "login", "credential", "unauthorized")),
        ("mcp", ("mcp", "model context protocol")),
        ("quota", ("rate limit", "quota", "credit", "billing")),
        ("model", ("model", "provider")),
        ("permission", ("permission", "approval", "denied")),
    ):
        if any(word in lowered for word in words):
            return category
    return "cli"


def _diagnostic_codes(output: str) -> list[str]:
    lowered = output.lower()
    patterns = (
        ("mcp_startup", ("mcp client", "mcp server", "handshaking", "initialize")),
        ("unknown_config", ("unknown config", "invalid config", "unrecognized")),
        ("tool_unavailable", ("tool unavailable", "not found", "unknown tool")),
        ("permission_denied", ("permission", "approval", "denied")),
        ("authentication", ("auth", "login", "credential", "unauthorized")),
        ("quota", ("rate limit", "quota", "credit", "billing")),
        ("network", ("connect", "network", "dns", "timeout")),
    )
    return [code for code, words in patterns if any(word in lowered for word in words)]


def _run(tool: str, prompt: str, session_id: str | None, expected: str | None) -> dict[str, Any]:
    command, stdin = _command(tool, prompt, session_id)
    try:
        result = subprocess.run(
            command,
            cwd=WORKSPACE,
            env=_environment(tool),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": tool, "failure_category": "timeout"}
    events = _json_lines(result.stdout)
    native_id = _native_session_id(tool, events)
    expected_seen = expected is None or expected in result.stdout
    ok = (
        result.returncode == 0
        and expected_seen
        and (session_id is not None or native_id is not None)
    )
    payload: dict[str, Any] = {
        "ok": ok,
        "tool": tool,
        "event_types": _event_types(events),
        "event_count": len(events),
        "expected_text_seen": expected_seen,
    }
    if native_id is not None:
        payload["native_session_id"] = native_id
    if not ok:
        combined = result.stdout + result.stderr
        payload["failure_category"] = _failure_category(combined)
        payload["diagnostic_codes"] = _diagnostic_codes(combined)
        payload["return_code"] = result.returncode
    return payload


def _create_binding() -> dict[str, Any]:
    BINDING_ROOT.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(32)}.binding"
    path = BINDING_ROOT / filename
    path.write_text(secrets.token_hex(32), encoding="utf-8")
    path.chmod(0o600)
    return {"ok": True, "binding_file": str(path)}


def _wait_for(query, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = query()
        if value is not None:
            return value
        time.sleep(0.25)
    return None


def _inspect(request: dict[str, Any]) -> dict[str, Any]:
    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import (
        AgentSession,
        Mailbox,
        MailboxAttachment,
        MailDelivery,
        MailMessage,
    )

    init_db()
    kind = request["kind"]

    if kind == "registration":
        canonical = CANONICAL_TOOLS[request["tool"]]

        def registration():
            with SessionLocal() as session:
                row = (
                    session.query(Mailbox, MailboxAttachment)
                    .join(MailboxAttachment, MailboxAttachment.mailbox_id == Mailbox.id)
                    .filter(
                        Mailbox.tool == canonical,
                        Mailbox.native_tool_session_id == request["native_session_id"],
                        MailboxAttachment.active_slot == 1,
                    )
                    .one_or_none()
                )
                if row is None:
                    return None
                mailbox, attachment = row
                return {
                    "ok": True,
                    "address": mailbox.address,
                    "mailbox_id": mailbox.id,
                    "brain_session_id": attachment.session_id,
                }

        return _wait_for(registration) or {"ok": False, "failure_category": "registration"}

    if kind == "ended":
        with SessionLocal() as session:
            row = session.get(AgentSession, request["brain_session_id"])
            return {"ok": bool(row and row.ended_at is not None)}

    if kind == "message":
        with SessionLocal() as session:
            message = (
                session.query(MailMessage)
                .filter(MailMessage.subject == request["subject"])
                .one_or_none()
            )
            if message is None:
                return {"ok": False, "failure_category": "message"}
            deliveries = (
                session.query(MailDelivery).filter(MailDelivery.message_id == message.id).all()
            )
            expected = int(request["recipient_count"])
            return {
                "ok": len(deliveries) == expected and all(row.accepted_at for row in deliveries),
                "message_id": message.message_id,
                "delivery_count": len(deliveries),
            }

    if kind == "recovery":
        canonical = CANONICAL_TOOLS[request["tool"]]

        def recovery():
            with SessionLocal() as session:
                mailbox = (
                    session.query(Mailbox)
                    .filter(
                        Mailbox.tool == canonical,
                        Mailbox.native_tool_session_id == request["native_session_id"],
                    )
                    .one_or_none()
                )
                source = (
                    session.query(MailMessage)
                    .filter(MailMessage.message_id == request["message_id"])
                    .one_or_none()
                )
                if mailbox is None or source is None:
                    return None
                attachment = (
                    session.query(MailboxAttachment)
                    .filter(
                        MailboxAttachment.mailbox_id == mailbox.id,
                        MailboxAttachment.active_slot == 1,
                    )
                    .one_or_none()
                )
                delivery = (
                    session.query(MailDelivery)
                    .filter(
                        MailDelivery.message_id == source.id,
                        MailDelivery.recipient_mailbox_id == mailbox.id,
                    )
                    .one_or_none()
                )
                reply = (
                    session.query(MailMessage)
                    .filter(
                        MailMessage.in_reply_to_id == source.id,
                        MailMessage.sender_mailbox_id == mailbox.id,
                        MailMessage.subject == request["reply_subject"],
                    )
                    .one_or_none()
                )
                if attachment is None or delivery is None or reply is None:
                    return None
                if (
                    delivery.read_at is None
                    or attachment.session_id == request["old_brain_session_id"]
                ):
                    return None
                return {
                    "ok": True,
                    "brain_session_id": attachment.session_id,
                    "reply_message_id": reply.message_id,
                }

        return _wait_for(recovery) or {"ok": False, "failure_category": "recovery"}

    if kind == "sender_read":
        canonical = CANONICAL_TOOLS[request["tool"]]
        with SessionLocal() as session:
            mailbox = (
                session.query(Mailbox)
                .filter(
                    Mailbox.tool == canonical,
                    Mailbox.native_tool_session_id == request["native_session_id"],
                )
                .one_or_none()
            )
            if mailbox is None:
                return {"ok": False, "failure_category": "sender"}
            read = 0
            for public_id in request["reply_message_ids"]:
                message = (
                    session.query(MailMessage)
                    .filter(MailMessage.message_id == public_id)
                    .one_or_none()
                )
                if message is None:
                    continue
                delivery = (
                    session.query(MailDelivery)
                    .filter(
                        MailDelivery.message_id == message.id,
                        MailDelivery.recipient_mailbox_id == mailbox.id,
                        MailDelivery.read_at.isnot(None),
                    )
                    .one_or_none()
                )
                read += int(delivery is not None)
            return {"ok": read == len(request["reply_message_ids"]), "read_count": read}

    raise ValueError(f"unknown inspection kind: {kind}")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure = subparsers.add_parser("configure")
    configure.add_argument("--tool", choices=TOOLS, required=True)
    subparsers.add_parser("binding")
    run = subparsers.add_parser("run")
    run.add_argument("--tool", choices=TOOLS, required=True)
    run.add_argument("--prompt-b64", required=True)
    run.add_argument("--session-id")
    run.add_argument("--expected-b64")
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--request-b64", required=True)
    args = parser.parse_args()

    try:
        if args.command == "configure":
            _emit(_configure(args.tool))
        if args.command == "binding":
            _emit(_create_binding())
        if args.command == "run":
            payload = _run(
                args.tool,
                _decode(args.prompt_b64),
                args.session_id,
                _decode(args.expected_b64) if args.expected_b64 else None,
            )
            _emit(payload, 0 if payload["ok"] else 1)
        if args.command == "inspect":
            request = json.loads(_decode(args.request_b64))
            payload = _inspect(request)
            _emit(payload, 0 if payload["ok"] else 1)
    except Exception as exc:
        _emit(
            {"ok": False, "failure_category": type(exc).__name__, "command": args.command},
            1,
        )


if __name__ == "__main__":
    main()

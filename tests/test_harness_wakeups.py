"""Supported harness stop-hook wakeups and truthful pull fallback."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brains import wire
from brains.cli.app import app as cli_app
from brains.control.common import utc_now
from brains.control.durable_mail import (
    MAILBOX_NUDGE,
    send_mailbox_message,
    take_mailbox_notification,
)
from brains.control.durable_mailbox import (
    MailboxUnavailableError,
    create_managed_agent_mailbox,
    read_mailbox_binding_file,
    register_agent_mailbox,
    revoke_managed_agent_mailbox_binding,
)
from brains.control.harness_wakeup import handle_harness_wakeup
from brains.control.operators import ensure_admin_operator
from brains.control.sessions import end_session, link_session_successor, start_session
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import MailboxAttachment, MailNotificationAttempt, SessionLease


def _external_writer(target: str, start, done, replacement: bytes) -> None:
    start.wait(timeout=5)
    Path(target).write_bytes(replacement)
    done.set()


def _native(tool: str) -> str:
    return f"{tool}-{uuid.uuid4().hex}"


def _binding() -> str:
    return f"binding-{uuid.uuid4().hex}"


@pytest.fixture(autouse=True)
def _bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    ensure_admin_operator()
    monkeypatch.setattr(wire, "_opencode_compatibility", lambda: (True, "supported"))


def _managed_recipient(workspace: Path, tool: str) -> dict:
    started = start_session(str(workspace), tool=tool)
    native_id = _native(tool)
    managed = create_managed_agent_mailbox(
        str(workspace),
        tool,
        native_id,
        started["session_id"],
        notification_mode="turn_boundary",
    )
    return {"session": started, "native_id": native_id, "mailbox": managed}


def _send_private_message(workspace: Path, recipient: dict) -> tuple[str, str]:
    sender_secret = _binding()
    sender = start_session(str(workspace), tool="codex")
    sender_mailbox = register_agent_mailbox(
        str(workspace),
        "codex",
        _native("codex"),
        sender["session_id"],
        sender_secret,
    )
    private_body = "synthetic-private-body-never-in-a-hook"
    send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Synthetic private subject",
        f"hook-{uuid.uuid4().hex}",
        body=private_body,
        sender_address=sender_mailbox["address"],
        sender_session_id=sender["session_id"],
        binding_secret=sender_secret,
    )
    return private_body, sender_secret


def _invoke_handler(adapter: str, payload: dict) -> dict:
    emitted: list[dict[str, str]] = []
    result = handle_harness_wakeup(adapter, payload, emit=emitted.append)
    assert emitted == [result["output"]]
    return result


def _notification_attempt(session, recipient: dict) -> MailNotificationAttempt:
    attachment = (
        session.query(MailboxAttachment)
        .filter(
            MailboxAttachment.mailbox_id == recipient["mailbox"]["mailbox_id"],
            MailboxAttachment.active_slot == 1,
        )
        .one()
    )
    return (
        session.query(MailNotificationAttempt)
        .filter(MailNotificationAttempt.attachment_id == attachment.id)
        .one()
    )


def test_supported_stop_hook_delivers_only_fixed_nudge(tmp_path: Path) -> None:
    workspace = tmp_path / "claude-code"
    recipient = _managed_recipient(workspace, "claude-code")
    private_body, sender_secret = _send_private_message(workspace, recipient)

    result = _invoke_handler(
        "claude-code",
        {
            "hook_event_name": "Stop",
            "session_id": recipient["native_id"],
            "cwd": str(workspace),
            "stop_hook_active": False,
        },
    )

    assert result == {
        "state": "delivered",
        "reason": "hook-continuation",
        "output": {"decision": "block", "reason": MAILBOX_NUDGE},
    }
    rendered = json.dumps(result, sort_keys=True)
    assert private_body not in rendered
    assert sender_secret not in rendered
    assert recipient["native_id"] not in rendered
    assert recipient["mailbox"]["address"] not in rendered


def test_generated_hook_command_emits_one_body_free_object(tmp_path: Path) -> None:
    recipient = _managed_recipient(tmp_path, "claude-code")
    private_body, sender_secret = _send_private_message(tmp_path, recipient)

    result = CliRunner().invoke(
        cli_app,
        ["mailbox", "harness-wakeup", "--adapter", "claude-code"],
        input=json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": recipient["native_id"],
                "cwd": str(tmp_path),
                "stop_hook_active": False,
            }
        ),
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"decision": "block", "reason": MAILBOX_NUDGE}
    assert private_body not in result.stdout
    assert sender_secret not in result.stdout
    assert recipient["native_id"] not in result.stdout


def test_forged_legacy_copilot_hook_cannot_claim_or_settle(tmp_path: Path) -> None:
    recipient = _managed_recipient(tmp_path, "copilot-cli")
    _send_private_message(tmp_path, recipient)

    result = _invoke_handler(
        "copilot-cli",
        {"sessionId": recipient["native_id"], "cwd": str(tmp_path)},
    )

    assert result == {"state": "pull", "reason": "adapter-unavailable", "output": {}}
    with SessionLocal() as session:
        assert _notification_attempt(session, recipient).status == "queued"


def test_malformed_hook_input_fails_closed_with_one_empty_object() -> None:
    result = CliRunner().invoke(
        cli_app,
        ["mailbox", "harness-wakeup", "--adapter", "claude-code"],
        input="not-json",
    )

    assert result.exit_code == 0
    assert result.stdout == "{}\n"


def test_output_failure_is_not_retried_or_settled(tmp_path: Path) -> None:
    recipient = _managed_recipient(tmp_path, "claude-code")
    _send_private_message(tmp_path, recipient)
    outputs: list[dict[str, str]] = []

    def fail_after_write(output: dict[str, str]) -> None:
        outputs.append(output)
        raise OSError("synthetic closed output")

    result = handle_harness_wakeup(
        "claude-code",
        {"session_id": recipient["native_id"], "cwd": str(tmp_path)},
        emit=fail_after_write,
    )

    assert outputs == [{"decision": "block", "reason": MAILBOX_NUDGE}]
    assert result == {"state": "uncertain", "reason": "output-unconfirmed", "output": {}}
    with SessionLocal() as session:
        assert _notification_attempt(session, recipient).status == "claimed"


def test_missing_or_unmanaged_attachment_is_truthful_pull(tmp_path: Path) -> None:
    result = _invoke_handler(
        "claude-code",
        {
            "hook_event_name": "Stop",
            "session_id": _native("claude"),
            "cwd": str(tmp_path),
        },
    )
    assert result == {
        "state": "pull",
        "reason": "adapter-unavailable",
        "output": {},
    }


def test_expired_attachment_falls_back_without_claiming(tmp_path: Path) -> None:
    recipient = _managed_recipient(tmp_path, "claude-code")
    _send_private_message(tmp_path, recipient)
    with SessionLocal() as session:
        lease = session.get(SessionLease, recipient["session"]["session_id"])
        assert lease is not None
        lease.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    result = _invoke_handler(
        "claude-code",
        {
            "hook_event_name": "Stop",
            "session_id": recipient["native_id"],
            "cwd": str(tmp_path),
            "stop_hook_active": False,
        },
    )

    assert result == {
        "state": "pull",
        "reason": "adapter-unavailable",
        "output": {},
    }
    with SessionLocal() as session:
        assert _notification_attempt(session, recipient).status == "queued"


def test_revoked_attachment_falls_back_without_disclosure(tmp_path: Path) -> None:
    recipient = _managed_recipient(tmp_path, "claude-code")
    private_body, sender_secret = _send_private_message(tmp_path, recipient)
    recipient_secret = read_mailbox_binding_file(
        recipient["mailbox"]["binding_file"], managed_only=True
    )
    revoke_managed_agent_mailbox_binding(
        str(tmp_path),
        "claude-code",
        recipient["native_id"],
        recipient["session"]["session_id"],
    )

    result = _invoke_handler(
        "claude-code",
        {
            "hook_event_name": "Stop",
            "session_id": recipient["native_id"],
            "cwd": str(tmp_path),
            "stop_hook_active": False,
        },
    )

    assert result == {
        "state": "pull",
        "reason": "adapter-unavailable",
        "output": {},
    }
    rendered = json.dumps(result, sort_keys=True)
    assert private_body not in rendered
    assert sender_secret not in rendered
    assert recipient_secret not in rendered
    assert recipient["native_id"] not in rendered


def test_successor_attachment_receives_pending_nudge(tmp_path: Path) -> None:
    recipient = _managed_recipient(tmp_path, "claude-code")
    _send_private_message(tmp_path, recipient)
    binding = read_mailbox_binding_file(
        recipient["mailbox"]["binding_file"], managed_only=True
    )
    end_session(recipient["session"]["session_id"], "synthetic restart")
    successor = start_session(str(tmp_path), tool="claude-code", reuse_existing=False)
    link_session_successor(
        recipient["session"]["session_id"],
        successor["session_id"],
        tool="claude-code",
        native_tool_session_id=recipient["native_id"],
        mailbox_binding_secret=binding,
        mailbox_notification_mode="turn_boundary",
    )

    result = _invoke_handler(
        "claude-code",
        {
            "hook_event_name": "Stop",
            "session_id": recipient["native_id"],
            "cwd": str(tmp_path),
            "stop_hook_active": False,
        },
    )

    assert result == {
        "state": "delivered",
        "reason": "hook-continuation",
        "output": {"decision": "block", "reason": MAILBOX_NUDGE},
    }
    with SessionLocal() as session:
        active = (
            session.query(MailboxAttachment)
            .filter(
                MailboxAttachment.mailbox_id == recipient["mailbox"]["mailbox_id"],
                MailboxAttachment.active_slot == 1,
            )
            .one()
        )
        assert active.session_id == successor["session_id"]


def test_stop_hook_recursion_guard_leaves_notification_for_pull(tmp_path: Path) -> None:
    recipient = _managed_recipient(tmp_path, "claude-code")
    _send_private_message(tmp_path, recipient)

    result = _invoke_handler(
        "claude-code",
        {
            "hook_event_name": "Stop",
            "session_id": recipient["native_id"],
            "cwd": str(tmp_path),
            "stop_hook_active": True,
        },
    )

    assert result["state"] == "pull"
    assert result["reason"] == "continuation-bounded"
    with SessionLocal() as session:
        assert _notification_attempt(session, recipient).status == "queued"


def test_abandoned_claim_retries_are_bounded_and_end_uncertain(tmp_path: Path) -> None:
    recipient = _managed_recipient(tmp_path, "claude-code")
    _send_private_message(tmp_path, recipient)
    binding = read_mailbox_binding_file(recipient["mailbox"]["binding_file"], managed_only=True)
    session_id = recipient["session"]["session_id"]

    first = take_mailbox_notification(session_id, binding)
    assert first["notification"]["attempt"] == 1
    for expected_attempt in (2, 3):
        with SessionLocal() as session:
            row = _notification_attempt(session, recipient)
            row.started_at = utc_now() - timedelta(minutes=5)
            session.commit()
        reclaimed = take_mailbox_notification(session_id, binding)
        assert reclaimed["reclaimed"] is True
        assert reclaimed["notification"]["attempt"] == expected_attempt

    with SessionLocal() as session:
        row = _notification_attempt(session, recipient)
        row.started_at = utc_now() - timedelta(minutes=5)
        session.commit()
    exhausted = take_mailbox_notification(session_id, binding)
    assert exhausted["uncertain"] is True
    assert exhausted["fallback"] == "pull"
    assert exhausted["notification"]["status"] == "failed"
    assert exhausted["notification"]["error_code"] == "delivery_uncertain"
    assert exhausted["notification"]["nudge"] is None


def test_emitted_nudge_with_failed_settlement_is_reclaimable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipient = _managed_recipient(tmp_path, "claude-code")
    _send_private_message(tmp_path, recipient)

    def fail_settlement(*_args, **_kwargs):
        raise MailboxUnavailableError("mailbox unavailable")

    monkeypatch.setattr(
        "brains.control.harness_wakeup.settle_mailbox_notification",
        fail_settlement,
    )
    result = _invoke_handler(
        "claude-code",
        {
            "hook_event_name": "Stop",
            "session_id": recipient["native_id"],
            "cwd": str(tmp_path),
        },
    )
    assert result == {
        "state": "uncertain",
        "reason": "settlement-unconfirmed",
        "output": {"decision": "block", "reason": MAILBOX_NUDGE},
    }

    with SessionLocal() as session:
        row = _notification_attempt(session, recipient)
        assert row.status == "claimed"
        row.started_at = utc_now() - timedelta(minutes=5)
        session.commit()
    binding = read_mailbox_binding_file(recipient["mailbox"]["binding_file"], managed_only=True)
    reclaimed = take_mailbox_notification(recipient["session"]["session_id"], binding)
    assert reclaimed["reclaimed"] is True
    assert reclaimed["notification"]["attempt"] == 2


@pytest.fixture
def harness_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".copilot").mkdir()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    monkeypatch.setenv("BRAINS_MCP_BEARER_TOKEN", "synthetic-client-key")
    return tmp_path


def _wire_context() -> wire.WireContext:
    return wire.WireContext(
        transport="streamable-http",
        url="http://127.0.0.1:9877/mcp",
        api_key="synthetic-client-key",
    )


def test_wakeup_install_requires_explicit_consent(harness_home: Path) -> None:
    report = wire.wire(harness_home, _wire_context(), rules=False)
    assert all(row["mailbox_notification_mode"] == "pull" for row in report["tools"])
    assert not (harness_home / ".copilot" / "hooks" / "brains.json").exists()
    assert not (harness_home / ".claude" / "settings.json").exists()


def test_wakeup_dry_run_reports_plan_without_claiming_install(harness_home: Path) -> None:
    report = wire.wire(
        harness_home,
        _wire_context(),
        tools=["copilot-cli", "claude-code"],
        rules=False,
        mailbox_wakeups=True,
        dry_run=True,
    )

    assert all(row["mailbox_notification_mode"] == "pull" for row in report["tools"])
    by_tool = {row["tool"]: row for row in report["tools"]}
    assert by_tool["copilot-cli"]["mailbox_wakeup"]["reason"] == "adapter-unavailable"
    assert by_tool["claude-code"]["mailbox_wakeup"]["planned_mode"] == "turn_boundary"
    assert not (harness_home / ".copilot" / "hooks" / "brains.json").exists()
    assert not (harness_home / ".claude" / "settings.json").exists()


def test_consented_wire_is_idempotent_reversible_and_truthful(harness_home: Path) -> None:
    first = wire.wire(
        harness_home,
        _wire_context(),
        rules=False,
        mailbox_wakeups=True,
    )
    second = wire.wire(
        harness_home,
        _wire_context(),
        rules=False,
        mailbox_wakeups=True,
    )
    by_tool = {row["tool"]: row for row in first["tools"]}
    assert by_tool["copilot-cli"]["mailbox_notification_mode"] == "pull"
    assert by_tool["claude-code"]["mailbox_notification_mode"] == "turn_boundary"
    assert by_tool["codex"]["mailbox_wakeup"] == {
        "action": "unavailable",
        "mode": "pull",
        "reason": "adapter-unavailable",
    }
    assert by_tool["opencode"]["mailbox_notification_mode"] == "pull"
    assert all(
        row["mailbox_wakeup"]["action"] in {"unchanged", "unavailable"} for row in second["tools"]
    )
    status = {row["tool"]: row for row in wire.status(harness_home)["tools"]}
    assert status["copilot-cli"]["mailbox_wakeup"]["installed"] is False
    assert status["claude-code"]["mailbox_wakeup"]["installed"] is True
    assert status["codex"]["mailbox_wakeup"]["reason"] == "adapter-unavailable"
    assert status["opencode"]["mailbox_notification_mode"] == "pull"

    removed = wire.unwire(harness_home, rules=False)
    assert all(row["mailbox_wakeup"]["action"] in {"remove", "absent"} for row in removed["tools"])
    assert not (harness_home / ".copilot" / "hooks" / "brains.json").exists()
    assert not (harness_home / ".claude" / "settings.json").exists()
    assert wire.status(harness_home)["tools"][0]["mailbox_notification_mode"] == "pull"


def test_claude_wakeup_preserves_unrelated_hooks(harness_home: Path) -> None:
    settings_path = harness_home / ".claude" / "settings.json"
    original = b'{\r\n  "hooks": {\r\n    "Stop": [{"matcher":"", "hooks":[{"type":"command","command":"synthetic-existing-hook"}]}]\r\n  }\r\n}\r\n'
    settings_path.write_bytes(original)

    wire.wire(
        harness_home,
        _wire_context(),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=True,
    )
    wire.unwire(harness_home, tools=["claude-code"], rules=False)

    assert settings_path.read_bytes() == original


@pytest.mark.parametrize(
    "original",
    [None, b"", b"{}", b'{"hooks":{}}\n', b'{"hooks":{"Stop":[]}}\r\n'],
)
def test_claude_wakeup_restores_exact_prior_shape(
    harness_home: Path,
    original: bytes | None,
) -> None:
    settings_path = harness_home / ".claude" / "settings.json"
    if original is not None:
        settings_path.write_bytes(original)

    installed = wire.wire(
        harness_home,
        _wire_context(),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=True,
    )
    assert installed["ok"] is True
    state = harness_home / ".claude" / ".brains-wakeup"
    manifest = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {
        "version",
        "target",
        "prior_exists",
        "prior_sha256",
        "prior_protection",
        "installed_sha256",
    }
    assert "synthetic-existing-hook" not in json.dumps(manifest)

    removed = wire.unwire(harness_home, tools=["claude-code"], rules=False)
    assert removed["tools"][0]["mailbox_wakeup"]["action"] == "remove"
    if original is None:
        assert not settings_path.exists()
    else:
        assert settings_path.read_bytes() == original
    assert not (state / "manifest.json").exists()
    assert not (state / "prior-settings.bin").exists()
    assert not state.exists()


def test_claude_unwire_preserves_external_edit_and_fails_closed(harness_home: Path) -> None:
    settings_path = harness_home / ".claude" / "settings.json"
    wire.wire(
        harness_home,
        _wire_context(),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=True,
    )
    external = b'{"operator":"concurrent-edit"}\n'
    settings_path.write_bytes(external)

    removed = wire.unwire(harness_home, tools=["claude-code"], rules=False)

    assert removed["tools"][0]["mailbox_wakeup"]["reason"] == "settings-changed"
    assert settings_path.read_bytes() == external


def test_stale_process_lock_is_recovered(harness_home: Path) -> None:
    lock, _manifest, _backup = wire._claude_wakeup_state_paths(harness_home)
    lock.parent.mkdir(parents=True)
    lock.write_text("99999999\n", encoding="ascii")

    report = wire.wire(
        harness_home,
        _wire_context(),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=True,
    )

    assert report["ok"] is True


@pytest.mark.skipif(os.name != "nt", reason="native Windows DPAPI/DACL contract")
def test_windows_claude_recovery_state_is_dpapi_protected_and_owner_only(
    harness_home: Path,
) -> None:
    settings = harness_home / ".claude" / "settings.json"
    original = b'{"synthetic_secret":"not-a-real-secret"}\r\n'
    settings.write_bytes(original)

    report = wire.wire(
        harness_home,
        _wire_context(),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=True,
    )
    assert report["ok"] is True
    _lock, manifest, backup = wire._claude_wakeup_state_paths(harness_home)
    assert backup.read_bytes() != original
    from brains.control.durable_mailbox import (
        _windows_binding_acl_sids,
        _windows_current_user_sid,
    )

    sid = _windows_current_user_sid()
    for path in (settings, manifest.parent, manifest, backup):
        subprocess.run(["icacls", str(path), "/verify"], check=True, capture_output=True)
        assert _windows_binding_acl_sids(path) == (sid,)

    wire.unwire(harness_home, tools=["claude-code"], rules=False)
    assert settings.read_bytes() == original


def test_macos_exchange_uses_renamex_np_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes, int]] = []

    class RenameSwap:
        argtypes = None
        restype = None

        def __call__(self, target: bytes, replacement: bytes, flags: int) -> int:
            calls.append((target, replacement, flags))
            return 0

    rename_swap = RenameSwap()
    monkeypatch.setattr(wire.sys, "platform", "darwin")
    monkeypatch.setattr(
        wire.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: type("LibC", (), {"renamex_np": rename_swap})(),
    )

    wire._exchange_files(Path("synthetic-settings"), Path("synthetic-replacement"))

    assert calls == [(b"synthetic-settings", b"synthetic-replacement", 0x00000002)]
    assert rename_swap.argtypes == [
        wire.ctypes.c_char_p,
        wire.ctypes.c_char_p,
        wire.ctypes.c_uint,
    ]
    assert rename_swap.restype is wire.ctypes.c_int


def test_noncooperating_process_write_is_atomically_captured_and_restored(
    harness_home: Path,
    monkeypatch,
) -> None:
    target = harness_home / ".claude" / "settings.json"
    target.write_bytes(b"{}\n")
    external = b'{"operator":"simultaneous"}\n'
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    done = ctx.Event()
    worker = ctx.Process(target=_external_writer, args=(str(target), start, done, external))
    worker.start()
    actual_exchange = wire._exchange_files

    def exchange_after_external_write(path: Path, replacement: Path) -> None:
        start.set()
        assert done.wait(timeout=20)
        actual_exchange(path, replacement)

    monkeypatch.setattr(wire, "_exchange_files", exchange_after_external_write)
    report = wire.wire(
        harness_home,
        _wire_context(),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=True,
    )
    worker.join(timeout=20)
    assert worker.exitcode == 0
    assert report["tools"][0]["mailbox_wakeup"]["reason"] == "settings-changed"
    assert target.read_bytes() == external


@pytest.mark.parametrize("phase", ["prepared", "swapped", "validated", "metadata"])
def test_install_transaction_recovers_after_each_crash_phase(
    harness_home: Path,
    monkeypatch,
    phase: str,
) -> None:
    target = harness_home / ".claude" / "settings.json"
    original = b'{"hooks":{"Stop":[]}}\r\n'
    target.write_bytes(original)

    def crash(name: str) -> None:
        if name == phase:
            raise SystemExit("synthetic transaction crash")

    monkeypatch.setattr(wire, "_TRANSACTION_PHASE_HOOK", crash)
    with pytest.raises(SystemExit, match="synthetic transaction crash"):
        wire.wire(
            harness_home,
            _wire_context(),
            tools=["claude-code"],
            rules=False,
            mailbox_wakeups=True,
        )
    monkeypatch.setattr(wire, "_TRANSACTION_PHASE_HOOK", None)

    recovered = wire.wire(
        harness_home,
        _wire_context(),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=True,
    )
    assert recovered["ok"] is True
    removed = wire.unwire(harness_home, tools=["claude-code"], rules=False)
    assert removed["tools"][0]["mailbox_wakeup"]["action"] == "remove"
    assert target.read_bytes() == original


@pytest.mark.parametrize("phase", ["prepared", "swapped", "validated", "metadata"])
def test_remove_transaction_recovers_after_each_crash_phase(
    harness_home: Path,
    monkeypatch,
    phase: str,
) -> None:
    target = harness_home / ".claude" / "settings.json"
    original = b'{"hooks":{"Stop":[]}}\r\n'
    target.write_bytes(original)
    installed = wire.wire(
        harness_home,
        _wire_context(),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=True,
    )
    assert installed["ok"] is True

    def crash(name: str) -> None:
        if name == phase:
            raise SystemExit("synthetic transaction crash")

    monkeypatch.setattr(wire, "_TRANSACTION_PHASE_HOOK", crash)
    with pytest.raises(SystemExit, match="synthetic transaction crash"):
        wire.unwire(harness_home, tools=["claude-code"], rules=False)
    monkeypatch.setattr(wire, "_TRANSACTION_PHASE_HOOK", None)

    recovered = wire.unwire(harness_home, tools=["claude-code"], rules=False)
    assert recovered["tools"][0]["mailbox_wakeup"]["action"] == "remove"
    assert target.read_bytes() == original


def test_copilot_wakeup_stays_pull_and_preserves_existing_file(harness_home: Path) -> None:
    path = harness_home / ".copilot" / "hooks" / "brains.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"operator":"owned"}', encoding="utf-8")

    report = wire.wire(
        harness_home,
        _wire_context(),
        tools=["copilot-cli"],
        rules=False,
        mailbox_wakeups=True,
    )

    assert report["ok"] is True
    assert report["tools"][0]["mailbox_wakeup"]["reason"] == "adapter-unavailable"
    assert json.loads(path.read_text(encoding="utf-8")) == {"operator": "owned"}
    status = {row["tool"]: row for row in wire.status(harness_home)["tools"]}
    assert status["copilot-cli"]["mailbox_wakeup"]["reason"] == "adapter-unavailable"


def test_mcp_conflict_prevents_wakeup_install(harness_home: Path) -> None:
    config_path = harness_home / ".copilot" / "mcp-config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"brains": {"command": "operator-owned"}}}),
        encoding="utf-8",
    )

    report = wire.wire(
        harness_home,
        _wire_context(),
        tools=["copilot-cli"],
        rules=False,
        mailbox_wakeups=True,
    )

    assert report["ok"] is False
    assert report["tools"][0]["mailbox_wakeup"] == {
        "action": "skipped",
        "mode": "pull",
        "reason": "mcp-wiring-failed",
    }
    assert not (harness_home / ".copilot" / "hooks" / "brains.json").exists()

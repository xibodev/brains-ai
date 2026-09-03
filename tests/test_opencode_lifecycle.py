from __future__ import annotations

import inspect
import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brains.cli.app import app
from brains.config import brains_state_dir
from brains.control.common import utc_now
from brains.control.durable_mailbox import (
    read_mailbox_binding_file,
    revoke_managed_agent_mailbox_binding,
)
from brains.control.opencode_lifecycle import attach_opencode_session, delete_opencode_session
from brains.storage.db import SessionLocal
from brains.storage.models import AgentSession, Mailbox, MailboxAttachment, SessionLease
from brains.wire import (
    OPENCODE_SUPPORTED_VERSION,
    WireContext,
    render_opencode_plugin,
    status,
    unwire,
    wire,
)


@pytest.fixture(autouse=True)
def _compatible_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    from brains import wire as wire_module

    monkeypatch.setattr(wire_module.shutil, "which", lambda _name: "/bin/opencode")
    monkeypatch.setattr(
        wire_module.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Completed", (), {"returncode": 0, "stdout": "opencode 1.18.25\n"}
        )(),
    )


def _expire(session_id: str) -> None:
    with SessionLocal() as session:
        lease = session.get(SessionLease, session_id)
        assert lease is not None
        lease.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()


def test_turn_boundary_attaches_renews_and_recovers_same_native_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native_id = "ses_authoritative_lifecycle_123456"

    first = attach_opencode_session(str(workspace), native_id)
    renewed = attach_opencode_session(str(workspace), native_id)
    assert first["state"] == "attached"
    assert renewed == {"ok": True, "state": "renewed", "session_id": first["session_id"]}

    _expire(first["session_id"])
    recovered = attach_opencode_session(str(workspace), native_id)
    assert recovered["state"] == "recovered"
    assert recovered["session_id"] != first["session_id"]
    with SessionLocal() as session:
        previous = session.get(AgentSession, first["session_id"])
        current = session.get(AgentSession, recovered["session_id"])
        assert previous is not None and previous.state == "dormant"
        assert current is not None and current.state == "running"
        mailbox = session.query(Mailbox).filter_by(native_tool_session_id=native_id).one()
        attachment = (
            session.query(MailboxAttachment).filter_by(mailbox_id=mailbox.id, active_slot=1).one()
        )
        assert attachment.session_id == recovered["session_id"]

    journal = (brains_state_dir() / "adapter-lifecycle" / "opencode.jsonl").read_text()
    assert native_id not in journal
    assert str(workspace) not in journal
    assert "binding" not in journal.lower()
    assert {json.loads(line)["result"] for line in journal.splitlines()} >= {
        "attached",
        "renewed",
        "recovered",
    }


def test_workspace_move_conflict_is_non_enumerating(tmp_path: Path) -> None:
    first_workspace = tmp_path / "one"
    second_workspace = tmp_path / "two"
    first_workspace.mkdir()
    second_workspace.mkdir()
    native_id = "ses_workspace_conflict_123456"
    attach_opencode_session(str(first_workspace), native_id)

    with pytest.raises(RuntimeError, match="adapter identity is unavailable") as exc:
        attach_opencode_session(str(second_workspace), native_id)
    assert native_id not in str(exc.value)
    assert str(first_workspace) not in str(exc.value)


def test_revocation_and_old_proof_cannot_reattach(tmp_path: Path) -> None:
    workspace = tmp_path / "revoke"
    workspace.mkdir()
    native_id = "ses_revoked_lifecycle_123456"
    attached = attach_opencode_session(str(workspace), native_id)
    with SessionLocal() as session:
        mailbox = session.query(Mailbox).filter_by(native_tool_session_id=native_id).one()
        binding_path = Path(mailbox_ctl_path(session, mailbox))
    old_secret = read_mailbox_binding_file(binding_path, managed_only=True)
    revoke_managed_agent_mailbox_binding(
        str(workspace), "opencode", native_id, attached["session_id"]
    )
    with pytest.raises(RuntimeError, match="adapter identity is unavailable"):
        attach_opencode_session(str(workspace), native_id)
    assert old_secret not in (
        brains_state_dir() / "adapter-lifecycle" / "opencode.jsonl"
    ).read_text(encoding="utf-8")


def test_native_delete_terminally_detaches_and_rejects_old_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "deleted"
    workspace.mkdir()
    native_id = "ses_deleted_lifecycle_123456"
    attached = attach_opencode_session(str(workspace), native_id)

    deleted = delete_opencode_session(str(workspace), native_id)
    assert deleted == {"ok": True, "state": "deleted"}
    assert delete_opencode_session(str(workspace), native_id) == {
        "ok": True,
        "state": "already-deleted",
    }
    with pytest.raises(RuntimeError, match="adapter identity is unavailable"):
        attach_opencode_session(str(workspace), native_id)
    with SessionLocal() as session:
        lifecycle = session.get(AgentSession, attached["session_id"])
        mailbox = session.query(Mailbox).filter_by(native_tool_session_id=native_id).one()
        assert lifecycle is not None and lifecycle.state == "completed"
        assert mailbox.status == "retired"
        assert (
            session.query(MailboxAttachment)
            .filter_by(mailbox_id=mailbox.id, active_slot=1)
            .one_or_none()
            is None
        )


def test_native_delete_retry_repairs_finalize_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brains.control import opencode_lifecycle as lifecycle

    workspace = tmp_path / "delete-repair"
    workspace.mkdir()
    native_id = "ses_delete_repair_123456"
    attached = attach_opencode_session(str(workspace), native_id)
    real_finalize = lifecycle.finalize_session
    monkeypatch.setattr(
        lifecycle,
        "finalize_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )
    with pytest.raises(RuntimeError, match="adapter identity is unavailable"):
        delete_opencode_session(str(workspace), native_id)
    with SessionLocal() as session:
        mailbox = session.query(Mailbox).filter_by(native_tool_session_id=native_id).one()
        agent = session.get(AgentSession, attached["session_id"])
        assert mailbox.status == "retired"
        assert agent is not None and agent.state == "running"

    monkeypatch.setattr(lifecycle, "finalize_session", real_finalize)
    assert delete_opencode_session(str(workspace), native_id) == {
        "ok": True,
        "state": "already-deleted",
    }
    with SessionLocal() as session:
        repaired = session.get(AgentSession, attached["session_id"])
        assert repaired is not None and repaired.state == "completed"


def mailbox_ctl_path(session, mailbox: Mailbox) -> str:
    from brains.control import durable_mailbox as mailbox_ctl
    from brains.storage.models import Workspace

    workspace = session.get(Workspace, mailbox.workspace_id)
    assert workspace is not None
    return str(
        mailbox_ctl._managed_binding_path(workspace, "opencode", mailbox.native_tool_session_id)
    )


def test_cli_is_opencode_only_and_does_not_echo_native_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "cli"
    workspace.mkdir()
    native_id = "ses_cli_lifecycle_123456"
    result = CliRunner().invoke(
        app,
        [
            "adapter-attach",
            "--adapter",
            "opencode",
            "--native-tool-session-id",
            native_id,
            "--workspace",
            str(workspace),
        ],
    )
    assert result.exit_code == 0
    assert native_id not in result.stdout
    assert str(workspace) not in result.stdout
    rejected = CliRunner().invoke(
        app,
        [
            "adapter-attach",
            "--adapter",
            "codex",
            "--native-tool-session-id",
            native_id,
        ],
    )
    assert rejected.exit_code == 2
    assert native_id not in rejected.stdout


def test_wire_owns_exact_dependency_free_global_plugin(tmp_path: Path) -> None:
    from brains import wire as wire_module

    home = tmp_path / "home"
    ctx = WireContext(transport="stdio")
    verified_version, _detail = wire_module._opencode_compatibility()
    assert verified_version == OPENCODE_SUPPORTED_VERSION
    interpreter, state_dir = wire_module._opencode_runtime_inputs()
    plan = wire_module._opencode_plugin_plan(
        home,
        ctx,
        verified_version=verified_version,
        interpreter=interpreter,
        state_dir=state_dir,
    )
    report = wire(home, ctx, tools=["opencode"], force=True)
    plugin = home / ".config/opencode/plugins/brains-lifecycle.js"
    rendered = render_opencode_plugin(ctx)
    assert report["ok"] is True
    assert plugin.read_text(encoding="utf-8") == rendered
    assert plugin.read_text(encoding="utf-8") == plan["content"]
    assert plugin.with_suffix(".sha256").read_text(encoding="utf-8") == plan["manifest_content"]
    assert "chat.message" in rendered
    assert "session.deleted" in rendered
    assert "adapter-detach" in rendered
    assert "Brains lifecycle detach failed" in rendered
    assert "await child.exited" in rendered
    assert "sessionID" in rendered
    assert "@opencode-ai/plugin" not in rendered
    assert "brains-ai" not in rendered
    assert "process.env" not in rendered
    assert '"BRAINS_DB_URL"' in rendered
    assert '"BRAINS_STATE_DIR"' in rendered
    assert '"PATH"' not in rendered
    assert status(home)["tools"][-1]["lifecycle_plugin"] == "managed"
    removed = unwire(home, tools=["opencode"])
    assert removed["tools"][0]["lifecycle_plugin"]["action"] == "remove"
    assert not plugin.exists()
    assert not plugin.with_suffix(".sha256").exists()


def test_wire_refuses_and_unwire_preserves_unowned_plugin(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plugin = home / ".config/opencode/plugins/brains-lifecycle.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("export const UserPlugin = async () => ({})\n", encoding="utf-8")
    report = wire(home, WireContext(transport="stdio"), tools=["opencode"], force=True)
    assert report["ok"] is False
    assert report["tools"][0]["lifecycle_plugin"]["action"] == "conflict"
    removed = unwire(home, tools=["opencode"])
    assert removed["tools"][0]["lifecycle_plugin"]["action"] == "skipped"
    assert plugin.read_text(encoding="utf-8").startswith("export const UserPlugin")


def test_wire_fails_closed_outside_pinned_opencode_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brains import wire as wire_module

    monkeypatch.setattr(
        wire_module.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Completed", (), {"returncode": 0, "stdout": "opencode 9.9.9\n"}
        )(),
    )
    home = tmp_path / "unsupported"
    report = wire(
        home,
        WireContext(transport="stdio"),
        tools=["opencode"],
        force=True,
    )
    assert report["ok"] is False
    assert report["tools"][0]["lifecycle_plugin"]["action"] == "error"
    assert not (home / ".config/opencode/plugins/brains-lifecycle.js").exists()
    assert not (home / ".config/opencode/opencode.json").exists()
    assert OPENCODE_SUPPORTED_VERSION not in str(report)


def test_public_wire_has_no_version_verification_bypass(tmp_path: Path) -> None:
    assert "verified_version" not in inspect.signature(wire).parameters
    with pytest.raises(TypeError):
        wire(  # type: ignore[call-arg]
            tmp_path,
            WireContext(transport="stdio"),
            tools=["opencode"],
            force=True,
            verified_version=OPENCODE_SUPPORTED_VERSION,
        )


def test_internal_plugin_plan_requires_exact_verified_version(tmp_path: Path) -> None:
    from brains import wire as wire_module

    with pytest.raises(ValueError, match="outside the supported lifecycle contract"):
        wire_module._opencode_plugin_plan(
            tmp_path,
            WireContext(transport="stdio"),
            verified_version="0.0.0",
            interpreter=Path("/surface/python"),
            state_dir=Path("/surface/state"),
        )


@pytest.mark.parametrize(
    ("ctx", "interpreter", "state_dir"),
    [
        (WireContext(transport="stdio"), Path("python"), Path("/surface/state")),
        (WireContext(transport="stdio"), Path("/surface/python"), Path("state")),
        (
            WireContext(transport="stdio", db_url="sqlite:///relative.db"),
            Path("/surface/python"),
            Path("/surface/state"),
        ),
    ],
)
def test_internal_plugin_plan_rejects_unvalidated_runtime_paths(
    tmp_path: Path, ctx: WireContext, interpreter: Path, state_dir: Path
) -> None:
    from brains import wire as wire_module

    with pytest.raises(ValueError, match="runtime paths must be absolute"):
        wire_module._opencode_plugin_plan(
            tmp_path,
            ctx,
            verified_version=OPENCODE_SUPPORTED_VERSION,
            interpreter=interpreter,
            state_dir=state_dir,
        )


def test_wire_fails_closed_when_opencode_binary_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brains import wire as wire_module

    monkeypatch.setattr(wire_module.shutil, "which", lambda _name: None)
    home = tmp_path / "missing"
    report = wire(home, WireContext(transport="stdio"), tools=["opencode"], force=True)

    assert report["ok"] is False
    assert report["tools"][0]["lifecycle_plugin"]["action"] == "error"
    assert not (home / ".config/opencode/plugins/brains-lifecycle.js").exists()
    assert not (home / ".config/opencode/opencode.json").exists()


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(0, "opencode unknown\n"), (1, ""), (0, "opencode 1.18.24\n")],
)
def test_opencode_compatibility_requires_the_exact_pinned_version(
    returncode: int,
    stdout: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains import wire as wire_module

    monkeypatch.setattr(
        wire_module.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Completed", (), {"returncode": returncode, "stdout": stdout}
        )(),
    )
    verified_version, detail = wire_module._opencode_compatibility()
    assert verified_version is None
    assert OPENCODE_SUPPORTED_VERSION not in detail

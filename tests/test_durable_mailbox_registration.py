from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from brains.authz.resolver import (
    principal_for_operator_slug,
    principal_for_secret,
    principal_slot,
    set_current_principal,
)
from brains.cli.app import app as cli_app
from brains.config import settings
from brains.control.durable_mailbox import (
    MailboxUnavailableError,
    MailboxValidationError,
    canonical_mailbox_tool,
    create_managed_agent_mailbox,
    ensure_operator_mailboxes,
    extract_native_tool_session_id,
    list_phonebook,
    lookup_mailbox,
    read_mailbox_binding_file,
    recover_managed_agent_mailbox_binding,
    register_agent_mailbox,
    revoke_managed_agent_mailbox_binding,
    rotate_managed_agent_mailbox_binding,
    validate_native_tool_session_id,
)
from brains.control.events import append_event
from brains.control.memberships import add_membership, set_workspace_visibility
from brains.control.operators import add_operator, ensure_admin_operator
from brains.control.orgs import add_member, create_org
from brains.control.resume import resume_brain_session
from brains.control.sessions import (
    end_session,
    heartbeat_session,
    link_session_successor,
    register_workspace,
    require_live_session,
    start_session,
    sweep_stale_session_leases,
)
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    Event,
    Mailbox,
    MailboxAttachment,
    Operator,
    SessionLease,
    SessionSuccessor,
    Workspace,
)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _native(prefix: str = "native") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _binding() -> str:
    return f"binding-{uuid.uuid4().hex}"


def _write_binding(path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


@pytest.fixture(autouse=True)
def _bootstrap_mailboxes():
    init_db()
    ensure_admin_operator()
    ensure_operator_mailboxes()


def test_canonical_tool_and_native_id_validation_reject_placeholders() -> None:
    assert canonical_mailbox_tool(" Copilot ") == "copilot-cli"
    assert canonical_mailbox_tool("claude-code") == "claude-code"
    assert canonical_mailbox_tool("codex") == "codex"
    assert canonical_mailbox_tool("OpenCode") == "opencode"
    with pytest.raises(MailboxValidationError):
        canonical_mailbox_tool("pytest")

    valid = _native("claude")
    assert validate_native_tool_session_id(valid) == valid
    for invalid in (
        "current",
        "short",
        "task-123456789",
        "gpt-5.6-preview",
        "claude-opus-4.8",
        "ses_0123456789ab",
        " native-0123456789abcdef ",
    ):
        with pytest.raises(MailboxValidationError):
            validate_native_tool_session_id(invalid)


@pytest.mark.parametrize(
    ("adapter", "source"),
    [
        ("github-copilot", "copilot_session_id"),
        ("claude", "claude_session_id"),
        ("codex", "codex_thread_id"),
        ("OpenCode", "opencode_session_id"),
    ],
)
def test_native_id_extraction_is_explicit_and_preserves_adapter_provenance(
    adapter: str, source: str
) -> None:
    native_id = _native("harness")
    resolved = extract_native_tool_session_id(adapter, {source: native_id})
    assert resolved == {
        "status": "resolved",
        "adapter": adapter,
        "tool": canonical_mailbox_tool(adapter),
        "native_tool_session_id": native_id,
        "source": source,
    }
    unavailable = extract_native_tool_session_id(adapter, {})
    assert unavailable["status"] == "unavailable"
    assert unavailable["native_tool_session_id"] is None


def test_codex_native_id_extraction_refuses_ambiguous_adapter_evidence() -> None:
    result = extract_native_tool_session_id(
        "codex",
        {
            "codex_thread_id": _native("thread"),
            "codex_session_id": _native("session"),
        },
    )
    assert result["status"] == "ambiguous"
    assert result["native_tool_session_id"] is None
    assert result["source"] is None


def test_binding_file_is_bounded_and_owner_only(tmp_path) -> None:
    secure = tmp_path / "secure-binding"
    value = _binding()
    _write_binding(secure, value)
    assert read_mailbox_binding_file(secure) == value

    oversized = tmp_path / "oversized-binding"
    _write_binding(oversized, "x" * 1025)
    with pytest.raises(MailboxValidationError, match="unavailable"):
        read_mailbox_binding_file(oversized)

    if os.name != "nt":
        insecure = tmp_path / "insecure-binding"
        _write_binding(insecure, value)
        insecure.chmod(0o644)
        with pytest.raises(MailboxValidationError, match="only by its owner"):
            read_mailbox_binding_file(insecure)


@pytest.mark.parametrize("adapter", ["github-copilot", "claude", "codex", "OpenCode"])
def test_managed_binding_create_rotate_recover_revoke_and_restart_journey(
    tmp_path, monkeypatch, adapter: str
) -> None:
    from brains.api.admin_key import state_dir
    from brains.control import sessions as sessions_ctl

    workspace = str(tmp_path / f"managed-{canonical_mailbox_tool(adapter)}")
    native_id = _native("native")
    started = start_session(workspace, tool=adapter)
    created = create_managed_agent_mailbox(workspace, adapter, native_id, started["session_id"])
    binding_path = state_dir() / "mailbox-bindings" / Path(created["binding_file"]).name
    assert binding_path == Path(created["binding_file"])
    original_secret = read_mailbox_binding_file(binding_path, managed_only=True)
    assert original_secret not in repr(created)
    assert created["binding_version"] == 1
    with SessionLocal() as session:
        registration_event = (
            session.query(Event)
            .filter(Event.kind == "mailbox_registered", Event.session_id == started["session_id"])
            .one()
        )
        assert f'"adapter": "{adapter}"' in (registration_event.metadata_json or "")
        assert original_secret not in (registration_event.metadata_json or "")

    conflict = start_session(workspace, tool=adapter)
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        rotate_managed_agent_mailbox_binding(workspace, adapter, native_id, conflict["session_id"])
    assert read_mailbox_binding_file(binding_path, managed_only=True) == original_secret

    moved = tmp_path / f"moved-{canonical_mailbox_tool(adapter)}"
    moved.mkdir()
    canonical = Path(workspace).resolve()
    identity = f"git:{tmp_path / 'shared.git'}"
    monkeypatch.setattr(sessions_ctl, "workspace_identity", lambda _path: identity)
    monkeypatch.setattr(
        sessions_ctl,
        "_git_worktree_paths",
        lambda _path: (str(canonical), str(moved.resolve())),
    )
    assert register_workspace(str(moved)).slug == started["workspace"]

    rotated = rotate_managed_agent_mailbox_binding(
        str(moved), adapter, native_id, started["session_id"]
    )
    rotated_secret = read_mailbox_binding_file(binding_path, managed_only=True)
    assert rotated_secret != original_secret
    assert rotated_secret not in repr(rotated)
    assert rotated["binding_version"] == 2
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        heartbeat_session(
            started["session_id"],
            tool=adapter,
            native_tool_session_id=native_id,
            mailbox_binding_secret=original_secret,
        )

    end_session(started["session_id"], "abrupt adapter exit")
    successor = start_session(workspace, tool=adapter)
    resumed = rotate_managed_agent_mailbox_binding(
        workspace, adapter, native_id, successor["session_id"]
    )
    assert resumed["mailbox_id"] == created["mailbox_id"]
    assert resumed["attachment"]["session_id"] == successor["session_id"]

    binding_path.unlink()
    recovered = recover_managed_agent_mailbox_binding(
        workspace, adapter, native_id, successor["session_id"]
    )
    recovered_secret = read_mailbox_binding_file(binding_path, managed_only=True)
    assert recovered_secret not in repr(recovered)
    assert recovered["binding_version"] == 4
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        heartbeat_session(
            successor["session_id"],
            tool=adapter,
            native_tool_session_id=native_id,
            mailbox_binding_secret=rotated_secret,
        )

    packet = resume_brain_session(
        successor["session_id"],
        tool=adapter,
        native_tool_session_id=native_id,
        mailbox_binding_secret=recovered_secret,
    )
    assert packet["mailbox"]["mailbox_id"] == created["mailbox_id"]
    assert packet["mailbox"]["attachment"]["session_id"] == successor["session_id"]

    revoked = revoke_managed_agent_mailbox_binding(
        workspace, adapter, native_id, successor["session_id"]
    )
    assert revoked["status"] == "retired"
    assert revoked["action"] == "revoked"
    assert not binding_path.exists()
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        heartbeat_session(
            successor["session_id"],
            tool=adapter,
            native_tool_session_id=native_id,
            mailbox_binding_secret=recovered_secret,
        )


def test_invalid_mailbox_start_does_not_register_workspace(tmp_path) -> None:
    workspace_path = str(tmp_path / "invalid-start")
    with pytest.raises(MailboxValidationError):
        start_session(
            workspace_path,
            tool="opencode",
            native_tool_session_id="current",
            mailbox_binding_secret=_binding(),
        )

    with SessionLocal() as session:
        assert session.query(Workspace).filter(Workspace.path == workspace_path).count() == 0


def test_operator_creation_provisions_exactly_one_durable_inbox() -> None:
    operator, _key = add_operator(_slug("mail-op"))

    ensure_operator_mailboxes()
    ensure_operator_mailboxes()

    with SessionLocal() as session:
        rows = (
            session.query(Mailbox)
            .filter(
                Mailbox.owner_operator_id == operator["id"],
                Mailbox.operator_slot == 1,
            )
            .all()
        )
        assert len(rows) == 1
        assert rows[0].address == f"operator:{operator['slug']}@brains"
        assert rows[0].kind == "operator"
        assert rows[0].binding_key_hash is None


def test_startup_backfill_provisions_preexisting_operator_inbox() -> None:
    slug = _slug("legacy-op")
    with SessionLocal() as session:
        operator = Operator(slug=slug, display_name="Legacy Operator")
        session.add(operator)
        session.commit()
        operator_id = operator.id

    ensure_operator_mailboxes()
    rows = list_phonebook()

    assert f"operator:{slug}@brains" in {row["address"] for row in rows}
    with SessionLocal() as session:
        assert (
            session.query(Mailbox)
            .filter(Mailbox.owner_operator_id == operator_id, Mailbox.operator_slot == 1)
            .count()
            == 1
        )


def test_registration_is_idempotent_and_never_exposes_the_binding(tmp_path) -> None:
    started = start_session(str(tmp_path), tool="opencode")
    native_id = _native("opencode")
    binding = _binding()

    first = register_agent_mailbox(
        str(tmp_path), "opencode", native_id, started["session_id"], binding
    )
    replay = register_agent_mailbox(
        str(tmp_path), "opencode", native_id, started["session_id"], binding
    )

    assert first["created"] is True
    assert replay["created"] is False
    assert replay["mailbox_id"] == first["mailbox_id"]
    assert replay["attachment"]["session_id"] == started["session_id"]
    assert replay["attachment"]["cursor"] == 0
    assert binding not in repr(first)
    assert "binding" not in first
    assert first["address"] == f"opencode:{native_id}@{first['workspace']}"

    with SessionLocal() as session:
        mailbox = session.get(Mailbox, first["mailbox_id"])
        assert mailbox is not None
        assert mailbox.binding_key_hash != binding
        assert len(mailbox.binding_key_hash or "") == 64
        assert mailbox.binding_key_version == 1
        assert (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.mailbox_id == mailbox.id)
            .count()
            == 1
        )
        events = session.query(Event).filter(Event.session_id == started["session_id"]).all()
        assert all(binding not in (event.message or "") for event in events)
        assert all(binding not in (event.metadata_json or "") for event in events)


def test_session_start_registers_atomically_and_wrong_binding_rolls_back(tmp_path) -> None:
    workspace = str(tmp_path / "atomic-start")
    native_id = _native("opencode")
    binding = _binding()
    first = start_session(
        workspace,
        tool="opencode",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )
    assert first["mailbox"]["address"] == f"opencode:{native_id}@{first['workspace']}"

    with SessionLocal() as session:
        first_row = session.get(AgentSession, first["session_id"])
        assert first_row is not None
        workspace_id = first_row.workspace_id
        before = (
            session.query(AgentSession).filter(AgentSession.workspace_id == workspace_id).count()
        )

    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        start_session(
            workspace,
            tool="opencode",
            auto_link_predecessor=True,
            native_tool_session_id=native_id,
            mailbox_binding_secret=_binding(),
        )
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        start_session(workspace, tool="opencode", reuse_existing=True)

    with SessionLocal() as session:
        assert (
            session.query(AgentSession).filter(AgentSession.workspace_id == workspace_id).count()
            == before
        )
        original = session.get(AgentSession, first["session_id"])
        assert original is not None
        assert original.state == "running"
        assert session.get(SessionSuccessor, first["session_id"]) is None
        attachment = (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == first["session_id"])
            .one()
        )
        assert attachment.active_slot == 1


def test_resume_verifies_binding_before_reactivating_and_preserves_cursor(tmp_path) -> None:
    workspace = str(tmp_path / "atomic-resume")
    native_id = _native("codex")
    binding = _binding()
    started = start_session(
        workspace,
        tool="codex",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
        mailbox_notification_mode="turn_boundary",
    )
    with SessionLocal() as session:
        attachment = (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == started["session_id"])
            .one()
        )
        attachment.last_seen_delivery_id = 23
        lease = session.get(SessionLease, started["session_id"])
        assert lease is not None
        from brains.control.common import utc_now

        lease.lease_expires_at = utc_now().replace(year=2000)
        session.commit()
    sweep_stale_session_leases()

    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        resume_brain_session(
            started["session_id"],
            tool="codex",
            tool_session_id=_native("tool-link"),
            native_tool_session_id=native_id,
            mailbox_binding_secret=_binding(),
        )
    with SessionLocal() as session:
        dormant = session.get(AgentSession, started["session_id"])
        assert dormant is not None
        assert dormant.state == "dormant"
        assert (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == started["session_id"])
            .one()
            .active_slot
            is None
        )

    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        heartbeat_session(started["session_id"])
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        resume_brain_session(started["session_id"])

    packet = resume_brain_session(
        started["session_id"],
        tool="codex",
        tool_session_id=_native("tool-link"),
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
        mailbox_notification_mode="turn_boundary",
    )
    assert packet["brain_session"]["state"] == "running"
    assert packet["mailbox"]["address"] == started["mailbox"]["address"]
    assert packet["mailbox"]["attachment"]["cursor"] == 23
    assert packet["mailbox"]["attachment"]["notification_mode"] == "turn_boundary"
    renewed = heartbeat_session(
        started["session_id"],
        tool="codex",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )
    assert renewed["state"] == "running"


def test_generic_activity_does_not_extend_mailbox_reachability(tmp_path) -> None:
    workspace = str(tmp_path / "activity-not-reachability")
    native_id = _native("opencode")
    binding = _binding()
    started = start_session(
        workspace,
        tool="opencode",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )
    with SessionLocal() as session:
        lease = session.get(SessionLease, started["session_id"])
        assert lease is not None
        from brains.control.common import utc_now

        expired = utc_now().replace(year=2000)
        lease.lease_expires_at = expired
        session.commit()

    append_event(
        "task_progress", "activity without adapter proof", session_id=started["session_id"]
    )

    with SessionLocal() as session:
        lease = session.get(SessionLease, started["session_id"])
        agent = session.get(AgentSession, started["session_id"])
        assert lease is not None and agent is not None
        persisted_expiry = lease.lease_expires_at
        if persisted_expiry.tzinfo is None:
            persisted_expiry = persisted_expiry.replace(tzinfo=expired.tzinfo)
        assert persisted_expiry == expired
        assert agent.last_activity_at is not None


def test_proofed_heartbeat_reattaches_dormant_mailbox(tmp_path) -> None:
    workspace = str(tmp_path / "heartbeat-reattach")
    native_id = _native("opencode")
    binding = _binding()
    started = start_session(
        workspace,
        tool="opencode",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )
    with SessionLocal() as session:
        lease = session.get(SessionLease, started["session_id"])
        assert lease is not None
        from brains.control.common import utc_now

        lease.lease_expires_at = utc_now().replace(year=2000)
        session.commit()
    sweep_stale_session_leases()

    heartbeat = heartbeat_session(
        started["session_id"],
        tool="opencode",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )

    assert heartbeat["state"] == "running"
    with SessionLocal() as session:
        attachment = (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == started["session_id"])
            .one()
        )
        assert attachment.active_slot == 1
        assert attachment.detached_at is None


def test_pid_owned_mailbox_heartbeat_does_not_require_lease(tmp_path) -> None:
    native_id = _native("opencode")
    binding = _binding()
    started = start_session(
        str(tmp_path / "pid-heartbeat"),
        tool="opencode",
        pid=os.getpid(),
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )
    with SessionLocal() as session:
        assert session.get(SessionLease, started["session_id"]) is None

    heartbeat = heartbeat_session(
        started["session_id"],
        tool="opencode",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )

    assert heartbeat["state"] == "running"


def test_activity_cannot_heal_ended_mailbox_session(tmp_path) -> None:
    workspace = str(tmp_path / "ended-not-healed")
    started = start_session(
        workspace,
        tool="opencode",
        native_tool_session_id=_native("opencode"),
        mailbox_binding_secret=_binding(),
    )
    end_session(started["session_id"], "finished")
    append_event("late_result", "arrived after end", session_id=started["session_id"])
    with SessionLocal() as session:
        row = session.get(AgentSession, started["session_id"])
        assert row is not None
        assert row.ended_at is not None
        row.last_activity_at = row.ended_at.replace(year=row.ended_at.year + 1)
        session.commit()

    with SessionLocal() as session, pytest.raises(ValueError, match="ended"):
        require_live_session(session, started["session_id"], action="test")


def test_automatic_successor_refuses_still_reachable_mailbox(tmp_path) -> None:
    workspace = str(tmp_path / "reachable-predecessor")
    native_id = _native("opencode")
    binding = _binding()
    first = start_session(
        workspace,
        tool="opencode",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )

    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        start_session(
            workspace,
            tool="opencode",
            auto_link_predecessor=True,
            native_tool_session_id=native_id,
            mailbox_binding_secret=binding,
        )

    with SessionLocal() as session:
        assert session.get(SessionSuccessor, first["session_id"]) is None
        attachment = (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == first["session_id"])
            .one()
        )
        assert attachment.active_slot == 1


def test_legacy_tool_link_never_fabricates_a_mailbox(tmp_path) -> None:
    from brains.control.resume import link_tool_session

    started = start_session(str(tmp_path / "legacy-link"), tool="opencode")
    link_tool_session(started["session_id"], "opencode", "current")

    with SessionLocal() as session:
        assert (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == started["session_id"])
            .count()
            == 0
        )


def test_proofed_historical_tool_link_does_not_reattach_ended_mailbox(tmp_path) -> None:
    from brains.control.resume import link_tool_session

    native_id = _native("opencode")
    binding = _binding()
    started = start_session(
        str(tmp_path / "historical-link"),
        tool="opencode",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )
    end_session(started["session_id"], "finished")

    linked = link_tool_session(
        started["session_id"],
        "opencode",
        _native("tool-link"),
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )

    assert linked["brain_session_id"] == started["session_id"]
    with SessionLocal() as session:
        attachment = (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == started["session_id"])
            .one()
        )
        assert attachment.active_slot is None
        assert attachment.detached_at is not None


def test_explicit_successor_requires_binding_and_reattaches_same_mailbox(tmp_path) -> None:
    workspace = str(tmp_path / "explicit-successor")
    native_id = _native("opencode")
    binding = _binding()
    predecessor = start_session(
        workspace,
        tool="opencode",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )
    end_session(predecessor["session_id"], "restart")
    successor = start_session(workspace, tool="opencode")

    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        link_session_successor(predecessor["session_id"], successor["session_id"])
    with SessionLocal() as session:
        assert session.get(SessionSuccessor, predecessor["session_id"]) is None

    linked = link_session_successor(
        predecessor["session_id"],
        successor["session_id"],
        tool="opencode",
        native_tool_session_id=native_id,
        mailbox_binding_secret=binding,
    )
    assert linked["mailbox"]["mailbox_id"] == predecessor["mailbox"]["mailbox_id"]
    assert linked["mailbox"]["attachment"]["session_id"] == successor["session_id"]
    assert linked["mailbox"]["address"] == predecessor["mailbox"]["address"]


def test_wrong_binding_owner_tool_and_workspace_share_one_refusal(tmp_path) -> None:
    workspace = tmp_path / "owner"
    started = start_session(str(workspace), tool="opencode")
    native_id = _native("opencode")
    binding = _binding()
    register_agent_mailbox(str(workspace), "opencode", native_id, started["session_id"], binding)
    other = start_session(str(tmp_path / "other"), tool="opencode")

    attempts = [
        (str(workspace), "opencode", native_id, started["session_id"], _binding()),
        (str(workspace), "codex", _native("codex"), started["session_id"], _binding()),
        (str(workspace), "opencode", _native("other"), other["session_id"], _binding()),
    ]
    messages = []
    for args in attempts:
        with pytest.raises(MailboxUnavailableError) as raised:
            register_agent_mailbox(*args)
        messages.append(str(raised.value))
    assert messages == ["mailbox unavailable"] * len(attempts)


def test_concurrent_registration_converges_on_one_mailbox_and_attachment(tmp_path) -> None:
    workspace = str(tmp_path / "concurrent-same")
    started = start_session(workspace, tool="opencode")
    native_id = _native("opencode")
    binding = _binding()

    def register() -> dict:
        return register_agent_mailbox(
            workspace,
            "opencode",
            native_id,
            started["session_id"],
            binding,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: register(), range(2)))

    assert len({result["mailbox_id"] for result in results}) == 1
    assert sorted(result["created"] for result in results) == [False, True]
    with SessionLocal() as session:
        mailbox_id = results[0]["mailbox_id"]
        assert session.query(Mailbox).filter(Mailbox.id == mailbox_id).count() == 1
        assert (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.mailbox_id == mailbox_id)
            .count()
            == 1
        )


def test_concurrent_attachment_allows_only_one_live_incarnation(tmp_path) -> None:
    workspace = str(tmp_path / "concurrent-attachment")
    first = start_session(workspace, tool="codex")
    second = start_session(workspace, tool="codex")
    native_id = _native("codex")
    binding = _binding()

    def attach(session_id: str) -> tuple[str, str]:
        try:
            result = register_agent_mailbox(
                workspace,
                "codex",
                native_id,
                session_id,
                binding,
            )
            return "ok", result["attachment"]["session_id"]
        except MailboxUnavailableError as exc:
            return "unavailable", str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attach, (first["session_id"], second["session_id"])))

    assert sorted(status for status, _detail in results) == ["ok", "unavailable"]
    assert next(detail for status, detail in results if status == "unavailable") == (
        "mailbox unavailable"
    )
    with SessionLocal() as session:
        assert (
            session.query(MailboxAttachment).filter(MailboxAttachment.active_slot == 1).count() >= 1
        )


def test_withdrawn_execution_session_cannot_register_mailbox(tmp_path) -> None:
    started = start_session(str(tmp_path / "execution-row"), tool="opencode")
    with SessionLocal() as session:
        row = session.get(AgentSession, started["session_id"])
        assert row is not None
        row.runtime_id = 1
        session.commit()

    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        register_agent_mailbox(
            str(tmp_path / "execution-row"),
            "opencode",
            _native("opencode"),
            started["session_id"],
            _binding(),
        )


def test_active_attachment_conflicts_then_replacement_preserves_cursor(tmp_path) -> None:
    workspace = str(tmp_path / "continuity")
    first_session = start_session(workspace, tool="claude")
    native_id = _native("claude")
    binding = _binding()
    registered = register_agent_mailbox(
        workspace,
        "claude-code",
        native_id,
        first_session["session_id"],
        binding,
    )
    second_session = start_session(workspace, tool="claude")

    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        register_agent_mailbox(
            workspace,
            "claude-code",
            native_id,
            second_session["session_id"],
            binding,
        )

    with SessionLocal() as session:
        attachment = (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == first_session["session_id"])
            .one()
        )
        attachment.last_seen_delivery_id = 17
        session.commit()
    end_session(first_session["session_id"], "replace this incarnation")

    replacement = register_agent_mailbox(
        workspace,
        "claude-code",
        native_id,
        second_session["session_id"],
        binding,
    )
    assert replacement["mailbox_id"] == registered["mailbox_id"]
    assert replacement["created"] is False
    assert replacement["attachment"]["cursor"] == 17

    with SessionLocal() as session:
        old = (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == first_session["session_id"])
            .one()
        )
        current = (
            session.query(MailboxAttachment)
            .filter(MailboxAttachment.session_id == second_session["session_id"])
            .one()
        )
        assert old.active_slot is None
        assert old.detached_at is not None
        assert old.detach_reason == "session_ended"
        assert current.active_slot == 1


def test_private_workspace_phonebook_and_lookup_do_not_disclose(tmp_path) -> None:
    operator_a, _ = add_operator(_slug("mail-a"))
    operator_b, _ = add_operator(_slug("mail-b"))
    org_a = create_org(_slug("mail-org-a"), "Mailbox Org A")
    org_b = create_org(_slug("mail-org-b"), "Mailbox Org B")
    add_member(org_a["id"], operator_a["slug"], role="member")
    add_member(org_b["id"], operator_b["slug"], role="member")

    path_a = str(tmp_path / "private-a")
    path_b = str(tmp_path / "private-b")
    workspace_a = register_workspace(path_a, org_id=org_a["id"])
    workspace_b = register_workspace(path_b, org_id=org_b["id"])
    set_workspace_visibility(workspace_a.slug, "private")
    set_workspace_visibility(workspace_b.slug, "private")
    add_membership(workspace_a.slug, operator_a["slug"])
    add_membership(workspace_b.slug, operator_b["slug"])

    principal_a = principal_for_operator_slug(operator_a["slug"])
    principal_b = principal_for_operator_slug(operator_b["slug"])
    assert principal_a is not None and principal_b is not None
    started = start_session(path_a, tool="codex", operator=operator_a["slug"])
    mailbox = register_agent_mailbox(
        path_a,
        "codex",
        _native("codex"),
        started["session_id"],
        _binding(),
        principal=principal_a,
    )

    visible_a = list_phonebook(path_a, include_paths=True, principal=principal_a)
    assert mailbox["address"] in {row["address"] for row in visible_a}
    agent_row = next(row for row in visible_a if row["address"] == mailbox["address"])
    assert "workspace_path" not in agent_row, "ordinary members may not see local paths"

    visible_b = list_phonebook(path_b, principal=principal_b)
    assert mailbox["address"] not in {row["address"] for row in visible_b}
    unauthorized = None
    unknown = None
    with pytest.raises(MailboxUnavailableError) as raised:
        lookup_mailbox(mailbox["address"], principal=principal_b)
    unauthorized = str(raised.value)
    with pytest.raises(MailboxUnavailableError) as raised:
        lookup_mailbox(f"codex:{_native()}@missing", principal=principal_b)
    unknown = str(raised.value)
    assert unauthorized == unknown == "mailbox unavailable"


def test_private_workspace_operator_address_requires_shared_workspace_visibility(tmp_path) -> None:
    owner, _ = add_operator(_slug("private-owner"))
    reader, _ = add_operator(_slug("private-reader"))
    org = create_org(_slug("private-phonebook-org"), "Private Phonebook Org")
    add_member(org["id"], owner["slug"], role="member")
    add_member(org["id"], reader["slug"], role="member")
    workspace = register_workspace(str(tmp_path / "private-phonebook"), org_id=org["id"])
    set_workspace_visibility(workspace.slug, "private")
    add_membership(workspace.slug, reader["slug"])
    reader_principal = principal_for_operator_slug(reader["slug"])
    assert reader_principal is not None

    without_shared_scope = list_phonebook(
        workspace.path,
        principal=reader_principal,
    )
    assert f"operator:{owner['slug']}@brains" not in {
        row["address"] for row in without_shared_scope
    }

    add_membership(workspace.slug, owner["slug"])
    with_shared_scope = list_phonebook(workspace.path, principal=reader_principal)
    assert f"operator:{owner['slug']}@brains" in {row["address"] for row in with_shared_scope}


def test_archived_workspace_address_is_not_discoverable_or_attachable(tmp_path) -> None:
    workspace_path = str(tmp_path / "archived-mailbox")
    started = start_session(workspace_path, tool="codex")
    binding = _binding()
    mailbox = register_agent_mailbox(
        workspace_path,
        "codex",
        _native("codex"),
        started["session_id"],
        binding,
    )
    with SessionLocal() as session:
        workspace = session.query(Workspace).filter(Workspace.slug == started["workspace"]).one()
        workspace.status = "archived"
        session.commit()

    assert mailbox["address"] not in {row["address"] for row in list_phonebook()}
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        lookup_mailbox(mailbox["address"])
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        register_agent_mailbox(
            workspace_path,
            "codex",
            mailbox["address"].split(":", 1)[1].split("@", 1)[0],
            started["session_id"],
            binding,
        )


def test_destructive_workspace_prune_removes_agent_mailbox_but_keeps_operator_inbox(
    tmp_path,
) -> None:
    from brains.cli.app import _apply_workspace_cascade

    workspace_path = str(tmp_path / "pruned-mailbox")
    started = start_session(workspace_path, tool="opencode")
    mailbox = register_agent_mailbox(
        workspace_path,
        "opencode",
        _native("opencode"),
        started["session_id"],
        _binding(),
    )
    operator_address = f"operator:{started['operator']}@brains"

    with SessionLocal() as session:
        workspace = session.query(Workspace).filter(Workspace.slug == started["workspace"]).one()
        affected = _apply_workspace_cascade(session, [workspace.id])

    assert affected["mailboxes"] == 1
    with SessionLocal() as session:
        assert session.get(Mailbox, mailbox["mailbox_id"]) is None
        assert session.query(Mailbox).filter(Mailbox.address == operator_address).count() == 1


def test_http_registration_and_lookup_are_protected_and_non_disclosing(
    tmp_path, auth_headers
) -> None:
    client = TestClient(app)
    started = start_session(str(tmp_path), tool="copilot")
    native_id = _native("copilot")
    binding = _binding()
    workspace = started["workspace"]
    payload = {
        "tool": "copilot-cli",
        "native_tool_session_id": native_id,
        "session_id": started["session_id"],
    }
    route = f"/v1/operator/workspaces/{workspace}/mailboxes/register"

    assert client.post(route, json=payload).status_code == 401
    missing = client.post(route, json=payload, headers=auth_headers)
    assert missing.status_code == 400
    response = client.post(
        route,
        json=payload,
        headers={**auth_headers, "x-brains-mailbox-binding": binding},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert binding not in response.text

    phonebook = client.get(
        "/v1/operator/mailboxes",
        params={"workspace": workspace},
        headers=auth_headers,
    )
    assert phonebook.status_code == 200
    assert result["address"] in {row["address"] for row in phonebook.json()["data"]}

    known = client.get(
        "/v1/operator/mailboxes/lookup",
        params={"address": result["address"]},
        headers=auth_headers,
    )
    unknown = client.get(
        "/v1/operator/mailboxes/lookup",
        params={"address": f"copilot-cli:{_native()}@missing"},
        headers=auth_headers,
    )
    assert known.status_code == 200
    assert unknown.status_code == 404
    assert unknown.json()["error"]["message"] == "unknown mailbox: 'unavailable'"


def test_mcp_and_cli_adapters_expose_phonebook_without_binding_values(tmp_path) -> None:
    from brains.mcp import server as mcp_server

    assert {"mailbox_register", "mailbox_phonebook", "mailbox_lookup"} <= set(
        mcp_server.TOOL_REGISTRY
    )

    started = start_session(str(tmp_path), tool="opencode")
    binding = _binding()
    binding_file = tmp_path / "mailbox-binding"
    _write_binding(binding_file, binding)
    native_id = _native("opencode")
    runner = CliRunner()

    registered = runner.invoke(
        cli_app,
        [
            "mailbox",
            "register",
            "--workspace",
            str(tmp_path),
            "--tool",
            "opencode",
            "--native-tool-session-id",
            native_id,
            "--session",
            started["session_id"],
            "--binding-file",
            str(binding_file),
        ],
    )
    assert registered.exit_code == 0, registered.output
    assert binding not in registered.output

    lifecycle_dir = tmp_path / "lifecycle"
    lifecycle_binding = tmp_path / "lifecycle-binding"
    lifecycle_secret = _binding()
    _write_binding(lifecycle_binding, lifecycle_secret)
    lifecycle_native = _native("opencode")
    lifecycle = mcp_server.call_tool(
        "brains_start_session",
        workspace_path=str(lifecycle_dir),
        tool="opencode",
        native_tool_session_id=lifecycle_native,
        mailbox_binding_file=str(lifecycle_binding),
    )
    assert lifecycle["mailbox"]["address"].startswith(f"opencode:{lifecycle_native}@")
    assert lifecycle["mailbox"]["attachment"]["notification_mode"] == "pull"
    assert lifecycle_secret not in repr(lifecycle)

    second = start_session(str(tmp_path / "mcp"), tool="opencode")
    mcp_binding = tmp_path / "mcp-binding"
    _write_binding(mcp_binding, _binding())
    mcp_registered = mcp_server.call_tool(
        "brains_mailbox_register",
        workspace_path=str(tmp_path / "mcp"),
        tool="opencode",
        native_tool_session_id=_native("opencode"),
        session_id=second["session_id"],
        binding_file=str(mcp_binding),
    )
    assert mcp_registered["created"] is True
    assert mcp_registered["attachment"]["notification_mode"] == "pull"
    assert "binding" not in mcp_registered

    phonebook = mcp_server.call_tool("brains_mailbox_phonebook", workspace_path=str(tmp_path))
    address = f"opencode:{native_id}@{started['workspace']}"
    assert address in {row["address"] for row in phonebook}
    looked_up = mcp_server.call_tool("brains_mailbox_lookup", address=address)
    assert looked_up["address"] == address
    assert binding not in repr(looked_up)


def test_authenticated_mcp_binding_path_is_confined_to_managed_state(tmp_path) -> None:
    from brains.api.admin_key import state_dir
    from brains.mcp import tools as mcp_tools

    unmanaged = tmp_path / "unmanaged-binding"
    _write_binding(unmanaged, _binding())
    managed_dir = state_dir() / "mailbox-bindings"
    managed_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        managed_dir.chmod(0o700)
    managed = managed_dir / f"{uuid.uuid4().hex}{uuid.uuid4().hex}.binding"
    _write_binding(managed, _binding())
    principal = principal_for_secret(settings.api_key)
    assert principal is not None

    try:
        with principal_slot():
            set_current_principal(principal)
            with pytest.raises(MailboxValidationError, match="outside managed state"):
                mcp_tools._read_mailbox_binding_file(str(unmanaged))
            assert (
                mcp_tools._read_mailbox_binding_file(str(managed))
                == managed.read_text(encoding="utf-8").strip()
            )
    finally:
        managed.unlink(missing_ok=True)


def test_admin_phonebook_may_show_resolved_path(tmp_path) -> None:
    started = start_session(str(tmp_path), tool="codex")
    result = register_agent_mailbox(
        str(tmp_path), "codex", _native("codex"), started["session_id"], _binding()
    )
    rows = list_phonebook(str(tmp_path), include_paths=True)
    row = next(item for item in rows if item["address"] == result["address"])
    assert row["workspace_path"] == str(tmp_path.resolve())
    assert settings.api_key not in repr(row)

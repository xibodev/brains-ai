from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from brains.api.auth import mint_browser_token
from brains.authz.principal import CHANNEL_API, CHANNEL_BROWSER
from brains.authz.resolver import principal_for_operator_slug
from brains.cli.app import app as cli_app
from brains.config import settings
from brains.control.durable_mail import (
    broadcast_mailbox_message,
    forward_mailbox_message,
    list_browser_mailboxes,
    read_mailbox_inbox,
    read_mailbox_sent,
    read_mailbox_thread,
    reply_mailbox_message,
    send_mailbox_message,
    settle_mailbox_notification,
    take_mailbox_notification,
)
from brains.control.durable_mailbox import (
    MailboxUnavailableError,
    MailboxValidationError,
    register_agent_mailbox,
)
from brains.control.events import list_events
from brains.control.memberships import add_membership, remove_membership, set_workspace_visibility
from brains.control.operators import add_operator, ensure_admin_operator
from brains.control.orgs import add_member, create_org
from brains.control.sessions import end_session, register_workspace, start_session
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import MailDelivery, MailMessage, MailNotificationAttempt, Workspace


def _native(prefix: str = "native") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _binding() -> str:
    return f"binding-{uuid.uuid4().hex}"


def _operation(prefix: str = "op") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _write_binding(path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _agent(path, tool: str = "opencode", *, operator: str | None = None) -> dict:
    binding = _binding()
    started = start_session(str(path), tool=tool, operator=operator)
    principal = principal_for_operator_slug(operator) if operator else None
    mailbox = register_agent_mailbox(
        str(path),
        tool,
        _native(tool),
        started["session_id"],
        binding,
        principal=principal,
    )
    return {"path": str(path), "session": started, "mailbox": mailbox, "binding": binding}


def _notified_agent(path, tool: str = "opencode", *, mode: str = "immediate") -> dict:
    binding = _binding()
    started = start_session(str(path), tool=tool)
    mailbox = register_agent_mailbox(
        str(path),
        tool,
        _native(tool),
        started["session_id"],
        binding,
        notification_mode=mode,
    )
    return {"path": str(path), "session": started, "mailbox": mailbox, "binding": binding}


@pytest.fixture(autouse=True)
def _bootstrap() -> None:
    init_db()
    ensure_admin_operator()


def test_offline_accept_survives_restart_and_cursor_advances(tmp_path) -> None:
    sender = _agent(tmp_path / "sender")
    recipient = _agent(tmp_path / "recipient")
    end_session(recipient["session"]["session_id"], "offline")

    sent = send_mailbox_message(
        sender["path"],
        [recipient["mailbox"]["address"]],
        "Durable note",
        _operation(),
        body="available after restart",
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    assert sent["deliveries"][0]["state"] == "accepted"
    assert sent["deliveries"][0]["read_at"] is None

    replacement = start_session(recipient["path"], tool="opencode")
    register_agent_mailbox(
        recipient["path"],
        "opencode",
        recipient["mailbox"]["address"].split(":", 1)[1].split("@", 1)[0],
        replacement["session_id"],
        recipient["binding"],
    )
    inbox = read_mailbox_inbox(
        session_id=replacement["session_id"],
        binding_secret=recipient["binding"],
    )

    assert [message["message_id"] for message in inbox["messages"]] == [sent["message_id"]]
    assert inbox["messages"][0]["sender_session_id"] == sender["session"]["session_id"]
    assert inbox["messages"][0]["inbox_delivery"]["read_by_session_id"] == replacement["session_id"]
    assert inbox["cursor"] == inbox["messages"][0]["inbox_delivery"]["cursor"]
    assert inbox["unread_count"] == 0
    assert (
        read_mailbox_inbox(
            session_id=replacement["session_id"],
            binding_secret=recipient["binding"],
        )["messages"]
        == []
    )
    with SessionLocal() as session:
        assert session.query(MailNotificationAttempt).count() == 0


def test_explicit_cursor_replays_read_history_without_regressing_attachment(tmp_path) -> None:
    workspace = tmp_path / "cursor-replay"
    sender = _agent(workspace)
    recipient = _agent(workspace, tool="codex")
    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Replayable",
        _operation(),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    first = read_mailbox_inbox(
        session_id=recipient["session"]["session_id"],
        binding_secret=recipient["binding"],
    )
    replay = read_mailbox_inbox(
        session_id=recipient["session"]["session_id"],
        binding_secret=recipient["binding"],
        include_read=True,
        after_delivery_id=0,
        mark_read=False,
    )

    assert first["cursor"] == replay["cursor"]
    assert [message["message_id"] for message in replay["messages"]] == [sent["message_id"]]


def test_fixed_nudge_attempt_is_body_free_idempotent_and_pull_safe(tmp_path) -> None:
    workspace = tmp_path / "notification"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace, mode="immediate")
    private_body = "private body must remain only in durable mail"
    operation = _operation("notify")

    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Notify without body",
        operation,
        body=private_body,
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    first = take_mailbox_notification(
        recipient["session"]["session_id"],
        recipient["binding"],
    )

    assert first["fallback"] == "pull"
    assert first["notification"]["adapter"] == "opencode"
    assert first["notification"]["status"] == "claimed"
    assert first["notification"]["nudge"] == (
        "Brains mailbox: new mail is waiting. Pull your durable inbox."
    )
    assert private_body not in repr(first)
    assert sent["subject"] not in repr(first)
    assert first["notification"]["notification_id"] not in repr(sent)
    assert (
        take_mailbox_notification(recipient["session"]["session_id"], recipient["binding"])[
            "notification"
        ]
        is None
    )
    with SessionLocal() as session:
        row = session.query(MailNotificationAttempt).one()
        assert row.delivery_id == sent["deliveries"][0]["cursor"]
    assert row.attempt == 1
    assert private_body not in repr(row.__dict__)

    replayed_send = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Notify without body",
        operation,
        body=private_body,
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    assert replayed_send["created"] is False
    with SessionLocal() as session:
        assert session.query(MailNotificationAttempt).count() == 1

    failed = settle_mailbox_notification(
        recipient["session"]["session_id"],
        recipient["binding"],
        first["notification"]["notification_id"],
        status="failed",
        error_code="adapter_unavailable",
    )
    assert failed["notification"]["status"] == "failed"
    assert failed["fallback"] == "pull"
    inbox = read_mailbox_inbox(
        session_id=recipient["session"]["session_id"],
        binding_secret=recipient["binding"],
        mark_read=False,
    )
    assert inbox["messages"][0]["body"] == private_body


def test_notification_modes_are_harness_capabilities_not_caller_claims(tmp_path) -> None:
    workspace = tmp_path / "notification-mode-matrix"
    copilot = start_session(str(workspace), tool="copilot")
    with pytest.raises(MailboxValidationError, match="unavailable for copilot-cli"):
        register_agent_mailbox(
            str(workspace),
            "copilot",
            _native("copilot"),
            copilot["session_id"],
            _binding(),
            notification_mode="immediate",
        )
    codex = start_session(str(workspace), tool="codex")
    with pytest.raises(MailboxValidationError, match="unavailable for codex"):
        register_agent_mailbox(
            str(workspace),
            "codex",
            _native("codex"),
            codex["session_id"],
            _binding(),
            notification_mode="immediate",
        )
    opencode = start_session(str(workspace), tool="opencode")
    attached = register_agent_mailbox(
        str(workspace),
        "opencode",
        _native("opencode"),
        opencode["session_id"],
        _binding(),
    )
    assert attached["attachment"]["notification_mode"] == "pull"


def test_pull_mode_and_detached_mail_never_fabricate_a_notification(tmp_path) -> None:
    workspace = tmp_path / "pull-fallback"
    sender = _agent(workspace)
    pull_recipient = _agent(workspace, tool="codex")
    detached = _notified_agent(workspace, tool="claude", mode="immediate")
    end_session(detached["session"]["session_id"], "offline")

    sent = send_mailbox_message(
        str(workspace),
        [pull_recipient["mailbox"]["address"], detached["mailbox"]["address"]],
        "Pull remains authoritative",
        _operation("pull"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )

    assert take_mailbox_notification(
        pull_recipient["session"]["session_id"], pull_recipient["binding"]
    ) == {"notification": None, "fallback": "pull", "timeout": False}
    with SessionLocal() as session:
        delivery_ids = [delivery["cursor"] for delivery in sent["deliveries"]]
        assert (
            session.query(MailNotificationAttempt)
            .filter(MailNotificationAttempt.delivery_id.in_(delivery_ids))
            .count()
            == 0
        )


def test_send_end_race_fails_the_nudge_but_preserves_pull_delivery(
    tmp_path,
) -> None:
    workspace = tmp_path / "notification-end-race"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace)
    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Commit before detach",
        _operation("end-race"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    end_session(recipient["session"]["session_id"], "ended before nudge")
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        take_mailbox_notification(recipient["session"]["session_id"], recipient["binding"])
    with SessionLocal() as session:
        row = (
            session.query(MailNotificationAttempt)
            .filter(MailNotificationAttempt.delivery_id == sent["deliveries"][0]["cursor"])
            .one()
        )
        assert row.status == "failed"
        assert row.attempt == 1
        assert row.error_code == "attachment_detached"


def test_reattachment_queues_unread_catchup_for_the_new_incarnation(tmp_path) -> None:
    workspace = tmp_path / "reattach-notification"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace, mode="immediate")
    end_session(recipient["session"]["session_id"], "restart")
    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Waiting while detached",
        _operation("reattach"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    with SessionLocal() as session:
        assert (
            session.query(MailNotificationAttempt)
            .filter(MailNotificationAttempt.delivery_id == sent["deliveries"][0]["cursor"])
            .count()
            == 0
        )

    successor = start_session(str(workspace), tool="opencode")
    attached = register_agent_mailbox(
        str(workspace),
        "opencode",
        recipient["mailbox"]["address"].split(":", 1)[1].split("@", 1)[0],
        successor["session_id"],
        recipient["binding"],
        notification_mode="immediate",
    )
    notification = take_mailbox_notification(successor["session_id"], recipient["binding"])
    assert attached["attachment"]["notification_mode"] == "immediate"
    assert notification["notification"]["adapter"] == "opencode"


def test_notification_claim_and_settle_require_current_binding_and_exact_replay(tmp_path) -> None:
    workspace = tmp_path / "notification-proof"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace)
    send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Proof bound",
        _operation("proof"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        take_mailbox_notification(recipient["session"]["session_id"], _binding())
    claimed = take_mailbox_notification(recipient["session"]["session_id"], recipient["binding"])
    delivered = settle_mailbox_notification(
        recipient["session"]["session_id"],
        recipient["binding"],
        claimed["notification"]["notification_id"],
        status="delivered",
    )
    replay = settle_mailbox_notification(
        recipient["session"]["session_id"],
        recipient["binding"],
        claimed["notification"]["notification_id"],
        status="delivered",
    )
    assert replay == delivered
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        settle_mailbox_notification(
            recipient["session"]["session_id"],
            recipient["binding"],
            claimed["notification"]["notification_id"],
            status="failed",
            error_code="late_failure",
        )
    with pytest.raises(MailboxValidationError):
        settle_mailbox_notification(
            recipient["session"]["session_id"],
            recipient["binding"],
            claimed["notification"]["notification_id"],
            status="failed",
        )


def test_concurrent_identical_notification_settlement_is_idempotent(tmp_path) -> None:
    workspace = tmp_path / "notification-settle-race"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace)
    send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Settle once",
        _operation("notification-settle-race"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    claimed = take_mailbox_notification(recipient["session"]["session_id"], recipient["binding"])

    def settle() -> dict:
        return settle_mailbox_notification(
            recipient["session"]["session_id"],
            recipient["binding"],
            claimed["notification"]["notification_id"],
            status="delivered",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: settle(), range(2)))
    assert [result["notification"]["status"] for result in results] == [
        "delivered",
        "delivered",
    ]


def test_concurrent_notification_take_claims_once(tmp_path) -> None:
    workspace = tmp_path / "notification-race"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace)
    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Claim once",
        _operation("notification-race"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )

    def take() -> dict:
        return take_mailbox_notification(recipient["session"]["session_id"], recipient["binding"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: take(), range(2)))
    assert sum(result["notification"] is not None for result in results) == 1
    with SessionLocal() as session:
        row = (
            session.query(MailNotificationAttempt)
            .filter(MailNotificationAttempt.delivery_id == sent["deliveries"][0]["cursor"])
            .one()
        )
        assert row.status == "claimed"
        assert row.attempt == 1


def test_reading_mail_cancels_an_unclaimed_nudge(tmp_path) -> None:
    workspace = tmp_path / "notification-read-race"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace)
    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Read before adapter",
        _operation("notification-read"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )

    inbox = read_mailbox_inbox(
        session_id=recipient["session"]["session_id"],
        binding_secret=recipient["binding"],
    )
    assert inbox["unread_count"] == 0
    assert (
        take_mailbox_notification(recipient["session"]["session_id"], recipient["binding"])[
            "notification"
        ]
        is None
    )
    with SessionLocal() as session:
        row = (
            session.query(MailNotificationAttempt)
            .filter(MailNotificationAttempt.delivery_id == sent["deliveries"][0]["cursor"])
            .one()
        )
        assert row.status == "failed"
        assert row.error_code == "mail_already_read"
        assert row.attempt == 1


def test_browser_read_cancels_an_agent_nudge(tmp_path) -> None:
    workspace = tmp_path / "notification-browser-read"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace)
    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Human read first",
        _operation("notification-browser-read"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    browser = principal_for_operator_slug("admin")
    assert browser is not None
    read_mailbox_inbox(
        address=recipient["mailbox"]["address"],
        principal=browser.with_channel(CHANNEL_BROWSER),
    )

    with SessionLocal() as session:
        row = (
            session.query(MailNotificationAttempt)
            .filter(MailNotificationAttempt.delivery_id == sent["deliveries"][0]["cursor"])
            .one()
        )
        assert row.status == "failed"
        assert row.error_code == "mail_already_read"


def test_notification_mode_change_cancels_only_unclaimed_nudges(tmp_path) -> None:
    workspace = tmp_path / "notification-mode-change"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace)
    sent = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "Mode changed",
        _operation("notification-mode-change"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    register_agent_mailbox(
        str(workspace),
        "opencode",
        recipient["mailbox"]["address"].split(":", 1)[1].split("@", 1)[0],
        recipient["session"]["session_id"],
        recipient["binding"],
        notification_mode="pull",
    )

    with SessionLocal() as session:
        row = (
            session.query(MailNotificationAttempt)
            .filter(MailNotificationAttempt.delivery_id == sent["deliveries"][0]["cursor"])
            .one()
        )
        assert row.status == "failed"
        assert row.error_code == "notification_mode_changed"


def test_send_is_idempotent_and_replay_mismatch_is_refused(tmp_path) -> None:
    workspace = tmp_path / "idempotent"
    sender = _agent(workspace)
    recipient = _agent(workspace, tool="codex")
    operation = _operation()

    first = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "One message",
        operation,
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    replay = send_mailbox_message(
        str(workspace),
        [recipient["mailbox"]["address"]],
        "One message",
        operation,
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )

    assert replay["message_id"] == first["message_id"]
    assert replay["created"] is False
    with pytest.raises(ValueError, match="different mailbox message"):
        send_mailbox_message(
            str(workspace),
            [recipient["mailbox"]["address"]],
            "Changed payload",
            operation,
            sender_session_id=sender["session"]["session_id"],
            binding_secret=sender["binding"],
        )
    with SessionLocal() as session:
        assert (
            session.query(MailMessage).filter(MailMessage.message_id == first["message_id"]).count()
            == 1
        )
        assert (
            session.query(MailDelivery).filter(MailDelivery.message_id == first["cursor"]).count()
            == 1
        )
    events = [
        event
        for event in list_events(limit=100)
        if event.kind == "mailbox_message_sent"
        and first["message_id"] in (event.metadata_json or "")
    ]
    assert len(events) == 1


def test_threads_reply_forward_sent_and_per_recipient_read_state(tmp_path) -> None:
    workspace = tmp_path / "thread"
    alice = _agent(workspace, tool="opencode")
    bob = _agent(workspace, tool="codex")
    carol = _agent(workspace, tool="claude-code")
    root = send_mailbox_message(
        str(workspace),
        [bob["mailbox"]["address"], carol["mailbox"]["address"]],
        "Review plan",
        _operation("root"),
        body="Please review",
        sender_session_id=alice["session"]["session_id"],
        binding_secret=alice["binding"],
    )

    bob_inbox = read_mailbox_inbox(
        session_id=bob["session"]["session_id"],
        binding_secret=bob["binding"],
    )
    assert bob_inbox["messages"][0]["inbox_delivery"]["state"] == "read"
    alice_sent = read_mailbox_sent(
        session_id=alice["session"]["session_id"],
        binding_secret=alice["binding"],
    )
    delivery_states = {
        delivery["recipient"]: delivery["state"]
        for delivery in alice_sent["messages"][0]["deliveries"]
    }
    assert delivery_states == {
        bob["mailbox"]["address"]: "read",
        carol["mailbox"]["address"]: "accepted",
    }

    reply = reply_mailbox_message(
        str(workspace),
        root["message_id"],
        _operation("reply"),
        body="Looks sound",
        sender_session_id=bob["session"]["session_id"],
        binding_secret=bob["binding"],
    )
    assert reply["thread_id"] == root["thread_id"]
    assert reply["in_reply_to"] == root["message_id"]
    assert [delivery["recipient"] for delivery in reply["deliveries"]] == [
        alice["mailbox"]["address"]
    ]
    forwarded = forward_mailbox_message(
        str(workspace),
        root["message_id"],
        [carol["mailbox"]["address"]],
        _operation("forward"),
        body="For context",
        sender_session_id=bob["session"]["session_id"],
        binding_secret=bob["binding"],
    )
    assert forwarded["forwarded_from"] == root["message_id"]
    assert forwarded["forwarded_message"] == {
        "message_id": root["message_id"],
        "forwarded_from": None,
        "sender": alice["mailbox"]["address"],
        "origin_workspace": alice["session"]["workspace"],
        "kind": "info",
        "subject": "Review plan",
        "body": "Please review",
        "created_at": root["created_at"],
    }
    assert forwarded["thread_id"] != root["thread_id"]

    thread = read_mailbox_thread(
        root["thread_id"],
        session_id=alice["session"]["session_id"],
        binding_secret=alice["binding"],
    )
    assert [message["message_id"] for message in thread["messages"]] == [
        root["message_id"],
        reply["message_id"],
    ]
    assert thread["cursor"] == reply["deliveries"][0]["cursor"]
    carol_thread = read_mailbox_thread(
        root["thread_id"],
        session_id=carol["session"]["session_id"],
        binding_secret=carol["binding"],
    )
    assert [message["message_id"] for message in carol_thread["messages"]] == [root["message_id"]]
    assert carol_thread["updated_at"] == root["created_at"]
    assert len(carol_thread["messages"][0]["deliveries"]) == 1
    assert carol_thread["messages"][0]["deliveries"][0]["recipient"] == carol["mailbox"]["address"]
    assert carol_thread["messages"][0]["deliveries"][0]["state"] == "read"


def test_explicit_broadcast_is_workspace_local(tmp_path) -> None:
    workspace = tmp_path / "broadcast"
    sender = _agent(workspace)
    first = _agent(workspace, tool="codex")
    second = _agent(workspace, tool="claude-code")
    outsider = _agent(tmp_path / "other", tool="codex")

    broadcast = broadcast_mailbox_message(
        str(workspace),
        "Workspace notice",
        _operation("broadcast"),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )

    assert broadcast["audience"] == "broadcast"
    assert {delivery["recipient"] for delivery in broadcast["deliveries"]} == {
        first["mailbox"]["address"],
        second["mailbox"]["address"],
    }
    assert (
        read_mailbox_inbox(
            session_id=outsider["session"]["session_id"],
            binding_secret=outsider["binding"],
        )["messages"]
        == []
    )


def test_cross_workspace_and_operator_provenance_disclosure_fail_closed(tmp_path) -> None:
    owner_a, _ = add_operator(f"owner-a-{uuid.uuid4().hex[:8]}")
    owner_b, _ = add_operator(f"owner-b-{uuid.uuid4().hex[:8]}")
    org_a = create_org(f"org-a-{uuid.uuid4().hex[:8]}", "Org A")
    org_b = create_org(f"org-b-{uuid.uuid4().hex[:8]}", "Org B")
    add_member(org_a["id"], owner_a["slug"], role="member")
    add_member(org_b["id"], owner_b["slug"], role="member")
    path_a = tmp_path / "private-a"
    path_b = tmp_path / "private-b"
    workspace_a = register_workspace(str(path_a), org_id=org_a["id"])
    workspace_b = register_workspace(str(path_b), org_id=org_b["id"])
    set_workspace_visibility(workspace_a.slug, "private")
    set_workspace_visibility(workspace_b.slug, "private")
    add_membership(workspace_a.slug, owner_a["slug"])
    add_membership(workspace_b.slug, owner_b["slug"])
    alice = _agent(path_a, operator=owner_a["slug"])
    bob = _agent(path_b, operator=owner_b["slug"])
    principal_a = principal_for_operator_slug(owner_a["slug"])
    assert principal_a is not None

    with pytest.raises(MailboxUnavailableError) as hidden:
        send_mailbox_message(
            str(path_a),
            [bob["mailbox"]["address"]],
            "Not authorized",
            _operation(),
            sender_session_id=alice["session"]["session_id"],
            binding_secret=alice["binding"],
            principal=principal_a,
        )
    with pytest.raises(MailboxUnavailableError) as unknown:
        send_mailbox_message(
            str(path_a),
            [f"opencode:{_native()}@missing"],
            "Unknown",
            _operation(),
            sender_session_id=alice["session"]["session_id"],
            binding_secret=alice["binding"],
            principal=principal_a,
        )
    assert str(hidden.value) == str(unknown.value) == "mailbox unavailable"

    root = send_mailbox_message(
        str(path_a),
        [f"operator:{owner_a['slug']}@brains"],
        "Human only",
        _operation(),
        sender_session_id=alice["session"]["session_id"],
        binding_secret=alice["binding"],
        principal=principal_a,
    )
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        forward_mailbox_message(
            str(path_b),
            root["message_id"],
            [bob["mailbox"]["address"]],
            _operation(),
            sender_session_id=bob["session"]["session_id"],
            binding_secret=bob["binding"],
            principal=principal_for_operator_slug(owner_b["slug"]),
        )


def test_cross_workspace_history_disappears_after_visibility_is_revoked(tmp_path) -> None:
    operator, _ = add_operator(f"history-owner-{uuid.uuid4().hex[:8]}")
    org = create_org(f"history-org-{uuid.uuid4().hex[:8]}", "History Org")
    add_member(org["id"], operator["slug"], role="member")
    first_path = tmp_path / "history-first"
    second_path = tmp_path / "history-second"
    first_workspace = register_workspace(str(first_path), org_id=org["id"])
    second_workspace = register_workspace(str(second_path), org_id=org["id"])
    set_workspace_visibility(first_workspace.slug, "private")
    set_workspace_visibility(second_workspace.slug, "private")
    add_membership(first_workspace.slug, operator["slug"])
    add_membership(second_workspace.slug, operator["slug"])
    sender = _agent(first_path, operator=operator["slug"])
    recipient = _agent(second_path, tool="codex", operator=operator["slug"])
    principal = principal_for_operator_slug(operator["slug"])
    assert principal is not None
    sent = send_mailbox_message(
        str(first_path),
        [recipient["mailbox"]["address"]],
        "Visible before revocation",
        _operation(),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
        principal=principal,
    )
    assert (
        read_mailbox_inbox(
            session_id=recipient["session"]["session_id"],
            binding_secret=recipient["binding"],
            mark_read=False,
            principal=principal,
        )["messages"][0]["message_id"]
        == sent["message_id"]
    )

    remove_membership(first_workspace.slug, operator["slug"])
    refreshed = principal_for_operator_slug(operator["slug"])
    assert refreshed is not None
    inbox = read_mailbox_inbox(
        session_id=recipient["session"]["session_id"],
        binding_secret=recipient["binding"],
        mark_read=False,
        principal=refreshed,
    )
    assert inbox["messages"] == []
    assert inbox["unread_count"] == 0

    add_membership(first_workspace.slug, operator["slug"])
    restored = principal_for_operator_slug(operator["slug"])
    assert restored is not None
    recovered = read_mailbox_inbox(
        session_id=recipient["session"]["session_id"],
        binding_secret=recipient["binding"],
        principal=restored,
    )
    assert [message["message_id"] for message in recovered["messages"]] == [sent["message_id"]]


def test_forward_cannot_widen_operator_mail_into_an_unshared_workspace(tmp_path) -> None:
    human, _ = add_operator(f"forward-human-{uuid.uuid4().hex[:8]}")
    agent_owner, _ = add_operator(f"forward-agent-{uuid.uuid4().hex[:8]}")
    org = create_org(f"forward-org-{uuid.uuid4().hex[:8]}", "Forward Org")
    add_member(org["id"], human["slug"], role="member")
    add_member(org["id"], agent_owner["slug"], role="member")
    source_path = tmp_path / "forward-source"
    target_path = tmp_path / "forward-target"
    source_workspace = register_workspace(str(source_path), org_id=org["id"])
    target_workspace = register_workspace(str(target_path), org_id=org["id"])
    set_workspace_visibility(source_workspace.slug, "private")
    set_workspace_visibility(target_workspace.slug, "private")
    add_membership(source_workspace.slug, human["slug"])
    add_membership(source_workspace.slug, agent_owner["slug"])
    add_membership(target_workspace.slug, agent_owner["slug"])
    source_agent = _agent(source_path, operator=agent_owner["slug"])
    target_agent = _agent(target_path, tool="codex", operator=agent_owner["slug"])
    human_principal = principal_for_operator_slug(human["slug"])
    agent_principal = principal_for_operator_slug(agent_owner["slug"])
    assert human_principal is not None and agent_principal is not None

    source = send_mailbox_message(
        str(source_path),
        [source_agent["mailbox"]["address"]],
        "Human-scoped context",
        _operation(),
        sender_address=f"operator:{human['slug']}@brains",
        principal=human_principal,
    )
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        forward_mailbox_message(
            str(source_path),
            source["message_id"],
            [target_agent["mailbox"]["address"]],
            _operation(),
            sender_session_id=source_agent["session"]["session_id"],
            binding_secret=source_agent["binding"],
            principal=agent_principal,
        )


def test_http_human_mailbox_read_requires_browser_cookie(tmp_path, auth_headers) -> None:
    sender = _agent(tmp_path / "http")
    operator_address = "operator:admin@brains"
    sent = send_mailbox_message(
        sender["path"],
        [operator_address],
        "Human inbox",
        _operation(),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )
    client = TestClient(app)

    raw = client.get(
        "/v1/operator/mailboxes/inbox",
        params={"address": operator_address},
        headers=auth_headers,
    )
    assert raw.status_code == 404
    client.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    browser = client.get(
        "/v1/operator/mailboxes/inbox",
        params={"address": operator_address},
    )
    assert browser.status_code == 200, browser.text
    assert browser.json()["messages"][0]["message_id"] == sent["message_id"]
    assert browser.json()["messages"][0]["inbox_delivery"]["state"] == "accepted"
    read = client.post(
        "/v1/operator/mailboxes/inbox/read",
        json={"address": operator_address},
    )
    assert read.status_code == 200, read.text
    assert read.json()["messages"][0]["inbox_delivery"]["read_channel"] == "browser"


def test_browser_mailbox_selector_reports_truthful_open_and_send_capabilities(
    tmp_path, auth_headers
) -> None:
    agent = _agent(tmp_path / "browser-access")
    client = TestClient(app)

    assert client.get("/v1/operator/mailboxes/access", headers=auth_headers).status_code == 404
    client.cookies.set("brains_admin_key", mint_browser_token(settings.api_key))
    response = client.get("/v1/operator/mailboxes/access")

    assert response.status_code == 200, response.text
    rows = {row["address"]: row for row in response.json()["data"]}
    assert rows["operator:admin@brains"] == {
        "address": "operator:admin@brains",
        "kind": "operator",
        "workspace": None,
        "tool": None,
        "owner_operator": "admin",
        "unread_count": 0,
        "can_open": True,
        "can_send": True,
        "deep_link": "/app/coordination?mailbox=operator%3Aadmin%40brains",
    }
    agent_row = rows[agent["mailbox"]["address"]]
    assert agent_row["can_open"] is True
    assert agent_row["can_send"] is False
    assert agent_row["workspace"] == agent["session"]["workspace"]
    assert agent_row["deep_link"].endswith(
        f"mailbox={agent['mailbox']['address'].replace(':', '%3A').replace('@', '%40')}"
    )

    assert list_browser_mailboxes()[0]["can_send"] is True

    coordination = client.get("/v1/operator/coordination")
    assert coordination.status_code == 200, coordination.text
    projected = next(
        row
        for row in coordination.json()["live_agents"]
        if row["session_id"] == agent["session"]["session_id"]
    )
    assert projected["mailbox_address"] == agent["mailbox"]["address"]
    assert projected["mailbox_deep_link"] == agent_row["deep_link"]

    api_principal = principal_for_operator_slug("admin")
    assert api_principal is not None
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        list_browser_mailboxes(principal=api_principal.with_channel(CHANNEL_API))


def test_browser_member_cannot_open_or_link_another_owners_agent_mailbox(tmp_path) -> None:
    owner, _ = add_operator(f"mail-owner-{uuid.uuid4().hex[:8]}")
    reader, _ = add_operator(f"mail-reader-{uuid.uuid4().hex[:8]}")
    org = create_org(f"mail-browser-org-{uuid.uuid4().hex[:8]}", "Mailbox Browser Org")
    add_member(org["id"], owner["slug"], role="member")
    add_member(org["id"], reader["slug"], role="member")
    path = tmp_path / "browser-private"
    workspace = register_workspace(str(path), org_id=org["id"])
    set_workspace_visibility(workspace.slug, "private")
    add_membership(workspace.slug, owner["slug"])
    add_membership(workspace.slug, reader["slug"])
    agent = _agent(path, operator=owner["slug"])
    reader_principal = principal_for_operator_slug(reader["slug"])
    assert reader_principal is not None
    browser_principal = reader_principal.with_channel(CHANNEL_BROWSER)

    rows = list_browser_mailboxes(principal=browser_principal)
    assert agent["mailbox"]["address"] not in {row["address"] for row in rows}
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        read_mailbox_inbox(
            address=agent["mailbox"]["address"],
            principal=browser_principal,
            mark_read=False,
        )

    from brains.api.operator import _live_agents

    projected = next(
        row
        for row in _live_agents(browser_principal)
        if row["session_id"] == agent["session"]["session_id"]
    )
    assert "mailbox_address" not in projected
    assert "mailbox_deep_link" not in projected


def test_archived_workspace_agent_mailbox_is_not_openable_or_linked(tmp_path) -> None:
    agent = _agent(tmp_path / "browser-archived")
    browser_principal = principal_for_operator_slug("admin")
    assert browser_principal is not None
    browser_principal = browser_principal.with_channel(CHANNEL_BROWSER)

    with SessionLocal() as session:
        workspace = (
            session.query(Workspace)
            .filter(Workspace.slug == agent["session"]["workspace"])
            .one_or_none()
        )
        assert workspace is not None
        workspace.status = "archived"
        session.commit()

    assert agent["mailbox"]["address"] not in {
        row["address"] for row in list_browser_mailboxes(principal=browser_principal)
    }

    from brains.api.operator import _live_agents

    assert agent["session"]["session_id"] not in {
        row["session_id"] for row in _live_agents(browser_principal)
    }


def test_concurrent_operator_reads_attribute_a_delivery_once(tmp_path) -> None:
    sender = _agent(tmp_path / "operator-read-race")
    operator_address = "operator:admin@brains"
    sent = send_mailbox_message(
        sender["path"],
        [operator_address],
        "Read once",
        _operation(),
        sender_session_id=sender["session"]["session_id"],
        binding_secret=sender["binding"],
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: read_mailbox_inbox(address=operator_address),
                range(2),
            )
        )

    returned = [message["message_id"] for result in results for message in result["messages"]]
    assert returned == [sent["message_id"]]
    with SessionLocal() as session:
        delivery = (
            session.query(MailDelivery)
            .filter(MailDelivery.delivery_id == sent["deliveries"][0]["delivery_id"])
            .one()
        )
        assert delivery.read_by_operator_id is not None
        assert delivery.read_by_session_id is None


def test_http_agent_send_and_read_require_binding_proof(tmp_path, auth_headers) -> None:
    workspace = tmp_path / "http-agent"
    sender = _agent(workspace)
    recipient = _agent(workspace, tool="codex")
    client = TestClient(app)
    route = f"/v1/operator/workspaces/{sender['session']['workspace']}/mailboxes/messages"
    payload = {
        "recipients": [recipient["mailbox"]["address"]],
        "subject": "HTTP agent delivery",
        "operation_id": _operation("http-agent"),
        "sender_session_id": sender["session"]["session_id"],
    }

    missing = client.post(route, json=payload, headers=auth_headers)
    malformed = client.post(
        route,
        json=payload,
        headers={**auth_headers, "x-brains-mailbox-binding": "short"},
    )
    wrong = client.post(
        route,
        json=payload,
        headers={**auth_headers, "x-brains-mailbox-binding": _binding()},
    )
    sent = client.post(
        route,
        json=payload,
        headers={**auth_headers, "x-brains-mailbox-binding": sender["binding"]},
    )
    assert missing.status_code == malformed.status_code == wrong.status_code == 404
    assert sent.status_code == 200, sent.text
    assert sender["binding"] not in sent.text

    private_body = "private-mail-content" * 4000
    oversized = client.post(
        route,
        json={**payload, "operation_id": _operation("oversized"), "body": private_body},
        headers={**auth_headers, "x-brains-mailbox-binding": sender["binding"]},
    )
    assert oversized.status_code == 422
    assert "private-mail-content" not in oversized.text

    inbox_params = {"session_id": recipient["session"]["session_id"]}
    hidden = client.get(
        "/v1/operator/mailboxes/inbox",
        params=inbox_params,
        headers=auth_headers,
    )
    inbox = client.get(
        "/v1/operator/mailboxes/inbox",
        params=inbox_params,
        headers={**auth_headers, "x-brains-mailbox-binding": recipient["binding"]},
    )
    assert hidden.status_code == 404
    assert inbox.status_code == 200, inbox.text
    assert inbox.json()["messages"][0]["inbox_delivery"]["state"] == "accepted"
    read = client.post(
        "/v1/operator/mailboxes/inbox/read",
        json=inbox_params,
        headers={**auth_headers, "x-brains-mailbox-binding": recipient["binding"]},
    )
    assert read.status_code == 200, read.text
    assert read.json()["messages"][0]["inbox_delivery"]["state"] == "read"


def test_cli_and_mcp_agent_adapters_use_binding_files(tmp_path) -> None:
    from brains.mcp import server as mcp_server

    workspace = tmp_path / "adapters"
    sender = _agent(workspace)
    recipient = _agent(workspace, tool="codex")
    sender_file = tmp_path / "sender.binding"
    recipient_file = tmp_path / "recipient.binding"
    _write_binding(sender_file, sender["binding"])
    _write_binding(recipient_file, recipient["binding"])
    runner = CliRunner()

    sent = runner.invoke(
        cli_app,
        [
            "mailbox",
            "send",
            "--workspace",
            str(workspace),
            "--to",
            recipient["mailbox"]["address"],
            "--subject",
            "CLI delivery",
            "--operation-id",
            _operation("cli"),
            "--session",
            sender["session"]["session_id"],
            "--binding-file",
            str(sender_file),
        ],
    )
    assert sent.exit_code == 0, sent.output
    payload = json.loads(sent.output)
    assert sender["binding"] not in sent.output

    assert {
        "mailbox_send",
        "mailbox_broadcast",
        "mailbox_reply",
        "mailbox_forward",
        "mailbox_inbox",
        "mailbox_sent",
        "mailbox_thread",
        "mailbox_notification_take",
        "mailbox_notification_settle",
    } <= set(mcp_server.TOOL_REGISTRY)
    inbox = mcp_server.call_tool(
        "brains_mailbox_inbox",
        session_id=recipient["session"]["session_id"],
        binding_file=str(recipient_file),
    )
    assert inbox["messages"][0]["message_id"] == payload["message_id"]
    assert recipient["binding"] not in repr(inbox)


def test_cli_and_mcp_notification_adapters_expose_only_the_fixed_nudge(tmp_path) -> None:
    from brains.mcp import server as mcp_server

    workspace = tmp_path / "notification-adapters"
    sender = _agent(workspace, tool="codex")
    recipient = _notified_agent(workspace, tool="codex", mode="turn_boundary")
    sender_file = tmp_path / "notify-sender.binding"
    recipient_file = tmp_path / "notify-recipient.binding"
    _write_binding(sender_file, sender["binding"])
    _write_binding(recipient_file, recipient["binding"])
    runner = CliRunner()
    sent = runner.invoke(
        cli_app,
        [
            "mailbox",
            "send",
            "--workspace",
            str(workspace),
            "--to",
            recipient["mailbox"]["address"],
            "--subject",
            "Adapter notification",
            "--body",
            "adapter-private-body",
            "--operation-id",
            _operation("notify-adapter"),
            "--session",
            sender["session"]["session_id"],
            "--binding-file",
            str(sender_file),
        ],
    )
    assert sent.exit_code == 0, sent.output
    taken = mcp_server.call_tool(
        "brains_mailbox_notification_take",
        session_id=recipient["session"]["session_id"],
        binding_file=str(recipient_file),
    )
    assert taken["notification"]["nudge"] == (
        "Brains mailbox: new mail is waiting. Pull your durable inbox."
    )
    assert "adapter-private-body" not in repr(taken)
    settled = runner.invoke(
        cli_app,
        [
            "mailbox",
            "notification-settle",
            taken["notification"]["notification_id"],
            "--session",
            recipient["session"]["session_id"],
            "--status",
            "delivered",
            "--binding-file",
            str(recipient_file),
        ],
    )
    assert settled.exit_code == 0, settled.output
    assert json.loads(settled.output)["notification"]["status"] == "delivered"


def test_concurrent_retry_commits_one_message_and_sender_end_refuses(tmp_path) -> None:
    workspace = tmp_path / "race"
    sender = _agent(workspace)
    recipient = _agent(workspace, tool="codex")
    operation = _operation("race")

    def send() -> dict:
        return send_mailbox_message(
            str(workspace),
            [recipient["mailbox"]["address"]],
            "One concurrent message",
            operation,
            sender_session_id=sender["session"]["session_id"],
            binding_secret=sender["binding"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: send(), range(2)))

    assert len({result["message_id"] for result in results}) == 1
    assert sorted(result["created"] for result in results) == [False, True]
    with SessionLocal() as session:
        assert (
            session.query(MailMessage)
            .filter(MailMessage.operation_key.like(f"mail:%:send:{operation}"))
            .count()
            == 1
        )
        message_id = (
            session.query(MailMessage.id)
            .filter(MailMessage.operation_key.like(f"mail:%:send:{operation}"))
            .scalar()
        )
        assert (
            session.query(MailDelivery).filter(MailDelivery.message_id == message_id).count() == 1
        )

    end_session(sender["session"]["session_id"], "finished")
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        send_mailbox_message(
            str(workspace),
            [recipient["mailbox"]["address"]],
            "Too late",
            _operation("ended"),
            sender_session_id=sender["session"]["session_id"],
            binding_secret=sender["binding"],
        )

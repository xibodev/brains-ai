"""Interest-scoped topic delivery and cursor-based mailbox polling."""

from __future__ import annotations

import uuid

import pytest
from typer.testing import CliRunner

from brains.cli.app import app
from brains.control.events import append_event
from brains.control.mailbox import inbox_wait, read_messages, send_message
from brains.control.sessions import end_session, start_session
from brains.control.topics import (
    list_topic_subscriptions,
    pending_topic_updates,
    post_topic,
    read_topic,
    subscribe_topic,
    unsubscribe_topic,
)
from brains.storage.db import SessionLocal
from brains.storage.integrity import workspace_cascade_tables
from brains.storage.models import (
    Event,
    MailboxMessage,
    TopicAnnouncement,
    TopicSubscription,
)


def _topic(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def test_post_creates_one_announcement_and_no_mailbox_fanout(tmp_path) -> None:
    topic = _topic("fanout")
    poster = start_session(str(tmp_path / "poster"), tool="opencode")
    interested = start_session(str(tmp_path / "interested"), tool="claude")
    uninterested = start_session(str(tmp_path / "uninterested"), tool="codex")
    same_workspace = start_session(str(tmp_path / "poster"), tool="copilot")
    subscribe_topic(topic, interested["session_id"])
    subscribe_topic(topic, same_workspace["session_id"])

    with SessionLocal() as session:
        mailbox_before = session.query(MailboxMessage).count()
    posted = post_topic(
        topic,
        "one durable announcement",
        from_session_id=poster["session_id"],
        workspace_path=str(tmp_path / "poster"),
    )

    with SessionLocal() as session:
        assert session.query(TopicAnnouncement).filter_by(post_id=posted["id"]).count() == 1
        assert session.query(MailboxMessage).count() == mailbox_before
    assert posted["notified_sessions"] == [interested["session_id"]]
    assert pending_topic_updates(interested["session_id"])[0]["id"] == posted["id"]
    assert pending_topic_updates(uninterested["session_id"]) == []
    assert pending_topic_updates(same_workspace["session_id"]) == []


def test_subscription_defaults_to_future_posts_and_is_idempotent(tmp_path) -> None:
    topic = _topic("future")
    poster = start_session(str(tmp_path / "poster"), tool="opencode")
    reader = start_session(str(tmp_path / "reader"), tool="claude")
    historical = post_topic(topic, "before", from_session_id=poster["session_id"])

    first = subscribe_topic(topic, reader["session_id"])
    second = subscribe_topic(topic, reader["session_id"])

    assert first["last_seen_post_id"] == historical["id"]
    assert second["last_seen_post_id"] == historical["id"]
    assert pending_topic_updates(reader["session_id"]) == []


def test_include_existing_cursor_reads_oldest_first_without_skipping(tmp_path) -> None:
    topic = _topic("cursor")
    poster = start_session(str(tmp_path / "poster"), tool="opencode")
    reader = start_session(str(tmp_path / "reader"), tool="claude")
    posts = [
        post_topic(topic, f"post {index}", from_session_id=poster["session_id"])
        for index in range(3)
    ]
    subscribe_topic(topic, reader["session_id"], include_existing=True)

    first_page = read_topic(topic, session_id=reader["session_id"], limit=2)
    assert [row["id"] for row in first_page] == [posts[0]["id"], posts[1]["id"]]
    assert list_topic_subscriptions(reader["session_id"])[0]["pending"] == 1

    second_page = read_topic(topic, session_id=reader["session_id"], limit=2)
    assert [row["id"] for row in second_page] == [posts[2]["id"]]
    assert pending_topic_updates(reader["session_id"]) == []


def test_no_blast_post_stays_on_board_without_waking_subscriber(tmp_path) -> None:
    topic = _topic("quiet")
    poster = start_session(str(tmp_path / "poster"), tool="opencode")
    reader = start_session(str(tmp_path / "reader"), tool="claude")
    subscribe_topic(topic, reader["session_id"])

    posted = post_topic(topic, "board only", from_session_id=poster["session_id"], blast=False)

    with SessionLocal() as session:
        assert session.get(TopicAnnouncement, posted["id"]) is None
    assert pending_topic_updates(reader["session_id"]) == []
    assert read_topic(topic)[0]["id"] == posted["id"]


def test_unsubscribe_stops_future_topic_wakeups(tmp_path) -> None:
    topic = _topic("unsubscribe")
    poster = start_session(str(tmp_path / "poster"), tool="opencode")
    reader = start_session(str(tmp_path / "reader"), tool="claude")
    subscribe_topic(topic, reader["session_id"])
    assert unsubscribe_topic(topic, reader["session_id"])["unsubscribed"] is True

    post_topic(topic, "after unsubscribe", from_session_id=poster["session_id"])

    assert pending_topic_updates(reader["session_id"]) == []


def test_inbox_wait_ignores_persisted_topic_rows(tmp_path) -> None:
    topic = _topic("wake")
    poster = start_session(str(tmp_path / "poster"), tool="opencode")
    reader = start_session(str(tmp_path / "reader"), tool="claude")
    subscribe_topic(topic, reader["session_id"])
    post_topic(topic, "wake reader", from_session_id=poster["session_id"])

    first = inbox_wait(reader["session_id"], timeout_ms=100)
    second = inbox_wait(reader["session_id"], timeout_ms=100)
    assert first["wakeup"] is None
    assert second["wakeup"] is None

    read_topic(topic, session_id=reader["session_id"])
    assert inbox_wait(reader["session_id"], timeout_ms=100)["wakeup"] is None


def test_mailbox_after_id_skips_processed_prefix_and_empty_read_emits_no_event(tmp_path) -> None:
    reader = start_session(str(tmp_path / "reader"), tool="claude")
    session_id = reader["session_id"]
    with SessionLocal() as session:
        before = session.query(Event).filter_by(session_id=session_id, kind="message_read").count()
    assert read_messages(session_id) == []
    with SessionLocal() as session:
        assert (
            session.query(Event).filter_by(session_id=session_id, kind="message_read").count()
            == before
        )

    first = send_message("first", to_session_id=session_id)
    second = send_message("second", to_session_id=session_id)
    rows = read_messages(session_id, after_id=first["id"])
    assert [row["id"] for row in rows] == [second["id"]]


def test_inbox_wait_ignores_persisted_running_agent_mail(tmp_path) -> None:
    reader = start_session(str(tmp_path / "reader"), tool="claude")
    session_id = reader["session_id"]
    old = send_message("old", to_session_id=session_id)

    assert inbox_wait(session_id, timeout_ms=100, after_message_id=old["id"])["wakeup"] is None
    new = send_message("new", to_session_id=session_id)
    assert new["id"] > old["id"]
    assert inbox_wait(session_id, timeout_ms=100, after_message_id=old["id"])["wakeup"] is None


def test_cursor_read_requires_subscription_and_unfiltered_topic(tmp_path) -> None:
    topic = _topic("validation")
    reader = start_session(str(tmp_path / "reader"), tool="claude")
    with pytest.raises(ValueError, match="requires one topic"):
        read_topic(session_id=reader["session_id"])
    with pytest.raises(ValueError, match="not subscribed"):
        read_topic(topic, session_id=reader["session_id"])
    subscribe_topic(topic, reader["session_id"])
    with pytest.raises(ValueError, match="cannot filter a thread"):
        read_topic(topic, session_id=reader["session_id"], reply_to=1)
    with pytest.raises(ValueError, match="stored cursor"):
        read_topic(topic, session_id=reader["session_id"], after_post_id=1)


def test_board_after_post_id_pages_oldest_first(tmp_path) -> None:
    topic = _topic("board-cursor")
    posts = [post_topic(topic, f"post {index}") for index in range(3)]

    first_page = read_topic(topic, after_post_id=0, limit=2)
    second_page = read_topic(topic, after_post_id=first_page[-1]["id"], limit=2)

    assert [row["id"] for row in first_page] == [posts[0]["id"], posts[1]["id"]]
    assert [row["id"] for row in second_page] == [posts[2]["id"]]


def test_successor_inherits_topic_cursor(tmp_path) -> None:
    topic = _topic("successor")
    old = start_session(str(tmp_path / "reader"), tool="opencode")
    subscribe_topic(topic, old["session_id"])
    append_event("cursor_probe", "renew", session_id=old["session_id"])
    end_session(old["session_id"], summary="tool restarted")

    new = start_session(
        str(tmp_path / "reader"),
        tool="opencode",
        pid=999_999,
        predecessor_session_id=old["session_id"],
    )

    with SessionLocal() as session:
        assert session.get(TopicSubscription, (old["session_id"], topic)) is None
        assert session.get(TopicSubscription, (new["session_id"], topic)) is not None


def test_topic_post_refuses_dead_or_mismatched_origin(tmp_path) -> None:
    origin = start_session(str(tmp_path / "origin"), tool="opencode")
    with pytest.raises(ValueError, match="must match"):
        post_topic(
            _topic("mismatch"),
            "wrong Workspace",
            from_session_id=origin["session_id"],
            workspace_path=str(tmp_path / "other"),
        )

    end_session(origin["session_id"])
    with pytest.raises(ValueError, match="ended"):
        post_topic(
            _topic("dead"),
            "dead origin",
            from_session_id=origin["session_id"],
        )


def test_topic_subscription_cli_commands_are_withdrawn(tmp_path) -> None:
    topic = _topic("cli")
    reader = start_session(str(tmp_path / "reader"), tool="claude")
    session_id = reader["session_id"]
    runner = CliRunner()

    for command in ("topic-subscribe", "topic-subscriptions", "topic-unsubscribe"):
        result = runner.invoke(app, [command, topic, "--session", session_id])
        assert result.exit_code != 0
        assert "No such command" in result.output


def test_workspace_cascade_includes_topic_delivery_state() -> None:
    with SessionLocal() as session:
        raw = session.connection().connection
        connection = getattr(raw, "driver_connection", None) or getattr(raw, "connection", raw)
        tables = {step.table for step in workspace_cascade_tables(connection)}

    assert {"topic_announcements", "topic_subscriptions"} <= tables

"""Durable mail waits on disposable state, without a harness or network."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from test_coordination_transport import FakeMCPClient
from typer.testing import CliRunner

from brains.api.auth import reset_rate_limit_state
from brains.capabilities import CORE_MCP_TOOLS
from brains.cli.app import app
from brains.config import settings
from brains.control import durable_mail
from brains.control.durable_mailbox import (
    MailboxUnavailableError,
    MailboxValidationError,
    _binding_hash,
    create_managed_agent_mailbox,
)
from brains.control.help import file_help_request
from brains.control.operators import add_operator, ensure_admin_operator
from brains.control.sessions import end_session, start_session
from brains.mcp import server as mcp_server
from brains.mcp import tools
from brains.storage import db, migrations
from brains.storage.db import SessionLocal
from brains.storage.models import (
    AgentSession,
    Event,
    HelpRequest,
    Mailbox,
    MailboxAttachment,
    MailDelivery,
    MailMessage,
    MailNotificationAttempt,
    SessionLease,
    WorkAssignment,
    WorkAssignmentAttempt,
)


@pytest.fixture(autouse=True)
def isolated_mailbox_store(tmp_path, monkeypatch):
    """Rebind the shared factory, including aliases captured in tools/closures.

    Like the durable mailbox observability fixture, restore the factory rather
    than deleting rows from the process-wide (conftest-owned) synthetic store.
    """
    previous_bind = SessionLocal.kw["bind"]
    url = f"sqlite:///{(tmp_path / 'wait.sqlite').as_posix()}"
    engine = create_engine(url)
    event.listen(engine, "connect", db._set_sqlite_pragmas)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(migrations, "engine", engine)
    monkeypatch.setattr(settings, "db_url", url)
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path / "state"))
    SessionLocal.configure(bind=engine)
    try:
        migrations.init_db()
        ensure_admin_operator()
        yield
    finally:
        SessionLocal.configure(bind=previous_bind)
        engine.dispose()


@pytest.fixture
def peers(tmp_path):
    result = []
    for name in ("sender", "recipient", "other"):
        secret = f"synthetic-wait-{uuid.uuid4().hex}"
        started = start_session(
            str(tmp_path),
            tool="opencode",
            native_tool_session_id=f"wait-{name}-{tmp_path.name}",
            mailbox_binding_secret=secret,
            mailbox_notification_mode="immediate",
        )
        binding_file = tmp_path / f"{name}.binding"
        binding_file.write_text(secret + "\n", encoding="utf-8")
        binding_file.chmod(0o600)
        result.append(
            SimpleNamespace(
                session_id=started["session_id"],
                address=started["mailbox"]["address"],
                secret=secret,
                binding_file=str(binding_file),
                workspace=str(tmp_path),
            )
        )
    return result


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0
        sleeps = None
        on_sleep = None

        def __init__(self):
            self.sleeps = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            assert 0 < seconds <= 0.2
            self.sleeps.append(seconds)
            self.now += seconds
            if self.on_sleep:
                self.on_sleep()

    fake = Clock()
    monkeypatch.setattr(durable_mail, "time", fake)
    return fake


def _send(sender, *recipients, operation="synthetic-wait"):
    return durable_mail.send_mailbox_message(
        sender.workspace,
        [recipient.address for recipient in recipients],
        "Review this synthetic work",
        operation,
        body="Mail alone does not accept work.",
        sender_session_id=sender.session_id,
        binding_secret=sender.secret,
    )


def _wait(recipient, **kwargs):
    return durable_mail.wait_mailbox(recipient.session_id, recipient.secret, **kwargs)


def _state():
    models = (
        AgentSession,
        SessionLease,
        Mailbox,
        MailboxAttachment,
        MailDelivery,
        MailMessage,
        MailNotificationAttempt,
        HelpRequest,
        WorkAssignment,
        WorkAssignmentAttempt,
        Event,
    )
    with SessionLocal() as session:
        return {
            model.__tablename__: session.execute(
                select(model.__table__).order_by(*model.__table__.primary_key.columns)
            ).all()
            for model in models
        }


def test_unread_immediate_is_repeatable_and_changes_no_state(peers, clock):
    sender, recipient, _ = peers
    sent = _send(sender, recipient)
    before = _state()
    result = _wait(recipient)
    assert result == _wait(recipient, timeout_ms=0)
    assert result["mail_available"] is True
    assert result["wait_timed_out"] is False
    assert result["messages"][0]["message_id"] == sent["message_id"]
    assert result["messages"][0]["inbox_delivery"]["read_at"] is None
    assert result["next_after_delivery_id"] == sent["deliveries"][0]["cursor"]
    assert result["cursor"] == 0
    assert result["unread_count"] == 1
    assert not clock.sleeps
    assert _state() == before


def test_committed_delivery_between_polls_is_visible(peers, clock):
    sender, recipient, _ = peers
    sent = []
    after_send = []

    def deliver():
        # A separate connection commits while the waiter holds no transaction.
        sent.append(_send(sender, recipient))
        after_send.append(_state())

    clock.on_sleep = deliver
    result = _wait(recipient, timeout_ms=1000)
    assert result["mail_available"] is True
    assert result["wait_timed_out"] is False
    assert result["messages"][0]["message_id"] == sent[0]["message_id"]
    assert clock.sleeps == [0.2]
    assert _state() == after_send[0]


@pytest.mark.parametrize("timeout_ms", [0, 450, 25_000])
def test_timeout_keeps_help_request_and_all_state(peers, clock, timeout_ms):
    sender, recipient, _ = peers
    request = file_help_request(
        "Synthetic help remains open",
        "Review the fixture?",
        from_session_id=sender.session_id,
        to_session_id=recipient.session_id,
        timeout_ms=60_000,
    )
    before = _state()
    result = _wait(recipient, timeout_ms=timeout_ms, after_delivery_id=123)
    assert result["messages"] == []
    assert result["mail_available"] is False
    assert result["wait_timed_out"] is True
    assert result["next_after_delivery_id"] == 123
    assert result["cursor"] == 0
    assert clock.now == pytest.approx(timeout_ms / 1000)
    assert _state() == before
    with SessionLocal() as session:
        row = session.query(HelpRequest).filter_by(code=request["code"]).one()
        assert row.status == "open"
        assert row.claimed_by_session_id is None


def test_delivery_cursor_pages_without_consuming_or_using_message_ids(peers, clock):
    sender, recipient, other = peers
    _send(sender, other, recipient, operation="first")
    _send(sender, other, recipient, operation="second")
    before = _state()
    first = _wait(recipient, timeout_ms=0, limit=1)
    assert len(first["messages"]) == 1
    cursor = first["next_after_delivery_id"]
    assert cursor == first["messages"][0]["inbox_delivery"]["cursor"]
    assert cursor != first["messages"][0]["cursor"]
    second = _wait(recipient, timeout_ms=0, after_delivery_id=cursor, limit=1)
    assert len(second["messages"]) == 1
    assert second["messages"][0]["message_id"] != first["messages"][0]["message_id"]
    assert second["next_after_delivery_id"] > cursor
    empty = _wait(recipient, timeout_ms=0, after_delivery_id=second["next_after_delivery_id"])
    assert empty["messages"] == []
    assert empty["next_after_delivery_id"] == second["next_after_delivery_id"]
    assert _wait(recipient, timeout_ms=0, limit=1) == first
    assert _state() == before


def test_wait_excludes_read_mail_even_with_explicit_cursor(peers, clock):
    sender, recipient, _ = peers
    _send(sender, recipient)
    read = durable_mail.read_mailbox_inbox(
        session_id=recipient.session_id, binding_secret=recipient.secret, mark_read=True
    )
    before = _state()
    result = _wait(recipient, timeout_ms=0, after_delivery_id=0)
    assert result["messages"] == []
    assert result["cursor"] == read["cursor"] > 0
    assert result["next_after_delivery_id"] == 0
    assert _state() == before


@pytest.mark.parametrize("proof", ["wrong", "missing", "address", "ended"])
def test_invalid_current_proof_fails_before_sleep(peers, clock, proof):
    _, recipient, other = peers
    secret = recipient.secret
    address = None
    if proof == "wrong":
        secret = other.secret
    elif proof == "missing":
        secret = ""
    elif proof == "address":
        address = other.address
    else:
        end_session(recipient.session_id)
    before = _state()
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        durable_mail.wait_mailbox(recipient.session_id, secret, address=address)
    assert not clock.sleeps
    assert _state() == before


@pytest.mark.parametrize("change", ["rotate", "end", "expire"])
def test_auth_loss_between_polls_raises_instead_of_timeout(peers, clock, change):
    _, recipient, _ = peers
    after_change = []

    def invalidate():
        if change == "end":
            end_session(recipient.session_id)
        else:
            with SessionLocal() as session:
                if change == "rotate":
                    mailbox = session.query(Mailbox).filter_by(address=recipient.address).one()
                    mailbox.binding_key_hash = _binding_hash(
                        "synthetic-rotated-mailbox-wait-binding"
                    )
                else:
                    lease = session.get(SessionLease, recipient.session_id)
                    lease.lease_expires_at = lease.lease_expires_at.replace(year=2000)
                session.commit()
        after_change.append(_state())

    clock.on_sleep = invalidate
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        _wait(recipient, timeout_ms=200)
    assert clock.sleeps == [0.2]
    assert _state() == after_change[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [("timeout_ms", v) for v in (-1, 25_001, True, False, 1.5, "10", None)]
    + [("limit", v) for v in (0, 201, True, False, 1.5, "10", None)],
)
def test_strict_core_integer_bounds_before_poll(monkeypatch, field, value):
    def unexpected(**kwargs):
        pytest.fail("invalid input must not poll")

    monkeypatch.setattr(durable_mail, "read_mailbox_inbox", unexpected)
    with pytest.raises(MailboxValidationError, match=field):
        durable_mail.wait_mailbox("synthetic-session", "synthetic-binding", **{field: value})


def test_mcp_and_cli_wait_use_binding_file_and_same_envelope(peers, clock):
    sender, recipient, _ = peers
    _send(sender, recipient)
    before = _state()
    expected = _wait(
        recipient, timeout_ms=0, address=recipient.address, after_delivery_id=0, limit=1
    )
    actual = mcp_server.call_tool(
        "brains_mailbox_wait",
        session_id=recipient.session_id,
        binding_file=recipient.binding_file,
        address=recipient.address,
        timeout_ms=0,
        after_delivery_id=0,
        limit=1,
    )
    cli = CliRunner().invoke(
        app,
        [
            "mailbox",
            "wait",
            "--session",
            recipient.session_id,
            "--binding-file",
            recipient.binding_file,
            "--address",
            recipient.address,
            "--timeout-ms",
            "0",
            "--after-delivery-id",
            "0",
            "--limit",
            "1",
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.stdout) == actual == expected
    assert _state() == before


@pytest.mark.parametrize("args", [[], ["--session", "synthetic"]])
def test_cli_requires_agent_options(args):
    result = CliRunner().invoke(app, ["mailbox", "wait", *args])
    assert result.exit_code == 2


@pytest.mark.parametrize(
    "args",
    [["--timeout-ms", "-1"], ["--timeout-ms", "25001"], ["--limit", "0"], ["--limit", "201"]],
)
def test_cli_rejects_invalid_bounds_with_valid_agent_options(peers, args):
    _, recipient, _ = peers
    result = CliRunner().invoke(
        app,
        [
            "mailbox",
            "wait",
            "--session",
            recipient.session_id,
            "--binding-file",
            recipient.binding_file,
            *args,
        ],
    )
    assert result.exit_code == 2


def test_mcp_and_cli_reject_another_mailbox_binding(peers, clock):
    _, recipient, other = peers
    before = _state()
    with pytest.raises(MailboxUnavailableError, match="mailbox unavailable"):
        mcp_server.call_tool(
            "brains_mailbox_wait",
            session_id=recipient.session_id,
            binding_file=other.binding_file,
            timeout_ms=0,
        )
    cli = CliRunner().invoke(
        app,
        [
            "mailbox",
            "wait",
            "--session",
            recipient.session_id,
            "--binding-file",
            other.binding_file,
            "--timeout-ms",
            "0",
        ],
    )
    assert cli.exit_code != 0
    assert _state() == before
    assert not clock.sleeps


def test_native_mcp_signature_and_advertisement(monkeypatch):
    name = "mailbox_wait"
    assert len(CORE_MCP_TOOLS) == 89
    assert name in CORE_MCP_TOOLS and name in mcp_server.LEAN_TOOLS
    assert mcp_server.TOOL_REGISTRY[name] is tools.mailbox_wait_tool
    for selection in ("full", "lean", name):
        monkeypatch.setenv("BRAINS_MCP_TOOLS", selection)
        assert name in mcp_server._resolve_active_tools()
    signature = inspect.signature(tools.mailbox_wait_tool)
    assert list(signature.parameters) == [
        "session_id",
        "binding_file",
        "address",
        "timeout_ms",
        "after_delivery_id",
        "limit",
    ]
    assert signature.parameters["timeout_ms"].default == 25_000
    registered = {tool.name: tool for tool in mcp_server.mcp._tool_manager.list_tools()}
    schema = registered["brains_mailbox_wait"].parameters
    assert set(schema["required"]) == {"session_id", "binding_file"}
    assert set(schema["properties"]) == set(signature.parameters)
    assert schema["properties"]["timeout_ms"]["type"] == "integer"
    assert schema["properties"]["limit"]["default"] == 50


@pytest.fixture
def protocol_peers(tmp_path, monkeypatch):
    """Managed proof files are required by authenticated MCP's real reader."""
    actors = []
    for name in ("sender", "recipient"):
        actor = start_session(str(tmp_path), tool="opencode")["session_id"]
        mailbox = create_managed_agent_mailbox(
            str(tmp_path), "opencode", f"wait-{name}-{uuid.uuid4().hex}", actor
        )
        actors.append(
            {
                "session_id": actor,
                "binding_file": mailbox["binding_file"],
                "address": mailbox["address"],
                "workspace_path": str(tmp_path),
            }
        )
    monkeypatch.setattr(settings, "allow_unauthenticated_api", False)
    monkeypatch.setattr(mcp_server.mcp, "_session_manager", None)
    reset_rate_limit_state()
    return actors


@asynccontextmanager
async def mailbox_clients():
    application = mcp_server._build_http_app("streamable-http", "127.0.0.1")
    inner = application.app
    clients = [FakeMCPClient(application, settings.api_key) for _ in range(2)]
    async with inner.router.lifespan_context(inner), clients[0].http, clients[1].http:
        async with asyncio.timeout(10):
            for client in clients:
                await client.initialize()
            yield clients


async def _protocol_call(client, name, **arguments):
    return await client.request("tools/call", {"name": f"brains_{name}", "arguments": arguments})


def _protocol_result(response):
    assert response.status_code == 200, response.text
    result = FakeMCPClient.message(response)["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def _wait_arguments(actor, **kwargs):
    return {key: actor[key] for key in ("session_id", "binding_file")} | kwargs


@pytest.mark.parametrize(
    ("field", "value"),
    [("timeout_ms", False), ("limit", True), ("timeout_ms", 25_001), ("limit", 201)],
)
def test_protocol_wait_rejects_invalid_integers(protocol_peers, field, value):
    async def scenario():
        async with mailbox_clients() as (client, _):
            before = _state()
            response = await _protocol_call(
                client,
                "mailbox_wait",
                **_wait_arguments(protocol_peers[1], timeout_ms=0) | {field: value},
            )
            result = client.message(response)
            assert result.get("error") or result.get("result", {}).get("isError"), result
            assert _state() == before
            listing = client.message(await client.request("tools/list"))["result"]["tools"]
            schema = next(tool for tool in listing if tool["name"] == "brains_mailbox_wait")
            for name in ("timeout_ms", "limit"):
                assert schema["inputSchema"]["properties"][name]["type"] == "integer"

    asyncio.run(scenario())


def test_protocol_wait_allows_concurrent_send(protocol_peers, monkeypatch):
    started = threading.Event()
    original = durable_mail.read_mailbox_inbox

    def observe(**kwargs):
        result = original(**kwargs)
        started.set()
        return result

    monkeypatch.setattr(durable_mail, "read_mailbox_inbox", observe)

    async def scenario():
        sender, recipient = protocol_peers
        async with mailbox_clients() as (waiting, sending):
            beginning = time.monotonic()
            pending = asyncio.create_task(
                _protocol_call(
                    waiting, "mailbox_wait", **_wait_arguments(recipient, timeout_ms=2000)
                )
            )
            try:
                while not started.is_set():
                    await asyncio.sleep(0.01)
                # With the locked SDK's synchronous dispatch this resumes only
                # after the wait times out, proving event-loop starvation.
                assert time.monotonic() - beginning < 1.5
                sent = _protocol_result(
                    await _protocol_call(
                        sending,
                        "mailbox_send",
                        workspace_path=sender["workspace_path"],
                        recipients=[recipient["address"]],
                        subject="Synthetic concurrent delivery",
                        operation_id="concurrent-wait",
                        sender_session_id=sender["session_id"],
                        binding_file=sender["binding_file"],
                    )
                )
                result = _protocol_result(await pending)
                assert result["mail_available"] is True
                assert result["wait_timed_out"] is False
                assert result["messages"][0]["message_id"] == sent["message_id"]
                assert result["cursor"] == 0
            finally:
                await pending

    asyncio.run(scenario())


def test_protocol_wait_worker_preserves_request_authority(protocol_peers):
    _, foreign_key = add_operator(f"wait-foreign-{uuid.uuid4().hex[:10]}")

    async def scenario():
        async with mailbox_clients() as (client, _):
            before = _state()
            response = await client.request(
                "tools/call",
                {
                    "name": "brains_mailbox_wait",
                    "arguments": _wait_arguments(protocol_peers[1], timeout_ms=0),
                },
                key=foreign_key,
            )
            result = client.message(response)
            assert result.get("error") or result.get("result", {}).get("isError"), result
            assert _state() == before
            result = _protocol_result(
                await _protocol_call(
                    client, "mailbox_wait", **_wait_arguments(protocol_peers[1], timeout_ms=0)
                )
            )
            assert result["wait_timed_out"] is True

    asyncio.run(scenario())


def test_protocol_cancelled_wait_worker_finishes_within_budget(protocol_peers, monkeypatch):
    started = threading.Event()
    finished = threading.Event()
    original = tools.wait_mailbox

    def observe(*args, **kwargs):
        started.set()
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(tools, "wait_mailbox", observe)

    async def scenario():
        async with mailbox_clients() as (client, _):
            before = _state()
            pending = asyncio.create_task(
                _protocol_call(
                    client, "mailbox_wait", **_wait_arguments(protocol_peers[1], timeout_ms=400)
                )
            )
            try:
                while not started.is_set():
                    await asyncio.sleep(0.01)
                assert not finished.is_set(), "wait blocked the protocol event loop"
                response = await client.request(
                    "notifications/cancelled",
                    {"requestId": client.next_id, "reason": "test"},
                    notification=True,
                )
                assert response.status_code == 202
                async with asyncio.timeout(2):
                    while not finished.is_set():
                        await asyncio.sleep(0.01)
                assert _state() == before
            finally:
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending

    asyncio.run(scenario())

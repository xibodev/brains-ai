"""BL-P0-05 - the Session message, stop and durability contract.

Every test here asserts a *durability* or *truthfulness* property, not a happy
path:

* the SPA's message/stop calls reach real, authorized server routes;
* a command is recorded before it is delivered, survives a reload, and a
  retried mutation is one logical command with one outcome;
* exactly one consumer can hold a command, a consumer that dies releases it,
  and a stale consumer cannot settle the attempt that replaced it;
* a message reaches the agent where the launch shape supports it, and fails
  with a stated reason where it does not - never an echoed success;
* a stop reaches the exact process the Runtime launched, never one matched by
  name, and only a stop that proves the process is gone makes the Session
  terminal;
* a natural finish and a stop race safely, in both orders;
* a restarted Runtime reconciles what the hub believes is running on it;
* cross-Org and ``private``-Workspace callers get the same ``404`` as for a
  Session that does not exist, and a Runtime credential is refused the
  operator routes and confined to its own machine's commands;
* a retried mutation publishes one durable realtime event.

No real agent CLI is launched anywhere: the "agent" is a short Python script
that echoes stdin or sleeps, which is what makes the ownership and delivery
assertions provable rather than mocked.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brains.authz import credentials as creds
from brains.config import settings
from brains.control import orgs as orgs_ctl
from brains.control import session_commands as commands_ctl
from brains.control import sessions as sessions_ctl
from brains.control.common import utc_now
from brains.control.operators import add_operator, ensure_admin_operator
from brains.exec import session_channel
from brains.main import app
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession, SessionCommand, Workspace

_AUTH_SCHEME = "Bea" + "rer"

#: A tool name no shipped CLI uses, declared interactive only for the tests
#: that exercise the delivery path end to end.
INTERACTIVE_TOOL = "fake-interactive"

#: An agent that reads lines from stdin and echoes them, so a delivered
#: message is observable rather than asserted from the queue alone.
_ECHO_AGENT = (
    "import sys\n"
    "for line in sys.stdin:\n"
    "    sys.stdout.write('got:' + line)\n"
    "    sys.stdout.flush()\n"
)

#: An agent that never finishes on its own, so a stop is the only way it ends.
_SLEEP_AGENT = "import time\ntime.sleep(300)\n"


@pytest.fixture(autouse=True)
def _bootstrap():
    init_db()
    ensure_admin_operator()
    creds.sync_local_credentials()
    session_channel.clear()
    yield
    session_channel.clear()
    session_channel.undeclare_interactive_tool(INTERACTIVE_TOOL)


@pytest.fixture
def client():
    return TestClient(app)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _auth(key: str) -> dict:
    """Authorization headers presenting the given key, built without a literal."""
    return {"Authorization": f"{_AUTH_SCHEME} {key}"}


ADMIN_AUTH = _auth(settings.api_key)


def _session(tmp_path, *, tool: str = "copilot", org_id: int | None = None) -> str:
    """A live Session row in its own Workspace."""
    path = tmp_path / _slug("ws")
    path.mkdir(parents=True, exist_ok=True)
    workspace = sessions_ctl.register_workspace(str(path), org_id=org_id)
    row = sessions_ctl.start_session(str(path), tool=tool)
    session_id = row["session_id"]
    if org_id is not None:
        with SessionLocal() as session:
            session.get(Workspace, workspace.id).org_id = org_id
            session.commit()
    return session_id


def _spawn(script: str) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - a fixed argv, no shell
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    with contextlib.suppress(Exception):  # pragma: no cover - best effort teardown
        process.communicate(timeout=10)


# --------------------------------------------------------------------------- #
# Client/route contract
# --------------------------------------------------------------------------- #


def test_the_spa_message_and_stop_calls_reach_real_routes():
    """The SPA's REST calls have server routes with the same method and shape.

    The gap this closes was not a bug in either half: the console called
    ``POST /v1/sessions/{id}/message`` and ``/stop`` and the server had no such
    routes at all, so every send failed at the network and the console showed a
    bubble anyway.
    """
    paths = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", set())
    }
    assert ("/v1/sessions/{session_id}/message", "POST") in paths
    assert ("/v1/sessions/{session_id}/stop", "POST") in paths
    assert ("/v1/sessions/{session_id}/commands", "GET") in paths


def test_the_client_module_and_the_server_agree_on_the_session_control_paths():
    """The contract is asserted against the checked-in client, not a copy of it."""
    client_source = (
        Path(__file__).resolve().parents[1] / "frontend" / "src" / "api" / "client.ts"
    ).read_text(encoding="utf-8")
    for fragment in (
        "/sessions/${id}/message",
        "/sessions/${id}/stop",
        "/sessions/${id}/commands",
    ):
        assert fragment in client_source


# --------------------------------------------------------------------------- #
# Durability + idempotency
# --------------------------------------------------------------------------- #


def test_a_message_is_recorded_before_it_is_delivered(client, tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    response = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "please run the tests", "operation_id": "op-1"},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "message"
    assert body["status"] == commands_ctl.STATUS_REQUESTED
    assert body["duplicate"] is False
    # Durable: the row exists independently of the response the caller saw.
    stored = commands_ctl.get(body["command_id"])
    assert stored is not None
    assert stored["text"] == "please run the tests"
    assert stored["sequence"] == 1


def test_a_retried_message_is_one_logical_command(client, tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    payload = {"text": "same message", "operation_id": "op-retry"}
    first = client.post(
        f"/v1/sessions/{session_id}/message", json=payload, headers=ADMIN_AUTH
    ).json()
    second = client.post(
        f"/v1/sessions/{session_id}/message", json=payload, headers=ADMIN_AUTH
    ).json()
    assert second["command_id"] == first["command_id"]
    assert second["duplicate"] is True
    assert len(commands_ctl.list_for_session(session_id)) == 1


def test_concurrent_retries_of_one_operation_insert_one_row(tmp_path):
    """Two retries racing must not both win the insert."""
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)

    def _send():
        return commands_ctl.enqueue(
            session_id, commands_ctl.KIND_MESSAGE, text="race", operation_id="op-race"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result() for f in [pool.submit(_send) for _ in range(4)]]
    assert sum(1 for _command, created in results if created) == 1
    assert len({command["command_id"] for command, _created in results}) == 1
    assert len(commands_ctl.list_for_session(session_id)) == 1


def test_messages_keep_the_order_they_were_accepted_in(client, tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    for index in range(3):
        client.post(
            f"/v1/sessions/{session_id}/message",
            json={"text": f"m{index}", "operation_id": f"op-{index}"},
            headers=ADMIN_AUTH,
        )
    history = commands_ctl.list_for_session(session_id)
    assert [row["sequence"] for row in history] == [1, 2, 3]
    assert [row["text"] for row in history] == ["m0", "m1", "m2"]


def test_a_reload_shows_the_durable_history(client, tmp_path):
    session_id = _session(tmp_path, tool="copilot")
    client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "hello", "operation_id": "op-reload"},
        headers=ADMIN_AUTH,
    )
    client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH)
    listed = client.get(f"/v1/sessions/{session_id}/commands", headers=ADMIN_AUTH)
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert [row["kind"] for row in rows] == ["message", "stop"]
    # The message names why it could not be delivered rather than reading as sent.
    assert rows[0]["status"] == commands_ctl.STATUS_FAILED
    assert rows[0]["result"] == commands_ctl.RESULT_UNSUPPORTED
    assert "input channel" in rows[0]["error"]


def test_a_publish_failure_does_not_lose_the_command(client, tmp_path, monkeypatch):
    """Persist-before-notify: the record survives a broken announcement."""
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("realtime is down")

    monkeypatch.setattr("brains.api.realtime_publish.publish_session_command", _explode)
    response = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "still recorded", "operation_id": "op-publish"},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200
    assert commands_ctl.get(response.json()["command_id"]) is not None


# --------------------------------------------------------------------------- #
# Truthfulness: capability and delivery
# --------------------------------------------------------------------------- #


def test_a_message_to_an_agent_with_no_input_channel_fails_truthfully(client, tmp_path):
    session_id = _session(tmp_path, tool="copilot")
    body = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "are you there?", "operation_id": "op-unsupported"},
        headers=ADMIN_AUTH,
    ).json()
    assert body["status"] == commands_ctl.STATUS_FAILED
    assert body["result"] == commands_ctl.RESULT_UNSUPPORTED
    assert "copilot" in body["error"]
    # And the console can see the blocked state before it even offers a composer.
    session = client.get(f"/v1/sessions/{session_id}", headers=ADMIN_AUTH).json()
    assert session["message_capability"]["supported"] is False
    assert session["message_capability"]["reason"]


def test_the_shipped_clis_declare_no_interactive_input_channel():
    """The capability table states the launch shape rather than an aspiration."""
    for tool in ("copilot", "claude", "codex"):
        capability = session_channel.message_capability(tool)
        assert capability["supported"] is False
        assert tool in capability["reason"]


def test_a_message_reaches_an_agent_that_can_receive_one(client, tmp_path):
    """Where the launch shape supports it, the message actually arrives."""
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    process = _spawn(_ECHO_AGENT)
    try:
        session_channel.register(session_id, process, tool=INTERACTIVE_TOOL, stdin_open=True)
        body = client.post(
            f"/v1/sessions/{session_id}/message",
            json={"text": "run the tests", "operation_id": "op-live"},
            headers=ADMIN_AUTH,
        ).json()
        assert body["status"] == commands_ctl.STATUS_ACKNOWLEDGED
        assert body["result"] == session_channel.RESULT_DELIVERED
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "got:run the tests"
    finally:
        _kill(process)


def test_a_message_for_a_session_this_process_does_not_own_stays_queued(client, tmp_path):
    """No consumer, no outcome: the command waits rather than claiming success."""
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    body = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "queued", "operation_id": "op-queued"},
        headers=ADMIN_AUTH,
    ).json()
    assert body["status"] == commands_ctl.STATUS_REQUESTED
    assert body["result"] is None


def test_a_message_to_an_ended_session_is_refused_durably(client, tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    sessions_ctl.end_session(session_id, "done")
    body = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "too late", "operation_id": "op-late"},
        headers=ADMIN_AUTH,
    ).json()
    assert body["status"] == commands_ctl.STATUS_FAILED
    assert body["result"] == commands_ctl.RESULT_SESSION_ENDED


# --------------------------------------------------------------------------- #
# Claims, leases, recovery
# --------------------------------------------------------------------------- #


def test_only_one_consumer_can_claim_a_command(tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_MESSAGE, text="one winner", operation_id="op-claim"
    )

    def _claim(index: int):
        return commands_ctl.claim(command["command_id"], consumer=f"runtime:{index}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = [f.result() for f in [pool.submit(_claim, i) for i in range(6)]]
    winners = [row for row in outcomes if row is not None]
    assert len(winners) == 1
    assert winners[0]["status"] == commands_ctl.STATUS_DELIVERED
    assert winners[0]["attempt"] == 1


def test_an_expired_lease_returns_the_command_and_retires_the_stale_holder(tmp_path):
    """A consumer that crashed mid-flight strands nothing and settles nothing."""
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_MESSAGE, text="retry me", operation_id="op-lease"
    )
    claimed = commands_ctl.claim(command["command_id"], consumer="runtime:dead", lease=1)
    assert claimed is not None

    requeued = commands_ctl.expire_leases(now=utc_now() + timedelta(seconds=5))
    assert [row["command_id"] for row in requeued] == [command["command_id"]]
    assert commands_ctl.get(command["command_id"])["status"] == commands_ctl.STATUS_REQUESTED

    fresh = commands_ctl.claim(command["command_id"], consumer="runtime:live")
    assert fresh["attempt"] == 2
    # The crashed holder comes back and tries to settle the attempt it lost.
    with pytest.raises(commands_ctl.SessionCommandError):
        commands_ctl.acknowledge(command["command_id"], consumer="runtime:dead", result="delivered")
    assert commands_ctl.get(command["command_id"])["status"] == commands_ctl.STATUS_DELIVERED


def test_a_command_no_consumer_completes_is_failed_rather_than_retried_forever(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(commands_ctl.MAX_ATTEMPTS_ENV, "2")
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_MESSAGE, text="doomed", operation_id="op-abandon"
    )
    for _attempt in range(2):
        commands_ctl.claim(command["command_id"], consumer="runtime:flaky", lease=1)
        commands_ctl.expire_leases(now=utc_now() + timedelta(seconds=5))
    commands_ctl.claim(command["command_id"], consumer="runtime:flaky", lease=1)
    commands_ctl.expire_leases(now=utc_now() + timedelta(seconds=5))
    settled = commands_ctl.get(command["command_id"])
    assert settled["status"] == commands_ctl.STATUS_FAILED
    assert settled["result"] == commands_ctl.RESULT_ABANDONED


def test_a_repeated_acknowledgement_returns_the_recorded_outcome(tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_MESSAGE, text="once", operation_id="op-ack"
    )
    commands_ctl.claim(command["command_id"], consumer="runtime:1")
    first = commands_ctl.acknowledge(
        command["command_id"], consumer="runtime:1", result="delivered"
    )
    again = commands_ctl.acknowledge(
        command["command_id"], consumer="runtime:1", result="delivered"
    )
    assert first["completed_at"] == again["completed_at"]
    assert again["status"] == commands_ctl.STATUS_ACKNOWLEDGED


# --------------------------------------------------------------------------- #
# Stop: ownership, idempotency, terminal state
# --------------------------------------------------------------------------- #


def test_stop_is_idempotent_without_an_operation_id(client, tmp_path):
    session_id = _session(tmp_path, tool="copilot")
    first = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    second = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    assert second["command_id"] == first["command_id"]
    assert second["duplicate"] is True
    assert len(commands_ctl.list_for_session(session_id)) == 1


def _fail_stop(session_id: str, *, result: str, error: str) -> dict:
    """Take the Session's open stop and settle it terminally as a failure."""
    open_stops = [
        row
        for row in commands_ctl.list_for_session(session_id)
        if row["kind"] == commands_ctl.KIND_STOP and row["status"] in commands_ctl.OPEN_STATUSES
    ]
    assert open_stops, "expected an open stop to fail"
    command_id = open_stops[-1]["command_id"]
    if open_stops[-1]["status"] == commands_ctl.STATUS_REQUESTED:
        commands_ctl.claim(command_id, consumer="runtime:gone")
    return commands_ctl.acknowledge(
        command_id, consumer="runtime:gone", result=result, error=error, ok=False
    )


def test_a_stop_that_failed_can_be_pressed_again(client, tmp_path):
    """A stop that stopped nothing must not make the button permanently inert.

    ``not_owned`` is the Runtime saying it never reached the process. The
    Session is still running, so the operator's next press is a *new* request,
    not a replay of the dead one - dedupe there would hand back the same
    failure forever and leave no way to stop the Session at all.
    """
    session_id = _session(tmp_path, tool="copilot")
    first = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    failed = _fail_stop(
        session_id, result=session_channel.RESULT_NOT_OWNED, error="the Runtime restarted"
    )
    assert failed["status"] == commands_ctl.STATUS_FAILED
    assert sessions_ctl.get_agent_session(session_id)["status"] == "running"

    retry = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    assert retry["command_id"] != first["command_id"]
    assert retry["duplicate"] is False
    assert retry["status"] == commands_ctl.STATUS_REQUESTED
    stops = [row for row in commands_ctl.list_for_session(session_id) if row["kind"] == "stop"]
    assert [row["status"] for row in stops] == [
        commands_ctl.STATUS_FAILED,
        commands_ctl.STATUS_REQUESTED,
    ]
    assert stops[1]["sequence"] == 2


def test_a_stop_abandoned_by_every_consumer_can_be_pressed_again(client, tmp_path, monkeypatch):
    """An abandoned stop is a failure like any other: the next press is new."""
    monkeypatch.setenv(commands_ctl.MAX_ATTEMPTS_ENV, "1")
    monkeypatch.setenv(commands_ctl.LEASE_SECONDS_ENV, "1")
    session_id = _session(tmp_path, tool="copilot")
    first = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    commands_ctl.claim(first["command_id"], consumer="runtime:crashed", lease=1)
    commands_ctl.expire_leases(now=utc_now() + timedelta(seconds=120))
    abandoned = commands_ctl.get(first["command_id"])
    assert abandoned["status"] == commands_ctl.STATUS_FAILED
    assert abandoned["result"] == commands_ctl.RESULT_ABANDONED

    retry = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    assert retry["command_id"] != first["command_id"]
    assert retry["status"] == commands_ctl.STATUS_REQUESTED


def test_a_stop_still_in_flight_is_not_sent_twice(client, tmp_path):
    """Pressing stop while an attempt is delivered is still one command."""
    session_id = _session(tmp_path, tool="copilot")
    first = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    claimed = commands_ctl.claim(first["command_id"], consumer="runtime:working")
    assert claimed["status"] == commands_ctl.STATUS_DELIVERED
    second = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    assert second["command_id"] == first["command_id"]
    assert second["duplicate"] is True
    assert len(commands_ctl.list_for_session(session_id)) == 1


def test_a_stop_that_worked_is_not_sent_again(client, tmp_path):
    """A delivered stop stays idempotent: the Session is gone, so is the retry."""
    session_id = _session(tmp_path, tool="copilot")
    process = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(session_id, process, tool="copilot")
        first = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    finally:
        _kill(process)
    assert first["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert first["result"] == session_channel.RESULT_STOPPED
    second = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    assert second["command_id"] == first["command_id"]
    assert second["duplicate"] is True
    assert len(commands_ctl.list_for_session(session_id)) == 1


def test_a_cancelled_stop_on_an_ended_session_is_not_re_minted(client, tmp_path):
    """Nothing is left to stop once the Session ended, however its stop settled."""
    session_id = _session(tmp_path, tool="copilot")
    first = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    sessions_ctl.end_session(session_id, "finished on its own")
    assert commands_ctl.get(first["command_id"])["status"] == commands_ctl.STATUS_CANCELLED
    second = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    assert second["command_id"] == first["command_id"]
    assert second["duplicate"] is True
    assert len(commands_ctl.list_for_session(session_id)) == 1


def test_concurrent_retries_of_a_failed_stop_mint_one_new_attempt(tmp_path):
    """The retry key is derived, not minted, so racing presses still collide."""
    session_id = _session(tmp_path, tool="copilot")
    commands_ctl.enqueue(session_id, commands_ctl.KIND_STOP)
    _fail_stop(session_id, result=session_channel.RESULT_NOT_OWNED, error="the Runtime restarted")

    def _press():
        return commands_ctl.enqueue(session_id, commands_ctl.KIND_STOP)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result() for f in [pool.submit(_press) for _ in range(4)]]
    assert sum(1 for _command, created in results if created) == 1
    assert len({command["command_id"] for command, _created in results}) == 1
    stops = [row for row in commands_ctl.list_for_session(session_id) if row["kind"] == "stop"]
    assert len(stops) == 2, "a failed stop must be retried exactly once per press"


def test_an_explicit_operation_id_stop_is_one_command_however_it_ended(client, tmp_path):
    """A caller that names the operation owns its identity, failure included."""
    session_id = _session(tmp_path, tool="copilot")
    payload = {"operation_id": "op-explicit-stop"}
    first = client.post(f"/v1/sessions/{session_id}/stop", json=payload, headers=ADMIN_AUTH).json()
    _fail_stop(session_id, result=session_channel.RESULT_NOT_OWNED, error="the Runtime restarted")
    again = client.post(f"/v1/sessions/{session_id}/stop", json=payload, headers=ADMIN_AUTH).json()
    assert again["command_id"] == first["command_id"]
    assert again["duplicate"] is True
    assert again["status"] == commands_ctl.STATUS_FAILED
    # ...while the default key is still free to mint a new attempt.
    fresh = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    assert fresh["command_id"] != first["command_id"]
    assert fresh["status"] == commands_ctl.STATUS_REQUESTED


def test_the_console_offers_a_retry_for_a_stop_that_failed():
    """A retryable failure the operator cannot press is not retryable.

    Asserted against the checked-in dock: a failed stop renders its own retry
    control, and pressing it goes back through ``stop()`` - which is what mints
    the new durable attempt on the server.
    """
    dock = (
        Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "ChatDock.tsx"
    ).read_text(encoding="utf-8")
    assert "Retry stop" in dock
    assert 'm.kind === "stop"' in dock
    assert "void stop()" in dock


def test_stop_reaches_only_the_process_the_runtime_owns(client, tmp_path):
    """Ownership is a handle, never a name: a bystander process survives."""
    session_id = _session(tmp_path, tool="copilot")
    target = _spawn(_SLEEP_AGENT)
    bystander = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(session_id, target, tool="copilot")
        body = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
        assert body["status"] == commands_ctl.STATUS_ACKNOWLEDGED
        assert body["result"] == session_channel.RESULT_STOPPED
        assert target.poll() is not None
        # The bystander runs the same executable with the same argv.
        assert bystander.poll() is None
    finally:
        _kill(target)
        _kill(bystander)


def test_a_stop_the_runtime_cannot_deliver_is_not_reported_as_a_stop(client, tmp_path):
    """``not_owned`` is a failure, and it must not make the Session terminal."""
    session_id = _session(tmp_path, tool="copilot")
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-notowned"
    )
    commands_ctl.claim(command["command_id"], consumer="runtime:remote")
    settled = commands_ctl.acknowledge(
        command["command_id"],
        consumer="runtime:remote",
        result=session_channel.RESULT_NOT_OWNED,
        error="the Runtime restarted",
        ok=False,
    )
    assert settled["status"] == commands_ctl.STATUS_FAILED
    assert sessions_ctl.get_agent_session(session_id)["status"] == "running"


def test_a_delivered_stop_synchronizes_the_session_issue_and_locks(client, tmp_path):
    from brains.control import claims as claims_ctl
    from brains.control import issues as issues_ctl
    from brains.control import projects as projects_ctl
    from brains.control import tasks as tasks_ctl

    org = orgs_ctl.create_org(_slug("org"), "Acme")
    path = tmp_path / _slug("ws")
    path.mkdir(parents=True, exist_ok=True)
    workspace = sessions_ctl.register_workspace(str(path), org_id=org["id"])
    session_id = sessions_ctl.start_session(str(path), tool="copilot")["session_id"]
    project = projects_ctl.create_project(org["id"], _slug("proj"), "Proj")
    issue = issues_ctl.create_issue(project["id"], "Do the thing")
    issues_ctl.transition(issue["id"], "in_progress")
    with SessionLocal() as session:
        session.get(AgentSession, session_id).issue_id = issue["id"]
        session.commit()
    claims_ctl.claim_workspace(str(path), session_id, scope="exec", duration_minutes=30)
    task = tasks_ctl.create_task(str(path), "task title")
    tasks_ctl.claim_task(task["code"], session_id)

    process = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(session_id, process, tool="copilot")
        client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH)
    finally:
        _kill(process)

    row = sessions_ctl.get_agent_session(session_id)
    assert row["status"] == "ended"
    assert row["state"] == "failed"
    assert row["ended_at"] is not None
    assert "stopped by operator request" in row["summary"]
    assert issues_ctl.get_issue(issue["id"])["status"] == "blocked"
    assert claims_ctl.list_workspace_claims(str(path)) == []
    assert tasks_ctl.get_task(task["code"])["status"] == "available"
    assert workspace.id == row["workspace_id"]


def test_a_retried_stop_that_works_ends_the_session_once(client, tmp_path):
    """The retry path and terminal synchronisation still agree.

    A second attempt is a new command, but the Session ends once: the winning
    attempt stamps `ended_at`, the failed one is left as the record of what
    happened, and the next press is answered with the attempt that worked
    rather than a third one.
    """
    session_id = _session(tmp_path, tool="copilot")
    client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH)
    _fail_stop(session_id, result=session_channel.RESULT_NOT_OWNED, error="the Runtime restarted")

    process = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(session_id, process, tool="copilot")
        retry = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    finally:
        _kill(process)
    assert retry["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert retry["result"] == session_channel.RESULT_STOPPED
    row = sessions_ctl.get_agent_session(session_id)
    assert row["status"] == "ended"
    assert row["state"] == "failed"
    assert "stopped by operator request" in row["summary"]

    again = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    assert again["command_id"] == retry["command_id"]
    assert again["duplicate"] is True
    stops = [row for row in commands_ctl.list_for_session(session_id) if row["kind"] == "stop"]
    assert [row["status"] for row in stops] == [
        commands_ctl.STATUS_FAILED,
        commands_ctl.STATUS_ACKNOWLEDGED,
    ]


def test_a_stop_after_a_natural_finish_is_truthful_not_an_error(client, tmp_path):
    session_id = _session(tmp_path, tool="copilot")
    sessions_ctl.end_session(session_id, "finished on its own")
    body = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH).json()
    assert body["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert body["result"] == commands_ctl.RESULT_ALREADY_TERMINAL
    row = sessions_ctl.get_agent_session(session_id)
    assert row["state"] == "completed"
    assert row["summary"] == "finished on its own"


def test_a_natural_finish_racing_a_stop_keeps_the_first_outcome(tmp_path):
    """``finalize_session`` is a conditional stamp, so the loser changes nothing."""
    session_id = _session(tmp_path, tool="copilot")
    sessions_ctl.end_session(session_id, "completed naturally")
    assert sessions_ctl.finalize_session(session_id, state="failed", summary="stopped") is None
    row = sessions_ctl.get_agent_session(session_id)
    assert row["state"] == "completed"
    assert row["summary"] == "completed naturally"


def test_a_session_that_ends_cancels_its_open_commands(client, tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    queued = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "never delivered", "operation_id": "op-cancel"},
        headers=ADMIN_AUTH,
    ).json()
    assert queued["status"] == commands_ctl.STATUS_REQUESTED
    sessions_ctl.end_session(session_id, "done")
    settled = commands_ctl.get(queued["command_id"])
    assert settled["status"] == commands_ctl.STATUS_CANCELLED
    assert settled["result"] == commands_ctl.RESULT_SESSION_ENDED


# --------------------------------------------------------------------------- #
# Runtime consumer surface + reconciliation
# --------------------------------------------------------------------------- #


def _runtime(machine_id: str, org_id: int) -> dict:
    from brains.control import runtimes as runtimes_ctl

    registered = runtimes_ctl.register_runtime(
        machine_id,
        "copilot",
        org_id=org_id,
        machine_label="box",
        os="linux",
        status="online",
        health="healthy",
    )
    return registered


def _bind_session_to_runtime(session_id: str, runtime: dict) -> None:
    with SessionLocal() as session:
        row = session.get(AgentSession, session_id)
        row.runtime_id = runtime["id"]
        row.machine_id = runtime["machine_id"]
        session.commit()
    with SessionLocal() as session:
        # The command rows carry the binding they were created with, so any
        # command queued before the binding is re-pointed at it.
        for command in session.query(SessionCommand).filter(
            SessionCommand.session_id == session_id
        ):
            command.runtime_id = runtime["id"]
            command.machine_id = runtime["machine_id"]
        session.commit()


def test_a_runtime_claims_and_acknowledges_its_own_sessions_commands(client, tmp_path):
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = _runtime(_slug("box"), org["id"])
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)
    client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH)
    _bind_session_to_runtime(session_id, runtime)

    listed = client.get(f"/v1/runtimes/{runtime['id']}/session-commands", headers=ADMIN_AUTH).json()
    assert [row["session_id"] for row in listed["commands"]] == [session_id]
    command_id = listed["commands"][0]["command_id"]

    claimed = client.post(
        f"/v1/runtimes/{runtime['id']}/session-commands/{command_id}/claim", headers=ADMIN_AUTH
    ).json()
    assert claimed["claimed"] is True
    again = client.post(
        f"/v1/runtimes/{runtime['id']}/session-commands/{command_id}/claim", headers=ADMIN_AUTH
    ).json()
    assert again["claimed"] is False

    acked = client.post(
        f"/v1/runtimes/{runtime['id']}/session-commands/{command_id}/ack",
        json={"result": "not_owned", "ok": False, "error": "restarted"},
        headers=ADMIN_AUTH,
    ).json()
    assert acked["status"] == commands_ctl.STATUS_FAILED


def test_a_runtime_credential_is_confined_to_its_own_machine(client, tmp_path):
    from brains.control import enrolment as enrolment_ctl

    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine_id = _slug("box")
    runtime = _runtime(machine_id, org["id"])
    minted = enrolment_ctl.mint_token(label="box", ttl_seconds=900, org_id=org["id"])
    redeemed = enrolment_ctl.redeem_token(
        minted["token"], machine_id=machine_id, clis=[{"tool": "copilot"}], org_id=org["id"]
    )
    daemon_key = redeemed["daemon_key"]
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)

    # The Runtime credential may poll its own machine's commands ...
    allowed = client.get(
        f"/v1/runtimes/{runtime['id']}/session-commands", headers=_auth(daemon_key)
    )
    assert allowed.status_code == 200
    # ... and is refused every operator route, including the ones this work adds.
    refused = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "nope"},
        headers=_auth(daemon_key),
    )
    assert refused.status_code == 403
    assert (
        client.post(
            f"/v1/sessions/{session_id}/stop", json={}, headers=_auth(daemon_key)
        ).status_code
        == 403
    )
    assert (
        client.get(f"/v1/sessions/{session_id}/commands", headers=_auth(daemon_key)).status_code
        == 403
    )


def test_a_runtime_cannot_claim_another_machines_command(client, tmp_path):
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    mine = _runtime(_slug("box-a"), org["id"])
    theirs = _runtime(_slug("box-b"), org["id"])
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, theirs)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-foreign"
    )
    response = client.post(
        f"/v1/runtimes/{mine['id']}/session-commands/{command['command_id']}/claim",
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Consumer ownership: who may deliver a command, on a shared machine
# --------------------------------------------------------------------------- #


def test_a_second_worker_on_the_same_machine_is_not_a_second_owner(client, tmp_path):
    """Two Runtimes, one box. A command belongs to the one running the Session.

    Sharing a machine is not sharing a process handle: the worker that did not
    launch the agent can only answer ``not_owned``, and answering it settles
    the operator's command against a delivery that was never attempted.
    """
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine = _slug("shared-box")
    from brains.control import runtimes as runtimes_ctl

    owner = runtimes_ctl.register_runtime(
        machine, "copilot", org_id=org["id"], machine_label="box", os="linux"
    )
    neighbour = runtimes_ctl.register_runtime(
        machine, "claude", org_id=org["id"], machine_label="box", os="linux"
    )
    assert owner["id"] != neighbour["id"]
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, owner)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-shared"
    )

    listed = client.get(
        f"/v1/runtimes/{neighbour['id']}/session-commands", headers=ADMIN_AUTH
    ).json()
    assert listed["commands"] == []
    refused = client.post(
        f"/v1/runtimes/{neighbour['id']}/session-commands/{command['command_id']}/claim",
        headers=ADMIN_AUTH,
    )
    assert refused.status_code == 404

    mine = client.get(f"/v1/runtimes/{owner['id']}/session-commands", headers=ADMIN_AUTH).json()
    assert [row["command_id"] for row in mine["commands"]] == [command["command_id"]]
    claimed = client.post(
        f"/v1/runtimes/{owner['id']}/session-commands/{command['command_id']}/claim",
        headers=ADMIN_AUTH,
    ).json()
    assert claimed["claimed"] is True
    assert commands_ctl.get(command["command_id"])["status"] == commands_ctl.STATUS_DELIVERED


def test_a_cli_session_on_a_shared_machine_belongs_to_the_local_consumer(client, tmp_path):
    """An unbound Session is the local process's to deliver, not the Runtime's.

    A Session started from the CLI or streamed by the hub has no Runtime
    binding; the process handle lives in whichever process launched it. A
    daemon that claimed it because it happened to share the box would take a
    lease it cannot honour and burn an attempt the real owner needed.
    """
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine = sessions_ctl.current_machine_id()
    from brains.control import runtimes as runtimes_ctl

    runtime = runtimes_ctl.register_runtime(
        machine, "copilot", org_id=org["id"], machine_label="box", os="linux"
    )
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    assert sessions_ctl.get_agent_session(session_id)["runtime_id"] is None
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-unbound"
    )
    assert command["machine_id"] == machine

    listed = client.get(f"/v1/runtimes/{runtime['id']}/session-commands", headers=ADMIN_AUTH).json()
    assert listed["commands"] == []
    refused = client.post(
        f"/v1/runtimes/{runtime['id']}/session-commands/{command['command_id']}/claim",
        headers=ADMIN_AUTH,
    )
    assert refused.status_code == 404
    assert commands_ctl.get(command["command_id"])["status"] == commands_ctl.STATUS_REQUESTED

    # ...and the process that actually launched the agent still delivers it.
    process = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(session_id, process, tool="copilot")
        from brains.exec import session_dispatch

        settled = session_dispatch.dispatch_owned(session_id=session_id)
    finally:
        _kill(process)
    assert [row["command_id"] for row in settled] == [command["command_id"]]
    assert settled[0]["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert settled[0]["result"] == session_channel.RESULT_STOPPED


def test_the_local_consumer_leaves_a_runtime_bound_command_alone(client, tmp_path):
    """Ownership is checked even where this process does hold the handle."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = _runtime(_slug("box"), org["id"])
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-remote"
    )
    process = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(session_id, process, tool="copilot")
        from brains.exec import session_dispatch

        assert session_dispatch.dispatch_owned(session_id=session_id) == []
        assert process.poll() is None
    finally:
        _kill(process)
    assert commands_ctl.get(command["command_id"])["status"] == commands_ctl.STATUS_REQUESTED


def test_a_consumer_holding_a_command_it_does_not_own_requeues_it(client, tmp_path):
    """A non-owner hands the command back rather than settling it failed.

    The Session was re-bound while the command was in flight. Acknowledging it
    ``not_owned`` would consume the operator's stop on behalf of a Runtime that
    never saw it; releasing it costs one poll interval and keeps the record
    true.
    """
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine = _slug("shared-box")
    from brains.control import runtimes as runtimes_ctl

    first = runtimes_ctl.register_runtime(
        machine, "copilot", org_id=org["id"], machine_label="box", os="linux"
    )
    second = runtimes_ctl.register_runtime(
        machine, "claude", org_id=org["id"], machine_label="box", os="linux"
    )
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, first)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-rebound"
    )
    claimed = client.post(
        f"/v1/runtimes/{first['id']}/session-commands/{command['command_id']}/claim",
        headers=ADMIN_AUTH,
    ).json()
    assert claimed["claimed"] is True

    # The Session moves to the other worker while the first one holds it.
    _bind_session_to_runtime(session_id, second)
    from brains.exec import session_dispatch

    held = commands_ctl.get(command["command_id"])
    assert not session_dispatch.owns(held, runtime_id=first["id"], machine_id=machine)
    released = client.post(
        f"/v1/runtimes/{first['id']}/session-commands/{command['command_id']}/release",
        json={"reason": "not this Runtime's Session"},
        headers=ADMIN_AUTH,
    ).json()
    assert released["released"] is True
    requeued = commands_ctl.get(command["command_id"])
    assert requeued["status"] == commands_ctl.STATUS_REQUESTED
    assert requeued["claimed_by"] is None

    # ...and its owner can now take it.
    taken = client.post(
        f"/v1/runtimes/{second['id']}/session-commands/{command['command_id']}/claim",
        headers=ADMIN_AUTH,
    ).json()
    assert taken["claimed"] is True


def test_the_holder_may_report_an_outcome_it_observed_after_a_rebind(client, tmp_path):
    """A true report is not discarded because the binding moved under it.

    The Runtime claimed the command, delivered it and watched the process go.
    It is the only party that holds that fact, so its acknowledgement is
    accepted - while a Runtime that never held the command is still refused.
    """
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine = _slug("shared-box")
    from brains.control import runtimes as runtimes_ctl

    holder = runtimes_ctl.register_runtime(
        machine, "copilot", org_id=org["id"], machine_label="box", os="linux"
    )
    other = runtimes_ctl.register_runtime(
        machine, "claude", org_id=org["id"], machine_label="box", os="linux"
    )
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, holder)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-observed"
    )
    client.post(
        f"/v1/runtimes/{holder['id']}/session-commands/{command['command_id']}/claim",
        headers=ADMIN_AUTH,
    )
    _bind_session_to_runtime(session_id, other)

    refused = client.post(
        f"/v1/runtimes/{other['id']}/session-commands/{command['command_id']}/ack",
        json={"result": session_channel.RESULT_STOPPED},
        headers=ADMIN_AUTH,
    )
    assert refused.status_code in (404, 409), "a Runtime settled an attempt it never delivered"
    assert commands_ctl.get(command["command_id"])["status"] == commands_ctl.STATUS_DELIVERED
    accepted = client.post(
        f"/v1/runtimes/{holder['id']}/session-commands/{command['command_id']}/ack",
        json={"result": session_channel.RESULT_STOPPED},
        headers=ADMIN_AUTH,
    ).json()
    assert accepted["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert accepted["result"] == session_channel.RESULT_STOPPED


def test_a_runtime_cannot_release_a_command_it_never_held(client, tmp_path):
    """Release is a hand-back, never a way to reopen somebody else's attempt."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine = _slug("shared-box")
    from brains.control import runtimes as runtimes_ctl

    owner = runtimes_ctl.register_runtime(
        machine, "copilot", org_id=org["id"], machine_label="box", os="linux"
    )
    neighbour = runtimes_ctl.register_runtime(
        machine, "claude", org_id=org["id"], machine_label="box", os="linux"
    )
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, owner)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-held"
    )
    client.post(
        f"/v1/runtimes/{owner['id']}/session-commands/{command['command_id']}/claim",
        headers=ADMIN_AUTH,
    )
    response = client.post(
        f"/v1/runtimes/{neighbour['id']}/session-commands/{command['command_id']}/release",
        json={},
        headers=ADMIN_AUTH,
    ).json()
    assert response["released"] is False
    still_held = commands_ctl.get(command["command_id"])
    assert still_held["status"] == commands_ctl.STATUS_DELIVERED
    assert still_held["claimed_by"] == f"runtime:{owner['id']}:{machine}"


def test_the_daemon_requeues_a_command_it_does_not_own(tmp_path):
    """The daemon's own loop must not settle another consumer's command.

    Two layers are asserted, because they fail differently: a command the hub
    should never have listed is skipped before it is claimed, and one that was
    re-bound *after* it was claimed is released rather than acknowledged.
    """
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine_id = _slug("box")
    runtime = _runtime(machine_id, org["id"])
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-daemon-foreign"
    )
    _bind_session_to_runtime(session_id, runtime)
    foreign_runtime_id = int(runtime["id"]) + 1000

    daemon = _daemon(machine_id)
    listed = commands_ctl.get(command["command_id"])
    hub = daemon.client
    released: list[str] = []

    def _rebind_to_another_runtime() -> None:
        with SessionLocal() as session:
            row = (
                session.query(SessionCommand)
                .filter(SessionCommand.command_id == command["command_id"])
                .one()
            )
            row.runtime_id = foreign_runtime_id
            session.commit()

    class _StubClient:
        """The hub as a Runtime that is losing the Session sees it."""

        def __init__(self, offered: dict, *, rebind_on_claim: bool = False):
            self.offered = offered
            self.rebind_on_claim = rebind_on_claim

        def get_session_commands(self, _runtime_id, **_kwargs):
            return [self.offered]

        def claim_session_command(self, runtime_id, command_id):
            claim = hub.claim_session_command(runtime_id, command_id)
            if self.rebind_on_claim:
                # The Session moves to another Runtime in the window between
                # the claim and the delivery.
                _rebind_to_another_runtime()
                claim["command"] = {**claim["command"], "runtime_id": foreign_runtime_id}
            return claim

        def release_session_command(self, runtime_id, command_id, *, reason=None):
            released.append(command_id)
            return hub.release_session_command(runtime_id, command_id, reason=reason)

        def ack_session_command(self, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("a non-owner settled a command it did not deliver")

        def __getattr__(self, name):
            return getattr(hub, name)

    # 1. Offered a command bound to another Runtime: never claimed.
    daemon.client = _StubClient({**listed, "runtime_id": foreign_runtime_id})
    assert daemon.poll_session_commands() == []
    assert commands_ctl.get(command["command_id"])["status"] == commands_ctl.STATUS_REQUESTED
    assert released == []

    # 2. Owned when claimed, re-bound before it could be delivered: released.
    daemon.client = _StubClient(listed, rebind_on_claim=True)
    acted = daemon.poll_session_commands()
    assert [row["result"] for row in acted] == ["released"]
    assert released == [command["command_id"]]
    requeued = commands_ctl.get(command["command_id"])
    assert requeued["status"] == commands_ctl.STATUS_REQUESTED
    assert requeued["claimed_by"] is None


def test_a_restarted_runtime_reconciles_what_it_no_longer_owns(client, tmp_path):
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = _runtime(_slug("box"), org["id"])
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)
    queued, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-reconcile"
    )
    # Age the Session past the reconciliation grace window.
    with SessionLocal() as session:
        row = session.get(AgentSession, session_id)
        row.started_at = utc_now() - timedelta(seconds=600)
        session.commit()

    response = client.post(
        f"/v1/runtimes/{runtime['id']}/sessions/reconcile",
        json={"owned_session_ids": []},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200
    assert session_id in response.json()["reconciled"]
    row = sessions_ctl.get_agent_session(session_id)
    assert row["state"] == "failed"
    assert row["ended_at"] is not None
    assert commands_ctl.get(queued["command_id"])["status"] == commands_ctl.STATUS_CANCELLED


def test_reconciliation_leaves_owned_and_young_sessions_alone(client, tmp_path):
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = _runtime(_slug("box"), org["id"])
    owned_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    young_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(owned_id, runtime)
    _bind_session_to_runtime(young_id, runtime)
    with SessionLocal() as session:
        session.get(AgentSession, owned_id).started_at = utc_now() - timedelta(seconds=600)
        session.commit()

    response = client.post(
        f"/v1/runtimes/{runtime['id']}/sessions/reconcile",
        json={"owned_session_ids": [owned_id]},
        headers=ADMIN_AUTH,
    ).json()
    assert response["reconciled"] == []
    assert sessions_ctl.get_agent_session(owned_id)["status"] == "running"
    assert sessions_ctl.get_agent_session(young_id)["status"] == "running"


# --------------------------------------------------------------------------- #
# Production shape: the hub is not the box the agent runs on
# --------------------------------------------------------------------------- #


def _remote_runtime(org_id: int, tool: str = "copilot") -> dict:
    """A Runtime on a machine that is *not* the hub's."""
    from brains.control import runtimes as runtimes_ctl

    machine = _slug("remote-box")
    assert machine != sessions_ctl.current_machine_id()
    return runtimes_ctl.register_runtime(
        machine,
        tool,
        org_id=org_id,
        machine_label="remote",
        os="linux",
        status="online",
        health="healthy",
    )


def _spawned_session(tmp_path, runtime: dict, org_id: int, *, tool: str = "copilot") -> str:
    """A persona/issue spawn Session, opened by the hub as production does it."""
    from brains.control import issues as issues_ctl
    from brains.control import personas as personas_ctl
    from brains.control import projects as projects_ctl

    persona = personas_ctl.create_persona(
        org_id, _slug("p"), "Forge", tool=tool, default_runtime_id=runtime["id"]
    )
    project = projects_ctl.create_project(org_id, _slug("proj"), "Proj")
    issue = issues_ctl.create_issue(project["id"], "Fix the thruster", body="broken")
    path = tmp_path / _slug("ws")
    path.mkdir(parents=True, exist_ok=True)
    sessions_ctl.register_workspace(str(path), org_id=org_id)
    opened = sessions_ctl.open_spawn_session(
        persona_id=persona["id"],
        tool=tool,
        issue_id=issue["id"],
        runtime_id=runtime["id"],
        workspace_path=str(path),
    )
    with SessionLocal() as session:
        workspace = session.get(AgentSession, opened["id"]).workspace_id
        session.get(Workspace, workspace).org_id = org_id
        session.commit()
    return opened["id"]


def _stamp_hub_machine(session_id: str) -> None:
    """Rewrite a Session and its commands the way a pre-fix install recorded them.

    The hub opened the row, so the machine stamp names the *hub*, not the box
    the agent runs on. Nothing legacy is migrated, so every guard has to keep
    working against these rows.
    """
    hub_machine = sessions_ctl.current_machine_id()
    with SessionLocal() as session:
        session.get(AgentSession, session_id).machine_id = hub_machine
        for command in session.query(SessionCommand).filter(
            SessionCommand.session_id == session_id
        ):
            command.machine_id = hub_machine
        session.commit()


def test_a_spawned_session_is_stamped_with_the_machine_that_will_run_it(tmp_path):
    """The Runtime's box, not the hub's, is where the agent will actually run.

    ``open_spawn_session`` executes inside the hub process, so the obvious
    stamp is the hub's machine - and every surface that reads the stamp (the
    zombie reaper, reconciliation, command routing) then reasons about a box
    the agent was never on.
    """
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = _remote_runtime(org["id"])
    session_id = _spawned_session(tmp_path, runtime, org["id"])

    row = sessions_ctl.get_agent_session(session_id)
    assert row["machine_id"] == runtime["machine_id"]
    assert row["machine_id"] != sessions_ctl.current_machine_id()


def test_the_runtime_that_opens_a_session_restamps_a_hub_created_row(client, tmp_path):
    """A legacy row is corrected the moment its Runtime opens it for real."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = _remote_runtime(org["id"])
    session_id = _spawned_session(tmp_path, runtime, org["id"])
    _stamp_hub_machine(session_id)
    assert sessions_ctl.get_agent_session(session_id)["machine_id"] != runtime["machine_id"]

    response = client.post(
        f"/v1/runtimes/{runtime['id']}/sessions",
        json={"session_id": session_id, "workspace_path": str(tmp_path)},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 200
    assert sessions_ctl.get_agent_session(session_id)["machine_id"] == runtime["machine_id"]


def test_a_remote_runtime_delivers_a_stop_for_a_hub_stamped_session(client, tmp_path):
    """The production shape: hub here, agent there, and a stop that must arrive.

    A spawn Session recorded before the machine stamp was corrected names the
    hub's machine while its agent runs on the Runtime's box. Ownership is the
    Session's Runtime binding, so the Runtime that holds the process lists,
    claims, delivers and settles the operator's stop regardless of the stamp -
    the alternative is a stop button that is durably queued to nobody.
    """
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = _remote_runtime(org["id"])
    session_id = _spawned_session(tmp_path, runtime, org["id"])
    stop = client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=ADMIN_AUTH)
    assert stop.status_code == 200
    command_id = stop.json()["command_id"]
    _stamp_hub_machine(session_id)

    listed = client.get(f"/v1/runtimes/{runtime['id']}/session-commands", headers=ADMIN_AUTH).json()
    assert [row["command_id"] for row in listed["commands"]] == [command_id]

    process = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(session_id, process, tool="copilot", runtime_id=runtime["id"])
        acted = _daemon(runtime["machine_id"]).poll_session_commands()
    finally:
        _kill(process)

    assert [row.get("result") for row in acted] == [session_channel.RESULT_STOPPED]
    settled = commands_ctl.get(command_id)
    assert settled["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert process.poll() is not None
    assert sessions_ctl.get_agent_session(session_id)["ended_at"] is not None


def test_a_remote_runtime_delivers_a_message_for_a_hub_stamped_session(client, tmp_path):
    """Same binding, the other command kind: the text reaches the live agent."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    runtime = _remote_runtime(org["id"], tool=INTERACTIVE_TOOL)
    session_id = _spawned_session(tmp_path, runtime, org["id"], tool=INTERACTIVE_TOOL)
    sent = client.post(
        f"/v1/sessions/{session_id}/message", json={"text": "keep going"}, headers=ADMIN_AUTH
    )
    assert sent.status_code == 200
    command_id = sent.json()["command_id"]
    _stamp_hub_machine(session_id)

    process = _spawn(_ECHO_AGENT)
    try:
        session_channel.register(
            session_id,
            process,
            tool=INTERACTIVE_TOOL,
            stdin_open=True,
            runtime_id=runtime["id"],
        )
        acted = _daemon(runtime["machine_id"]).poll_session_commands()
        assert [row.get("result") for row in acted] == [session_channel.RESULT_DELIVERED]
        assert process.stdout.readline().strip() == "got:keep going"
    finally:
        _kill(process)

    settled = commands_ctl.get(command_id)
    assert settled["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert settled["result"] == session_channel.RESULT_DELIVERED


def test_a_command_records_the_machine_of_the_runtime_that_will_run_it(tmp_path):
    """The stamp is a diagnostic, so it should still name the right box."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = _remote_runtime(org["id"])
    session_id = _spawned_session(tmp_path, runtime, org["id"])
    with SessionLocal() as session:
        # Even from a row that still carries the hub's stamp.
        session.get(AgentSession, session_id).machine_id = sessions_ctl.current_machine_id()
        session.commit()

    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-stamp"
    )
    assert command["machine_id"] == runtime["machine_id"]


def test_a_remote_runtime_reports_the_terminal_state_of_a_hub_stamped_session(client, tmp_path):
    """A Runtime credential may settle its own Session whatever the stamp says.

    Refusing it left the Session ``running`` in the console for the rest of
    time, because the daemon's state report is best effort and its refusal is
    invisible to the operator.
    """
    from brains.control import enrolment as enrolment_ctl

    org = orgs_ctl.create_org(_slug("org"), "Acme")
    runtime = _remote_runtime(org["id"])
    session_id = _spawned_session(tmp_path, runtime, org["id"])
    _stamp_hub_machine(session_id)
    minted = enrolment_ctl.mint_token(label="remote", ttl_seconds=900, org_id=org["id"])
    daemon_key = enrolment_ctl.redeem_token(
        minted["token"],
        machine_id=runtime["machine_id"],
        clis=[{"tool": "copilot"}],
        org_id=org["id"],
    )["daemon_key"]

    response = client.post(
        f"/v1/sessions/{session_id}/state",
        json={"state": "completed", "summary": "agent finished"},
        headers=_auth(daemon_key),
    )
    assert response.status_code == 200
    assert sessions_ctl.get_agent_session(session_id)["state"] == "completed"


# --------------------------------------------------------------------------- #
# Multi-CLI machine: several Runtimes, one daemon process
# --------------------------------------------------------------------------- #


def _two_runtimes_on_one_machine(org_id: int, machine: str) -> tuple[dict, dict]:
    from brains.control import runtimes as runtimes_ctl

    first = runtimes_ctl.register_runtime(
        machine, "copilot", org_id=org_id, machine_label="box", os="linux", status="online"
    )
    second = runtimes_ctl.register_runtime(
        machine, "claude", org_id=org_id, machine_label="box", os="linux", status="online"
    )
    return first, second


def test_each_runtime_is_reconciled_against_only_the_handles_it_holds(tmp_path):
    """One box, two CLIs, one daemon: each Runtime hears about its own Sessions.

    Reporting the machine's whole owned set to every Runtime made each sibling
    claim ownership of the others' Sessions. The hub refuses a foreign claim,
    so the sibling's entire reconciliation failed - and the stale rows it
    should have ended stayed ``running`` forever.
    """
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine = _slug("multi-cli-box")
    owner, sibling = _two_runtimes_on_one_machine(org["id"], machine)
    live_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    stale_id = _session(tmp_path, tool="claude", org_id=org["id"])
    _bind_session_to_runtime(live_id, owner)
    _bind_session_to_runtime(stale_id, sibling)
    with SessionLocal() as session:
        for sid in (live_id, stale_id):
            session.get(AgentSession, sid).started_at = utc_now() - timedelta(seconds=600)
        session.commit()

    process = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(live_id, process, tool="copilot", runtime_id=owner["id"])
        results = _daemon(machine).reconcile_sessions()
    finally:
        _kill(process)

    by_runtime = {row.get("runtime_id"): row for row in results}
    assert not any(row.get("error") for row in results), results
    # The Runtime that holds the handle keeps its Session ...
    assert by_runtime[owner["id"]]["owned"] == [live_id]
    assert by_runtime[owner["id"]]["reconciled"] == []
    # ... and the sibling, which holds nothing, says so and ends its own row.
    assert by_runtime[sibling["id"]]["owned"] == []
    assert by_runtime[sibling["id"]]["reconciled"] == [stale_id]
    assert sessions_ctl.get_agent_session(live_id)["status"] == "running"
    assert sessions_ctl.get_agent_session(stale_id)["state"] == "failed"


def test_the_handle_registry_groups_owned_sessions_by_runtime(tmp_path):
    """The grouping is a property of the handles, not of a hub round-trip."""
    first = _spawn(_SLEEP_AGENT)
    second = _spawn(_SLEEP_AGENT)
    third = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register("ses-a", first, tool="copilot", runtime_id=7)
        session_channel.register("ses-b", second, tool="claude", runtime_id=9)
        session_channel.register("ses-c", third, tool="copilot")
        grouped = session_channel.owned_session_ids_by_runtime()
        flat = session_channel.owned_session_ids()
    finally:
        for process in (first, second, third):
            _kill(process)

    assert grouped[7] == ["ses-a"]
    assert grouped[9] == ["ses-b"]
    assert grouped[None] == ["ses-c"]
    assert flat == ["ses-a", "ses-b", "ses-c"]


def test_a_runtime_that_holds_nothing_still_reconciles_its_own_rows(tmp_path):
    """The empty list is a fact, not an absence: it is what ends the stale row."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine = _slug("multi-cli-box")
    owner, sibling = _two_runtimes_on_one_machine(org["id"], machine)
    stale_id = _session(tmp_path, tool="claude", org_id=org["id"])
    _bind_session_to_runtime(stale_id, sibling)
    with SessionLocal() as session:
        session.get(AgentSession, stale_id).started_at = utc_now() - timedelta(seconds=600)
        session.commit()

    results = _daemon(machine).reconcile_sessions()
    assert {row.get("runtime_id") for row in results} == {owner["id"], sibling["id"]}
    assert all(row["owned"] == [] for row in results)
    assert sessions_ctl.get_agent_session(stale_id)["state"] == "failed"


def test_a_failed_reconciliation_is_recorded_rather_than_swallowed(tmp_path):
    """A step that failed must be distinguishable from one with nothing to do.

    A silently swallowed exception is how a Runtime stops reconciling without
    anybody noticing, so every failed stage is returned - and logged - in one
    consistent shape.
    """
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine = _slug("box")
    runtime = _runtime(machine, org["id"])
    daemon = _daemon(machine)
    hub = daemon.client

    class _BrokenClient:
        def reconcile_sessions(self, *_args, **_kwargs):
            raise RuntimeError("hub unreachable")

        def get_session_commands(self, *_args, **_kwargs):
            raise RuntimeError("hub unreachable")

        def __getattr__(self, name):
            return getattr(hub, name)

    daemon.client = _BrokenClient()
    reconciled = daemon.reconcile_sessions()
    assert [row["stage"] for row in reconciled] == ["reconcile_failed"]
    assert reconciled[0]["runtime_id"] == runtime["id"]
    assert "hub unreachable" in reconciled[0]["error"]

    polled = daemon.poll_session_commands()
    assert [row["stage"] for row in polled] == ["poll_failed"]
    assert polled[0]["ok"] is False


# --------------------------------------------------------------------------- #
# Daemon integration: claim, deliver, acknowledge, reconcile
# --------------------------------------------------------------------------- #


def _daemon(machine_id: str):
    from brains.daemon import Daemon
    from brains.daemon.client import HubClient
    from brains.daemon.config import DaemonConfig

    return Daemon(
        DaemonConfig(machine_id=machine_id),
        client=HubClient("http://testserver", settings.api_key, http=TestClient(app)),
    )


def test_the_daemon_claims_delivers_and_acknowledges_a_stop(tmp_path):
    """The Runtime is the only party holding the handle, so it is the deliverer."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine_id = _slug("box")
    runtime = _runtime(machine_id, org["id"])
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-daemon-stop"
    )
    _bind_session_to_runtime(session_id, runtime)

    process = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(session_id, process, tool="copilot")
        acted = _daemon(machine_id).poll_session_commands()
    finally:
        _kill(process)

    assert [row["command_id"] for row in acted] == [command["command_id"]]
    assert acted[0]["result"] == session_channel.RESULT_STOPPED
    settled = commands_ctl.get(command["command_id"])
    assert settled["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert sessions_ctl.get_agent_session(session_id)["state"] == "failed"


def test_the_daemon_reports_a_message_it_cannot_deliver(tmp_path):
    """No handle, no delivery - and the operator is told exactly that."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine_id = _slug("box")
    runtime = _runtime(machine_id, org["id"])
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL, org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_MESSAGE, text="hello", operation_id="op-daemon-msg"
    )
    _bind_session_to_runtime(session_id, runtime)

    acted = _daemon(machine_id).poll_session_commands()
    assert acted[0]["result"] == session_channel.RESULT_NOT_OWNED
    settled = commands_ctl.get(command["command_id"])
    assert settled["status"] == commands_ctl.STATUS_FAILED
    assert "restarted" in settled["error"] or "does not own" in settled["error"]


def test_a_restarted_daemon_reconciles_before_it_claims_anything(tmp_path):
    """Process death is the crash case: the handles are gone, the rows are not."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine_id = _slug("box")
    runtime = _runtime(machine_id, org["id"])
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL, org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)
    queued, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_MESSAGE, text="lost", operation_id="op-restart"
    )
    assert queued["status"] == commands_ctl.STATUS_REQUESTED
    with SessionLocal() as session:
        session.get(AgentSession, session_id).started_at = utc_now() - timedelta(seconds=600)
        session.commit()

    # The daemon process restarted: it owns nothing at all.
    session_channel.clear()
    reconciled = _daemon(machine_id).reconcile_sessions()
    assert any(session_id in row["reconciled"] for row in reconciled)
    assert sessions_ctl.get_agent_session(session_id)["state"] == "failed"
    assert commands_ctl.get(queued["command_id"])["status"] == commands_ctl.STATUS_CANCELLED


def test_a_reconciled_session_is_not_reconciled_twice(tmp_path):
    """Reconciliation is idempotent: a second pass finds nothing left to end."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine_id = _slug("box")
    runtime = _runtime(machine_id, org["id"])
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)
    with SessionLocal() as session:
        session.get(AgentSession, session_id).started_at = utc_now() - timedelta(seconds=600)
        session.commit()
    daemon = _daemon(machine_id)
    first = daemon.reconcile_sessions()
    second = daemon.reconcile_sessions()
    assert any(session_id in row["reconciled"] for row in first)
    assert all(row["reconciled"] == [] for row in second)


def test_an_over_long_operation_id_is_refused_rather_than_truncated(client, tmp_path):
    """The operation id *is* the uniqueness key, so it is never trimmed to fit.

    A truncated key would make two genuinely different operations collide on
    their shared prefix, and the second message would be silently answered
    with the first one's command.
    """
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    prefix = "x" * commands_ctl.MAX_OPERATION_ID_CHARS
    response = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "first", "operation_id": f"{prefix}-a"},
        headers=ADMIN_AUTH,
    )
    assert response.status_code == 400
    assert commands_ctl.list_for_session(session_id) == []


def test_two_operation_ids_sharing_a_long_prefix_are_two_commands(client, tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    prefix = "y" * (commands_ctl.MAX_OPERATION_ID_CHARS - 1)
    first = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "first", "operation_id": f"{prefix}a"},
        headers=ADMIN_AUTH,
    ).json()
    second = client.post(
        f"/v1/sessions/{session_id}/message",
        json={"text": "second", "operation_id": f"{prefix}b"},
        headers=ADMIN_AUTH,
    ).json()
    assert first["command_id"] != second["command_id"]
    assert [row["text"] for row in commands_ctl.list_for_session(session_id)] == [
        "first",
        "second",
    ]


def test_lease_expiry_never_reopens_a_command_that_was_just_settled(tmp_path):
    """The requeue is conditional on the lease it read, not a blind overwrite.

    The holder can be acknowledging at the moment the sweep decides its lease
    is over. A read-modify-write would clobber that outcome and hand the
    command to a second consumer - a second stop signal, or the operator's
    prompt delivered twice.
    """
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_MESSAGE, text="settled", operation_id="op-sweep-race"
    )
    commands_ctl.claim(command["command_id"], consumer="runtime:1", lease=1)
    commands_ctl.acknowledge(command["command_id"], consumer="runtime:1", result="delivered")

    swept = commands_ctl.expire_leases(now=utc_now() + timedelta(seconds=30))
    assert [row["command_id"] for row in swept] == []
    settled = commands_ctl.get(command["command_id"])
    assert settled["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert settled["result"] == "delivered"


def test_the_daemon_polls_commands_off_the_assignment_thread():
    """A stop must be deliverable *while* an agent is running.

    Assignment execution blocks for the whole life of the agent CLI, so a
    command polled from that same loop could only ever be delivered when this
    Runtime owned no process at all - precisely when a stop cannot be
    delivered. The command poll therefore has its own loop.
    """
    from brains.daemon import daemon as daemon_module

    source = Path(daemon_module.__file__).read_text(encoding="utf-8")
    assert "def _command_loop(self)" in source
    assert 'name="brains-daemon-commands"' in source
    body = source.split("def run(self, *, once: bool = False)", 1)[1].split("def _heartbeat_loop")[
        0
    ]
    # The blocking assignment loop must not be the thing that drains commands.
    assert "self.poll_and_execute()" in body
    assert body.count("self.poll_session_commands()") == 1  # the ``once`` path only


def test_the_daemon_command_thread_delivers_while_an_assignment_blocks(tmp_path):
    """End to end: the loop that owns the process is not the loop that polls."""
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    machine_id = _slug("box")
    runtime = _runtime(machine_id, org["id"])
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    _bind_session_to_runtime(session_id, runtime)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_STOP, operation_id="op-thread"
    )
    _bind_session_to_runtime(session_id, runtime)

    daemon = _daemon(machine_id)
    daemon.command_poll_s = 1
    process = _spawn(_SLEEP_AGENT)
    try:
        session_channel.register(session_id, process, tool="copilot")
        thread = threading.Thread(target=daemon._command_loop, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if commands_ctl.get(command["command_id"])["status"] in commands_ctl.TERMINAL_STATUSES:
                break
            time.sleep(0.2)
        daemon.stop()
        thread.join(timeout=10)
    finally:
        _kill(process)

    settled = commands_ctl.get(command["command_id"])
    assert settled["status"] == commands_ctl.STATUS_ACKNOWLEDGED
    assert settled["result"] == session_channel.RESULT_STOPPED
    assert process.poll() is not None


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


def test_another_orgs_session_is_not_found_rather_than_forbidden(client, tmp_path):
    record, key = add_operator(_slug("outsider"))
    creds.sync_local_credentials()
    outsider = _auth(key)
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    orgs_ctl.create_org(_slug("other"), "Other")
    session_id = _session(tmp_path, tool="copilot", org_id=org["id"])
    assert record["slug"]

    for path, payload in (
        (f"/v1/sessions/{session_id}/message", {"text": "hi"}),
        (f"/v1/sessions/{session_id}/stop", {}),
    ):
        response = client.post(path, json=payload, headers=outsider)
        assert response.status_code == 404
    assert client.get(f"/v1/sessions/{session_id}/commands", headers=outsider).status_code == 404
    assert commands_ctl.list_for_session(session_id) == []


def test_a_private_workspace_session_is_not_found_for_a_non_member(client, tmp_path):
    from brains.control import memberships as memberships_ctl

    record, key = add_operator(_slug("member"))
    creds.sync_local_credentials()
    org = orgs_ctl.create_org(_slug("org"), "Acme")
    orgs_ctl.add_member(org["id"], record["slug"], "member")
    creds.sync_local_credentials()
    path = tmp_path / _slug("ws")
    path.mkdir(parents=True, exist_ok=True)
    workspace = sessions_ctl.register_workspace(str(path), org_id=org["id"])
    session_id = sessions_ctl.start_session(str(path), tool="copilot")["session_id"]
    memberships_ctl.set_workspace_visibility(str(path), "private")
    assert workspace.id

    headers = _auth(key)
    assert (
        client.post(
            f"/v1/sessions/{session_id}/message", json={"text": "hi"}, headers=headers
        ).status_code
        == 404
    )
    assert (
        client.post(f"/v1/sessions/{session_id}/stop", json={}, headers=headers).status_code == 404
    )


def test_an_unknown_session_is_not_found(client):
    assert (
        client.post(
            "/v1/sessions/ses_missing/message", json={"text": "hi"}, headers=ADMIN_AUTH
        ).status_code
        == 404
    )


def test_an_empty_message_is_refused(client, tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    response = client.post(
        f"/v1/sessions/{session_id}/message", json={"text": "   "}, headers=ADMIN_AUTH
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Realtime: durable, deduped by operation
# --------------------------------------------------------------------------- #


def _realtime_rows(command_id: str) -> list:
    from brains.storage.models import RealtimeEvent

    with SessionLocal() as session:
        return [
            row
            for row in session.query(RealtimeEvent)
            .filter(RealtimeEvent.entity == "session_command")
            .all()
            if row.entity_id == command_id
        ]


def test_a_retried_mutation_publishes_one_durable_event(client, tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    payload = {"text": "dedupe me", "operation_id": "op-dedupe"}
    first = client.post(
        f"/v1/sessions/{session_id}/message", json=payload, headers=ADMIN_AUTH
    ).json()
    client.post(f"/v1/sessions/{session_id}/message", json=payload, headers=ADMIN_AUTH)
    rows = _realtime_rows(first["command_id"])
    # One event per (command, state) - the chat stream and the Org channel -
    # and not a second pair for the retry.
    assert len(rows) == 2
    assert {row.topic.split("/")[-1] for row in rows} == {"chat", "sessions"}
    assert all(row.dedupe_key for row in rows)


def test_each_state_change_is_its_own_durable_event(tmp_path):
    session_id = _session(tmp_path, tool=INTERACTIVE_TOOL)
    session_channel.declare_interactive_tool(INTERACTIVE_TOOL)
    command, _created = commands_ctl.enqueue(
        session_id, commands_ctl.KIND_MESSAGE, text="states", operation_id="op-states"
    )
    commands_ctl.claim(command["command_id"], consumer="runtime:1")
    commands_ctl.acknowledge(command["command_id"], consumer="runtime:1", result="delivered")
    statuses = {
        row.payload_json.split('"status": "')[1].split('"')[0]
        for row in _realtime_rows(command["command_id"])
    }
    assert statuses == {"requested", "delivered", "acknowledged"}

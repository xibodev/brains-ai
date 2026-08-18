"""Tests for the executor messaging relay + webhook endpoints."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from brains.exec import relay


def test_inbound_reply_parsing_variants():
    assert relay._REPLY_RE.match("approve ASK-0001")
    assert relay._REPLY_RE.match("deny ASK-0002 too risky")
    assert relay._REPLY_RE.match("  YES ASK-0003  ")
    assert relay._REPLY_RE.match("ok ASK-0004") is not None
    assert relay._REPLY_RE.match("hello there") is None
    assert relay._REPLY_RE.match("approve everything") is None


def test_handle_inbound_reply_approves(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    from brains.control.decisions import file_decision_request, get_decision

    repo = tmp_path / "ws"
    repo.mkdir()
    code = file_decision_request(str(repo), title="[gate] approve outward action: git push")["code"]
    out = relay.handle_inbound_reply(f"approve {code} ship it")
    assert out == {"handled": True, "code": code, "status": "resolved"}
    assert get_decision(code)["status"] == "resolved"


def test_handle_inbound_reply_denies(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    from brains.control.decisions import file_decision_request, get_decision

    repo = tmp_path / "ws2"
    repo.mkdir()
    code = file_decision_request(str(repo), title="[gate] approve outward action: vercel deploy")[
        "code"
    ]
    out = relay.handle_inbound_reply(f"deny {code} not now")
    assert out["status"] == "rejected"
    assert get_decision(code)["status"] == "rejected"


def test_handle_inbound_reply_unknown_and_nonmatch():
    assert relay.handle_inbound_reply("random chatter") == {"handled": False}
    out = relay.handle_inbound_reply("approve ASK-9999")
    assert out["handled"] is True and "error" in out


def test_notify_pending_approval_uses_configured_bridges(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    sent: list[str] = []

    def _send(text: str, **_kwargs) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(
        relay,
        "_active_bridge_senders",
        lambda: [_send],
    )
    n = relay.notify_pending_approval("ASK-0007", "git push", "git push origin main", "/repo")
    assert n == 1
    assert "ASK-0007" in sent[0]
    assert "approve ASK-0007" in sent[0]


def test_trigger_triage_spawns_gated_session(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    captured: dict = {}

    def _fake_start(**kwargs):
        captured.update(kwargs)
        return "exec_fake123"

    import brains.exec.runner as runner

    monkeypatch.setattr(runner, "start_streamed_session", _fake_start)
    out = relay.trigger_triage(str(tmp_path), source="bugsink", payload="NullPointer in foo.py")
    assert out["exec_id"] == "exec_fake123"
    assert "bugsink" in captured["prompt"]
    assert "NullPointer in foo.py" in captured["prompt"]
    assert "gated for human approval" in captured["prompt"]


def test_relay_endpoints_require_token(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("BRAINS_RELAY_TOKEN", raising=False)
    from fastapi.testclient import TestClient

    from brains.main import app

    client = TestClient(app)
    # disabled when no token configured
    r = client.post("/relay/triage", json={"workspace": str(tmp_path)})
    assert r.status_code == 503


def test_relay_triage_endpoint_authed(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAINS_RELAY_TOKEN", "secret-relay")
    captured: dict = {}
    import brains.exec.relay as relaymod

    monkeypatch.setattr(
        relaymod, "trigger_triage", lambda **kw: captured.update(kw) or {"exec_id": "exec_x"}
    )
    from fastapi.testclient import TestClient

    from brains.main import app

    client = TestClient(app)
    # wrong token -> 401
    r = client.post(
        "/relay/triage", headers={"Authorization": "Bearer nope"}, json={"workspace": str(tmp_path)}
    )
    assert r.status_code == 401
    # right token -> spawns
    r = client.post(
        "/relay/triage",
        headers={"Authorization": "Bearer secret-relay"},
        json={"workspace": str(tmp_path), "source": "chatwoot", "payload": {"bug": "x"}},
    )
    assert r.status_code == 200
    assert r.json()["exec_id"] == "exec_x"
    assert captured["workspace"] == str(tmp_path)
    assert captured["source"] == "chatwoot"


def test_relay_reply_endpoint_extracts_whatsapp_text(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAINS_RELAY_TOKEN", "secret-relay")
    seen: dict = {}
    import brains.exec.relay as relaymod

    monkeypatch.setattr(
        relaymod, "handle_inbound_reply", lambda msg: seen.update(text=msg) or {"handled": True}
    )
    from fastapi.testclient import TestClient

    from brains.main import app

    client = TestClient(app)
    wa = {
        "entry": [{"changes": [{"value": {"messages": [{"text": {"body": "approve ASK-0005"}}]}}]}]
    }
    r = client.post("/relay/reply", headers={"Authorization": "Bearer secret-relay"}, json=wa)
    assert r.status_code == 200
    assert seen["text"] == "approve ASK-0005"


def test_relay_reply_deduplicates_before_repeating_effect(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    relay_token = "relay-test-token"
    monkeypatch.setenv("BRAINS_RELAY_TOKEN", relay_token)
    calls: list[str] = []
    import brains.exec.relay as relaymod

    monkeypatch.setattr(
        relaymod,
        "handle_inbound_reply",
        lambda msg: calls.append(msg) or {"handled": True},
    )
    from fastapi.testclient import TestClient

    from brains.main import app

    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {relay_token}",
        "X-Dedupe-Key": "reply-dedupe",
    }
    first = client.post("/relay/reply", headers=headers, json={"message": "approve ASK-0005"})
    replay = client.post("/relay/reply", headers=headers, json={"message": "approve ASK-0005"})
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert calls == ["approve ASK-0005"]


def test_bridge_delivery_results_are_durable(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    sent: list[str] = []

    def _ok(text: str) -> bool:
        sent.append(text)
        return True

    _ok._brains_bridge_name = "telegram"  # type: ignore[attr-defined]
    monkeypatch.setattr(relay, "_active_bridge_senders", lambda: [_ok])
    assert relay.notify_pending_approval("ASK-0010", "git push", "push branch", "/repo") == 1
    assert relay.notify_pending_approval("ASK-0010", "git push", "push branch", "/repo") == 1
    assert len(sent) == 1

    def _fail(_text: str) -> bool:
        raise RuntimeError("upstream unavailable")

    _fail._brains_bridge_name = "slack"  # type: ignore[attr-defined]
    monkeypatch.setattr(relay, "_active_bridge_senders", lambda: [_fail])
    assert relay.notify_pending_approval("ASK-0011", "deploy", "deploy UAT", "/repo") == 0

    def _false(_text: str) -> bool:
        return False

    _false._brains_bridge_name = "whatsapp"  # type: ignore[attr-defined]
    monkeypatch.setattr(relay, "_active_bridge_senders", lambda: [_false])
    assert relay.notify_pending_approval("ASK-0012", "deploy", "deploy UAT", "/repo") == 0

    from brains.storage.db import SessionLocal
    from brains.storage.models import IntegrationDelivery

    with SessionLocal() as session:
        rows = {
            row.channel: row.status
            for row in session.query(IntegrationDelivery)
            .filter(IntegrationDelivery.direction == "outbound")
            .all()
        }
    assert rows["telegram"] == "completed"
    assert rows["slack"] == "failed"
    assert rows["whatsapp"] == "failed"


def test_failed_delivery_is_reclaimed_by_only_one_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    from brains.control import integration_deliveries as deliveries_ctl

    key = f"retry-{uuid.uuid4()}"
    delivery, created = deliveries_ctl.claim("relay_reply", "inbound", key)
    assert created is True
    deliveries_ctl.settle(
        delivery["id"],
        "failed",
        attempt=delivery["attempts"],
        detail="test failure",
    )

    def _retry() -> tuple[dict, bool]:
        return deliveries_ctl.claim(
            "relay_reply",
            "inbound",
            key,
            retry_failed=True,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: _retry(), range(2)))
    assert sum(created for _delivery, created in results) == 1
    claimed = next(row for row, created in results if created)
    assert claimed["status"] == "processing"
    assert claimed["attempts"] == 2
    with pytest.raises(deliveries_ctl.IntegrationDeliveryOwnershipError):
        deliveries_ctl.settle(
            delivery["id"],
            "completed",
            attempt=delivery["attempts"],
            result={"stale": True},
        )
    from brains.control.common import utc_now
    from brains.storage.db import SessionLocal
    from brains.storage.models import IntegrationDelivery

    with SessionLocal() as session:
        row = session.get(IntegrationDelivery, delivery["id"])
        assert row is not None
        row.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
    expired, expired_created = deliveries_ctl.claim(
        "relay_reply",
        "inbound",
        key,
        retry_failed=True,
    )
    assert expired_created is False
    assert expired["status"] == "processing"
    deliveries_ctl.settle(
        delivery["id"],
        "failed",
        attempt=claimed["attempts"],
        detail="released_by_operator",
    )
    reclaimed, reclaimed_created = deliveries_ctl.claim(
        "relay_reply",
        "inbound",
        key,
        retry_failed=True,
    )
    assert reclaimed_created is True
    assert reclaimed["attempts"] == 3
    with pytest.raises(deliveries_ctl.IntegrationDeliveryOwnershipError):
        deliveries_ctl.settle(
            delivery["id"],
            "completed",
            attempt=claimed["attempts"],
            result={"stale": True},
        )

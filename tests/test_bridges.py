"""Tests for the operator-control bridges (Telegram, Slack, WhatsApp).

We never hit live APIs. Each bridge exposes ``build_sender`` with injectable
inputs (token strings, httpx transports) so we can stub the outbound call
and assert the formatted payload.

For the SDK-based bridges (Telegram, Slack) we install a fake module in
``sys.modules`` so the deferred import inside ``build_sender`` resolves to
our stub. This lets us validate the bridge wiring without requiring the
real ``python-telegram-bot`` / ``slack_sdk`` to be installed.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import httpx
import pytest

from brains.bridges import (
    BRIDGE_MODULES,
    BridgeKind,
    BridgeMessage,
    enabled_bridges,
    get_bridge,
    send_message,
    status_all,
)
from brains.bridges.base import BridgeStatus

# --- Shared base ----------------------------------------------------------


def test_send_message_formats_kind_prefix_and_correlation() -> None:
    captured: list[str] = []

    def fake_sender(body: str) -> bool:
        captured.append(body)
        return True

    msg = BridgeMessage(
        kind=BridgeKind.APPROVAL,
        text="Nightly summary needs your sign-off.",
        correlation_id="decision-7a3f",
    )
    assert send_message(fake_sender, msg) is True
    assert captured == ["[APPROVAL] Nightly summary needs your sign-off.\nref: decision-7a3f"]


def test_send_message_omits_ref_when_no_correlation() -> None:
    captured: list[str] = []
    send_message(
        lambda body: bool(captured.append(body) or True),
        BridgeMessage(kind=BridgeKind.INFO, text="hi"),
    )
    assert captured == ["[INFO] hi"]


# --- Registry -------------------------------------------------------------


def test_bridge_modules_registered() -> None:
    assert set(BRIDGE_MODULES) == {"telegram", "slack", "whatsapp", "whatsapp_web"}


def test_get_bridge_imports_module() -> None:
    mod = get_bridge("telegram")
    assert mod.NAME == "telegram"
    assert callable(mod.send)
    assert callable(mod.status)
    assert callable(mod.build_sender)


def test_get_bridge_unknown_name_raises() -> None:
    with pytest.raises(KeyError):
        get_bridge("not-a-bridge")


# --- enabled_bridges + status_all (no extras needed) ---------------------


class _SubsysStub:
    def __init__(self, telegram=False, slack=False, whatsapp=False, whatsapp_web=False):
        self.telegram = types.SimpleNamespace(enabled=telegram)
        self.slack = types.SimpleNamespace(enabled=slack)
        self.whatsapp = types.SimpleNamespace(enabled=whatsapp)
        self.whatsapp_web = types.SimpleNamespace(enabled=whatsapp_web)


class _SettingsStub:
    def __init__(self, telegram=False, slack=False, whatsapp=False, whatsapp_web=False):
        self.subsystems = types.SimpleNamespace(
            bridges=_SubsysStub(
                telegram=telegram, slack=slack, whatsapp=whatsapp, whatsapp_web=whatsapp_web
            )
        )


def test_enabled_bridges_reads_overlay_flags() -> None:
    assert enabled_bridges(_SettingsStub()) == []
    assert enabled_bridges(_SettingsStub(telegram=True)) == ["telegram"]
    assert sorted(enabled_bridges(_SettingsStub(slack=True, whatsapp=True))) == [
        "slack",
        "whatsapp",
    ]


def test_status_all_returns_status_for_every_bridge() -> None:
    statuses = status_all(_SettingsStub())
    names = {s.name for s in statuses}
    assert names == {"telegram", "slack", "whatsapp", "whatsapp_web"}
    for s in statuses:
        assert isinstance(s, BridgeStatus)
        assert s.enabled is False
        assert s.configured is False


# --- Telegram bridge ------------------------------------------------------


@pytest.fixture
def fake_telegram(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Install a fake ``telegram`` module so build_sender works without the extra."""
    calls: list[dict[str, Any]] = []

    class _Bot:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, chat_id: str, text: str) -> None:
            calls.append({"token": self.token, "chat_id": chat_id, "text": text})

    fake_module = types.ModuleType("telegram")
    fake_module.Bot = _Bot
    monkeypatch.setitem(sys.modules, "telegram", fake_module)
    return calls


def test_telegram_build_sender_requires_token_and_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains.bridges import telegram as tg

    monkeypatch.delenv("BRAINS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BRAINS_TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(RuntimeError, match="BRAINS_TELEGRAM_BOT_TOKEN"):
        tg.build_sender()
    with pytest.raises(RuntimeError, match="BRAINS_TELEGRAM_CHAT_ID"):
        tg.build_sender(token="tok")


def test_telegram_send_uses_injected_sender(
    fake_telegram: list[dict[str, Any]],
) -> None:
    """``send`` accepts an injected sender, bypassing build_sender entirely."""
    from brains.bridges import telegram as tg

    captured: list[str] = []
    ok = tg.send(
        "ready for sign-off",
        kind=BridgeKind.APPROVAL,
        correlation_id="d-42",
        sender=lambda body: bool(captured.append(body) or True),
    )
    assert ok is True
    assert captured == ["[APPROVAL] ready for sign-off\nref: d-42"]
    # The injected sender path should NOT have touched the fake_telegram bot.
    assert fake_telegram == []


def test_telegram_build_sender_round_trips_through_fake_bot(
    fake_telegram: list[dict[str, Any]],
) -> None:
    from brains.bridges import telegram as tg

    sender = tg.build_sender(token="testtok", chat_id="testchat")
    assert sender("[INFO] hi") is True
    assert fake_telegram == [{"token": "testtok", "chat_id": "testchat", "text": "[INFO] hi"}]


def test_telegram_status_reflects_env_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains.bridges import telegram as tg

    monkeypatch.delenv("BRAINS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BRAINS_TELEGRAM_CHAT_ID", raising=False)
    snap = tg.status(_SettingsStub(telegram=True))
    assert snap.enabled is True
    assert snap.configured is False
    assert "BRAINS_TELEGRAM_BOT_TOKEN" in snap.detail

    monkeypatch.setenv("BRAINS_TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("BRAINS_TELEGRAM_CHAT_ID", "y")
    snap2 = tg.status(_SettingsStub(telegram=True))
    assert snap2.configured is True
    assert snap2.detail == "ready"


# --- Slack bridge ---------------------------------------------------------


@pytest.fixture
def fake_slack(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _WebClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def chat_postMessage(self, channel: str, text: str) -> dict[str, Any]:
            calls.append({"token": self.token, "channel": channel, "text": text})
            return {"ok": True}

    class _SlackApiError(Exception):
        def __init__(self, message: str, response: dict[str, Any]):
            super().__init__(message)
            self.response = response

    fake_sdk = types.ModuleType("slack_sdk")
    fake_sdk.WebClient = _WebClient
    fake_errors = types.ModuleType("slack_sdk.errors")
    fake_errors.SlackApiError = _SlackApiError
    monkeypatch.setitem(sys.modules, "slack_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "slack_sdk.errors", fake_errors)
    return calls


def test_slack_build_sender_requires_token_and_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains.bridges import slack

    monkeypatch.delenv("BRAINS_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BRAINS_SLACK_CHANNEL", raising=False)
    with pytest.raises(RuntimeError, match="BRAINS_SLACK_BOT_TOKEN"):
        slack.build_sender()
    with pytest.raises(RuntimeError, match="BRAINS_SLACK_CHANNEL"):
        slack.build_sender(token="xoxb-x")


def test_slack_build_sender_round_trips(
    fake_slack: list[dict[str, Any]],
) -> None:
    from brains.bridges import slack

    sender = slack.build_sender(token="xoxb-x", channel="#ops")
    assert sender("[ALERT] disk full") is True
    assert fake_slack == [{"token": "xoxb-x", "channel": "#ops", "text": "[ALERT] disk full"}]


def test_slack_status_reflects_env_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains.bridges import slack

    monkeypatch.delenv("BRAINS_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BRAINS_SLACK_CHANNEL", raising=False)
    snap = slack.status(_SettingsStub(slack=True))
    assert snap.enabled is True
    assert snap.configured is False

    monkeypatch.setenv("BRAINS_SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("BRAINS_SLACK_CHANNEL", "#general")
    snap2 = slack.status(_SettingsStub(slack=True))
    assert snap2.configured is True


# --- WhatsApp bridge ------------------------------------------------------


def test_whatsapp_build_sender_requires_all_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains.bridges import whatsapp as wa

    for name in (
        "BRAINS_WHATSAPP_TOKEN",
        "BRAINS_WHATSAPP_PHONE_ID",
        "BRAINS_WHATSAPP_RECIPIENT",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="BRAINS_WHATSAPP_TOKEN"):
        wa.build_sender()


def test_whatsapp_build_sender_posts_expected_payload() -> None:
    from brains.bridges import whatsapp as wa

    captured: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append((str(request.url), body, dict(request.headers)))
        return httpx.Response(200, json={"messages": [{"id": "wamid.abc"}]})

    transport = httpx.MockTransport(_handler)
    sender = wa.build_sender(
        token="meta-tok",
        phone_id="1234567890",
        recipient="+447700900123",
        transport=transport,
    )
    assert sender("[ALERT] db down") is True
    assert captured, "expected an outbound HTTP call"
    url, body, headers = captured[0]
    assert "graph.facebook.com" in url
    assert "/1234567890/messages" in url
    assert body == {
        "messaging_product": "whatsapp",
        "to": "+447700900123",
        "type": "text",
        "text": {"body": "[ALERT] db down"},
    }
    assert headers["authorization"] == "Bearer meta-tok"


def test_whatsapp_send_propagates_http_errors() -> None:
    from brains.bridges import whatsapp as wa

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad recipient"}})

    transport = httpx.MockTransport(_handler)
    sender = wa.build_sender(token="t", phone_id="p", recipient="r", transport=transport)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        sender("test")


def test_whatsapp_status_reflects_env_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains.bridges import whatsapp as wa

    for name in (
        "BRAINS_WHATSAPP_TOKEN",
        "BRAINS_WHATSAPP_PHONE_ID",
        "BRAINS_WHATSAPP_RECIPIENT",
    ):
        monkeypatch.delenv(name, raising=False)
    snap = wa.status(_SettingsStub(whatsapp=True))
    assert snap.enabled is True
    assert snap.configured is False
    assert "BRAINS_WHATSAPP_TOKEN" in snap.detail

    monkeypatch.setenv("BRAINS_WHATSAPP_TOKEN", "tok")
    monkeypatch.setenv("BRAINS_WHATSAPP_PHONE_ID", "pid")
    monkeypatch.setenv("BRAINS_WHATSAPP_RECIPIENT", "+1")
    snap2 = wa.status(_SettingsStub(whatsapp=True))
    assert snap2.configured is True


# --- WhatsApp Web bridge (wa-web sidecar) --------------------------------


def test_whatsapp_web_build_sender_requires_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains.bridges import whatsapp_web as ww

    monkeypatch.delenv("BRAINS_WHATSAPP_WEB_URL", raising=False)
    with pytest.raises(RuntimeError, match="BRAINS_WHATSAPP_WEB_URL"):
        ww.build_sender()


def test_whatsapp_web_build_sender_posts_to_sidecar() -> None:
    from brains.bridges import whatsapp_web as ww

    captured: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append((str(request.url), body, dict(request.headers)))
        return httpx.Response(200, json={"sent": True, "id": "ABC123"})

    transport = httpx.MockTransport(_handler)
    sender = ww.build_sender(
        url="http://localhost:8788/",
        token="send-tok",
        transport=transport,
    )
    assert sender("[APPROVAL] push to main?") is True
    assert captured, "expected an outbound HTTP call"
    url, body, headers = captured[0]
    assert url == "http://localhost:8788/send"
    assert body == {"text": "[APPROVAL] push to main?"}
    assert headers["authorization"] == "Bearer send-tok"


def test_whatsapp_web_build_sender_omits_auth_without_token() -> None:
    from brains.bridges import whatsapp_web as ww

    captured: list[dict[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        return httpx.Response(200, json={"sent": True})

    transport = httpx.MockTransport(_handler)
    sender = ww.build_sender(url="http://localhost:8788", token=None, transport=transport)
    assert sender("hi") is True
    assert "authorization" not in captured[0]


def test_whatsapp_web_send_propagates_http_errors() -> None:
    from brains.bridges import whatsapp_web as ww

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "not linked"})

    transport = httpx.MockTransport(_handler)
    sender = ww.build_sender(url="http://localhost:8788", token="t", transport=transport)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        sender("test")


def test_whatsapp_web_status_reflects_env_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains.bridges import whatsapp_web as ww

    monkeypatch.delenv("BRAINS_WHATSAPP_WEB_URL", raising=False)
    snap = ww.status(_SettingsStub(whatsapp_web=True))
    assert snap.enabled is True
    assert snap.configured is False
    assert "BRAINS_WHATSAPP_WEB_URL" in snap.detail

    monkeypatch.setenv("BRAINS_WHATSAPP_WEB_URL", "http://localhost:8788")
    snap2 = ww.status(_SettingsStub(whatsapp_web=True))
    assert snap2.configured is True
    assert snap2.detail == "ready"

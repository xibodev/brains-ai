from __future__ import annotations

import pytest

from brains.control import webhooks
from brains.control.recurring import create_recurring_task, list_recurring_runs
from brains.control.sessions import register_workspace


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "wh"
    path.mkdir()
    register_workspace(str(path))
    return str(path)


@pytest.fixture
def names(request):
    """Unique definition + slug per test (the test DB is shared in-process)."""
    suffix = abs(hash(request.node.name)) % 100000
    return {"definition": f"deploy-notify-{suffix}", "slug": f"ci-deploy-{suffix}"}


def _make_definition(workspace, name):
    create_recurring_task(workspace, name, "Deploy {date}", cron_expr="manual")
    return name


def test_create_trigger_returns_token_once(workspace, names):
    _make_definition(workspace, names["definition"])
    res = webhooks.create_webhook_trigger(names["slug"], names["definition"])
    assert res["token"].startswith("whk_")
    assert res["url"] == f"/hooks/{names['slug']}"
    # The token never appears in the listing.
    listed = webhooks.list_webhook_triggers()
    assert all("token" not in t for t in listed)


def test_create_trigger_rejects_unknown_definition(workspace):
    with pytest.raises(ValueError, match="unknown recurring task"):
        webhooks.create_webhook_trigger("x-unknown", "does-not-exist")


def test_delivery_fires_task(workspace, names):
    _make_definition(workspace, names["definition"])
    created = webhooks.create_webhook_trigger(names["slug"], names["definition"])
    res = webhooks.deliver_webhook(
        names["slug"], created["token"], payload={"event": "push"}, dedupe_key="abc"
    )
    assert res["status"] == "fired"
    assert res["task_code"]
    runs = list_recurring_runs(names["definition"])
    assert runs[0]["source"] == "webhook"


def test_delivery_is_idempotent_on_dedupe_key(workspace, names):
    _make_definition(workspace, names["definition"])
    created = webhooks.create_webhook_trigger(names["slug"], names["definition"])
    first = webhooks.deliver_webhook(
        names["slug"], created["token"], payload={"event": "push"}, dedupe_key="same"
    )
    second = webhooks.deliver_webhook(
        names["slug"], created["token"], payload={"event": "push"}, dedupe_key="same"
    )
    assert first["status"] == "fired"
    assert second["status"] == "duplicate"
    assert second["task_code"] == first["task_code"]
    # Only one task minted.
    assert len(list_recurring_runs(names["definition"])) == 1


def test_delivery_rejects_bad_token(workspace, names):
    _make_definition(workspace, names["definition"])
    webhooks.create_webhook_trigger(names["slug"], names["definition"])
    with pytest.raises(webhooks.WebhookAuthError):
        webhooks.deliver_webhook(names["slug"], "whk_wrong", payload={})


def test_delivery_unknown_slug(workspace):
    with pytest.raises(webhooks.WebhookNotFound):
        webhooks.deliver_webhook("nope-slug", "whk_x", payload={})


def test_event_filter_gates_delivery(workspace, names):
    _make_definition(workspace, names["definition"])
    created = webhooks.create_webhook_trigger(
        names["slug"], names["definition"], event_filter="event=push"
    )
    filtered = webhooks.deliver_webhook(
        names["slug"], created["token"], payload={"event": "ping"}, dedupe_key="1"
    )
    assert filtered["status"] == "filtered"
    fired = webhooks.deliver_webhook(
        names["slug"], created["token"], payload={"event": "push"}, dedupe_key="2"
    )
    assert fired["status"] == "fired"


def test_disabled_trigger_is_not_found(workspace, names):
    _make_definition(workspace, names["definition"])
    created = webhooks.create_webhook_trigger(names["slug"], names["definition"])
    webhooks.set_webhook_enabled(names["slug"], False)
    with pytest.raises(webhooks.WebhookNotFound):
        webhooks.deliver_webhook(names["slug"], created["token"], payload={})

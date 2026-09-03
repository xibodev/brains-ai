from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from brains.audit import list_entries
from brains.authz import credentials as creds
from brains.config import reload_settings, settings
from brains.control import orgs as orgs_ctl
from brains.control.configuration import (
    ConfigurationConflict,
    ConfigurationError,
    apply_configuration,
    configuration_summary,
)
from brains.control.operators import add_operator, ensure_admin_operator
from brains.main import app
from brains.storage.migrations import init_db


@pytest.fixture
def isolated_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "runtime.yaml"
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(path))
    reload_settings()
    yield path
    monkeypatch.delenv("BRAINS_RUNTIME_OVERLAY")
    reload_settings()


@pytest.fixture
def client() -> TestClient:
    init_db()
    ensure_admin_operator()
    creds.sync_local_credentials()
    return TestClient(app)


def _member_headers() -> dict[str, str]:
    org = orgs_ctl.create_org("config-member-org", "Config member org")
    record, key = add_operator("config-member")
    orgs_ctl.add_member(org["id"], record["slug"], role="member")
    creds.sync_local_credentials()
    return {"Authorization": f"Bearer {key}"}


def test_summary_is_positive_redacted_core_manifest(
    isolated_overlay: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "synthetic-secret-that-must-not-escape"
    isolated_overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "rate_limit_per_minute": 17,
                "openai_compatible_api_key": secret,
                "smtp_password": secret,
                "subsystems": {"storage": {"backend": "sqlite"}},
            }
        ),
        encoding="utf-8",
    )
    reload_settings()
    monkeypatch.setattr(
        "brains.wire.status",
        lambda _home: {
            "tools": [
                {
                    "tool": "codex",
                    "detected": True,
                    "mcp_wired": True,
                    "mcp_transport": "streamable-http",
                    "rule_wired": True,
                    "mailbox_notification_mode": "pull",
                    "mcp_path": secret,
                    "mcp_url": secret,
                }
            ]
        },
    )

    result = configuration_summary()
    keys = {field["key"] for field in result["fields"]}
    assert keys == {
        "service.authentication",
        "service.binding",
        "service.gateway_port",
        "service.rate_limit_per_minute",
        "mcp.transport",
        "mcp.port",
        "sqlite.backend",
        "sqlite.database",
        "sqlite.busy_timeout_ms",
        "sqlite.enforce_foreign_keys",
    }
    rendered = str(result)
    for forbidden in (secret, "provider", "email", "bridge", "gateway_preamble", "db_url"):
        assert forbidden not in rendered.lower()
    assert result["harnesses"] == [
        {
            "tool": "codex",
            "detected": True,
            "mcp_wired": True,
            "mcp_transport": "streamable-http",
            "rule_wired": True,
            "mailbox_notification_mode": "pull",
        }
    ]


def test_live_and_restart_changes_are_atomic_reloadable_and_attributable(
    isolated_overlay: Path,
) -> None:
    before = configuration_summary()
    live = apply_configuration(
        {"service.rate_limit_per_minute": 29},
        expected_revision=before["revision"],
        actor="operator:config-test",
    )
    assert live["apply_mode"] == "live_reload"
    assert live["restart_required"] is False
    assert settings.rate_limit_per_minute == 29

    restarted = apply_configuration(
        {"sqlite.busy_timeout_ms": 4321, "sqlite.enforce_foreign_keys": True},
        expected_revision=live["revision"],
        actor="operator:config-test",
    )
    assert restarted["apply_mode"] == "restart_required"
    assert restarted["restart_required"] is True
    assert settings.sqlite_busy_timeout_ms == 4321
    assert settings.sqlite_enforce_foreign_keys is True
    persisted = yaml.safe_load(isolated_overlay.read_text(encoding="utf-8"))
    assert persisted["rate_limit_per_minute"] == 29
    assert persisted["sqlite_busy_timeout_ms"] == 4321
    audit = list_entries(action_prefix="config.core_update")
    assert any(row["actor"] == "operator:config-test" for row in audit)
    assert all("4321" not in str(row["payload"]) for row in audit)


@pytest.mark.parametrize(
    "changes",
    [
        {"smtp_password": "synthetic-secret"},
        {"openai_compatible_api_key": "synthetic-secret"},
        {"subsystems.bridges.slack.enabled": True},
        {"unknown": "synthetic-secret"},
        {"sqlite.busy_timeout_ms": "synthetic-secret"},
    ],
)
def test_hidden_secret_frozen_and_invalid_fields_fail_without_mutation_or_disclosure(
    isolated_overlay: Path, changes: dict[str, object]
) -> None:
    before = configuration_summary()
    with pytest.raises(ConfigurationError) as caught:
        apply_configuration(
            changes,
            expected_revision=before["revision"],
            actor="operator:config-test",
        )
    assert "synthetic-secret" not in str(caught.value)
    assert not isolated_overlay.exists()
    assert configuration_summary()["revision"] == before["revision"]


def test_stale_revision_refuses_without_overwrite(isolated_overlay: Path) -> None:
    current = configuration_summary()
    apply_configuration(
        {"service.rate_limit_per_minute": 4},
        expected_revision=current["revision"],
        actor="operator:first",
    )
    with pytest.raises(ConfigurationConflict, match="reload"):
        apply_configuration(
            {"service.rate_limit_per_minute": 5},
            expected_revision=current["revision"],
            actor="operator:second",
        )
    assert settings.rate_limit_per_minute == 4


def test_failed_reload_restores_previous_file_and_effective_state(
    isolated_overlay: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_configuration(
        {"service.rate_limit_per_minute": 7},
        expected_revision=configuration_summary()["revision"],
        actor="operator:baseline",
    )
    original = isolated_overlay.read_bytes()
    original_reload = reload_settings
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic apply failure")
        return original_reload()

    monkeypatch.setattr("brains.config.reload_settings", fail_once)
    with pytest.raises(ConfigurationError, match="restored"):
        apply_configuration(
            {"service.rate_limit_per_minute": 8},
            expected_revision=configuration_summary()["revision"],
            actor="operator:rollback",
        )
    assert isolated_overlay.read_bytes() == original
    assert settings.rate_limit_per_minute == 7
    failures = list_entries(action_prefix="config.core_update.failed")
    assert failures and failures[0]["payload"]["rollback"] == "restored"


def test_api_requires_bootstrap_admin_and_never_echoes_rejected_value(
    client: TestClient, auth_headers: dict[str, str], isolated_overlay: Path
) -> None:
    assert client.get("/v1/operator/configuration").status_code == 401
    member = _member_headers()
    assert client.get("/v1/operator/configuration", headers=member).status_code == 403
    assert (
        client.put(
            "/v1/operator/configuration",
            headers=member,
            json={"expected_revision": "0" * 64, "changes": {"service.rate_limit_per_minute": 3}},
        ).status_code
        == 403
    )
    summary = client.get("/v1/operator/configuration", headers=auth_headers)
    assert summary.status_code == 200
    secret = "synthetic-api-secret"
    rejected = client.put(
        "/v1/operator/configuration",
        headers=auth_headers,
        json={
            "expected_revision": summary.json()["revision"],
            "changes": {"smtp_password": secret},
        },
    )
    assert rejected.status_code == 400
    assert secret not in rejected.text
    assert not isolated_overlay.exists()

    applied = client.put(
        "/v1/operator/configuration",
        headers=auth_headers,
        json={
            "expected_revision": summary.json()["revision"],
            "changes": {"service.rate_limit_per_minute": 31},
        },
    )
    assert applied.status_code == 200
    entry = next(
        row
        for row in list_entries(action_prefix="config.core_update")
        if row["id"] == applied.json()["audit_id"]
    )
    assert entry["actor"] == "operator:admin"

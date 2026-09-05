from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

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
                    "tool": "claude-code",
                    "detected": True,
                    "mcp_wired": True,
                    "mcp_transport": "streamable-http",
                    "rule_wired": True,
                    "mailbox_notification_mode": "turn_boundary",
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
            "tool": "claude-code",
            "detected": True,
            "mcp_wired": True,
            "mcp_transport": "streamable-http",
            "rule_wired": True,
            "mailbox_notification_mode": "turn_boundary",
        }
    ]


def _race_configuration_write(
    revision: str,
    value: int,
    barrier: Any,
    outcomes: Any,
) -> None:
    barrier.wait()
    try:
        result = apply_configuration(
            {"service.rate_limit_per_minute": value},
            expected_revision=revision,
            actor=f"operator:race-{value}",
        )
    except ConfigurationConflict:
        outcomes.put(("conflict", value))
    else:
        outcomes.put(("applied", value, result["revision"]))


def test_supported_changes_are_atomic_restart_required_and_attributable(
    isolated_overlay: Path,
) -> None:
    before = configuration_summary()
    previous_rate = settings.rate_limit_per_minute
    new_rate = 29 if previous_rate != 29 else 30
    first = apply_configuration(
        {"service.rate_limit_per_minute": new_rate},
        expected_revision=before["revision"],
        actor="operator:config-test",
    )
    assert first["apply_mode"] == "restart_required"
    assert first["reload_applied"] is False
    assert first["restart_required"] is True
    assert settings.rate_limit_per_minute == previous_rate
    summary = configuration_summary()
    assert (
        next(
            field["value"]
            for field in summary["fields"]
            if field["key"] == "service.rate_limit_per_minute"
        )
        == new_rate
    )

    restarted = apply_configuration(
        {"sqlite.busy_timeout_ms": 4321, "sqlite.enforce_foreign_keys": True},
        expected_revision=first["revision"],
        actor="operator:config-test",
    )
    assert restarted["apply_mode"] == "restart_required"
    assert restarted["restart_required"] is True
    persisted = yaml.safe_load(isolated_overlay.read_text(encoding="utf-8"))
    assert persisted["rate_limit_per_minute"] == new_rate
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
    persisted = yaml.safe_load(isolated_overlay.read_text(encoding="utf-8"))
    assert persisted["rate_limit_per_minute"] == 4
    conflicts = list_entries(action_prefix="config.core_update.conflict")
    assert conflicts and conflicts[0]["actor"] == "operator:second"


def test_simultaneous_processes_have_one_cas_winner_and_audited_outcomes(
    isolated_overlay: Path,
) -> None:
    init_db()
    revision = configuration_summary()["revision"]
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    outcomes = context.Queue()
    workers = [
        context.Process(
            target=_race_configuration_write,
            args=(revision, value, barrier, outcomes),
        )
        for value in (41, 42)
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0

    results = [outcomes.get(timeout=2) for _ in workers]
    assert sorted(result[0] for result in results) == ["applied", "conflict"]
    winner = next(result for result in results if result[0] == "applied")
    payload = yaml.safe_load(isolated_overlay.read_text(encoding="utf-8"))
    assert payload["rate_limit_per_minute"] == winner[1]
    assert configuration_summary()["revision"] == winner[2]
    audit = list_entries(action_prefix="config.core_update")
    loser = next(result for result in results if result[0] == "conflict")
    outcomes_by_actor = {(row["actor"], row["action"]) for row in audit}
    assert (f"operator:race-{winner[1]}", "config.core_update") in outcomes_by_actor
    assert (
        f"operator:race-{loser[1]}",
        "config.core_update.conflict",
    ) in outcomes_by_actor


def test_failed_apply_restores_previous_file_and_effective_state(
    isolated_overlay: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_configuration(
        {"service.rate_limit_per_minute": 7},
        expected_revision=configuration_summary()["revision"],
        actor="operator:baseline",
    )
    original = isolated_overlay.read_bytes()
    calls = 0

    from brains.audit import record_required as original_record

    def fail_success_audit(*, actor: str, action: str, payload: dict[str, Any]) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic apply failure")
        return original_record(actor=actor, action=action, payload=payload)

    monkeypatch.setattr("brains.audit.record_required", fail_success_audit)
    with pytest.raises(ConfigurationError, match="restored"):
        apply_configuration(
            {"service.rate_limit_per_minute": 8},
            expected_revision=configuration_summary()["revision"],
            actor="operator:rollback",
        )
    assert isolated_overlay.read_bytes() == original
    effective = configuration_summary()
    assert (
        next(
            field["value"]
            for field in effective["fields"]
            if field["key"] == "service.rate_limit_per_minute"
        )
        == 7
    )
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

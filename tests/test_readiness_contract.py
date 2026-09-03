"""Acceptance-level contracts for bounded operational readiness."""

from __future__ import annotations

import sqlite3

import pytest

from brains.control import operations
from brains.control import readiness as readiness_module


def test_sqlite_integrity_runs_quick_full_and_foreign_key_checks(tmp_path, monkeypatch):
    database = tmp_path / "ready.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER REFERENCES parent(id)
            );
            INSERT INTO child (id, parent_id) VALUES (1, 99);
            """
        )
    import brains.storage.integrity as integrity

    monkeypatch.setattr(integrity, "resolve_sqlite_path", lambda *_args: database)
    report = readiness_module.sqlite_integrity_status()
    assert report == {
        "ready": False,
        "reason": "foreign-key-violations",
        "quick_check_ok": True,
        "integrity_check_ok": True,
        "foreign_key_violations": 1,
    }


def test_sqlite_integrity_failure_is_stable_and_secret_free(tmp_path, monkeypatch):
    missing = tmp_path / "private-name.sqlite"
    import brains.storage.integrity as integrity

    monkeypatch.setattr(integrity, "resolve_sqlite_path", lambda: missing)
    report = readiness_module.sqlite_integrity_status()
    assert report == {"ready": False, "reason": "database-unavailable"}
    assert str(missing) not in str(report)


def test_sqlite_readiness_rejects_withdrawn_runtime_backend(monkeypatch):
    import brains.storage.integrity as integrity

    monkeypatch.setattr(
        integrity,
        "resolve_sqlite_path",
        lambda: (_ for _ in ()).throw(integrity.UnsupportedDatabaseError("postgres")),
    )
    assert readiness_module.sqlite_integrity_status() == {
        "ready": False,
        "reason": "unsupported-runtime-backend",
    }


def test_backup_candidate_failure_does_not_expose_configured_path(tmp_path):
    missing = tmp_path / "private-candidate-name.tar.gz"
    report = readiness_module.backup_candidate_status(missing)
    assert report == {
        "ready": False,
        "configured": True,
        "reason": "candidate-unavailable",
    }
    assert str(missing) not in str(report)


def test_mcp_readiness_uses_only_authenticated_mcp_protocol(monkeypatch):
    import brains.service.common as common

    calls: list[tuple[str, int, float]] = []
    monkeypatch.setattr(
        common,
        "read_service_config",
        lambda: {"gateway_host": "127.0.0.1", "gateway_port": 1, "mcp_port": 43210},
    )
    monkeypatch.setattr(
        common,
        "mcp_protocol_status",
        lambda host, port, timeout: (
            calls.append((host, port, timeout))
            or {"ready": True, "stage": "tools/list", "reason": "ok", "tool_count": 4}
        ),
    )
    report = readiness_module.mcp_protocol_readiness()
    assert report == {
        "ready": True,
        "stage": "tools/list",
        "reason": "ok",
        "tool_count": 4,
    }
    assert calls == [("127.0.0.1", 43210, 1.0)]


def test_last_drill_ignores_attempts_and_failures(monkeypatch):
    import brains.audit as audit

    monkeypatch.setattr(audit, "assert_chain_intact", lambda: None)
    monkeypatch.setattr(
        audit,
        "list_entries",
        lambda **_kwargs: [
            {"action": "admin.recovery_drill.failed", "created_at": "later"},
            {"action": "admin.recovery_drill.attempted", "created_at": "earlier"},
        ],
    )
    assert readiness_module.last_restore_drill_status() == {
        "verified": False,
        "reason": "no-successful-drill-recorded",
        "at": None,
    }


def test_last_drill_requires_audited_candidate_and_restore_proof(monkeypatch):
    import brains.audit as audit

    monkeypatch.setattr(audit, "assert_chain_intact", lambda: None)
    monkeypatch.setattr(
        audit,
        "list_entries",
        lambda **_kwargs: [
            {
                "action": "admin.recovery_drill",
                "created_at": "2026-01-01T00:00:00Z",
                "payload": {
                    "candidate_verified": True,
                    "restore_verified": True,
                    "rollback_verified": True,
                    "data_fingerprint": "synthetic-fingerprint",
                },
            }
        ],
    )
    assert readiness_module.last_restore_drill_status() == {
        "verified": True,
        "reason": "successful-drill-recorded",
        "at": "2026-01-01T00:00:00Z",
        "data_fingerprint": "synthetic-fingerprint",
    }


def test_last_drill_fails_closed_when_audit_chain_is_not_verifiable(monkeypatch):
    import brains.audit as audit

    monkeypatch.setattr(
        audit,
        "assert_chain_intact",
        lambda: (_ for _ in ()).throw(RuntimeError("private audit detail")),
    )
    report = readiness_module.last_restore_drill_status()
    assert report == {
        "verified": False,
        "reason": "drill-evidence-unavailable",
        "at": None,
    }
    assert "private audit detail" not in str(report)


@pytest.fixture
def all_dependencies_ready(monkeypatch):
    import brains.control.mailbox_observability as mailbox
    import brains.control.queue_health as queue
    import brains.control.recovery_policy as recovery
    import brains.storage.migrations as migrations

    monkeypatch.setattr(
        migrations,
        "migration_status",
        lambda: {
            "backend": "sqlite",
            "healthy": True,
            "schema_verified": True,
            "pending": [],
            "failed": [],
        },
    )
    monkeypatch.setattr(
        readiness_module,
        "sqlite_integrity_status",
        lambda: {"ready": True, "reason": "checks-succeeded"},
    )
    monkeypatch.setattr(
        readiness_module,
        "mcp_protocol_readiness",
        lambda: {"ready": True, "stage": "tools/list", "reason": "ok"},
    )
    monkeypatch.setattr(queue, "summarize", lambda: {"families": {"work": {"stale_or_expired": 0}}})
    monkeypatch.setattr(mailbox, "mailbox_health_report", lambda: {"state": "ready"})
    monkeypatch.setattr(
        recovery,
        "recovery_readiness",
        lambda: {
            "ready": True,
            "policy": {"complete": True, "missing_fields": []},
            "candidate": {"ready": True, "reason": "candidate-verified"},
            "last_drill": {"verified": True, "reason": "successful-drill-recorded"},
            "reasons": [],
        },
    )
    return {
        "storage": (migrations, "migration_status"),
        "sqlite_integrity": (readiness_module, "sqlite_integrity_status"),
        "mcp_protocol": (readiness_module, "mcp_protocol_readiness"),
        "queue": (queue, "summarize"),
        "durable_mail": (mailbox, "mailbox_health_report"),
        "recovery_policy": (recovery, "recovery_readiness"),
    }


@pytest.mark.parametrize(
    "failed_component",
    ["storage", "sqlite_integrity", "mcp_protocol", "queue", "durable_mail", "recovery_policy"],
)
def test_dependency_failures_are_independent_and_secret_free(
    failed_component, all_dependencies_ready, monkeypatch
):
    module, attribute = all_dependencies_ready[failed_component]
    monkeypatch.setattr(
        module,
        attribute,
        lambda: (_ for _ in ()).throw(RuntimeError("private dependency detail")),
    )
    report = operations.readiness_report()
    assert report["status"] == "degraded"
    assert report["components"][failed_component]["state"] == "degraded"
    assert all(
        component["state"] == "ready"
        for name, component in report["components"].items()
        if name != failed_component
    )
    assert "private dependency detail" not in str(report)

"""State-driven failure drills for every retained readiness dependency.

These tests mutate real isolated SQLite/config/audit state. They never replace
a readiness component with a fake result; the complete operator projection is
evaluated before and after each fault.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import brains.audit as audit
import brains.control.mailbox_observability as mailbox_observability
import brains.control.queue_health as queue_health
import brains.storage.db as db
import brains.storage.migrations as migrations
from brains.backup import create_backup
from brains.config import settings
from brains.control.common import utc_now
from brains.control.operations import readiness_report
from brains.control.readiness import backup_candidate_status
from brains.storage.models import Handoff, Mailbox, Operator, Workspace

_POLICY_FIELDS = {
    "backup_scope": "sqlite_database",
    "backup_schedule": "daily",
    "backup_retention_days": 7,
    "backup_encryption_at_rest": False,
    "backup_encryption_owner": "",
    "backup_offsite_owner": "synthetic-operator",
    "backup_offsite_location": "synthetic-offsite-store",
    "backup_rto_minutes": 30,
    "backup_rpo_minutes": 15,
    "backup_restore_drill_required": False,
    "backup_last_restore_drill_at": "",
}


@pytest.fixture
def isolated_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    private_root = tmp_path / "private-readiness-state"
    private_root.mkdir()
    database = private_root / "private.sqlite"
    state_dir = private_root / "state"
    state_dir.mkdir()
    engine = create_engine(f"sqlite:///{database}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setenv("BRAINS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("BRAINS_AUDIT_KEY_FILE", str(private_root / "audit-key"))
    monkeypatch.delenv("BRAINS_AUDIT_KEY", raising=False)
    monkeypatch.setattr(settings, "db_url", f"sqlite:///{database}", raising=False)
    for name, value in _POLICY_FIELDS.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    for module in (db, migrations, audit, queue_health, mailbox_observability):
        monkeypatch.setattr(module, "SessionLocal", factory, raising=False)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(migrations, "engine", engine)
    audit._reset_key_cache()
    migrations.init_db()

    candidate = private_root / "candidate.tar.gz"
    create_backup(candidate)
    monkeypatch.setattr(settings, "backup_candidate_path", str(candidate), raising=False)
    yield {
        "root": private_root,
        "database": database,
        "engine": engine,
        "session": factory,
        "candidate": candidate,
    }
    engine.dispose()
    audit._reset_key_cache()


def _assert_targeted_degradation(
    before: dict,
    after: dict,
    *,
    degraded: set[str],
    private_root: Path,
) -> None:
    assert all(after["components"][name]["state"] == "degraded" for name in degraded)
    for name, component in before["components"].items():
        if name not in degraded:
            assert after["components"][name]["state"] == component["state"], name
    rendered = json.dumps(after, sort_keys=True)
    assert str(private_root) not in rendered
    assert "private.sqlite" not in rendered


def test_storage_schema_failure_isolated_from_unrelated_dependencies(isolated_readiness):
    before = readiness_report()
    with isolated_readiness["engine"].begin() as connection:
        connection.execute(text("DROP TABLE knowledge_entries"))

    after = readiness_report()

    assert after["components"]["storage"]["detail"]["schema_verified"] is False
    _assert_targeted_degradation(
        before,
        after,
        degraded={"storage", "recovery_policy"},
        private_root=isolated_readiness["root"],
    )


def test_sqlite_foreign_key_failure_does_not_poison_other_components(isolated_readiness):
    before = readiness_report()
    isolated_readiness["engine"].dispose()
    with sqlite3.connect(isolated_readiness["database"]) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO handoffs (workspace_id, title, set_at, status) "
            "VALUES (?, ?, CURRENT_TIMESTAMP, ?)",
            (999_999, "synthetic FK probe", "active"),
        )

    after = readiness_report()

    detail = after["components"]["sqlite_integrity"]["detail"]
    assert detail["reason"] == "foreign-key-violations"
    assert detail["foreign_key_violations"] == 1
    _assert_targeted_degradation(
        before,
        after,
        degraded={"sqlite_integrity"},
        private_root=isolated_readiness["root"],
    )


def test_stale_queue_work_degrades_only_queue_state(isolated_readiness):
    before = readiness_report()
    with isolated_readiness["session"]() as session:
        workspace = Workspace(
            path=str(isolated_readiness["root"] / "workspace"),
            slug="readiness-queue",
            status="active",
        )
        session.add(workspace)
        session.flush()
        session.add(
            Handoff(
                workspace_id=workspace.id,
                title="synthetic stale handoff",
                status="active",
                set_at=utc_now() - timedelta(hours=48),
            )
        )
        session.commit()

    after = readiness_report()

    assert after["components"]["queue"]["detail"]["stale_or_expired_total"] >= 1
    _assert_targeted_degradation(
        before,
        after,
        degraded={"queue"},
        private_root=isolated_readiness["root"],
    )


def test_invalid_durable_mail_state_degrades_only_mailbox(isolated_readiness):
    before = readiness_report()
    with isolated_readiness["session"]() as session:
        operator = Operator(slug="readiness-mail", display_name="Synthetic")
        session.add(operator)
        session.flush()
        session.add(
            Mailbox(
                address="private-invalid-mailbox@invalid",
                kind="operator",
                owner_operator_id=operator.id,
                operator_slot=1,
                status="active",
            )
        )
        session.commit()

    after = readiness_report()

    detail = after["components"]["durable_mail"]["detail"]
    assert "invalid_active_registration" in detail["reasons"]
    assert "private-invalid-mailbox" not in json.dumps(after, sort_keys=True)
    _assert_targeted_degradation(
        before,
        after,
        degraded={"durable_mail"},
        private_root=isolated_readiness["root"],
    )


def test_missing_recovery_policy_degrades_only_recovery(isolated_readiness, monkeypatch):
    before = readiness_report()
    monkeypatch.setattr(settings, "backup_scope", "", raising=False)

    after = readiness_report()

    assert "backup_scope" in after["components"]["recovery_policy"]["detail"]["missing_fields"]
    _assert_targeted_degradation(
        before,
        after,
        degraded={"recovery_policy"},
        private_root=isolated_readiness["root"],
    )


def test_stale_drill_evidence_degrades_only_recovery(isolated_readiness, monkeypatch):
    old = backup_candidate_status(isolated_readiness["candidate"])
    monkeypatch.setattr(settings, "backup_restore_drill_required", True, raising=False)
    audit.record_required(
        actor="synthetic-operator",
        action="admin.recovery_drill",
        payload={
            "candidate_verified": True,
            "restore_verified": True,
            "rollback_verified": True,
            "data_fingerprint": old["data_fingerprint"],
        },
    )
    before = readiness_report()
    assert before["components"]["recovery_policy"]["state"] == "ready"

    with isolated_readiness["session"]() as session:
        session.add(
            Workspace(
                path=str(isolated_readiness["root"] / "new-state"),
                slug="new-recovery-state",
                status="active",
            )
        )
        session.commit()
    replacement = isolated_readiness["root"] / "replacement.tar.gz"
    create_backup(replacement)
    monkeypatch.setattr(settings, "backup_candidate_path", str(replacement), raising=False)

    after = readiness_report()

    assert "candidate-not-drilled" in after["components"]["recovery_policy"]["detail"]["reasons"]
    _assert_targeted_degradation(
        before,
        after,
        degraded={"recovery_policy"},
        private_root=isolated_readiness["root"],
    )


def test_incompatible_recovery_candidate_degrades_only_recovery(isolated_readiness, monkeypatch):
    before = readiness_report()
    extracted = isolated_readiness["root"] / "incompatible"
    extracted.mkdir()
    with tarfile.open(isolated_readiness["candidate"], "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    manifest_path = extracted / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_versions"].append("999_unknown_future")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    incompatible = isolated_readiness["root"] / "incompatible.tar.gz"
    with tarfile.open(incompatible, "w:gz") as archive:
        archive.add(manifest_path, arcname="manifest.json")
        archive.add(extracted / "brains.sqlite", arcname="brains.sqlite")
    monkeypatch.setattr(settings, "backup_candidate_path", str(incompatible), raising=False)

    after = readiness_report()

    candidate = after["components"]["recovery_policy"]["detail"]["candidate"]
    assert candidate["reason"] == "candidate-schema-incompatible"
    _assert_targeted_degradation(
        before,
        after,
        degraded={"recovery_policy"},
        private_root=isolated_readiness["root"],
    )

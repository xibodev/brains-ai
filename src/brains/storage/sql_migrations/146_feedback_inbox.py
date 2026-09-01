"""Governed agent-experience feedback inbox (SQLite)."""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_reports (
            id INTEGER NOT NULL,
            code VARCHAR(32) NOT NULL,
            workspace_id INTEGER NOT NULL,
            reporter_session_id VARCHAR(32),
            category VARCHAR(32) NOT NULL,
            severity VARCHAR(16) NOT NULL,
            summary VARCHAR(500) NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            reproduction TEXT NOT NULL DEFAULT '',
            affected_version VARCHAR(64),
            surface VARCHAR(128),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            fingerprint VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'open',
            triage_note TEXT,
            triaged_by_operator_id INTEGER,
            triaged_at DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (code),
            CONSTRAINT uq_feedback_workspace_fingerprint UNIQUE (workspace_id, fingerprint),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(reporter_session_id) REFERENCES agent_sessions(id),
            FOREIGN KEY(triaged_by_operator_id) REFERENCES operators(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_enrichments (
            id INTEGER NOT NULL,
            feedback_report_id INTEGER NOT NULL,
            reporter_session_id VARCHAR(32),
            kind VARCHAR(32) NOT NULL DEFAULT 'enrichment',
            note TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            reproduction TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            fingerprint VARCHAR(64) NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_feedback_enrichment UNIQUE (feedback_report_id, fingerprint),
            FOREIGN KEY(feedback_report_id) REFERENCES feedback_reports(id),
            FOREIGN KEY(reporter_session_id) REFERENCES agent_sessions(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_promotions (
            feedback_report_id INTEGER NOT NULL,
            target_kind VARCHAR(16) NOT NULL,
            target_ref VARCHAR(128) NOT NULL,
            promoted_by_operator_id INTEGER,
            audit_entry_id INTEGER NOT NULL,
            promoted_at DATETIME NOT NULL,
            PRIMARY KEY (feedback_report_id),
            UNIQUE (audit_entry_id),
            FOREIGN KEY(feedback_report_id) REFERENCES feedback_reports(id),
            FOREIGN KEY(promoted_by_operator_id) REFERENCES operators(id),
            FOREIGN KEY(audit_entry_id) REFERENCES audit_log(id)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_feedback_reports_code ON feedback_reports (code)"
    )
    indexes = {
        "feedback_reports": (
            "workspace_id",
            "reporter_session_id",
            "category",
            "severity",
            "affected_version",
            "surface",
            "fingerprint",
            "status",
            "triaged_by_operator_id",
            "created_at",
            "updated_at",
        ),
        "feedback_enrichments": (
            "feedback_report_id",
            "reporter_session_id",
            "kind",
            "fingerprint",
            "created_at",
        ),
        "feedback_promotions": ("target_kind", "target_ref", "promoted_at"),
    }
    for table, columns in indexes.items():
        for column in columns:
            conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})")

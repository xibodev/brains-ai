"""Governed, privacy-safe agent-experience feedback inbox (BL-P1-15)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy.exc import IntegrityError

from brains import audit
from brains.control.common import insert_coded_row_in_session, next_sequential_code, utc_now
from brains.control.events import append_event
from brains.control.sessions import register_workspace, require_live_session
from brains.govern.redaction import REDACTED, is_secret_name, redact_text
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentTask,
    FeedbackEnrichment,
    FeedbackPromotion,
    FeedbackReport,
    KnowledgeEntry,
    Workspace,
)

CATEGORIES = {"defect", "friction", "feature_request", "environment", "documentation"}
SEVERITIES = {"info", "low", "medium", "high", "critical"}
STATUSES = {"open", "triaged", "planned", "resolved", "rejected"}
PROMOTION_TARGETS = {"task", "knowledge", "backlog"}
MAX_METADATA_BYTES = 8000
MAX_EVIDENCE_CHARS = 20000
MAX_NOTE_CHARS = 20000
MAX_REPRODUCTION_CHARS = 20000
_BACKLOG_REF = re.compile(r"^BL-P[0-3]-\d{2}$")


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and is_secret_name(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): _redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def _safe_metadata(metadata: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    safe = _redact_value(metadata or {})
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(f"feedback metadata is limited to {MAX_METADATA_BYTES} bytes")
    return safe, encoded


def _normalize_text(value: str | None) -> str:
    return " ".join(redact_text(value or "").strip().split())


def _bounded(value: str, limit: int, field: str) -> str:
    safe = redact_text(value).strip()
    if len(safe) > limit:
        raise ValueError(f"{field} is limited to {limit} characters")
    return safe


def _fingerprint(parts: list[str]) -> str:
    canonical = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _report_fingerprint(
    category: str,
    summary: str,
    affected_version: str | None,
    surface: str | None,
) -> str:
    return _fingerprint(
        [
            category,
            summary.casefold(),
            (affected_version or "").casefold(),
            (surface or "").casefold(),
        ]
    )


def _loads(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _promotion_dict(row: FeedbackPromotion) -> dict[str, Any]:
    return {
        "target_kind": row.target_kind,
        "target_ref": row.target_ref,
        "audit_entry_id": row.audit_entry_id,
        "promoted_at": row.promoted_at.isoformat(),
    }


def _report_to_dict(
    row: FeedbackReport,
    workspace_slug: str,
    *,
    promotion: FeedbackPromotion | None = None,
    enrichments: int | None = None,
) -> dict[str, Any]:
    return {
        "code": row.code,
        "workspace": workspace_slug,
        "workspace_id": row.workspace_id,
        "reporter_session_id": row.reporter_session_id,
        "category": row.category,
        "severity": row.severity,
        "summary": row.summary,
        "evidence": row.evidence,
        "reproduction": row.reproduction,
        "affected_version": row.affected_version,
        "surface": row.surface,
        "metadata": _loads(row.metadata_json),
        "status": row.status,
        "triage_note": row.triage_note,
        "triaged_at": row.triaged_at.isoformat() if row.triaged_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "enrichments": enrichments,
        "promotion": _promotion_dict(promotion) if promotion is not None else None,
    }


def _assert_visible_workspace(workspace_id: int) -> None:
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    if visible is not None and workspace_id not in visible:
        raise ValueError("feedback report is unavailable")


def _assert_human_writer(principal, workspace: Workspace) -> None:
    if getattr(principal, "is_runtime", False) or not getattr(principal, "is_human_channel", False):
        raise PermissionError("feedback triage and promotion require a human-bound principal")
    if workspace.org_id is None or not principal.has_capability("org.write", workspace.org_id):
        raise PermissionError("principal cannot manage feedback in this Workspace")
    from brains.authz.policy import visible_workspace_ids

    visible = visible_workspace_ids(principal)
    if visible is not None and workspace.id not in visible:
        raise PermissionError("principal cannot manage feedback in this Workspace")


def _next_code(session, model, prefix: str) -> str:
    return next_sequential_code(session, model.code, prefix)


def _insert_enrichment(
    session,
    report: FeedbackReport,
    *,
    session_id: str | None,
    kind: str,
    note: str,
    evidence: str,
    reproduction: str,
    metadata_json: str,
) -> tuple[FeedbackEnrichment | None, bool]:
    fingerprint = _fingerprint([kind, note, evidence, reproduction, metadata_json])
    existing = (
        session.query(FeedbackEnrichment)
        .filter(
            FeedbackEnrichment.feedback_report_id == report.id,
            FeedbackEnrichment.fingerprint == fingerprint,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing, True
    if not any((note, evidence, reproduction, metadata_json != "{}")):
        return None, False
    row = FeedbackEnrichment(
        feedback_report_id=report.id,
        reporter_session_id=session_id,
        kind=kind,
        note=note,
        evidence=evidence,
        reproduction=reproduction,
        metadata_json=metadata_json,
        fingerprint=fingerprint,
    )
    savepoint = session.begin_nested()
    try:
        session.add(row)
        session.flush()
    except IntegrityError:
        savepoint.rollback()
        existing = (
            session.query(FeedbackEnrichment)
            .filter(
                FeedbackEnrichment.feedback_report_id == report.id,
                FeedbackEnrichment.fingerprint == fingerprint,
            )
            .one()
        )
        return existing, True
    savepoint.commit()
    return row, False


def file_feedback(
    workspace_path: str,
    category: str,
    severity: str,
    summary: str,
    *,
    evidence: str = "",
    reproduction: str = "",
    affected_version: str | None = None,
    surface: str | None = None,
    reporter_session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """File or link a privacy-safe canonical feedback report."""
    if not reporter_session_id:
        raise ValueError("reporter_session_id is required")
    category = category.strip().lower()
    severity = severity.strip().lower()
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {sorted(CATEGORIES)}")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(SEVERITIES)}")
    safe_summary = _normalize_text(summary)
    if not safe_summary:
        raise ValueError("summary is required")
    if len(safe_summary) > 500:
        raise ValueError("summary is limited to 500 characters")
    safe_evidence = _bounded(evidence, MAX_EVIDENCE_CHARS, "evidence")
    safe_reproduction = _bounded(reproduction, MAX_REPRODUCTION_CHARS, "reproduction")
    safe_version = _normalize_text(affected_version) or None
    safe_surface = _normalize_text(surface) or None
    if safe_version and len(safe_version) > 64:
        raise ValueError("affected_version is limited to 64 characters")
    if safe_surface and len(safe_surface) > 128:
        raise ValueError("surface is limited to 128 characters")
    _safe, metadata_json = _safe_metadata(metadata)
    workspace = register_workspace(workspace_path)
    _assert_visible_workspace(workspace.id)
    fingerprint = _report_fingerprint(category, safe_summary, safe_version, safe_surface)
    init_db()

    for _attempt in range(8):
        with SessionLocal() as session:
            reporter = require_live_session(session, reporter_session_id, action="file_feedback")
            if reporter.workspace_id != workspace.id:
                raise ValueError("reporter Session and feedback Workspace must match")
            existing = (
                session.query(FeedbackReport)
                .filter(
                    FeedbackReport.workspace_id == workspace.id,
                    FeedbackReport.fingerprint == fingerprint,
                )
                .one_or_none()
            )
            if existing is not None:
                enrichment, enrichment_duplicate = _insert_enrichment(
                    session,
                    existing,
                    session_id=reporter_session_id,
                    kind="duplicate_report",
                    note=(
                        f"Duplicate report severity: {severity}"
                        if severity != existing.severity
                        else ""
                    ),
                    evidence=safe_evidence,
                    reproduction=safe_reproduction,
                    metadata_json=metadata_json,
                )
                session.commit()
                promotion = session.get(FeedbackPromotion, existing.id)
                result = _report_to_dict(
                    existing,
                    workspace.slug,
                    promotion=promotion,
                    enrichments=session.query(FeedbackEnrichment)
                    .filter_by(feedback_report_id=existing.id)
                    .count(),
                )
                if enrichment is not None and not enrichment_duplicate:
                    append_event(
                        "feedback_linked",
                        f"{existing.code}: duplicate report linked",
                        workspace_id=workspace.id,
                        session_id=reporter_session_id,
                        metadata={"code": existing.code, "enrichment_id": enrichment.id},
                    )
                return {
                    **result,
                    "duplicate": True,
                    "duplicate_of": existing.code,
                    "enrichment_id": enrichment.id if enrichment is not None else None,
                    "enrichment_duplicate": enrichment_duplicate,
                }
            row = FeedbackReport(
                code=_next_code(session, FeedbackReport, "FB"),
                workspace_id=workspace.id,
                reporter_session_id=reporter_session_id,
                category=category,
                severity=severity,
                summary=safe_summary,
                evidence=safe_evidence,
                reproduction=safe_reproduction,
                affected_version=safe_version,
                surface=safe_surface,
                metadata_json=metadata_json,
                fingerprint=fingerprint,
                status="open",
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
            session.refresh(row)
            result = _report_to_dict(row, workspace.slug, enrichments=0)
            code = row.code
        append_event(
            "feedback_reported",
            f"{code}: [{category}] {safe_summary[:160]}",
            workspace_id=workspace.id,
            session_id=reporter_session_id,
            metadata={"code": code, "category": category, "severity": severity},
        )
        return {**result, "duplicate": False, "duplicate_of": None}
    raise RuntimeError("could not file feedback after concurrent retries")


def enrich_feedback(
    code: str,
    *,
    reporter_session_id: str,
    kind: str = "enrichment",
    note: str = "",
    evidence: str = "",
    reproduction: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append deduplicated evidence from a live Session in the same Workspace."""
    if not reporter_session_id:
        raise ValueError("reporter_session_id is required")
    safe_kind = _normalize_text(kind).lower() or "enrichment"
    if len(safe_kind) > 32:
        raise ValueError("kind is limited to 32 characters")
    safe_note = _bounded(note, MAX_NOTE_CHARS, "note")
    safe_evidence = _bounded(evidence, MAX_EVIDENCE_CHARS, "evidence")
    safe_reproduction = _bounded(reproduction, MAX_REPRODUCTION_CHARS, "reproduction")
    _safe, metadata_json = _safe_metadata(metadata)
    if not any((safe_note, safe_evidence, safe_reproduction, metadata_json != "{}")):
        raise ValueError("an enrichment needs note, evidence, reproduction, or metadata")
    init_db()
    with SessionLocal() as session:
        report = session.query(FeedbackReport).filter(FeedbackReport.code == code).one_or_none()
        if report is None:
            raise ValueError(f"unknown feedback report: {code}")
        _assert_visible_workspace(report.workspace_id)
        reporter = require_live_session(session, reporter_session_id, action="enrich_feedback")
        if reporter.workspace_id != report.workspace_id:
            raise ValueError("reporter Session and feedback Workspace must match")
        row, duplicate = _insert_enrichment(
            session,
            report,
            session_id=reporter_session_id,
            kind=safe_kind,
            note=safe_note,
            evidence=safe_evidence,
            reproduction=safe_reproduction,
            metadata_json=metadata_json,
        )
        session.commit()
        assert row is not None
        result = {
            "id": row.id,
            "code": code,
            "kind": row.kind,
            "note": row.note,
            "evidence": row.evidence,
            "reproduction": row.reproduction,
            "metadata": _loads(row.metadata_json),
            "created_at": row.created_at.isoformat(),
            "duplicate": duplicate,
        }
    if not duplicate:
        append_event(
            "feedback_enriched",
            f"{code}: {safe_kind}",
            workspace_id=report.workspace_id,
            session_id=reporter_session_id,
            metadata={"code": code, "enrichment_id": row.id, "kind": safe_kind},
        )
    return result


def _visible_reports_query(session):
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    query = session.query(FeedbackReport, Workspace).join(
        Workspace, Workspace.id == FeedbackReport.workspace_id
    )
    if visible is not None:
        query = query.filter(FeedbackReport.workspace_id.in_(visible))
    return query


def list_feedback(
    workspace_path: str | None = None,
    *,
    status: str | None = None,
    category: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    init_db()
    with SessionLocal() as session:
        query = _visible_reports_query(session)
        if workspace_path:
            workspace = register_workspace(workspace_path)
            query = query.filter(FeedbackReport.workspace_id == workspace.id)
        if status:
            query = query.filter(FeedbackReport.status == status)
        if category:
            query = query.filter(FeedbackReport.category == category)
        rows = query.order_by(FeedbackReport.updated_at.desc(), FeedbackReport.id.desc()).limit(
            limit
        )
        return [
            _report_to_dict(
                report,
                workspace.slug,
                promotion=session.get(FeedbackPromotion, report.id),
                enrichments=session.query(FeedbackEnrichment)
                .filter_by(feedback_report_id=report.id)
                .count(),
            )
            for report, workspace in rows.all()
        ]


def get_feedback(code: str) -> dict[str, Any] | None:
    init_db()
    with SessionLocal() as session:
        row = _visible_reports_query(session).filter(FeedbackReport.code == code).one_or_none()
        if row is None:
            return None
        report, workspace = row
        enrichments = (
            session.query(FeedbackEnrichment)
            .filter(FeedbackEnrichment.feedback_report_id == report.id)
            .order_by(FeedbackEnrichment.created_at.asc(), FeedbackEnrichment.id.asc())
            .all()
        )
        result = _report_to_dict(
            report,
            workspace.slug,
            promotion=session.get(FeedbackPromotion, report.id),
            enrichments=len(enrichments),
        )
        result["enrichment_rows"] = [
            {
                "id": row.id,
                "reporter_session_id": row.reporter_session_id,
                "kind": row.kind,
                "note": row.note,
                "evidence": row.evidence,
                "reproduction": row.reproduction,
                "metadata": _loads(row.metadata_json),
                "created_at": row.created_at.isoformat(),
            }
            for row in enrichments
        ]
        return result


def triage_feedback(
    code: str,
    status: str,
    *,
    note: str = "",
    principal=None,
) -> dict[str, Any]:
    """Human-only feedback lifecycle transition, audited transactionally."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}")
    if principal is None:
        from brains.authz.resolver import resolve_local_principal

        principal = resolve_local_principal()
    safe_note = _bounded(note, MAX_NOTE_CHARS, "note") or None
    init_db()
    with SessionLocal() as session:
        report = session.query(FeedbackReport).filter(FeedbackReport.code == code).one_or_none()
        if report is None:
            raise ValueError(f"unknown feedback report: {code}")
        workspace = session.get(Workspace, report.workspace_id)
        assert workspace is not None
        _assert_human_writer(principal, workspace)
        duplicate = report.status == status and report.triage_note == safe_note
        if not duplicate:
            report.status = status
            report.triage_note = safe_note
            report.triaged_by_operator_id = principal.operator_id
            report.triaged_at = utc_now()
            report.updated_at = report.triaged_at
            audit.append_in_session(
                session,
                actor=principal.describe(),
                action="feedback.triaged",
                payload={"code": code, "status": status, "note": safe_note},
                workspace_id=workspace.id,
            )
        session.commit()
        result = {**_report_to_dict(report, workspace.slug), "duplicate": duplicate}
    if not duplicate:
        append_event(
            "feedback_triaged",
            f"{code} -> {status}",
            workspace_id=workspace.id,
            metadata={"code": code, "status": status},
        )
    return result


def _promotion_body(report: FeedbackReport) -> str:
    parts = [f"Feedback {report.code}: {report.summary}"]
    if report.reproduction:
        parts.append(f"Reproduction:\n{report.reproduction}")
    if report.evidence:
        parts.append(f"Evidence:\n{report.evidence}")
    if report.affected_version:
        parts.append(f"Affected version: {report.affected_version}")
    if report.surface:
        parts.append(f"Surface: {report.surface}")
    return "\n\n".join(parts)


def promote_feedback(
    code: str,
    target_kind: str,
    *,
    backlog_ref: str | None = None,
    principal=None,
) -> dict[str, Any]:
    """Promote feedback exactly once into Task, knowledge, or backlog reference."""
    target_kind = target_kind.strip().lower()
    if target_kind not in PROMOTION_TARGETS:
        raise ValueError(f"target_kind must be one of {sorted(PROMOTION_TARGETS)}")
    if target_kind == "backlog" and not _BACKLOG_REF.fullmatch(backlog_ref or ""):
        raise ValueError("backlog promotion requires an existing BL-PN-NN reference")
    if principal is None:
        from brains.authz.resolver import resolve_local_principal

        principal = resolve_local_principal()
    init_db()
    with SessionLocal() as session:
        # A no-op UPDATE is the cross-backend per-report fence: SQLite takes
        # the writer lock before downstream rows are created; Postgres locks
        # this report row. The second promoter then observes the first result.
        locked = (
            session.query(FeedbackReport)
            .filter(FeedbackReport.code == code)
            .update(
                {"updated_at": FeedbackReport.updated_at},
                synchronize_session=False,
            )
        )
        if not locked:
            session.rollback()
            raise ValueError(f"unknown feedback report: {code}")
        report = session.query(FeedbackReport).filter(FeedbackReport.code == code).one_or_none()
        assert report is not None
        workspace = session.get(Workspace, report.workspace_id)
        assert workspace is not None
        _assert_human_writer(principal, workspace)
        existing = session.get(FeedbackPromotion, report.id)
        if existing is not None:
            return {"code": code, **_promotion_dict(existing), "duplicate": True}
        if report.status in {"resolved", "rejected"}:
            raise ValueError(f"feedback report {code} is {report.status}, not promotable")
        report_id = report.id
        now = utc_now()
        body = _promotion_body(report)
        if target_kind == "task":
            severity_priority = {
                "critical": "p0",
                "high": "p1",
                "medium": "p2",
                "low": "p3",
                "info": "p3",
            }
            row = insert_coded_row_in_session(
                session,
                lambda: _next_code(session, AgentTask, "TASK"),
                lambda target_code: AgentTask(
                    code=target_code,
                    workspace_id=workspace.id,
                    title=report.summary[:256],
                    body=body,
                    priority=severity_priority[report.severity],
                    status="available",
                    tags=f"feedback:{report.code}",
                ),
            )
            target_ref = row.code
        elif target_kind == "knowledge":
            row = insert_coded_row_in_session(
                session,
                lambda: _next_code(session, KnowledgeEntry, "KNOW"),
                lambda target_code: KnowledgeEntry(
                    code=target_code,
                    type="caveat" if report.category == "defect" else "environment_note",
                    title=report.summary[:300],
                    body=body,
                    status="active",
                    scope="workspace",
                    workspace_id=workspace.id,
                    tags=f"feedback:{report.code}",
                    confidence="medium",
                    provenance="extracted",
                    importance=0.7,
                    severity=report.severity,
                    created_by_operator_id=principal.operator_id,
                    created_by_session_id=report.reporter_session_id,
                    evidence=report.evidence,
                    metadata_json=json.dumps({"feedback_code": report.code}),
                ),
            )
            target_ref = row.code
        else:
            target_ref = backlog_ref or ""
        audit_entry = audit.append_in_session(
            session,
            actor=principal.describe(),
            action="feedback.promoted",
            payload={"code": code, "target_kind": target_kind, "target_ref": target_ref},
            workspace_id=workspace.id,
        )
        promotion = FeedbackPromotion(
            feedback_report_id=report.id,
            target_kind=target_kind,
            target_ref=target_ref,
            promoted_by_operator_id=principal.operator_id,
            audit_entry_id=audit_entry.id,
            promoted_at=now,
        )
        session.add(promotion)
        report.status = "planned"
        report.triage_note = report.triage_note or f"Promoted to {target_kind}:{target_ref}"
        report.triaged_by_operator_id = principal.operator_id
        report.triaged_at = now
        report.updated_at = now
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.get(FeedbackPromotion, report_id)
            if existing is not None:
                return {"code": code, **_promotion_dict(existing), "duplicate": True}
            raise
        result = {"code": code, **_promotion_dict(promotion), "duplicate": False}
    append_event(
        "feedback_promoted",
        f"{code} -> {target_kind}:{target_ref}",
        workspace_id=workspace.id,
        metadata={"code": code, "target_kind": target_kind, "target_ref": target_ref},
    )
    return result


__all__ = [
    "CATEGORIES",
    "PROMOTION_TARGETS",
    "SEVERITIES",
    "STATUSES",
    "enrich_feedback",
    "file_feedback",
    "get_feedback",
    "list_feedback",
    "promote_feedback",
    "triage_feedback",
]

import json

from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import FreshnessCheck, Memory, RouteDecision, Trace

_db_initialized = False


def _ensure_db() -> None:
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True


def write_trace(route: str, payload: str) -> None:
    _ensure_db()
    payload = _truncate_trace_payload(payload)
    with SessionLocal() as session:
        session.add(Trace(route=route, payload=payload))
        session.commit()
        _prune_traces(session)
    try:
        from brains.control.events import append_event

        append_event("trace_written", f"trace written for route {route}")
    except Exception:
        pass


def _truncate_trace_payload(payload: str, cap: int | None = None) -> str:
    """Cap a trace payload at ``cap`` (defaults to
    ``settings.trace_max_payload_bytes``).

    Traces store a redacted copy of the request for the admin view; the
    full message/context JSON can be megabytes per call and is the #1 DB
    bloat source. We keep a head slice (enough to diagnose route/shape)
    and append a marker noting the original size. A cap of ``0`` (or
    negative) disables capping.
    """
    if cap is None:
        from brains.config import settings

        cap = settings.trace_max_payload_bytes
    if not cap or cap <= 0:
        return payload
    encoded = payload.encode("utf-8")
    if len(encoded) <= cap:
        return payload
    head = encoded[:cap].decode("utf-8", errors="ignore")
    return f"{head}…[truncated: {len(encoded)} bytes original, capped at {cap}]"


def _prune_traces(session) -> None:
    """Keep only the most recent ``settings.trace_retention_max_rows`` traces.

    Runs after each insert; the DELETE is a no-op once the table is at or
    below the cap, so the steady-state cost is one cheap bounded query.
    ``0`` disables pruning (retain everything).
    """
    from brains.config import settings

    keep = settings.trace_retention_max_rows
    if not keep or keep <= 0:
        return
    try:
        threshold = session.query(Trace.id).order_by(Trace.id.desc()).offset(keep).limit(1).scalar()
        if threshold is not None:
            session.query(Trace).filter(Trace.id <= threshold).delete(synchronize_session=False)
            session.commit()
    except Exception:
        session.rollback()


def write_route(
    task_type: str,
    model_tier: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    strategy: str | None = None,
    policy: dict | None = None,
) -> None:
    _ensure_db()
    with SessionLocal() as session:
        session.add(RouteDecision(task_type=task_type, model_tier=model_tier))
        session.commit()
    try:
        from brains.control.events import append_event

        append_event(
            "route_decision",
            f"{task_type} -> {model_tier}",
            metadata={
                "task_type": task_type,
                "model_tier": model_tier,
                "provider": provider,
                "model": model,
                "strategy": strategy,
                "policy": policy or {},
            },
        )
    except Exception:
        pass


def list_traces(limit: int = 20):
    _ensure_db()
    with SessionLocal() as session:
        return session.query(Trace).order_by(Trace.id.desc()).limit(limit).all()


def prune_traces_now(
    *, max_rows: int | None = None, max_payload_bytes: int | None = None
) -> dict[str, int]:
    """One-shot maintenance: enforce retention on the EXISTING traces table.

    The per-write hooks only bound traces written after this build ships;
    a brain that already accumulated a huge table (e.g. box0's 700MB) needs
    a backfill. This deletes rows beyond ``max_rows`` (newest kept) and
    truncates over-sized payloads in the survivors. Defaults fall back to
    the configured ``settings.trace_retention_max_rows`` /
    ``trace_max_payload_bytes``. Returns ``{deleted, truncated, remaining}``.
    Run ``VACUUM`` afterwards to reclaim the freed pages on disk.
    """
    from brains.config import settings

    keep = settings.trace_retention_max_rows if max_rows is None else max_rows
    cap = settings.trace_max_payload_bytes if max_payload_bytes is None else max_payload_bytes
    _ensure_db()
    deleted = 0
    truncated = 0
    with SessionLocal() as session:
        if keep and keep > 0:
            threshold = (
                session.query(Trace.id).order_by(Trace.id.desc()).offset(keep).limit(1).scalar()
            )
            if threshold is not None:
                deleted = (
                    session.query(Trace)
                    .filter(Trace.id <= threshold)
                    .delete(synchronize_session=False)
                )
                session.commit()
        if cap and cap > 0:
            for row in session.query(Trace).all():
                capped = _truncate_trace_payload(row.payload, cap=cap)
                if capped != row.payload:
                    row.payload = capped
                    truncated += 1
            if truncated:
                session.commit()
        remaining = session.query(Trace).count()
    return {"deleted": deleted, "truncated": truncated, "remaining": remaining}


def store_memory(key: str, value: str) -> None:
    _ensure_db()
    with SessionLocal() as session:
        session.add(Memory(key=key, value=value))
        session.commit()
    try:
        from brains.control.events import append_event

        append_event(
            "memory_write",
            f"memory key stored: {key}",
            metadata={"key": key, "value_length": len(value)},
        )
    except Exception:
        pass


def retrieve_memory(
    key: str,
    limit: int = 5,
    session_id: str | None = None,
) -> list[dict]:
    """Look up ``key`` and optionally attribute the retrieval to a Session."""
    _ensure_db()
    with SessionLocal() as session:
        rows = (
            session.query(Memory)
            .filter(Memory.key == key)
            .order_by(Memory.id.desc())
            .limit(limit)
            .all()
        )
    results = [{"id": r.id, "key": r.key, "value": r.value} for r in rows]
    try:
        from brains.control.events import append_event

        append_event(
            "memory_retrieved",
            f"memory retrieved: {key}",
            session_id=session_id,
            metadata={"key": key, "hits": len(results)},
        )
    except Exception:
        pass
    return results


def write_freshness_check(source: str, check_type: str, metadata: dict) -> None:
    _ensure_db()
    with SessionLocal() as session:
        session.add(
            FreshnessCheck(source=source, check_type=check_type, metadata_json=json.dumps(metadata))
        )
        session.commit()

"""Hybrid fusion ranking: RRF + type penalties + bi-temporal filter.

CodeSeeker RRF: score = 1/(60+rank_lex) + 1/(60+rank_sem).
Type adjustments: +0.10 source definitions, -0.15 test files.
Bi-temporal: drop rows whose valid_until is past or superseded.
"""

from __future__ import annotations

from datetime import UTC, datetime


def is_test_path(path: str | None) -> bool:
    if not path:
        return False
    low = path.replace("\\", "/").lower()
    return (
        "/tests/" in low
        or "/test/" in low
        or low.startswith("test_")
        or "test_" in low.split("/")[-1]
        or low.endswith("_test.py")
        or ".test." in low
        or ".spec." in low
        or "conftest" in low
    )


def rrf_fuse(
    lexical: list[str], semantic: list[str], *, lexical_all: list[str] | None = None
) -> list[tuple[str, float]]:
    k = 60.0
    lex_rank = {doc: i + 1 for i, doc in enumerate(lexical)}
    sem_rank = {doc: i + 1 for i, doc in enumerate(semantic)}
    universe = list(dict.fromkeys(list(lexical) + list(semantic)))
    scored = []
    for doc in universe:
        s = 0.0
        if doc in lex_rank:
            s += 1.0 / (k + lex_rank[doc])
        if doc in sem_rank:
            s += 1.0 / (k + sem_rank[doc])
        scored.append((doc, s))
    scored.sort(key=lambda t: -t[1])
    return scored


def apply_type_adjustment(doc_id: str, path: str | None, score: float) -> float:
    if is_test_path(path):
        return score - 0.15
    return score + 0.10


def bi_temporal_keep(row: dict, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    vu = row.get("valid_until")
    if vu:
        try:
            dt = datetime.fromisoformat(str(vu))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if dt < now:
                return False
        except Exception:
            pass
    if row.get("superseded") is True:
        return False
    if row.get("supersedes_code"):
        return False
    status = str(row.get("status") or "").lower()
    return status not in {"retired", "superseded", "expired"}

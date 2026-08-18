from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def next_sequential_code(
    session: Any,
    code_column: Any,
    prefix: str,
    *,
    also_reserved: Sequence[Any] = (),
) -> str:
    """Return the next ``PREFIX-NNNN`` code for a coded-row table.

    Computes ``max(existing numeric suffix) + 1`` rather than ``count() + 1``
    so the sequence is robust to gaps left by archived / deleted rows (where
    a plain count would re-mint an existing code). This is still racy on its
    own under concurrent writers — pair it with :func:`insert_with_code_retry`,
    which retries the unique-constraint violation that two racers hit when
    they pick the same code.

    ``also_reserved`` names further columns that hold *spent* codes of the same
    series. The live table is not always the whole record: an ``ASK`` code that
    a governed action consumed stays on ``governed_actions.approval_code``
    forever, while the ``approval_requests`` row it came from can be deleted by
    a Workspace prune. Deriving the next code from the live table alone would
    then re-mint a code that is still bound to a permanent row, so every column
    that permanently reserves a code is scanned alongside the live one.
    """
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    highest = 0
    for column in (code_column, *also_reserved):
        # The LIKE narrows the scan to this series (``governed_actions`` is
        # append-only and grows without bound); the regex still decides.
        for (value,) in session.query(column).filter(column.like(f"{prefix}-%")).all():
            match = pattern.match(value or "")
            if match:
                number = int(match.group(1))
                if number > highest:
                    highest = number
    return f"{prefix}-{highest + 1:04d}"


def insert_coded_row_in_session(
    session: Any,
    next_code: Callable[[], str],
    build: Callable[[str], Any],
    *,
    attempts: int = 64,
) -> Any:
    """Insert a coded row inside the caller's already-open transaction.

    The counterpart to :func:`insert_with_code_retry` for callers that must
    stay inside a transaction they do not own (an ASK is filed in the same
    transaction as the audit entry that explains it, so retrying by opening a
    fresh session would break that atomicity).

    Each attempt runs inside a ``SAVEPOINT``: a unique-code collision rolls
    back only the failed insert - leaving the surrounding work, and on
    Postgres the usability of the transaction, intact - and the next attempt
    recomputes the code from the rows that are now visible.
    """
    from sqlalchemy.exc import IntegrityError

    last_error: IntegrityError | None = None
    for _attempt in range(attempts):
        code = next_code()
        savepoint = session.begin_nested()
        try:
            row = build(code)
            session.add(row)
            session.flush()
        except IntegrityError as exc:
            savepoint.rollback()
            last_error = exc
            continue
        savepoint.commit()
        return row
    assert last_error is not None  # pragma: no cover - attempts>=1
    raise last_error


def insert_with_code_retry(
    build: Callable[[Any], Any],
    finalize: Callable[[Any, Any], Any],
    *,
    attempts: int = 64,
) -> Any:
    """Insert a row whose ``code`` races under concurrent writers on a shared DB.

    ``build(session) -> row`` computes the next code (via
    :func:`next_sequential_code`), constructs the row, and ``add``\\ s it (it
    may also ``flush`` and mutate sibling rows in the same transaction).
    ``finalize(session, row) -> result`` runs after a successful commit +
    refresh — while the row is still attached — to snapshot the caller's
    return value.

    Two writers that pick the same ``PREFIX-NNNN`` code collide on the unique
    index at ``commit`` time. We catch that ``IntegrityError``, roll the
    session back, and retry the whole build with a freshly-computed code.
    Validation errors raised inside ``build`` (e.g. an unknown supersede
    target) propagate unchanged because they fire before ``commit``.
    """
    from sqlalchemy.exc import IntegrityError

    from brains.storage.db import SessionLocal

    last_error: IntegrityError | None = None
    for _attempt in range(attempts):
        with SessionLocal() as session:
            try:
                row = build(session)
                session.commit()
            except IntegrityError as exc:
                # Collision can surface at flush() inside build (when the
                # caller needs the new id mid-transaction) or at commit, so
                # the whole build+commit is guarded.
                session.rollback()
                last_error = exc
                continue
            session.refresh(row)
            return finalize(session, row)
    # Exhausted retries — surface the last collision so the caller sees a
    # real error instead of silently dropping the write.
    assert last_error is not None  # pragma: no cover - attempts>=1
    raise last_error


def normalize_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def slug_from_path(path: str) -> str:
    name = Path(path).expanduser().resolve().name or "workspace"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug or "workspace"


def unique_slug(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"

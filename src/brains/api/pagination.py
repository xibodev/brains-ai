"""Cursor pagination helper for the native-battalion (WS3) list endpoints.

DESIGN-SYNTHESIS fork WS3-7: every NEW list endpoint wraps its rows in
``{"data": [...], "next_cursor": "<opaque|null>"}`` so the SPA client is
uniform. The cursor is an opaque base64 of an integer offset — adequate for the
admin-scale, in-process reads here (legacy tool-parity bare lists are untouched).
"""

from __future__ import annotations

import base64
from typing import Any

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return 0
    return max(0, value)


def paginate(
    items: list[Any],
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return ``{"data": window, "next_cursor": opaque|None}`` over ``items``."""
    effective = DEFAULT_LIMIT if limit is None else limit
    effective = max(1, min(effective, MAX_LIMIT))
    start = _decode_cursor(cursor)
    window = items[start : start + effective]
    next_start = start + effective
    next_cursor = _encode_cursor(next_start) if next_start < len(items) else None
    return {"data": window, "next_cursor": next_cursor}

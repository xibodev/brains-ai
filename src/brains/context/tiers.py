"""Three-tier context representation (OpenViking L0/L1/L2 model).

L0 Abstract: 1-sentence summary for relevance gating (embedded).
L1 Overview: signatures/exports/structure.
L2 Details: full chunk text, retrieved on demand.
"""

from __future__ import annotations

import re


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    m = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    s = m[0] if m else text
    return s[:280] if len(s) > 280 else s


def _overview_of(text: str, rel_path: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    sigs = [
        ln for ln in lines if re.match(r"^(def |class |function |export |import |const |fn )", ln)
    ]
    head = "; ".join(sigs[:8]) if sigs else "; ".join(lines[:3])
    return f"{rel_path}: {head}"[:600]


def chunk_three_tier(
    text: str, rel_path: str, max_chars: int = 1200, overlap: int = 100
) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    pieces: list[str] = []
    if len(text) <= max_chars:
        pieces = [text]
    else:
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            pieces.append(text[start:end])
            if end == len(text):
                break
            start = max(end - overlap, start + 1)
    out: list[dict] = []
    for ordinal, piece in enumerate(pieces):
        out.append(
            {
                "ordinal": ordinal,
                "rel_path": rel_path,
                "l0_abstract": _first_sentence(piece),
                "l1_overview": _overview_of(piece, rel_path),
                "l2_detail": piece,
            }
        )
    return out

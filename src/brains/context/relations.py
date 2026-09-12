"""Knowledge<->code relations (Blueprint #39 §6): CITES edges."""

from __future__ import annotations


def cite_edge(knowledge_code: str, target_urn: str, *, evidence: str = "") -> dict:
    if not knowledge_code or not target_urn:
        raise ValueError("knowledge_code and target_urn required")
    return {
        "src": f"knowledge:{knowledge_code}",
        "dst": target_urn,
        "relation": "cites",
        "confidence": 1.0,
        "evidence": evidence[:2000],
    }
